"""
Reconciliation worker for detecting and fixing database drift.

Runs periodically to check if KubeDB resources match database records.
When drift is detected, creates operations to bring them back in sync.
"""
import asyncio
import signal
import sys
from datetime import datetime
from typing import Optional, Dict, Any

from app.config.database import Database as DatabaseConnection
from app.config.logging import get_logger
from app.config.settings import settings
from app.models.operation import Operation, OperationType, OperationStatus
from app.repositories.models import Database, Provider
from app.models.database import DatabaseStatus
from app.services.operation_queue import operation_queue
from app.services.kubedb_service import kubedb_service
from app.services import metrics

logger = get_logger(__name__)


class ReconciliationWorker:
    """
    Detects drift between database records and KubeDB resources.

    Runs periodically (default: every 5 minutes) to ensure databases
    are in their desired state. When drift is detected, creates operations
    to fix it.

    Features:
    - Periodic reconciliation (configurable interval)
    - Drift detection (size, replicas, storage)
    - Creates operations via queue (no direct patching)
    - Deduplication (won't create duplicate operations)
    - Graceful shutdown
    """

    def __init__(self, reconcile_interval: int = 10):
        """
        Initialize reconciliation worker.

        Args:
            reconcile_interval: Seconds between reconciliation runs (default: 300 = 5 minutes)
        """
        self.reconcile_interval = reconcile_interval
        self.running = False
        self._sleep_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start reconciliation worker (runs until stopped)."""
        self.running = True

        logger.info(
            "reconciliation_worker_started",
            interval_seconds=self.reconcile_interval,
        )

        while self.running:
            try:
                # Run reconciliation cycle
                await self.reconcile_all_databases()

                # Wait before next cycle (with cancellation support)
                logger.info(
                    "reconciliation_cycle_completed",
                    next_run_in_seconds=self.reconcile_interval,
                )
                try:
                    self._sleep_task = asyncio.create_task(asyncio.sleep(self.reconcile_interval))
                    await self._sleep_task
                except asyncio.CancelledError:
                    logger.info("reconciliation_sleep_cancelled")
                    break
                finally:
                    self._sleep_task = None

            except asyncio.CancelledError:
                logger.info("reconciliation_worker_cancelled")
                break
            except Exception as e:
                logger.error(
                    "reconciliation_cycle_error",
                    error=str(e),
                    exc_info=True,
                )
                # Wait a bit before retrying (with cancellation support)
                if not self.running:
                    break
                try:
                    self._sleep_task = asyncio.create_task(asyncio.sleep(60))
                    await self._sleep_task
                except asyncio.CancelledError:
                    logger.info("reconciliation_error_sleep_cancelled")
                    break
                finally:
                    self._sleep_task = None

        logger.info("reconciliation_worker_stopped")

    async def stop(self):
        """Stop reconciliation worker gracefully."""
        logger.info("stopping_reconciliation_worker")
        self.running = False

        # Cancel sleep task if running
        if self._sleep_task and not self._sleep_task.done():
            self._sleep_task.cancel()
            try:
                await self._sleep_task
            except asyncio.CancelledError:
                pass

    async def reconcile_all_databases(self):
        """
        Reconcile all databases across all providers.

        This is the main reconciliation loop that:
        1. Fetches all databases
        2. For each database, checks if KubeDB resource matches desired state
        3. Creates operations to fix any drift
        """
        try:
            # Get all providers
            providers = await Provider.find_all().to_list()
            if not providers:
                logger.debug("no_providers_found")
                return

            logger.info(
                "reconciliation_started",
                provider_count=len(providers),
            )

            for provider in providers:
                try:
                    await self._reconcile_provider_databases(provider)
                except Exception as e:
                    logger.error(
                        "reconciliation_failed_for_provider",
                        provider_id=provider.id,
                        error=str(e),
                        exc_info=True,
                    )

        except Exception as e:
            logger.error(
                "reconciliation_failed",
                error=str(e),
                exc_info=True,
            )

    async def _reconcile_provider_databases(self, provider: Provider):
        """Reconcile all databases for a specific provider."""
        try:
            # Get all databases for this provider
            databases = await Database.find({"provider_id": provider.id}).to_list()

            if not databases:
                logger.debug("no_databases_found_for_provider", provider_id=provider.id)
                return

            logger.debug(
                "reconciling_provider_databases",
                provider_id=provider.id,
                database_count=len(databases),
            )

            for db in databases:
                try:
                    await self._reconcile_database(db, provider)
                except Exception as e:
                    logger.error(
                        "reconciliation_failed_for_database",
                        database_id=db.id,
                        provider_id=provider.id,
                        error=str(e),
                        exc_info=True,
                    )

        except Exception as e:
            logger.error(
                "reconciliation_failed_for_provider",
                provider_id=provider.id,
                error=str(e),
                exc_info=True,
            )

    async def _reconcile_database(self, db: Database, provider: Provider):
        """
        Reconcile a single database.

        Checks if KubeDB resource matches desired state and creates operations to fix drift.
        """
        try:
            # Skip reconciliation for databases in terminal or transient states
            skip_statuses = [
                DatabaseStatus.PENDING,
                DatabaseStatus.PROVISIONING,
                DatabaseStatus.PAUSED,
                DatabaseStatus.DELETING,
                DatabaseStatus.DELETED,
                DatabaseStatus.FAILED,
            ]
            if db.status in skip_statuses:
                logger.debug(
                    "skipping_reconciliation_for_status",
                    database_id=db.id,
                    status=db.status.value,
                    message="Database status does not require drift reconciliation",
                )
                return

            kubeconfig_content = provider.kubeconfig_content
            client_set = await kubedb_service.get_client_for_provider(provider.id, kubeconfig_content)

            # Get current KubeDB CR
            try:
                current_cr = await client_set.custom_api.get_namespaced_custom_object(
                    group=kubedb_service._get_kubedb_group(db.engine),
                    version=kubedb_service._get_kubedb_version(db.engine),
                    namespace=db.namespace,
                    plural=kubedb_service._get_kubedb_plural(db.engine),
                    name=db.kubedb_resource_name,
                )
            except Exception as e:
                error_str = str(e).lower()
                if "404" in error_str or "not found" in error_str:
                    logger.debug(
                        "database_cr_not_found",
                        database_id=db.id,
                        message="KubeDB CR not found, database may have been deleted",
                    )
                    return  # Skip rest of reconciliation
                else:
                    # Re-raise if it's not a 404
                    raise

            # STEP 0: Handle PAUSING/RESUMING statuses
            # If database is in PAUSING state, pause the KubeDB resource
            if db.status == DatabaseStatus.PAUSING:
                is_halted = current_cr.get("spec", {}).get("halted", False)
                if not is_halted:
                    logger.info(
                        "database_status_pausing_executing_pause",
                        database_id=db.id,
                        message="Status is PAUSING - pausing KubeDB resource",
                    )
                    try:
                        await kubedb_service.pause_database(
                            engine=db.engine,
                            name=db.kubedb_resource_name,
                            namespace=db.namespace,
                            provider_id=provider.id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        logger.info(
                            "database_pause_executed",
                            database_id=db.id,
                            message="KubeDB resource paused successfully. Status will transition to PAUSED via status_sync.",
                        )
                    except Exception as e:
                        logger.error(
                            "database_pause_failed",
                            database_id=db.id,
                            error=str(e),
                        )
                else:
                    logger.debug(
                        "database_already_halted",
                        database_id=db.id,
                        message="KubeDB resource is already halted. Waiting for status_sync to transition to PAUSED.",
                    )
                return  # Skip drift detection during pause operation

            # If database is in RESUMING state, resume the KubeDB resource
            if db.status == DatabaseStatus.RESUMING:
                is_halted = current_cr.get("spec", {}).get("halted", False)
                if is_halted:
                    logger.info(
                        "database_status_resuming_executing_resume",
                        database_id=db.id,
                        message="Status is RESUMING - resuming KubeDB resource",
                    )
                    try:
                        await kubedb_service.resume_database(
                            engine=db.engine,
                            name=db.kubedb_resource_name,
                            namespace=db.namespace,
                            provider_id=provider.id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        logger.info(
                            "database_resume_executed",
                            database_id=db.id,
                            message="KubeDB resource resumed successfully. Status will transition to RUNNING via status_sync.",
                        )
                    except Exception as e:
                        logger.error(
                            "database_resume_failed",
                            database_id=db.id,
                            error=str(e),
                        )
                else:
                    logger.debug(
                        "database_already_running",
                        database_id=db.id,
                        message="KubeDB resource is already running. Waiting for status_sync to transition to RUNNING.",
                    )
                return  # Skip drift detection during resume operation

            # STEP 1: Sync current state from KubeDB CR
            cr_replicas = current_cr.get("spec", {}).get("replicas", 1)
            cr_storage_gb = self._parse_storage(
                current_cr.get("spec", {}).get("storage", {}).get("resources", {}).get("requests", {}).get("storage", "")
            )
            cr_size = self._get_size_from_resources(current_cr, db.engine.value)

            # Build update dict for ONLY current_* fields
            # IMPORTANT: Do NOT use db.save() as it overwrites desired state!
            update_dict = {
                "current_replicas": cr_replicas,
            }

            # Only update storage if we successfully parsed it
            if cr_storage_gb is not None:
                update_dict["current_storage_gb"] = cr_storage_gb

            # Only update size if we successfully determined it from CR
            # If we can't determine it (returns None), trust the worker's value
            if cr_size is not None:
                update_dict["current_size"] = cr_size
            elif db.current_size is None:
                # If both are None, log a warning
                logger.warning(
                    "unable_to_determine_current_size_from_cr",
                    database_id=db.id,
                    message="Could not parse size from KubeDB CR. Will rely on worker updates."
                )

            # Update ONLY current_* fields using targeted update
            # This preserves desired state fields (replicas, size, storage_gb)
            await Database.find_one({"_id": db.id}).update({"$set": update_dict})

            # Reload database to get latest desired state
            # (in case user/API changed it while we were reconciling)
            db = await Database.get(db.id)

            # STEP 1.5: Check for stuck pods and delete them
            # Pods stuck in Init/Pending for > 5 minutes should be deleted
            # so Kubernetes can recreate them
            try:
                stuck_pods = await kubedb_service.check_and_delete_stuck_pods(
                    database_name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    stuck_threshold_seconds=300,  # 5 minutes
                    provider_id=provider.id,
                    kubeconfig_content=kubeconfig_content,
                )
                if stuck_pods:
                    logger.info(
                        "deleted_stuck_pods",
                        database_id=db.id,
                        stuck_pod_count=len(stuck_pods),
                    )
            except Exception as e:
                logger.debug(
                    "failed_to_check_stuck_pods",
                    database_id=db.id,
                    error=str(e),
                )

            # STEP 2: Detect drift between desired state and actual K8s CR state
            drift_detected = False
            operations_to_create = []

            # Check replicas drift (desired vs current)
            if db.replicas != db.current_replicas:
                logger.info(
                    "replica_drift_detected",
                    database_id=db.id,
                    desired=db.replicas,
                    current=db.current_replicas,
                )
                operations_to_create.append({
                    "type": OperationType.SCALE_HORIZONTAL,
                    "desired_state": {"replicas": db.replicas},
                })
                drift_detected = True

            # Check storage drift (desired vs current)
            if db.storage_gb != db.current_storage_gb:
                logger.info(
                    "storage_drift_detected",
                    database_id=db.id,
                    desired=db.storage_gb,
                    current=db.current_storage_gb,
                )
                # Storage can only be expanded, not reduced
                if db.storage_gb > db.current_storage_gb:
                    operations_to_create.append({
                        "type": OperationType.EXPAND_STORAGE,
                        "desired_state": {"storage_gb": db.storage_gb},
                    })
                    drift_detected = True
                else:
                    logger.warning(
                        "storage_reduction_not_supported",
                        database_id=db.id,
                        desired=db.storage_gb,
                        current=db.current_storage_gb,
                        message="Storage reduction is not supported, skipping",
                    )

            # Check size drift: Compare desired state (db.size) with actual K8s CR resources
            # This is the source of truth - we don't rely on db.current_size which might be stale
            if db.size:
                # Get expected resources for desired size
                expected_resources = kubedb_service._get_resource_limits(db.size)
                expected_cpu = str(expected_resources.get("cpu", ""))
                expected_memory = str(expected_resources.get("memory", ""))

                # Get actual resources from K8s CR (source of truth)
                cr_resources = self._get_cr_resources(current_cr, db.engine.value)
                
                # CRITICAL: If we can't extract resources from K8s CR, skip drift detection
                # This prevents false positives where resources exist but can't be parsed
                if not cr_resources:
                    logger.warning(
                        "cannot_extract_resources_from_cr_skipping_size_drift_check",
                        database_id=db.id,
                        desired_size=db.size.value,
                        message="Cannot extract resources from K8s CR - skipping size drift detection to prevent false positives",
                    )
                    # Skip size drift check - don't create OpsRequest if we can't verify actual state
                else:
                    cr_requests = cr_resources.get("requests", {})
                    cr_cpu = str(cr_requests.get("cpu", ""))
                    cr_memory = str(cr_requests.get("memory", ""))
                    
                    # CRITICAL: If extracted resources are empty, skip drift detection
                    # Empty resources mean we can't determine actual state - don't create OpsRequest
                    if not cr_cpu and not cr_memory:
                        logger.warning(
                            "empty_resources_in_cr_skipping_size_drift_check",
                            database_id=db.id,
                            desired_size=db.size.value,
                            message="K8s CR has empty resources - skipping size drift detection to prevent false positives",
                        )
                        # Skip size drift check
                    else:
                        # Direct comparison: desired state vs actual K8s CR state
                        if cr_cpu == expected_cpu and cr_memory == expected_memory:
                            # Resources already match desired state - sync current_size if needed
                            if db.size != db.current_size:
                                logger.debug(
                                    "resources_already_match_desired_state",
                                    database_id=db.id,
                                    desired_size=db.size.value,
                                    cr_cpu=cr_cpu,
                                    cr_memory=cr_memory,
                                    message="K8s CR already has desired resources, updating current_size only",
                                )
                                # Update current_size to match desired (no OpsRequest needed)
                                await Database.find_one({"_id": db.id}).update({
                                    "$set": {"current_size": db.size}
                                })
                        else:
                            # Resources don't match desired state - create OpsRequest
                            logger.info(
                                "size_drift_detected",
                                database_id=db.id,
                                desired_size=db.size.value,
                                desired_cpu=expected_cpu,
                                desired_memory=expected_memory,
                                actual_cpu=cr_cpu,
                                actual_memory=cr_memory,
                                message="K8s CR resources don't match desired state, creating OpsRequest",
                            )
                            operations_to_create.append({
                                "type": OperationType.SCALE_VERTICAL,
                                "desired_state": {"size": db.size.value},
                            })
                            drift_detected = True

            # STEP 3: Create operations for detected drift
            if drift_detected:
                logger.info(
                    "drift_detected_creating_operations",
                    database_id=db.id,
                    operation_count=len(operations_to_create),
                )

                for op_data in operations_to_create:
                    await self._create_reconciliation_operation(
                        db=db,
                        operation_type=op_data["type"],
                        desired_state=op_data["desired_state"],
                        provider=provider,
                    )
            else:
                logger.debug(
                    "no_drift_detected",
                    database_id=db.id,
                )

        except Exception as e:
            logger.error(
                "reconciliation_check_failed",
                database_id=db.id,
                error=str(e),
            )

    async def _create_reconciliation_operation(
        self,
        db: Database,
        operation_type: OperationType,
        desired_state: Dict[str, Any],
        provider: Provider,
    ):
        """
        Create OpsRequest directly to fix drift (fire-and-forget).

        Simple logic:
        1. Check if there's an active OpsRequest in Progressing - if yes, skip
        2. If no active Progressing OpsRequest, create OpsRequest
        """
        try:
            kubeconfig_content = provider.kubeconfig_content
            client_set = await kubedb_service.get_client_for_provider(provider.id, kubeconfig_content)

            # SIMPLE CHECK: Is there an active (non-terminal) OpsRequest?
            # KubeDB can only process one OpsRequest at a time
            existing_ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=db.namespace,
                plural=kubedb_service._get_ops_request_plural(db.engine),
            )

            # Filter OpsRequests that target this database
            all_items = existing_ops_requests.get("items", [])
            db_ops = [
                item for item in all_items
                if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
            ]
            
            # Check for active (non-terminal) OpsRequests: Pending, Progressing, or any other non-terminal state
            active_ops = [
                item for item in db_ops
                if item.get("status", {}).get("phase") not in ["Successful", "Failed", "Skipped"]
            ]
            
            # Check if the most recent completed OpsRequest matches the desired state
            # If it does, skip creating a new one (CR is being updated)
            recently_completed = []
            successful_ops = [
                item for item in db_ops
                if item.get("status", {}).get("phase") == "Successful"
            ]
            
            if successful_ops:
                # Sort by creation time (newest first)
                successful_ops.sort(
                    key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""),
                    reverse=True
                )
                latest_successful = successful_ops[0]
                
                # Check if this OpsRequest matches the desired state
                ops_spec = latest_successful.get("spec", {})
                matches_desired = False
                
                if operation_type == OperationType.SCALE_VERTICAL:
                    vertical_scaling = ops_spec.get("verticalScaling", {})
                    replica_set_resources = vertical_scaling.get("replicaSet", {}).get("resources", {})
                    requests = replica_set_resources.get("requests", {})
                    
                    from app.models.database import DatabaseSize
                    target_size = DatabaseSize(desired_state["size"])
                    expected_resources = kubedb_service._get_resource_limits(target_size)
                    
                    ops_cpu = str(requests.get("cpu", ""))
                    ops_memory = str(requests.get("memory", ""))
                    expected_cpu = str(expected_resources.get("cpu", ""))
                    expected_memory = str(expected_resources.get("memory", ""))
                    
                    if ops_cpu == expected_cpu and ops_memory == expected_memory:
                        matches_desired = True
                elif operation_type == OperationType.SCALE_HORIZONTAL:
                    horizontal_scaling = ops_spec.get("horizontalScaling", {})
                    ops_replicas = horizontal_scaling.get("replicas")
                    if ops_replicas == desired_state.get("replicas"):
                        matches_desired = True
                
                if matches_desired:
                    recently_completed.append(latest_successful)
            
            if active_ops:
                # Sort by creation time (newest first) to get the latest one
                active_ops.sort(
                    key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""),
                    reverse=True
                )
                latest_ops = active_ops[0]
                ops_phase = latest_ops.get("status", {}).get("phase", "Unknown")
                ops_name = latest_ops.get("metadata", {}).get("name", "Unknown")
                
                # There's an active OpsRequest - skip creating a new one
                logger.warning(
                    "active_ops_request_found_skipping",
                    database_id=db.id,
                    operation_type=operation_type.value,
                    active_ops_request=ops_name,
                    active_phase=ops_phase,
                    active_count=len(active_ops),
                    all_active_phases=[op.get("status", {}).get("phase", "Unknown") for op in active_ops],
                    message="Active OpsRequest found - skipping to avoid duplicate",
                )
                return  # Skip - let the current one complete
            
            # Check for recently completed OpsRequests that match desired state
            # If one completed and matches desired state, skip creating a new one (CR is being updated)
            if recently_completed:
                latest_completed = recently_completed[0]
                ops_name = latest_completed.get("metadata", {}).get("name", "Unknown")
                
                logger.info(
                    "completed_ops_request_matches_desired_skipping",
                    database_id=db.id,
                    operation_type=operation_type.value,
                    completed_ops_request=ops_name,
                    message="Recently completed OpsRequest matches desired state - waiting for K8s CR to update before creating new one",
                )
                return  # Skip - wait for K8s CR to update

            # Log all OpsRequests for debugging
            if db_ops:
                all_phases = [item.get("status", {}).get("phase", "Unknown") for item in db_ops]
                logger.debug(
                    "all_ops_requests_for_database",
                    database_id=db.id,
                    total_ops=len(db_ops),
                    all_phases=all_phases,
                    ops_names=[item.get("metadata", {}).get("name") for item in db_ops],
                )

            # FINAL CHECK: Double-check right before creating OpsRequest
            # This prevents race conditions where an OpsRequest was just created
            final_check_ops = await client_set.custom_api.list_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=db.namespace,
                plural=kubedb_service._get_ops_request_plural(db.engine),
            )
            final_items = final_check_ops.get("items", [])
            final_db_ops = [
                item for item in final_items
                if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
            ]
            final_active = [
                item for item in final_db_ops
                if item.get("status", {}).get("phase") not in ["Successful", "Failed", "Skipped"]
            ]
            
            if final_active:
                latest_final = sorted(final_active, key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""), reverse=True)[0]
                final_phase = latest_final.get("status", {}).get("phase", "Unknown")
                final_name = latest_final.get("metadata", {}).get("name", "Unknown")
                logger.error(
                    "final_check_active_ops_request_found_aborting",
                    database_id=db.id,
                    operation_type=operation_type.value,
                    active_ops_request=final_name,
                    active_phase=final_phase,
                    active_count=len(final_active),
                    message="FINAL CHECK: Active OpsRequest found just before creating - ABORTING",
                )
                return  # ABORT - do not create new OpsRequest

            # No active OpsRequest - create OpsRequest
            # Create OpsRequest directly (fire-and-forget)
            if operation_type == OperationType.SCALE_HORIZONTAL:
                ops_request = await kubedb_service.create_horizontal_scaling_ops_request(
                    engine=db.engine,
                    name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    replicas=desired_state["replicas"],
                    provider_id=provider.id,
                    kubeconfig_content=kubeconfig_content,
                )
            elif operation_type == OperationType.SCALE_VERTICAL:
                from app.models.database import DatabaseSize
                target_size = DatabaseSize(desired_state["size"])
                ops_request = await kubedb_service.create_vertical_scaling_ops_request(
                    engine=db.engine,
                    name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    size=target_size,
                    provider_id=provider.id,
                    kubeconfig_content=kubeconfig_content,
                )
            elif operation_type == OperationType.EXPAND_STORAGE:
                # Storage expansion uses direct patch, not OpsRequest
                await kubedb_service.patch_database(
                    engine=db.engine,
                    name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    storage_gb=desired_state["storage_gb"],
                    provider_id=provider.id,
                    kubeconfig_content=kubeconfig_content,
                )
                logger.info(
                    "reconciliation_storage_expanded",
                    database_id=db.id,
                    storage_gb=desired_state["storage_gb"],
                )
                return

            ops_request_name = ops_request.get("metadata", {}).get("name") if isinstance(ops_request, dict) else None
            logger.info(
                "reconciliation_ops_request_created",
                database_id=db.id,
                operation_type=operation_type.value,
                ops_request_name=ops_request_name,
                desired_state=desired_state,
                message="OpsRequest created (fire-and-forget), will be checked in next reconciliation cycle",
            )

        except Exception as e:
            import traceback
            logger.error(
                "failed_to_create_reconciliation_ops_request",
                database_id=db.id,
                operation_type=operation_type.value,
                error=str(e),
                exception_type=type(e).__name__,
                traceback=traceback.format_exc(),
            )

    def _parse_storage(self, storage_str: str) -> Optional[int]:
        """Parse storage string (e.g., '100Gi') to GB integer."""
        if not storage_str:
            return None

        try:
            # Remove 'Gi' or 'G' suffix and convert to int
            if storage_str.endswith("Gi"):
                return int(storage_str[:-2])
            elif storage_str.endswith("G"):
                return int(storage_str[:-1])
            else:
                # Try to parse as integer (assume GB)
                return int(storage_str)
        except (ValueError, AttributeError):
            return None

    def _get_cr_resources(self, cr: Dict[str, Any], engine: str) -> Optional[Dict[str, Any]]:
        """Extract resource requests from CR."""
        if "spec" not in cr or "podTemplate" not in cr["spec"]:
            return None

        containers = cr["spec"]["podTemplate"]["spec"].get("containers", [])
        for container in containers:
            # Match by engine name (mongodb, postgres, etc.)
            if container.get("name") in ["mongodb", "postgres", "mysql", engine]:
                return container.get("resources", {})

        return None

    def _get_size_from_resources(self, cr: Dict[str, Any], engine: str) -> Optional:
        """
        Determine DatabaseSize from CR resources.

        Converts CR resources back to DatabaseSize enum.
        """
        from app.models.database import DatabaseSize

        resources = self._get_cr_resources(cr, engine)
        if not resources or "requests" not in resources:
            return None

        requests = resources["requests"]
        cpu = requests.get("cpu", "")
        memory = requests.get("memory", "")

        # Map resources to DatabaseSize (approximate matching)
        # This is reverse lookup - from resources to size
        size_mapping = {
            ("500m", "1Gi"): DatabaseSize.MICRO,
            ("1", "2Gi"): DatabaseSize.SMALL,
            ("2", "4Gi"): DatabaseSize.MEDIUM,
            ("2", "8Gi"): DatabaseSize.LARGE,
            ("4", "16Gi"): DatabaseSize.XLARGE,
            ("8", "32Gi"): DatabaseSize.XXLARGE,
        }

        # Normalize CPU (remove 'm' suffix for comparison)
        cpu_normalized = cpu.replace("m", "") if cpu.endswith("m") else cpu
        # Try exact match first
        size = size_mapping.get((cpu, memory))
        if size:
            return size

        # Try normalized CPU match
        for (map_cpu, map_memory), map_size in size_mapping.items():
            map_cpu_normalized = map_cpu.replace("m", "") if map_cpu.endswith("m") else map_cpu
            if map_cpu_normalized == cpu_normalized and map_memory == memory:
                return map_size

        # No match found
        return None

    async def _cleanup_old_ops_requests(self, provider: Provider):
        """
        Clean up old completed OpsRequests to prevent accumulation.

        Keeps only the last 3 completed OpsRequests per database.
        """
        try:
            kubeconfig_content = provider.kubeconfig_content
            client_set = await kubedb_service.get_client_for_provider(provider.id, kubeconfig_content)

            # Get all databases for this provider
            databases = await Database.find({"provider_id": provider.id}).to_list()

            total_cleaned = 0

            for db in databases:
                try:
                    # Get all OpsRequests for this database
                    ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                        group="ops.kubedb.com",
                        version="v1alpha1",
                        namespace=db.namespace,
                        plural=kubedb_service._get_ops_request_plural(db.engine),
                        label_selector=f"app.kubernetes.io/instance={db.kubedb_resource_name}",
                    )

                    items = ops_requests.get("items", [])
                    db_ops = [
                        item for item in items
                        if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
                    ]

                    # Filter completed OpsRequests (Successful, Failed, Skipped)
                    completed_ops = [
                        item for item in db_ops
                        if item.get("status", {}).get("phase") in ["Successful", "Failed", "Skipped"]
                    ]

                    # Sort by creation timestamp (newest first)
                    completed_ops.sort(
                        key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""),
                        reverse=True
                    )

                    # Keep last 3, delete the rest
                    if len(completed_ops) > 3:
                        to_delete = completed_ops[3:]
                        for old_ops in to_delete:
                            try:
                                ops_name = old_ops["metadata"]["name"]
                                await client_set.custom_api.delete_namespaced_custom_object(
                                    group="ops.kubedb.com",
                                    version="v1alpha1",
                                    namespace=db.namespace,
                                    plural=kubedb_service._get_ops_request_plural(db.engine),
                                    name=ops_name,
                                )
                                total_cleaned += 1
                            except Exception as e:
                                error_str = str(e).lower()
                                if "404" not in error_str and "not found" not in error_str:
                                    logger.debug(
                                        "failed_to_delete_ops_request",
                                        ops_request_name=old_ops["metadata"]["name"],
                                        error=str(e),
                                    )

                except Exception as e:
                    error_str = str(e).lower()
                    if "404" not in error_str and "not found" not in error_str:
                        logger.debug(
                            "cleanup_check_failed_for_database",
                            database_id=db.id,
                            database_name=db.name,
                            error=str(e),
                        )

            if total_cleaned > 0:
                logger.info(
                    "ops_requests_cleanup_completed",
                    provider_id=provider.id,
                    total_cleaned=total_cleaned,
                )

        except Exception as e:
            logger.error(
                "ops_request_cleanup_failed",
                provider_id=provider.id,
                error=str(e),
                exc_info=True,
            )


# Main entry point
async def main():
    """Run reconciliation worker."""
    # Initialize database connection
    from app.repositories.models import Database as DatabaseModel
    from app.repositories.models import Provider as ProviderModel
    from app.repositories.models import AuditLog
    from app.models.operation import Operation as OperationModel

    document_models = [DatabaseModel, ProviderModel, AuditLog, OperationModel]
    await DatabaseConnection.connect_db(document_models)

    # Get reconciliation interval from settings (default from settings.py: 30 seconds)
    reconcile_interval = settings.reconcile_interval

    # Create worker
    worker = ReconciliationWorker(reconcile_interval=reconcile_interval)

    # Setup signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum, frame):
        logger.info("shutdown_signal_received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    async def monitor_shutdown():
        await shutdown_event.wait()
        logger.info("shutdown_event_triggered")
        await worker.stop()

    shutdown_task = asyncio.create_task(monitor_shutdown())

    # Run worker
    worker_task = None
    try:
        worker_task = asyncio.create_task(worker.start())
        await asyncio.gather(worker_task, shutdown_task, return_exceptions=True)
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt_received")
        await worker.stop()
        if worker_task and not worker_task.done():
            worker_task.cancel()
    finally:
        await DatabaseConnection.disconnect_db()
        logger.info("reconciliation_worker_exited")


if __name__ == "__main__":
    asyncio.run(main())
