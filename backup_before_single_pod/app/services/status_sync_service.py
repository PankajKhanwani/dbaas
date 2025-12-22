"""
Database Reconciler Service

Efficiently reconciles all databases by syncing their status from KubeDB.
Uses batching and provider grouping for optimal performance.

Reconciliation Pattern:
1. Fetch all databases that need syncing
2. Group by provider_id to reuse Kubernetes clients
3. Process in batches for efficiency
4. Update database statuses based on actual KubeDB state
5. Handle errors gracefully without stopping the reconciler
"""
import asyncio
from typing import Dict, List, Optional
from collections import defaultdict

from beanie import operators

from app.config.logging import get_logger
from app.config.settings import settings
from app.models.database import DatabaseStatus
from app.repositories.models import Database, Provider
from app.services.kubedb_service import kubedb_service
from app.services.resource_allocation import ResourceAllocationService

logger = get_logger(__name__)


class DatabaseReconciler:
    """
    Efficient database reconciler that syncs all databases from KubeDB.
    
    Features:
    - Groups databases by provider for connection reuse
    - Processes in configurable batch sizes
    - Fully async operations
    - Graceful error handling
    - Efficient resource usage
    """

    def __init__(self):
        """Initialize database reconciler."""
        self.running = False
        self.reconcile_interval = settings.reconcile_interval
        self.batch_size = settings.reconcile_batch_size
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the background reconciliation task."""
        if self.running:
            logger.warning("reconciler_already_running")
            return

        self.running = True
        self.task = asyncio.create_task(self._reconcile_loop())
        logger.info(
            "reconciler_started",
            interval_seconds=self.reconcile_interval,
            batch_size=self.batch_size
        )

    async def stop(self) -> None:
        """Stop the background reconciliation task."""
        if not self.running:
            return

        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        logger.info("reconciler_stopped")

    async def _reconcile_loop(self) -> None:
        """Main reconciliation loop that runs periodically."""
        while self.running:
            try:
                await self.reconcile_all_databases()
            except Exception as e:
                logger.error("reconcile_loop_error", error=str(e), exc_info=True)

            # Wait before next reconciliation
            await asyncio.sleep(self.reconcile_interval)

    async def reconcile_all_databases(self) -> None:
        """
        Reconcile all databases efficiently.
        
        Strategy:
        1. Fetch all databases that need reconciliation
        2. Group by provider_id to reuse Kubernetes clients
        3. Process in batches to avoid overwhelming the system
        4. Update statuses based on actual KubeDB state
        """
        try:
            # Find all databases that need reconciliation
            databases = await Database.find(
                operators.In(Database.status, [
                    DatabaseStatus.PENDING,
                    DatabaseStatus.PROVISIONING,
                    DatabaseStatus.RUNNING,
                    DatabaseStatus.UPDATING,
                    DatabaseStatus.SCALING,
                    DatabaseStatus.DELETING,
                    DatabaseStatus.PAUSED,
                ])
            ).to_list()

            if not databases:
                logger.debug("no_databases_to_reconcile")
                return

            logger.info("reconciling_databases", total_count=len(databases))

            # Group databases by provider_id for efficient processing
            # This allows us to reuse Kubernetes clients
            databases_by_provider: Dict[Optional[str], List[Database]] = defaultdict(list)
            databases_without_provider = []

            for db in databases:
                if db.provider_id:
                    databases_by_provider[db.provider_id].append(db)
                else:
                    databases_without_provider.append(db)

            # Log databases without provider (should be migrated or deleted)
            if databases_without_provider:
                logger.warning(
                    "databases_without_provider",
                    count=len(databases_without_provider),
                    database_ids=[db.id for db in databases_without_provider[:5]]  # Log first 5
                )

            # Process each provider group
            total_successes = 0
            total_failures = 0

            for provider_id, provider_databases in databases_by_provider.items():
                try:
                    successes, failures = await self._reconcile_provider_databases(
                        provider_id=provider_id,
                        databases=provider_databases
                    )
                    total_successes += successes
                    total_failures += failures
                except Exception as e:
                    logger.error(
                        "provider_reconciliation_failed",
                        provider_id=provider_id,
                        database_count=len(provider_databases),
                        error=str(e)
                    )
                    total_failures += len(provider_databases)

            logger.info(
                "reconciliation_completed",
                total=len(databases),
                successes=total_successes,
                failures=total_failures,
                providers_processed=len(databases_by_provider)
            )

        except Exception as e:
            logger.error("reconciliation_failed", error=str(e), exc_info=True)

    async def _reconcile_provider_databases(
        self,
        provider_id: str,
        databases: List[Database]
    ) -> tuple[int, int]:
        """
        Reconcile databases for a specific provider.
        
        Groups by provider to reuse Kubernetes client connections.
        Processes in batches for efficiency.
        
        Args:
            provider_id: Provider ID
            databases: List of databases for this provider
            
        Returns:
            Tuple of (successes, failures)
        """
        # Fetch provider once for all databases
        provider = await Provider.find_one(Provider.id == provider_id)
        if not provider:
            logger.error(
                "provider_not_found",
                provider_id=provider_id,
                database_count=len(databases)
            )
            return 0, len(databases)

        if not provider.kubeconfig_content:
            logger.error(
                "provider_missing_kubeconfig",
                provider_id=provider_id,
                database_count=len(databases)
            )
            return 0, len(databases)

        kubeconfig_content = provider.kubeconfig_content

        # Process databases in batches
        successes = 0
        failures = 0

        for i in range(0, len(databases), self.batch_size):
            batch = databases[i:i + self.batch_size]
            
            # Process batch concurrently
            batch_tasks = [
                self._reconcile_database(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content
                )
                for db in batch
            ]
            
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            
            # Count successes and failures
            for result in results:
                if isinstance(result, Exception):
                    failures += 1
                else:
                    successes += 1

            logger.debug(
                "batch_reconciled",
                provider_id=provider_id,
                batch_start=i,
                batch_end=min(i + self.batch_size, len(databases)),
                batch_size=len(batch)
            )

        return successes, failures

    async def _reconcile_database(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str
    ) -> None:
        """
        Reconcile a single database from KubeDB.
        
        Fully async operation that:
        1. Fetches current status from KubeDB
        2. Updates database record if status changed
        3. Fetches endpoint if database became ready
        
        Args:
            db: Database document to reconcile
            provider_id: Provider ID
            kubeconfig_content: Provider's kubeconfig content
        """
        try:
            # Handle legacy databases without kubedb_resource_name
            if not db.kubedb_resource_name:
                db.kubedb_resource_name = f"{db.name}-{db.domain}-{db.project}".lower()[:63].rstrip('-')
                await db.save()
                logger.info(
                    "reconstructed_kubedb_resource_name",
                    database_id=db.id,
                    kubedb_resource_name=db.kubedb_resource_name
                )

            # Fetch detailed status from KubeDB (fully async)
            detailed_status = await kubedb_service.get_detailed_status(
                engine=db.engine,
                name=db.kubedb_resource_name,
                namespace=db.namespace,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )

            if not detailed_status:
                # Resource not found - might be deleted or not created yet
                # Check if we need to perform operations
                if db.status == DatabaseStatus.DELETING:
                    # Handle deletion - resource already deleted or being deleted
                    # Deallocate resources and remove database record
                    await self._handle_database_deletion(db, provider_id, kubeconfig_content)
                    return
                elif db.status == DatabaseStatus.PENDING:
                    # Database not created yet - reconciler will handle creation
                    # (creation is handled by _handle_database_creation in database_service)
                    return
                else:
                    logger.debug(
                        "kubedb_resource_not_found",
                        database_id=db.id,
                        name=db.kubedb_resource_name,
                        status=db.status.value,
                    )
                    return

            # Extract status information
            phase = detailed_status.get("phase", "Unknown")
            is_ready = detailed_status.get("ready", False)
            ready_replicas = detailed_status.get("ready_replicas", 0)
            desired_replicas = detailed_status.get("replicas", db.replicas)

            # Check if status changed
            old_health_status = db.health_status
            old_status = db.status
            old_ready_replicas = db.ready_replicas
            old_replicas = db.replicas

            # Update health status
            db.health_status = phase

            # Update replica counts
            # IMPORTANT: Update current_replicas, NOT replicas (desired state)!
            db.current_replicas = desired_replicas
            db.ready_replicas = ready_replicas

            # Map KubeDB phase to application status
            db.status = self._map_kubedb_phase_to_status(phase, is_ready)

            # If database became ready, fetch endpoint if not already set
            if is_ready and not db.endpoint:
                endpoint_info = await kubedb_service.get_database_endpoint(
                    engine=db.engine,
                    name=db.kubedb_resource_name,
                    namespace=db.namespace,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                if endpoint_info:
                    db.endpoint = endpoint_info.get("host")
                    db.port = endpoint_info.get("port")

            # Handle pending operations based on OLD status (before we update it)
            # The reconciler performs the actual KubeDB operations
            operation_performed = False
            if old_status == DatabaseStatus.UPDATING:
                # Handle update operation - patch KubeDB resource
                await self._handle_database_update_operation(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                operation_performed = True
            elif old_status == DatabaseStatus.SCALING:
                # Handle scaling operation - patch KubeDB resource
                await self._handle_database_scaling_operation(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                operation_performed = True
            elif old_status == DatabaseStatus.DELETING:
                # Handle deletion operation
                await self._handle_database_deletion(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                operation_performed = True
            elif old_status == DatabaseStatus.PAUSED:
                # Check if we need to pause (status is PAUSED but KubeDB might not be)
                if phase.lower() not in ["halted", "paused"]:
                    await self._handle_database_pause_operation(
                        db=db,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    operation_performed = True
            elif old_status == DatabaseStatus.PROVISIONING and phase.lower() == "halted":
                # Database is halted but status is PROVISIONING - need to resume
                await self._handle_database_resume_operation(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                operation_performed = True

            # Only update status from KubeDB phase if no operation is in progress
            # This preserves operation status (UPDATING, SCALING, etc.) until operation completes
            if not operation_performed:
                mapped_status = self._map_kubedb_phase_to_status(phase, is_ready)
                # Only update if not in an operation state
                if old_status not in [
                    DatabaseStatus.UPDATING,
                    DatabaseStatus.SCALING,
                    DatabaseStatus.DELETING,
                    DatabaseStatus.PAUSED,
                ]:
                    db.status = mapped_status
                # If operation completed (status changed from operation state), update
                elif old_status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING] and mapped_status == DatabaseStatus.RUNNING:
                    db.status = mapped_status
            
            # Reconcile resources/size to ensure pods match desired state
            # This ensures the database stays at the configured size
            # Run for RUNNING, UPDATING, and SCALING databases that are ready
            if is_ready and db.status in [
                DatabaseStatus.RUNNING,
                DatabaseStatus.UPDATING,
                DatabaseStatus.SCALING,
            ]:
                try:
                    await self._reconcile_database_resources(
                        db=db,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                except Exception as e:
                    logger.warning(
                        "resource_reconciliation_failed",
                        database_id=db.id,
                        error=str(e),
                    )
                    # Don't fail the entire reconciliation if resource check fails

            # Save if status changed
            if (old_health_status != db.health_status or
                old_status != db.status or
                old_ready_replicas != db.ready_replicas or
                old_replicas != db.replicas or
                (is_ready and not db.endpoint)):
                
                await db.save()
                
                logger.info(
                    "database_reconciled",
                    database_id=db.id,
                    old_status=old_status.value if old_status else None,
                    new_status=db.status.value,
                    old_health=old_health_status,
                    new_health=db.health_status,
                    replicas=f"{ready_replicas}/{db.replicas}",
                    is_ready=is_ready,
                )

        except Exception as e:
            logger.error(
                "database_reconciliation_failed",
                database_id=db.id,
                name=db.name,
                error=str(e),
            )
            # Don't raise - let other databases continue reconciling
            raise

    async def _reconcile_database_resources(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Reconcile database resources to ensure pods match desired size.
        
        This checks if:
        1. KubeDB CR resources match the database size
        2. Actual pod resources match the KubeDB CR
        3. If mismatches are found, it fixes them to keep the database at the desired size
        
        Args:
            db: Database document
            provider_id: Provider ID
            kubeconfig_content: Provider's kubeconfig content
        """
        try:
            # Get the KubeDB CR to check desired resources
            client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
            
            cr = await client_set.custom_api.get_namespaced_custom_object(
                group=kubedb_service._get_kubedb_group(db.engine),
                version=kubedb_service._get_kubedb_version(db.engine),
                namespace=db.namespace,
                plural=kubedb_service._get_kubedb_plural(db.engine),
                name=db.kubedb_resource_name,
            )

            # Get expected resources from database size
            expected_resources = kubedb_service._get_resource_limits(db.size)
            expected_resources_dict = {
                "requests": expected_resources,
                "limits": expected_resources,
            }

            # Extract actual resources from CR
            cr_resources = None
            container_name = None
            if "spec" in cr and "podTemplate" in cr["spec"]:
                containers = cr["spec"]["podTemplate"]["spec"].get("containers", [])
                container_names = {
                    "mongodb": "mongodb",
                    "postgres": "postgres",
                    "mysql": "mysql",
                    "mariadb": "mariadb",
                    "redis": "redis",
                    "elasticsearch": "elasticsearch",
                }
                container_name = container_names.get(db.engine.value, "mongodb")

                for container in containers:
                    if container.get("name") == container_name:
                        cr_resources = container.get("resources", {})
                        break

            # Check if CR resources match expected size
            cr_needs_update = False
            if not cr_resources:
                # CR has no resources defined, need to add them
                cr_needs_update = True
                logger.info(
                    "cr_resources_missing",
                    database_id=db.id,
                    expected_size=db.size.value,
                    expected_resources=expected_resources,
                )
            else:
                cr_requests = {k: str(v) for k, v in cr_resources.get("requests", {}).items()}
                cr_limits = {k: str(v) for k, v in cr_resources.get("limits", {}).items()}
                expected_requests = {k: str(v) for k, v in expected_resources.items()}
                expected_limits = {k: str(v) for k, v in expected_resources.items()}

                if cr_requests != expected_requests or cr_limits != expected_limits:
                    cr_needs_update = True
                    logger.info(
                        "cr_resources_mismatch_detected",
                        database_id=db.id,
                        expected_size=db.size.value,
                        cr_requests=cr_requests,
                        expected_requests=expected_requests,
                        cr_limits=cr_limits,
                        expected_limits=expected_limits,
                    )

            # Always use expected resources from database size as the source of truth
            desired_resources = expected_resources_dict

            # If CR doesn't match expected size, update it via OpsRequest
            # This is the proper KubeDB way - OpsRequest handles:
            # 1. CR update
            # 2. PetSet update
            # 3. Rolling pod restart
            # All coordinated properly by KubeDB Ops Manager

            # DISABLED: Reconciliation now handled by operation queue + worker
            # The status_sync_service only monitors status, does NOT create OpsRequests
            if cr_needs_update:
                logger.info(
                    "detected_cr_needs_update_but_skipping_reconciliation",
                    database_id=db.id,
                    size=db.size.value,
                    expected_resources=expected_resources,
                    message="Operations are now handled by operation queue + worker",
                )
                # Do NOT create OpsRequests here - let the worker handle it
                return

            # OLD CODE - DISABLED TO PREVENT DUPLICATE OPSREQUESTS
            # if cr_needs_update:
            #     logger.info(
            #         "updating_cr_to_match_database_size_via_ops_request",
            #         database_id=db.id,
            #         size=db.size.value,
            #         expected_resources=expected_resources,
            #     )
            #     try:
            #         # patch_database now uses OpsRequest for vertical scaling
            #         # This will create an OpsRequest and wait for it to complete
            #         await kubedb_service.patch_database(
            #             engine=db.engine,
            #             name=db.kubedb_resource_name,
            #             namespace=db.namespace,
            #             size=db.size,
            #             provider_id=provider_id,
            #             kubeconfig_content=kubeconfig_content,
            #         )
            #         logger.info(
            #             "database_reconciled_successfully_via_ops_request",
            #             database_id=db.id,
            #             size=db.size.value,
            #         )
            #         # OpsRequest handles pod updates automatically
            #         # No need for manual pod reconciliation
            #         return
            #     except Exception as e:
            #         logger.error(
            #             "failed_to_reconcile_via_ops_request",
            #             database_id=db.id,
            #             error=str(e),
            #             exc_info=True,
            #         )
            #         # Don't try manual pod reconciliation - let OpsRequest handle it
            #         return

        except Exception as e:
            logger.warning(
                "resource_reconciliation_check_failed",
                database_id=db.id,
                error=str(e),
            )
            # Don't raise - this is a background check

    async def _check_ops_request_status(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Check for active OpsRequests and update database status accordingly.

        This monitors OpsRequests created by the fire-and-forget approach
        and updates the database status when they complete or fail.

        Args:
            db: Database document
            provider_id: Provider ID
            kubeconfig_content: Provider's kubeconfig content
        """
        try:
            client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)

            # List all OpsRequests in namespace and filter by database name
            # Note: OpsRequests don't have labels by default, so we filter by name pattern
            ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=db.namespace,
                plural=kubedb_service._get_ops_request_plural(db.engine),
            )

            # Filter OpsRequests that target this database
            all_items = ops_requests.get("items", [])
            items = [
                item for item in all_items
                if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
            ]

            if not items:
                return

            # Sort by creation timestamp (most recent first)
            items.sort(
                key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""),
                reverse=True
            )

            latest_ops_request = items[0]
            ops_request_name = latest_ops_request["metadata"]["name"]
            ops_status = latest_ops_request.get("status", {})
            phase = ops_status.get("phase", "Unknown")

            logger.debug(
                "checking_ops_request_status",
                database_id=db.id,
                ops_request_name=ops_request_name,
                phase=phase,
            )

            # Update database status based on OpsRequest phase
            if phase == "Successful":
                # OpsRequest completed successfully
                logger.info(
                    "ops_request_successful",
                    database_id=db.id,
                    ops_request_name=ops_request_name,
                )
                # Status will be updated to RUNNING by the main reconciler
                # based on KubeDB resource phase

            elif phase == "Failed":
                # OpsRequest failed
                conditions = ops_status.get("conditions", [])
                failure_reason = "Unknown"
                for condition in conditions:
                    if condition.get("type") == "Failed" or condition.get("reason") == "Failed":
                        failure_reason = condition.get("message", "Unknown")
                        break

                logger.error(
                    "ops_request_failed",
                    database_id=db.id,
                    ops_request_name=ops_request_name,
                    reason=failure_reason,
                )

                # Mark database as failed
                db.status = DatabaseStatus.FAILED
                db.health_status = f"OpsRequest failed: {failure_reason}"
                await db.save()

            elif phase in ["Pending", "Progressing"]:
                # OpsRequest is still in progress
                logger.debug(
                    "ops_request_in_progress",
                    database_id=db.id,
                    ops_request_name=ops_request_name,
                    phase=phase,
                )
                # Keep current status (UPDATING or SCALING)

            # Clean up old completed/failed OpsRequests (keep last 3)
            if len(items) > 3:
                terminal_phases = ["Successful", "Failed", "Skipped"]
                old_ops_requests = [
                    item for item in items[3:]  # Skip the 3 most recent
                    if item.get("status", {}).get("phase") in terminal_phases
                ]

                for old_ops in old_ops_requests:
                    try:
                        await client_set.custom_api.delete_namespaced_custom_object(
                            group="ops.kubedb.com",
                            version="v1alpha1",
                            namespace=db.namespace,
                            plural=kubedb_service._get_ops_request_plural(db.engine),
                            name=old_ops["metadata"]["name"],
                        )
                        logger.debug(
                            "cleaned_up_old_ops_request",
                            database_id=db.id,
                            ops_request_name=old_ops["metadata"]["name"],
                        )
                    except Exception as cleanup_error:
                        logger.debug(
                            "failed_to_cleanup_ops_request",
                            ops_request_name=old_ops["metadata"]["name"],
                            error=str(cleanup_error),
                        )

        except Exception as e:
            logger.warning(
                "ops_request_status_check_failed",
                database_id=db.id,
                error=str(e),
            )
            # Don't raise - this is a background check

    async def _handle_database_update_operation(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Handle database update operation.
        
        Patches KubeDB resource with updated values from database record.
        """
        try:
            # Get current CR to check what needs updating
            client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
            current_cr = await client_set.custom_api.get_namespaced_custom_object(
                group=kubedb_service._get_kubedb_group(db.engine),
                version=kubedb_service._get_kubedb_version(db.engine),
                namespace=db.namespace,
                plural=kubedb_service._get_kubedb_plural(db.engine),
                name=db.kubedb_resource_name,
            )
            
            # Check what needs updating by comparing CR with database record
            needs_update = False
            patch_replicas = None
            patch_storage = None
            patch_size = None
            
            # Check replicas
            cr_replicas = current_cr.get("spec", {}).get("replicas", 1)
            if cr_replicas != db.replicas:
                patch_replicas = db.replicas
                needs_update = True
            
            # Check storage
            cr_storage = current_cr.get("spec", {}).get("storage", {}).get("resources", {}).get("requests", {}).get("storage", "")
            # Parse storage (e.g., "20Gi" -> 20)
            cr_storage_gb = 0
            if cr_storage:
                try:
                    cr_storage_gb = int(cr_storage.replace("Gi", "").replace("G", ""))
                except:
                    pass
            if cr_storage_gb != db.storage_gb:
                patch_storage = db.storage_gb
                needs_update = True
            
            # Check size (resources)
            cr_resources = None
            if "spec" in current_cr and "podTemplate" in current_cr["spec"]:
                containers = current_cr["spec"]["podTemplate"]["spec"].get("containers", [])
                for c in containers:
                    if c.get("name") == "mongodb" or c.get("name") == db.engine.value:
                        cr_resources = c.get("resources", {})
                        break
            
            expected_resources = kubedb_service._get_resource_limits(db.size)
            if cr_resources:
                cr_requests = {k: str(v) for k, v in cr_resources.get("requests", {}).items()}
                expected_requests = {k: str(v) for k, v in expected_resources.items()}
                if cr_requests != expected_requests:
                    patch_size = db.size
                    needs_update = True
            
            if needs_update:
                # DISABLED: Updates now handled by operation queue + worker
                # The status_sync_service only monitors, does NOT create operations
                logger.info(
                    "detected_update_needed_but_skipping",
                    database_id=db.id,
                    replicas=patch_replicas,
                    storage_gb=patch_storage,
                    size=patch_size,
                    message="Operations are now handled by operation queue + worker",
                )
                # Do NOT call patch_database here - the worker handles it
                return

                # OLD CODE - DISABLED TO PREVENT DUPLICATE OPSREQUESTS
                # logger.info(
                #     "performing_database_update",
                #     database_id=db.id,
                #     replicas=patch_replicas,
                #     storage_gb=patch_storage,
                #     size=patch_size,
                # )
                #
                # await kubedb_service.patch_database(
                #     engine=db.engine,
                #     name=db.kubedb_resource_name,
                #     namespace=db.namespace,
                #     replicas=patch_replicas,
                #     storage_gb=patch_storage,
                #     size=patch_size,
                #     provider_id=provider_id,
                #     kubeconfig_content=kubeconfig_content,
                # )
                #
                # logger.info(
                #     "database_update_completed",
                #     database_id=db.id,
                #     message="Status will be updated by next reconciliation cycle"
                # )
            else:
                logger.debug(
                    "database_already_up_to_date",
                    database_id=db.id,
                )
                # Update status to RUNNING since nothing needs updating
                db.status = DatabaseStatus.RUNNING
                await db.save()
                
        except Exception as e:
            logger.error(
                "database_update_operation_failed",
                database_id=db.id,
                error=str(e),
                exc_info=True,
            )
            db.status = DatabaseStatus.FAILED
            await db.save()
    
    async def _handle_database_scaling_operation(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Handle database scaling operation.
        
        Same as update operation - patches KubeDB resource.
        """
        # Scaling is the same as update
        await self._handle_database_update_operation(
            db=db,
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )
    
    async def _handle_database_deletion(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Handle database deletion operation.
        
        Deletes KubeDB resource, deallocates resources, and removes database record.
        """
        try:
            logger.info(
                "performing_database_deletion",
                database_id=db.id,
                name=db.kubedb_resource_name,
            )
            
            # Delete from KubeDB
            deleted = await kubedb_service.delete_database(
                engine=db.engine,
                name=db.kubedb_resource_name,
                namespace=db.namespace,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )
            
            if deleted:
                # Deallocate resources from provider if provider_id exists
                if db.provider_id and db.allocated_cpu_cores is not None:
                    try:
                        await ResourceAllocationService.deallocate_resources(
                            provider_id=db.provider_id,
                            cpu_cores=db.allocated_cpu_cores,
                            memory_gb=db.allocated_memory_gb or 0,
                            storage_gb=db.allocated_storage_gb or 0,
                            database_id=db.id,
                        )
                        logger.info(
                            "resources_deallocated",
                            database_id=db.id,
                            provider_id=db.provider_id,
                        )
                    except Exception as e:
                        logger.error(
                            "resource_deallocation_failed",
                            database_id=db.id,
                            error=str(e),
                        )
                
                # Remove database record from MongoDB
                await db.delete()
                logger.info("database_deleted", database_id=db.id, name=db.name)
            else:
                logger.warning(
                    "database_deletion_failed",
                    database_id=db.id,
                    name=db.name,
                )
                db.status = DatabaseStatus.FAILED
                await db.save()
                
        except Exception as e:
            logger.error(
                "database_deletion_operation_failed",
                database_id=db.id,
                error=str(e),
                exc_info=True,
            )
            db.status = DatabaseStatus.FAILED
            await db.save()
    
    async def _handle_database_pause_operation(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Handle database pause operation.
        """
        try:
            logger.info(
                "performing_database_pause",
                database_id=db.id,
            )
            
            await kubedb_service.pause_database(
                engine=db.engine,
                name=db.kubedb_resource_name,
                namespace=db.namespace,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )
            
            logger.info(
                "database_pause_completed",
                database_id=db.id,
                message="Status will be updated by next reconciliation cycle"
            )
            
        except Exception as e:
            logger.error(
                "database_pause_operation_failed",
                database_id=db.id,
                error=str(e),
                exc_info=True,
            )
            db.status = DatabaseStatus.FAILED
            await db.save()
    
    async def _handle_database_resume_operation(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Handle database resume operation.
        """
        try:
            logger.info(
                "performing_database_resume",
                database_id=db.id,
            )
            
            await kubedb_service.resume_database(
                engine=db.engine,
                name=db.kubedb_resource_name,
                namespace=db.namespace,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )
            
            logger.info(
                "database_resume_completed",
                database_id=db.id,
                message="Status will be updated by next reconciliation cycle"
            )
            
        except Exception as e:
            logger.error(
                "database_resume_operation_failed",
                database_id=db.id,
                error=str(e),
                exc_info=True,
            )
            db.status = DatabaseStatus.FAILED
            await db.save()

    def _map_kubedb_phase_to_status(self, phase: str, is_ready: bool) -> DatabaseStatus:
        """
        Map KubeDB phase to application DatabaseStatus.

        Args:
            phase: KubeDB resource phase
            is_ready: Whether database is ready

        Returns:
            Mapped DatabaseStatus
        """
        phase_lower = phase.lower()

        # KubeDB phases: Provisioning, Ready, Running, Halted, Failed, Unknown
        if phase_lower in ["ready", "running"] and is_ready:
            return DatabaseStatus.RUNNING
        elif phase_lower == "provisioning":
            return DatabaseStatus.PROVISIONING
        elif phase_lower == "failed":
            return DatabaseStatus.FAILED
        elif phase_lower == "halted":
            return DatabaseStatus.PAUSED
        elif phase_lower in ["pending", "unknown"]:
            return DatabaseStatus.PENDING
        else:
            # Default: keep updating status
            return DatabaseStatus.UPDATING


# Global reconciler instance (replaces StatusSyncService)
database_reconciler = DatabaseReconciler()

# Backward compatibility alias
status_sync_service = database_reconciler
