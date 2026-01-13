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
import tempfile
from typing import Dict, List, Optional, Any
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
                    DatabaseStatus.PAUSING,
                    DatabaseStatus.PAUSED,
                    DatabaseStatus.RESUMING,
                    DatabaseStatus.DELETING,
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

            # Cleanup old OpsRequests for all providers
            try:
                await self._cleanup_old_ops_requests(databases_by_provider)
            except Exception as cleanup_error:
                logger.error("ops_request_cleanup_failed", error=str(cleanup_error))

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
                elif db.status == DatabaseStatus.DELETED:
                    # Already marked as deleted - just return
                    return
                else:
                    # Resource not found and status is RUNNING/UPDATING/etc
                    # This means the resource was deleted outside our system
                    # Mark as DELETED and clean up
                    logger.warning(
                        "kubedb_resource_not_found_marking_deleted",
                        database_id=db.id,
                        name=db.kubedb_resource_name,
                        current_status=db.status.value,
                    )
                    # Handle deletion to cleanup resources and remove record
                    await self._handle_database_deletion(db, provider_id, kubeconfig_content)
                    return

            # CRITICAL: If status is DELETING, preserve it and handle deletion IMMEDIATELY
            # This check must be BEFORE extracting phase/status info to prevent override
            # This prevents reconciler from overriding DELETING status back to PROVISIONING
            if db.status == DatabaseStatus.DELETING:
                logger.info(
                    "database_status_deleting_preserving",
                    database_id=db.id,
                    message="Status is DELETING - preserving it and handling deletion immediately"
                )
                # Call deletion handler to delete KubeDB resource and clean up
                await self._handle_database_deletion(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                return  # Exit after deletion is handled - don't update status based on KubeDB phase

            # Extract status information
            phase = detailed_status.get("phase", "Unknown")
            is_ready = detailed_status.get("ready", False)
            ready_replicas = detailed_status.get("ready_replicas", 0)
            desired_replicas = detailed_status.get("replicas", db.replicas)
            kubedb_version = detailed_status.get("version")  # Version from KubeDB resource
            deletion_timestamp = detailed_status.get("deletionTimestamp")  # Check if being deleted

            # If resource has deletionTimestamp, it's being deleted - set status and return
            if deletion_timestamp:
                logger.info(
                    "database_resource_being_deleted",
                    database_id=db.id,
                    deletion_timestamp=deletion_timestamp,
                )
                if db.status != DatabaseStatus.DELETING:
                    db.status = DatabaseStatus.DELETING
                    await db.save()
                return

            # Check if status changed
            old_health_status = db.health_status
            old_status = db.status
            old_ready_replicas = db.ready_replicas
            old_replicas = db.replicas
            old_version = db.version

            # Update health status
            db.health_status = phase
            
            # ABSOLUTE FIX: If status is FAILED but database is actually healthy, fix it immediately
            # Also check if it was recently SCALING - if so, restore SCALING status
            if db.status == DatabaseStatus.FAILED:
                if (phase.lower() in ["ready", "running"]) or (db.health_status and db.health_status.lower() in ["ready", "running"]):
                    # Check if there's an active OpsRequest (might be scaling in progress)
                    has_active_ops = await self._has_active_ops_request(db, provider_id, kubeconfig_content)
                    
                    if has_active_ops:
                        # There's an active OpsRequest - might be scaling, restore SCALING status
                        logger.error(
                            "fixing_failed_status_healthy_database_with_active_ops",
                            database_id=db.id,
                            current_status=db.status.value,
                            health_status=db.health_status,
                            kubedb_phase=phase,
                            is_ready=is_ready,
                            message="ABSOLUTE FIX: Status is FAILED but database is healthy with active OpsRequest - FORCING SCALING immediately",
                        )
                        db.status = DatabaseStatus.SCALING
                        await db.save()
                        logger.info(
                            "fixed_failed_status_to_scaling",
                            database_id=db.id,
                            message="Fixed FAILED status to SCALING because database is healthy with active OpsRequest",
                        )
                    else:
                        # No active OpsRequest - database is healthy, set to RUNNING
                        logger.error(
                            "fixing_failed_status_healthy_database",
                            database_id=db.id,
                            current_status=db.status.value,
                            health_status=db.health_status,
                            kubedb_phase=phase,
                            is_ready=is_ready,
                            message="ABSOLUTE FIX: Status is FAILED but database is healthy - FORCING RUNNING immediately",
                        )
                        db.status = DatabaseStatus.RUNNING
                        await db.save()
                        logger.info(
                            "fixed_failed_status_to_running",
                            database_id=db.id,
                            message="Fixed FAILED status to RUNNING because database is healthy",
                        )

            # Update replica counts
            # Check if there's an active OpsRequest for this database
            # If OpsRequest is in progress, don't update current_replicas yet
            # (reconciler needs to detect drift)
            has_active_ops_request = await self._has_active_ops_request(
                db, provider_id, kubeconfig_content
            )
            
            if not has_active_ops_request:
                # No active OpsRequest - safe to update current_replicas from K8s spec
                # This represents what KubeDB has achieved (spec.replicas)
                db.current_replicas = desired_replicas
            # If there's an active OpsRequest, don't update current_replicas
            # Let it be updated after OpsRequest completes (in next reconciliation cycle)
            
            # CRITICAL: ready_replicas should NEVER exceed desired_replicas
            # Cap it to desired_replicas to prevent showing 5/4 or similar
            # This can happen during scaling when old pods are still running
            capped_ready_replicas = min(ready_replicas, desired_replicas)
            if ready_replicas > desired_replicas:
                logger.warning(
                    "capping_ready_replicas_to_desired",
                    database_id=db.id,
                    ready_replicas=ready_replicas,
                    desired_replicas=desired_replicas,
                    capped_ready_replicas=capped_ready_replicas,
                    message=f"ready_replicas ({ready_replicas}) exceeds desired_replicas ({desired_replicas}) - capping to {capped_ready_replicas}",
                )
            db.ready_replicas = capped_ready_replicas
            
            # Update version if it changed in KubeDB (e.g., after upgrade)
            if kubedb_version:
                if kubedb_version != db.version:
                    logger.info(
                        "database_version_updated_from_kubedb",
                        database_id=db.id,
                        old_version=db.version,
                        new_version=kubedb_version,
                        kubedb_phase=phase,
                    )
                    db.version = kubedb_version
                else:
                    logger.debug(
                        "database_version_unchanged",
                        database_id=db.id,
                        version=db.version,
                    )
            else:
                logger.debug(
                    "kubedb_version_not_available",
                    database_id=db.id,
                    current_db_version=db.version,
                )

            # Update current_size from K8s CR resources
            # This ensures current_size reflects the actual state in K8s
            # Always update it - if there's an active OpsRequest, we'll update it after it completes
            if db.size:
                try:
                    # Get client set for fetching CR
                    client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
                    
                    # Get the CR to extract resources
                    cr = await client_set.custom_api.get_namespaced_custom_object(
                        group=kubedb_service._get_kubedb_group(db.engine),
                        version=kubedb_service._get_kubedb_version(db.engine),
                        namespace=db.namespace,
                        plural=kubedb_service._get_kubedb_plural(db.engine),
                        name=db.kubedb_resource_name,
                    )
                    
                    # Extract resources from CR
                    cr_resources = None
                    if "spec" in cr and "podTemplate" in cr["spec"]:
                        containers = cr["spec"]["podTemplate"]["spec"].get("containers", [])
                        for container in containers:
                            if container.get("name") in ["mongodb", "postgres", "mysql", db.engine.value]:
                                cr_resources = container.get("resources", {})
                                break
                    
                    if cr_resources:
                        # Get expected resources for current size
                        expected_resources = kubedb_service._get_resource_limits(db.size)
                        expected_cpu = str(expected_resources.get("cpu", ""))
                        expected_memory = str(expected_resources.get("memory", ""))
                        
                        # Get actual resources from CR
                        cr_requests = cr_resources.get("requests", {}) if cr_resources else {}
                        cr_cpu = str(cr_requests.get("cpu", ""))
                        cr_memory = str(cr_requests.get("memory", ""))
                        
                        # If CR resources match desired size, update current_size
                        if cr_cpu == expected_cpu and cr_memory == expected_memory:
                            if db.size != db.current_size:
                                logger.info(
                                    "updating_current_size_from_cr",
                                    database_id=db.id,
                                    old_current_size=db.current_size.value if db.current_size else None,
                                    new_current_size=db.size.value,
                                    cr_cpu=cr_cpu,
                                    cr_memory=cr_memory,
                                )
                                db.current_size = db.size
                        else:
                            # CR resources don't match desired - try to determine size from CR
                            from app.models.database import DatabaseSize
                            cr_size = self._get_size_from_cr_resources(cr, db.engine.value)
                            if cr_size and cr_size != db.current_size:
                                logger.info(
                                    "updating_current_size_from_cr_resources",
                                    database_id=db.id,
                                    old_current_size=db.current_size.value if db.current_size else None,
                                    new_current_size=cr_size.value,
                                    cr_cpu=cr_cpu,
                                    cr_memory=cr_memory,
                                )
                                db.current_size = cr_size
                except Exception as e:
                    logger.debug(
                        "failed_to_update_current_size_from_cr",
                        database_id=db.id,
                        error=str(e),
                    )

            # Check if there's an active OpsRequest in K8s (source of truth for scaling operations)
            # Scaling operations are now fire-and-forget, so we check K8s OpsRequests, not Operation records
            # Also check for recently created OpsRequests (within last 60 seconds) to prevent race conditions
            has_active_ops_request = await self._has_active_ops_request(
                db, provider_id, kubeconfig_content
            )
            
            # Also check for recently created OpsRequests (within last 60 seconds)
            # This prevents status_sync_service from immediately overriding UPDATING status
            # when PATCH endpoint just created an OpsRequest but it's not visible yet
            has_recent_ops_request = False
            if not has_active_ops_request and db.status == DatabaseStatus.UPDATING:
                try:
                    from datetime import datetime, timezone
                    client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
                    ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                        group="ops.kubedb.com",
                        version="v1alpha1",
                        namespace=db.namespace,
                        plural=kubedb_service._get_ops_request_plural(db.engine),
                    )
                    all_items = ops_requests.get("items", [])
                    db_ops = [
                        item for item in all_items
                        if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
                    ]
                    
                    now = datetime.now(timezone.utc)
                    for item in db_ops:
                        creation_time_str = item.get("metadata", {}).get("creationTimestamp", "")
                        if creation_time_str:
                            try:
                                if creation_time_str.endswith("Z"):
                                    creation_time_str_clean = creation_time_str[:-1] + "+00:00"
                                else:
                                    creation_time_str_clean = creation_time_str
                                creation_time = datetime.fromisoformat(creation_time_str_clean)
                                if creation_time.tzinfo is None:
                                    creation_time = creation_time.replace(tzinfo=timezone.utc)
                                age_seconds = (now - creation_time).total_seconds()
                                # If created within last 60 seconds, treat as active
                                if age_seconds < 60:
                                    has_recent_ops_request = True
                                    logger.debug(
                                        "recent_ops_request_found_preserving_updating_status",
                                        database_id=db.id,
                                        ops_request_name=item.get("metadata", {}).get("name"),
                                        age_seconds=age_seconds,
                                    )
                                    break
                            except Exception:
                                pass
                except Exception as e:
                    logger.debug(
                        "failed_to_check_recent_ops_request",
                        database_id=db.id,
                        error=str(e),
                    )

            # Check for active Operation records (only for upgrade operations, not scaling)
            # Also check for any upgrade operation that exists (even if not yet processed)
            # This prevents false positives where upgrade was just initiated
            from app.models.operation import Operation, OperationStatus, OperationType
            active_operation = await Operation.find_one({
                "database_id": db.id,
                "status": {"$in": [
                    OperationStatus.QUEUED.value,
                    OperationStatus.IN_PROGRESS.value,
                ]},
                "type": OperationType.UPGRADE_VERSION.value,  # Only check for upgrade operations
            })
            
            # Also check for any recent upgrade operation (created within last 5 minutes)
            # This catches cases where upgrade was just initiated but Operation status hasn't been set yet
            if not active_operation and db.status == DatabaseStatus.UPDATING:
                from datetime import datetime, timezone, timedelta
                recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                recent_upgrade_operation = await Operation.find_one({
                    "database_id": db.id,
                    "type": OperationType.UPGRADE_VERSION.value,
                    "created_at": {"$gte": recent_cutoff},
                })
                if recent_upgrade_operation:
                    active_operation = recent_upgrade_operation
                    logger.debug(
                        "found_recent_upgrade_operation",
                        database_id=db.id,
                        operation_id=str(recent_upgrade_operation.id),
                        operation_status=recent_upgrade_operation.status.value,
                    )

            # Log for debugging
            logger.info(
                "status_sync_checking_active_operation",
                database_id=db.id,
                has_active_ops_request=has_active_ops_request,
                has_active_operation=bool(active_operation),
                active_op_id=str(active_operation.id) if active_operation else None,
                current_db_status=db.status.value,
                kubedb_phase=phase,
                is_ready=is_ready,
            )

            # NOTE: DELETING status is now handled earlier in the function (after checking if resource exists)
            # This ensures DELETING status is preserved and deletion is handled before any status updates
            # The check above (after detailed_status check) handles DELETING status

            # Handle FAILED status - preserve it unless explicitly resolved
            # FAILED status should only change to RUNNING when:
            # 1. There's no drift (desired == current)
            # 2. Database is actually healthy (K8s phase is Ready/Running)
            # 3. No active OpsRequests
            # 4. User hasn't explicitly requested a change
            if db.status == DatabaseStatus.FAILED:
                # Check if database is actually healthy now and there's no drift
                is_healthy = phase.lower() in ["ready", "running"] and is_ready
                has_drift = False
                
                # Check for drift: compare desired vs current
                try:
                    # Check size drift
                    if db.size and db.current_size and db.size != db.current_size:
                        has_drift = True
                        logger.debug(
                            "failed_status_has_size_drift",
                            database_id=db.id,
                            desired_size=db.size.value,
                            current_size=db.current_size.value if db.current_size else None,
                        )
                    # Check replica drift
                    if db.replicas and db.current_replicas and db.replicas != db.current_replicas:
                        has_drift = True
                        logger.debug(
                            "failed_status_has_replica_drift",
                            database_id=db.id,
                            desired_replicas=db.replicas,
                            current_replicas=db.current_replicas,
                        )
                    # Check storage drift
                    if db.storage_gb and db.current_storage_gb and db.storage_gb != db.current_storage_gb:
                        has_drift = True
                        logger.debug(
                            "failed_status_has_storage_drift",
                            database_id=db.id,
                            desired_storage=db.storage_gb,
                            current_storage=db.current_storage_gb,
                        )
                except Exception as e:
                    logger.debug(
                        "failed_to_check_drift_for_failed_status",
                        database_id=db.id,
                        error=str(e),
                    )
                
                # Check for active OpsRequests
                has_active_ops = await self._has_active_ops_request(db, provider_id, kubeconfig_content)
                
                # Only transition from FAILED to RUNNING if:
                # - Database is healthy
                # - No drift
                # - No active OpsRequests
                if is_healthy and not has_drift and not has_active_ops:
                    logger.info(
                        "failed_status_resolved_transitioning_to_running",
                        database_id=db.id,
                        kubedb_phase=phase,
                        is_ready=is_ready,
                        message="Database is healthy, no drift, no active OpsRequests - transitioning from FAILED to RUNNING",
                    )
                    # Don't return - let status be updated to RUNNING below
                else:
                    # Keep FAILED status
                    logger.debug(
                        "preserving_failed_status",
                        database_id=db.id,
                        kubedb_phase=phase,
                        is_healthy=is_healthy,
                        has_drift=has_drift,
                        has_active_ops=has_active_ops,
                        message="Database status is FAILED - preserving until explicitly resolved",
                    )
                    await db.save()
                    return  # Exit - don't change status

            # Handle PAUSING/PAUSED/RESUMING status transitions based on K8s phase
            if db.status == DatabaseStatus.PAUSING:
                # Database is being paused - check if pause completed
                if phase.lower() in ["halted", "paused"]:
                    # Pause completed - update to PAUSED
                    logger.info(
                        "pause_completed_updating_to_paused",
                        database_id=db.id,
                        kubedb_phase=phase,
                    )
                    db.status = DatabaseStatus.PAUSED
                    await db.save()
                    return
                else:
                    # Database is not halted yet - need to pause it
                    logger.info(
                        "database_pausing_executing_pause",
                        database_id=db.id,
                        kubedb_phase=phase,
                        message="Status is PAUSING and K8s is not Halted - pausing KubeDB resource",
                    )
                    try:
                        await kubedb_service.pause_database(
                            engine=db.engine,
                            name=db.kubedb_resource_name,
                            namespace=db.namespace,
                            provider_id=provider_id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        logger.info(
                            "database_pause_executed",
                            database_id=db.id,
                            message="KubeDB resource paused successfully",
                        )
                    except Exception as e:
                        logger.error(
                            "database_pause_failed",
                            database_id=db.id,
                            error=str(e),
                        )
                    # Preserve PAUSING status - status_sync will update to PAUSED when K8s becomes Halted
                    logger.debug(
                        "preserving_pausing_status_after_pause_call",
                        database_id=db.id,
                        kubedb_phase=phase,
                    )
                    await db.save()
                    return

            elif db.status == DatabaseStatus.PAUSED:
                # Database is paused - only preserve if K8s is still halted
                # BUT: If user clicked resume, status should be RESUMING, not PAUSED
                # So if status is PAUSED and K8s is halted, preserve it
                # If status is PAUSED but K8s is not halted, it means it was resumed externally
                if phase.lower() in ["halted", "paused"]:
                    # K8s is still paused - preserve PAUSED status
                    logger.debug(
                        "preserving_paused_status_k8s_halted",
                        database_id=db.id,
                        kubedb_phase=phase,
                    )
                    await db.save()
                    return
                else:
                    # K8s is NOT paused - database was resumed externally or resume is in progress
                    # Check if there's a resume operation in progress
                    # If not, update to RUNNING
                    logger.info(
                        "database_unpaused_externally_or_resuming",
                        database_id=db.id,
                        kubedb_phase=phase,
                        is_ready=is_ready,
                        message="K8s is not halted but status is PAUSED - likely resume in progress"
                    )
                    # Don't return - let status be updated below to RUNNING if ready

            elif db.status == DatabaseStatus.RESUMING:
                # Database is being resumed - check if resume completed
                if phase.lower() in ["ready", "running"] and is_ready:
                    # Use comprehensive check before setting to RUNNING
                    truly_ready_result = await self._is_database_truly_ready(
                        db=db,
                        detailed_status=detailed_status,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    
                    if truly_ready_result["is_ready"]:
                        logger.info(
                            "resume_completed_updating_to_running",
                            database_id=db.id,
                            kubedb_phase=phase,
                            checks=truly_ready_result["checks"],
                        )
                        db.status = DatabaseStatus.RUNNING
                        await db.save()
                        return
                    else:
                        logger.info(
                            "resume_phase_ready_but_not_truly_ready",
                            database_id=db.id,
                            kubedb_phase=phase,
                            reasons=", ".join(truly_ready_result["reasons"]),
                            message="Resume phase is Ready but database not truly ready - keeping RESUMING status",
                        )
                        # Keep RESUMING status - will check again in next reconciliation
                        return
                elif phase.lower() in ["halted", "paused"]:
                    # Database is halted - need to resume it
                    logger.info(
                        "database_resuming_executing_resume",
                        database_id=db.id,
                        kubedb_phase=phase,
                        message="Status is RESUMING and K8s is Halted - resuming KubeDB resource",
                    )
                    try:
                        await kubedb_service.resume_database(
                            engine=db.engine,
                            name=db.kubedb_resource_name,
                            namespace=db.namespace,
                            provider_id=provider_id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        logger.info(
                            "database_resume_executed",
                            database_id=db.id,
                            message="KubeDB resource resumed successfully",
                        )
                    except Exception as e:
                        logger.error(
                            "database_resume_failed",
                            database_id=db.id,
                            error=str(e),
                        )
                    # Preserve RESUMING status - status_sync will update to RUNNING when K8s becomes Ready
                    logger.debug(
                        "preserving_resuming_status_after_resume_call",
                        database_id=db.id,
                        kubedb_phase=phase,
                    )
                    await db.save()
                    return
                else:
                    # In transition - preserve RESUMING status
                    logger.debug(
                        "preserving_resuming_status",
                        database_id=db.id,
                        kubedb_phase=phase,
                    )
                    await db.save()
                    return

            # Update status based on K8s state
            # CRITICAL: Final states (SCALING, UPDATING, DELETING) should ONLY change to terminal states (RUNNING, FAILED, DELETED)
            # SCALING -> RUNNING (not UPDATING)
            # UPDATING -> RUNNING (not SCALING)
            # DELETING -> DELETED (not anything else)
            # IMPORTANT: Don't change SCALING to UPDATING or vice versa
            if has_active_ops_request or has_recent_ops_request or active_operation:
                # If there's an active operation, preserve the current operation status
                # Don't change SCALING to UPDATING or UPDATING to SCALING
                if db.status not in [DatabaseStatus.SCALING, DatabaseStatus.UPDATING]:
                    # Only set to UPDATING if current status is not a final operation state
                    logger.info(
                        "setting_status_to_updating_due_to_active_operation",
                        database_id=db.id,
                        has_active_ops_request=has_active_ops_request,
                        has_recent_ops_request=has_recent_ops_request,
                        has_active_operation=bool(active_operation),
                        operation_id=str(active_operation.id) if active_operation else None,
                        current_status=db.status.value,
                    )
                    db.status = DatabaseStatus.UPDATING
                else:
                    # Preserve current operation status (SCALING or UPDATING)
                    logger.debug(
                        "preserving_operation_status",
                        database_id=db.id,
                        current_status=db.status.value,
                        has_active_ops_request=has_active_ops_request,
                        has_recent_ops_request=has_recent_ops_request,
                        has_active_operation=bool(active_operation),
                        message="Preserving current operation status (SCALING/UPDATING) - will only change to RUNNING when ready",
                    )
            elif db.status == DatabaseStatus.UPDATING:
                # Status is UPDATING but no active/recent OpsRequest found
                # This could mean OpsRequest completed or was never created
                # Check if OpsRequest actually completed by checking for successful ones
                try:
                    client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
                    ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                        group="ops.kubedb.com",
                        version="v1alpha1",
                        namespace=db.namespace,
                        plural=kubedb_service._get_ops_request_plural(db.engine),
                    )
                    all_items = ops_requests.get("items", [])
                    db_ops = [
                        item for item in all_items
                        if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
                    ]
                    
                    # Check if most recent OpsRequest is successful
                    if db_ops:
                        db_ops.sort(
                            key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""),
                            reverse=True
                        )
                        latest_ops = db_ops[0]
                        latest_phase = latest_ops.get("status", {}).get("phase", "Unknown")
                        
                        # If latest OpsRequest is successful, it's safe to change to RUNNING
                        if latest_phase == "Successful":
                            mapped_status = self._map_kubedb_phase_to_status(phase, is_ready)

                            # If OpsRequest succeeded but phase is still "failed", don't set to FAILED
                            # The database might be recovering - give it time to reach Ready state
                            if mapped_status == DatabaseStatus.FAILED:
                                logger.warning(
                                    "opsrequest_successful_but_phase_failed",
                                    database_id=db.id,
                                    kubedb_phase=phase,
                                    message="OpsRequest succeeded but database phase is Failed - keeping UPDATING until database recovers",
                                )
                                # Keep UPDATING status - don't change to FAILED
                            else:
                                db.status = mapped_status
                                logger.info(
                                    "changing_status_from_updating_opsrequest_completed",
                                    database_id=db.id,
                                    new_status=mapped_status.value,
                                    kubedb_phase=phase,
                                )
                        else:
                            # OpsRequest is still pending/progressing or failed - keep UPDATING
                            logger.debug(
                                "keeping_updating_status_opsrequest_not_complete",
                                database_id=db.id,
                                latest_phase=latest_phase,
                            )
                    else:
                        # No OpsRequest found - might have been created but not visible yet
                        # Or OpsRequest was never created - keep UPDATING for now
                        # Reconciler will handle it
                        logger.debug(
                            "keeping_updating_status_no_opsrequest_found",
                            database_id=db.id,
                            message="No OpsRequest found - may have been just created or never created",
                        )
                except Exception as e:
                    logger.debug(
                        "failed_to_check_opsrequest_for_status_update",
                        database_id=db.id,
                        error=str(e),
                    )
            else:
                # No active operations and status is not UPDATING - map KubeDB phase to application status
                # If K8s says Ready, database should be RUNNING
                # BUT: If status was UPDATING and we're here, double-check there's no upgrade OpsRequest
                # This prevents false positives where upgrade OpsRequest exists but wasn't detected
                if old_status == DatabaseStatus.UPDATING:
                    # Double-check for any upgrade OpsRequest before changing status
                    try:
                        client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
                        ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                            group="ops.kubedb.com",
                            version="v1alpha1",
                            namespace=db.namespace,
                            plural=kubedb_service._get_ops_request_plural(db.engine),
                        )
                        all_items = ops_requests.get("items", [])
                        db_ops = [
                            item for item in all_items
                            if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
                        ]
                        
                        # Check if any OpsRequest is for upgrade (has updateVersion field in spec)
                        # or is in non-terminal state
                        has_upgrade_ops = False
                        for ops in db_ops:
                            ops_phase = ops.get("status", {}).get("phase", "")
                            # Check if it's a version upgrade OpsRequest
                            spec = ops.get("spec", {})
                            # Version upgrade OpsRequests have spec.updateVersion or spec.type == "UpdateVersion"
                            if "updateVersion" in spec or spec.get("type") == "UpdateVersion":
                                has_upgrade_ops = True
                                logger.debug(
                                    "found_upgrade_opsrequest_preserving_updating_status",
                                    database_id=db.id,
                                    ops_request_name=ops.get("metadata", {}).get("name"),
                                    phase=ops_phase,
                                )
                                break
                            # Also check if OpsRequest is in non-terminal state (could be upgrade or scaling)
                            elif ops_phase not in ["Successful", "Failed", "Skipped"]:
                                has_upgrade_ops = True
                                logger.debug(
                                    "found_active_opsrequest_preserving_updating_status",
                                    database_id=db.id,
                                    ops_request_name=ops.get("metadata", {}).get("name"),
                                    phase=ops_phase,
                                )
                                break
                        
                        if has_upgrade_ops:
                            # Keep UPDATING status - upgrade is still in progress
                            logger.info(
                                "preserving_updating_status_due_to_upgrade_opsrequest",
                                database_id=db.id,
                            )
                            # Don't change status - keep it as UPDATING
                            return  # Exit early to prevent status change
                    except Exception as e:
                        logger.debug(
                            "failed_to_check_upgrade_opsrequest",
                            database_id=db.id,
                            error=str(e),
                        )
                        # On error, be conservative - keep UPDATING status if it was UPDATING
                        if old_status == DatabaseStatus.UPDATING:
                            logger.info(
                                "preserving_updating_status_on_error",
                                database_id=db.id,
                            )
                            return  # Exit early to prevent status change
                
                # Safe to update status
                mapped_status = self._map_kubedb_phase_to_status(phase, is_ready)

                # CRITICAL: If database was UPDATING, it should NEVER go to FAILED based on phase alone
                # The database phase might temporarily become "Failed" during operations (restart, scaling, etc.)
                # Only OpsRequest failures (handled separately) should set status to FAILED
                if old_status == DatabaseStatus.UPDATING and mapped_status == DatabaseStatus.FAILED:
                    logger.warning(
                        "preventing_updating_to_failed_transition",
                        database_id=db.id,
                        old_status=old_status.value,
                        kubedb_phase=phase,
                        message="Database was UPDATING but phase is Failed - keeping UPDATING status. Only OpsRequest failures should set to FAILED.",
                    )
                    mapped_status = DatabaseStatus.UPDATING

                # CRITICAL: If phase maps to FAILED, NEVER set to FAILED during updates/scaling
                # During operations, the database phase might temporarily become "Failed" (restart, rolling update, etc.)
                # This is normal and should NOT result in FAILED status
                # Only set to FAILED if:
                # 1. No active OpsRequest
                # 2. Not in UPDATING/SCALING state
                # 3. Not in any operation state (RESUMING, PAUSING, etc.)
                elif mapped_status == DatabaseStatus.FAILED:
                    # PRODUCT PERSPECTIVE: Updates should NEVER result in FAILED status
                    # If we're in any operation state, keep that state
                    if old_status in [
                        DatabaseStatus.UPDATING,
                        DatabaseStatus.SCALING,
                        DatabaseStatus.RESUMING,
                        DatabaseStatus.PAUSING,
                        DatabaseStatus.PROVISIONING,
                    ]:
                        logger.warning(
                            "database_phase_failed_during_operation_preserving_status",
                            database_id=db.id,
                            kubedb_phase=phase,
                            current_status=old_status.value,
                            message="Database phase is Failed during operation - preserving operation status, NOT setting to FAILED",
                        )
                        # Keep current status - don't change to FAILED
                        mapped_status = old_status
                    else:
                        # Not in operation state - check for active OpsRequests
                        try:
                            has_any_ops = await self._has_active_ops_request(db, provider_id, kubeconfig_content)
                            if has_any_ops:
                                logger.warning(
                                    "database_phase_failed_but_active_ops_exists",
                                    database_id=db.id,
                                    kubedb_phase=phase,
                                    message="Database phase is Failed but active OpsRequest exists - keeping current status instead of FAILED",
                                )
                                # Don't change status - keep current status
                                mapped_status = db.status
                            else:
                                # No active OpsRequest and not in operation state
                                # CRITICAL: Be VERY conservative about setting to FAILED
                                # Even if old_status is RUNNING, check if database is actually healthy
                                # K8s phase "Failed" can be temporary (restart, rolling update, etc.)
                                # Only set to FAILED if:
                                # 1. Status was RUNNING (not from operation)
                                # 2. Phase is consistently Failed (not Ready)
                                # 3. Database is actually not ready
                                if old_status == DatabaseStatus.RUNNING:
                                    # Check if database is actually ready (health_status might be Ready even if phase is Failed)
                                    # This handles case where phase is Failed but database is actually working
                                    if db.health_status and db.health_status.lower() in ["ready", "running"]:
                                        logger.warning(
                                            "database_phase_failed_but_health_ready_preserving_running",
                                            database_id=db.id,
                                            kubedb_phase=phase,
                                            health_status=db.health_status,
                                            message="Database phase is Failed but health_status is Ready - preserving RUNNING (temporary phase issue)",
                                        )
                                        # Preserve RUNNING - phase might be temporary
                                        mapped_status = DatabaseStatus.RUNNING
                                    elif not is_ready:
                                        # Database is actually not ready - might be real failure
                                        # But still be conservative: check if this is right after operation
                                        # Check for recent OpsRequests (within last 5 minutes)
                                        try:
                                            from datetime import datetime, timedelta, timezone
                                            recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                                            
                                            # Get OpsRequests to check for recent ones
                                            ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                                                group="ops.kubedb.com",
                                                version="v1alpha1",
                                                namespace=db.namespace,
                                                plural=kubedb_service._get_ops_request_plural(db.engine),
                                            )
                                            
                                            # Check if there are any OpsRequests that completed recently
                                            all_items = ops_requests.get("items", [])
                                            recent_ops = [
                                                item for item in all_items
                                                if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
                                                and item.get("status", {}).get("phase") in ["Successful", "Failed", "Skipped"]
                                                and item.get("metadata", {}).get("creationTimestamp")
                                            ]
                                            
                                            # Check if any recent OpsRequest completed in last 5 minutes
                                            has_recent_ops = False
                                            for ops in recent_ops:
                                                created_str = ops.get("metadata", {}).get("creationTimestamp")
                                                if created_str:
                                                    try:
                                                        created_time = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                                                        if created_time > recent_cutoff:
                                                            has_recent_ops = True
                                                            break
                                                    except:
                                                        pass
                                            
                                            if has_recent_ops:
                                                logger.warning(
                                                    "database_phase_failed_after_recent_operation_preserving_running",
                                                    database_id=db.id,
                                                    kubedb_phase=phase,
                                                    message="Database phase is Failed after recent operation - preserving RUNNING (might be temporary)",
                                                )
                                                mapped_status = DatabaseStatus.RUNNING
                                            else:
                                                # No recent operations - might be actual failure
                                                # BUT: Check health_status one more time - it might have been updated
                                                if db.health_status and db.health_status.lower() in ["ready", "running"]:
                                                    logger.warning(
                                                        "database_phase_failed_but_health_ready_final_check_preserving_running",
                                                        database_id=db.id,
                                                        kubedb_phase=phase,
                                                        health_status=db.health_status,
                                                        message="Database phase is Failed but health_status is Ready (final check) - preserving RUNNING",
                                                    )
                                                    mapped_status = DatabaseStatus.RUNNING
                                                else:
                                                    logger.error(
                                                        "database_phase_failed_no_operation_state",
                                                        database_id=db.id,
                                                        kubedb_phase=phase,
                                                        health_status=db.health_status,
                                                        is_ready=is_ready,
                                                        message="Database phase is Failed and not in operation state - setting to FAILED",
                                                    )
                                                    mapped_status = DatabaseStatus.FAILED
                                        except Exception as check_error:
                                            logger.debug(
                                                "failed_to_check_recent_ops_for_failed",
                                                database_id=db.id,
                                                error=str(check_error),
                                            )
                                            # On error, preserve RUNNING (safer)
                                            mapped_status = DatabaseStatus.RUNNING
                                    else:
                                        # Database is ready but phase is Failed - preserve RUNNING
                                        logger.warning(
                                            "database_phase_failed_but_ready_preserving_running",
                                            database_id=db.id,
                                            kubedb_phase=phase,
                                            is_ready=is_ready,
                                            message="Database phase is Failed but is_ready is True - preserving RUNNING",
                                        )
                                        mapped_status = DatabaseStatus.RUNNING
                                else:
                                    # Keep current status
                                    mapped_status = db.status
                        except Exception as e:
                            logger.debug(
                                "failed_to_check_ops_before_failed_status",
                                database_id=db.id,
                                error=str(e),
                            )
                            # On error, preserve current status if in operation state
                            # CRITICAL: Don't change SCALING to UPDATING or vice versa
                            if old_status == DatabaseStatus.SCALING:
                                mapped_status = DatabaseStatus.SCALING
                            elif old_status == DatabaseStatus.UPDATING:
                                mapped_status = DatabaseStatus.UPDATING
                            else:
                                mapped_status = db.status

                # CRITICAL: Before setting status, check if we're in an operation state OR just became RUNNING
                # If mapped_status is FAILED but we're in operation state, preserve operation status
                # Also: If status is RUNNING and mapped_status is FAILED, check for active OpsRequests
                # This prevents FAILED status right after vertical scaling completes
                # FINAL SAFEGUARD: If health_status is Ready OR K8s phase is Ready, NEVER set to FAILED
                if mapped_status == DatabaseStatus.FAILED:
                    # FINAL SAFEGUARD: If health_status is Ready OR K8s phase is Ready, NEVER set to FAILED
                    if (db.health_status and db.health_status.lower() in ["ready", "running"]) or phase.lower() in ["ready", "running"]:
                        logger.warning(
                            "final_safeguard_preventing_failed_health_or_phase_ready",
                            database_id=db.id,
                            old_status=old_status.value,
                            health_status=db.health_status,
                            kubedb_phase=phase,
                            message="FINAL SAFEGUARD: health_status or phase is Ready - preserving RUNNING, NOT setting to FAILED",
                        )
                        mapped_status = DatabaseStatus.RUNNING
                    elif old_status in [
                        DatabaseStatus.UPDATING,
                        DatabaseStatus.SCALING,
                        DatabaseStatus.RESUMING,
                        DatabaseStatus.PAUSING,
                        DatabaseStatus.PROVISIONING,
                    ]:
                        logger.warning(
                            "preventing_failed_status_during_operation_final_check",
                            database_id=db.id,
                            old_status=old_status.value,
                            mapped_status=mapped_status.value,
                            message="Final check: Preventing FAILED status during operation - preserving operation status",
                        )
                        mapped_status = old_status
                    elif old_status == DatabaseStatus.RUNNING:
                        # Status is RUNNING but phase is Failed - check for active OpsRequests
                        # This handles case where status just became RUNNING but next cycle sees Failed phase
                        try:
                            has_any_ops = await self._has_active_ops_request(db, provider_id, kubeconfig_content)
                            if has_any_ops:
                                logger.warning(
                                    "preventing_failed_status_after_running_active_ops",
                                    database_id=db.id,
                                    kubedb_phase=phase,
                                    message="Status is RUNNING but phase is Failed with active OpsRequest - preserving RUNNING status",
                                )
                                mapped_status = DatabaseStatus.RUNNING
                            else:
                                # No active OpsRequest but phase is Failed
                                # Check if this is a temporary phase issue (database might be restarting)
                                # Be conservative: only set to FAILED if phase stays Failed for multiple cycles
                                # For now, preserve RUNNING if it was just set (recent operation)
                                logger.warning(
                                    "preventing_failed_status_after_running_no_ops",
                                    database_id=db.id,
                                    kubedb_phase=phase,
                                    message="Status is RUNNING but phase is Failed - preserving RUNNING (might be temporary phase issue)",
                                )
                                mapped_status = DatabaseStatus.RUNNING
                        except Exception as e:
                            logger.debug(
                                "failed_to_check_ops_after_running",
                                database_id=db.id,
                                error=str(e),
                            )
                            # On error, preserve RUNNING status (safer than setting to FAILED)
                            mapped_status = DatabaseStatus.RUNNING

                # ABSOLUTE SAFEGUARD: If health_status is Ready OR phase is Ready, NEVER set to FAILED
                # This is the ULTIMATE check - if database is healthy, it CANNOT be FAILED
                if mapped_status == DatabaseStatus.FAILED:
                    if (db.health_status and db.health_status.lower() in ["ready", "running"]) or phase.lower() in ["ready", "running"]:
                        logger.error(
                            "absolute_safeguard_preventing_failed_healthy_database",
                            database_id=db.id,
                            old_status=old_status.value,
                            health_status=db.health_status,
                            kubedb_phase=phase,
                            is_ready=is_ready,
                            message="ABSOLUTE SAFEGUARD: Database is healthy (health_status or phase is Ready) - FORCING RUNNING, NOT FAILED",
                        )
                        mapped_status = DatabaseStatus.RUNNING
                
                db.status = mapped_status
                if mapped_status == DatabaseStatus.RUNNING:
                    logger.info(
                        "database_status_set_to_running",
                        database_id=db.id,
                        kubedb_phase=phase,
                        is_ready=is_ready,
                    )

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
                # Database is halted but status is PROVISIONING - this means resume was requested
                # Set status to RESUMING first, then call resume handler
                logger.info(
                    "provisioning_status_with_halted_phase_resuming",
                    database_id=db.id,
                    message="Status is PROVISIONING but K8s is halted - setting to RESUMING and resuming database"
                )
                db.status = DatabaseStatus.RESUMING
                await db.save()
                await self._handle_database_resume_operation(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                operation_performed = True

            # CRITICAL: Always preserve DELETING status - never override it
            # If user requested deletion, status must stay DELETING until deletion completes
            if db.status == DatabaseStatus.DELETING:
                logger.debug(
                    "preserving_deleting_status",
                    database_id=db.id,
                    kubedb_phase=phase,
                    message="Status is DELETING - preserving it, deletion handler will complete deletion"
                )
                await db.save()
                return  # Exit - don't update status based on KubeDB phase
            
            # CRITICAL: Check if database became Ready while in SCALING/UPDATING state
            # If phase is Ready and database is ready, we should transition to RUNNING
            # But ONLY if comprehensive checks pass
            if old_status in [DatabaseStatus.SCALING, DatabaseStatus.UPDATING]:
                # Database was in operation state - check if it's now ready
                if phase.lower() in ["ready", "running"] and is_ready:
                    # Database became Ready - use comprehensive check to ensure it's truly ready
                    truly_ready_result = await self._is_database_truly_ready(
                        db=db,
                        detailed_status=detailed_status,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    
                    is_truly_ready = truly_ready_result["is_ready"]
                    reasons = truly_ready_result["reasons"]
                    checks = truly_ready_result["checks"]
                    
                    if is_truly_ready:
                        logger.info(
                            "scaling_completed_database_truly_ready",
                            database_id=db.id,
                            old_status=old_status.value,
                            phase=phase,
                            checks=checks,
                            message="Database became Ready during SCALING/UPDATING - transitioning to RUNNING",
                        )
                        db.status = DatabaseStatus.RUNNING
                        await db.save()
                        return  # Exit early - status updated
                    else:
                        logger.info(
                            "scaling_not_completed_database_not_truly_ready",
                            database_id=db.id,
                            old_status=old_status.value,
                            phase=phase,
                            reasons=reasons,
                            checks=checks,
                            message="Database phase is Ready but not truly ready - preserving SCALING/UPDATING status",
                        )
                        # Keep SCALING/UPDATING status - not truly ready yet
                        await db.save()
                        return  # Exit early - status preserved
            
            # Only update status from KubeDB phase if no operation is in progress
            # This preserves operation status (UPDATING, SCALING, RESUMING, etc.) until operation completes
            if not operation_performed:
                mapped_status = self._map_kubedb_phase_to_status(phase, is_ready)
                # Only update if not in an operation state
                # IMPORTANT: Don't override RESUMING, PAUSING, DELETING, or FAILED status here - they're handled above
                if old_status not in [
                    DatabaseStatus.UPDATING,
                    DatabaseStatus.SCALING,
                    DatabaseStatus.DELETING,   # CRITICAL: Never override DELETING
                    DatabaseStatus.PAUSED,
                    DatabaseStatus.RESUMING,    # Preserve RESUMING status
                    DatabaseStatus.PAUSING,     # Preserve PAUSING status
                    DatabaseStatus.FAILED,      # Preserve FAILED status (only change when explicitly resolved above)
                ]:
                    db.status = mapped_status
                # If operation completed (status changed from operation state), use COMPREHENSIVE check
                # CRITICAL: Don't trust K8s Ready alone - check ALL conditions
                # IMPORTANT: If status is SCALING, be EXTRA careful - scaling might still be in progress
                elif old_status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING] and mapped_status == DatabaseStatus.RUNNING:
                    # Use comprehensive check to ensure database is TRULY ready
                    truly_ready_result = await self._is_database_truly_ready(
                        db=db,
                        detailed_status=detailed_status,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    
                    is_truly_ready = truly_ready_result["is_ready"]
                    reasons = truly_ready_result["reasons"]
                    checks = truly_ready_result["checks"]
                    
                    logger.info(
                        "status_sync_comprehensive_ready_check",
                        database_id=db.id,
                        old_status=old_status.value,
                        mapped_status=mapped_status.value,
                        is_truly_ready=is_truly_ready,
                        reasons=reasons,
                        checks=checks,
                    )
                    
                    if is_truly_ready:
                        logger.info(
                            "changing_status_to_running_all_checks_passed",
                            database_id=db.id,
                            checks=checks,
                        )
                        old_status_value = db.status
                        db.status = mapped_status
                        await db.save()
                        
                        # Trigger initial backup if database just became RUNNING and backup is enabled
                        if old_status_value != DatabaseStatus.RUNNING and db.backup_enabled:
                            await self._trigger_initial_backup(db, provider_id, kubeconfig_content)
                    else:
                        # CRITICAL: If not truly ready, preserve SCALING status
                        logger.warning(
                            "status_staying_scaling_not_truly_ready",
                            database_id=db.id,
                            old_status=old_status.value,
                            reasons=reasons,
                            checks=checks,
                            message="Database not truly ready - preserving SCALING status",
                        )
                        # Keep SCALING status - don't change to RUNNING
                        db.status = old_status
                        await db.save()
                        logger.info(
                            "keeping_updating_status_not_truly_ready",
                            database_id=db.id,
                            reasons=", ".join(reasons),
                            checks=checks,
                        )
            
            # Check OpsRequest status if database is in UPDATING or SCALING state
            # This monitors OpsRequests created by PATCH endpoint and updates status when they complete
            if db.status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING]:
                try:
                    await self._check_ops_request_status(
                        db=db,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                        is_ready=is_ready,
                    )
                except Exception as e:
                    logger.warning(
                        "ops_request_status_check_failed",
                        database_id=db.id,
                        error=str(e),
                    )
                    # Don't fail the entire reconciliation if OpsRequest check fails

            # Trigger backup for RUNNING databases if backup is enabled and no backup job exists
            # Make sure endpoint is fetched first
            if is_ready and db.status == DatabaseStatus.RUNNING and db.backup_enabled:
                # Ensure endpoint is set before triggering backup
                if not db.endpoint:
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
                        await db.save()
                
                # Check if backup was already triggered (avoid duplicate checks on every sync)
                # We'll trigger backup if no backup job exists
                if db.endpoint:  # Only trigger if endpoint is available
                    await self._ensure_backup_triggered(db, provider_id, kubeconfig_content)
            
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
                old_version != db.version or
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
                
                # Update current_size from K8s CR resources (OpsRequest has updated them)
                try:
                    current_cr = await client_set.custom_api.get_namespaced_custom_object(
                        group=kubedb_service._get_kubedb_group(db.engine),
                        version=kubedb_service._get_kubedb_version(db.engine),
                        namespace=db.namespace,
                        plural=kubedb_service._get_kubedb_plural(db.engine),
                        name=db.kubedb_resource_name,
                    )
                    
                    # Extract resources from CR
                    cr_resources = None
                    if "spec" in current_cr and "podTemplate" in current_cr["spec"]:
                        containers = current_cr["spec"]["podTemplate"]["spec"].get("containers", [])
                        for container in containers:
                            if container.get("name") in ["mongodb", "postgres", "mysql", db.engine.value]:
                                cr_resources = container.get("resources", {})
                                break
                    
                    if cr_resources and db.size:
                        # Get expected resources for desired size
                        expected_resources = kubedb_service._get_resource_limits(db.size)
                        expected_cpu = str(expected_resources.get("cpu", ""))
                        expected_memory = str(expected_resources.get("memory", ""))
                        
                        # Get actual resources from CR
                        cr_requests = cr_resources.get("requests", {})
                        cr_cpu = str(cr_requests.get("cpu", ""))
                        cr_memory = str(cr_requests.get("memory", ""))
                        
                        # If CR resources match desired size, update current_size
                        if cr_cpu == expected_cpu and cr_memory == expected_memory:
                            db.current_size = db.size
                            logger.info(
                                "current_size_updated_after_opsrequest_success",
                                database_id=db.id,
                                current_size=db.size.value,
                            )
                        else:
                            # Determine size from CR resources
                            cr_size = self._get_size_from_cr_resources(current_cr, db.engine.value)
                            if cr_size:
                                db.current_size = cr_size
                                logger.info(
                                    "current_size_updated_from_cr_after_opsrequest",
                                    database_id=db.id,
                                    current_size=cr_size.value,
                                )
                    
                    # Update current_replicas from CR
                    cr_replicas = current_cr.get("spec", {}).get("replicas", db.replicas)
                    if cr_replicas != db.current_replicas:
                        db.current_replicas = cr_replicas
                        logger.info(
                            "current_replicas_updated_after_opsrequest_success",
                            database_id=db.id,
                            current_replicas=cr_replicas,
                        )
                except Exception as e:
                    logger.warning(
                        "failed_to_update_current_state_after_opsrequest",
                        database_id=db.id,
                        error=str(e),
                    )
                
                # Update status to RUNNING ONLY if:
                # 1. Database is ready
                # 2. All replicas are up (ready_replicas == desired replicas)
                # 3. No active OpsRequests exist
                # Check if database is ready by fetching detailed status
                try:
                    detailed_status = await kubedb_service.get_detailed_status(
                        engine=db.engine,
                        name=db.kubedb_resource_name,
                        namespace=db.namespace,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    db_is_ready = detailed_status.get("ready", False) if detailed_status else False
                    db_phase = detailed_status.get("phase", "Unknown") if detailed_status else "Unknown"
                    ready_replicas = detailed_status.get("ready_replicas", 0) if detailed_status else 0
                    desired_replicas = detailed_status.get("replicas", db.replicas) if detailed_status else db.replicas
                    
                    # Use comprehensive check to ensure database is TRULY ready
                    # Don't just check K8s Ready - verify all conditions
                    truly_ready_result = await self._is_database_truly_ready(
                        db=db,
                        detailed_status=detailed_status,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    
                    is_truly_ready = truly_ready_result["is_ready"]
                    reasons = truly_ready_result["reasons"]
                    checks = truly_ready_result["checks"]
                    
                    if is_truly_ready and db.status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING]:
                        db.status = DatabaseStatus.RUNNING
                        logger.info(
                            "database_status_updated_to_running_after_opsrequest_all_checks_passed",
                            database_id=db.id,
                            ops_request_name=ops_request_name,
                            checks=checks,
                        )
                    elif not is_truly_ready:
                        logger.info(
                            "ops_request_successful_but_database_not_truly_ready",
                            database_id=db.id,
                            ops_request_name=ops_request_name,
                            reasons=", ".join(reasons),
                            checks=checks,
                            message="OpsRequest succeeded but database not truly ready - keeping UPDATING status",
                        )
                    elif not all_replicas_ready:
                        logger.info(
                            "not_setting_to_running_replicas_not_ready",
                            database_id=db.id,
                            ready_replicas=ready_replicas,
                            desired_replicas=desired_replicas,
                            current_status=db.status.value,
                            message="Not all replicas are ready - preserving current status",
                        )
                    elif has_active_ops:
                        logger.info(
                            "not_setting_to_running_active_ops_exists",
                            database_id=db.id,
                            current_status=db.status.value,
                            message="Active OpsRequest exists - preserving current status",
                        )
                except Exception as e:
                    logger.debug(
                        "failed_to_check_database_ready_status",
                        database_id=db.id,
                        error=str(e),
                    )
                
                await db.save()

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

                # IMPORTANT: During update/scaling operations, don't set status to FAILED
                # Instead, revert desired state to current state and set status to RUNNING
                # This keeps the database usable and allows the user to retry
                if db.status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING]:
                    logger.warning(
                        "ops_request_failed_during_update_reverting_to_running",
                        database_id=db.id,
                        ops_request_name=ops_request_name,
                        reason=failure_reason,
                        message="OpsRequest failed during update/scaling - reverting to RUNNING state instead of FAILED",
                    )
                    
                    # Revert desired state to current state to eliminate drift
                    # This prevents the reconciler from creating new OpsRequests
                    # Only revert if current state exists and differs from desired
                    if db.current_size is not None and db.size != db.current_size:
                        logger.info(
                            "reverting_size_to_current_after_opsrequest_failure",
                            database_id=db.id,
                            desired_size=db.size.value if db.size else None,
                            current_size=db.current_size.value if db.current_size else None,
                        )
                        db.size = db.current_size
                    
                    if db.current_replicas is not None and db.replicas != db.current_replicas:
                        logger.info(
                            "reverting_replicas_to_current_after_opsrequest_failure",
                            database_id=db.id,
                            desired_replicas=db.replicas,
                            current_replicas=db.current_replicas,
                        )
                        db.replicas = db.current_replicas
                    
                    if db.current_storage_gb is not None and db.storage_gb is not None and db.storage_gb != db.current_storage_gb:
                        logger.info(
                            "reverting_storage_to_current_after_opsrequest_failure",
                            database_id=db.id,
                            desired_storage=db.storage_gb,
                            current_storage=db.current_storage_gb,
                        )
                        db.storage_gb = db.current_storage_gb
                    
                    # Set status to RUNNING and log failure in health_status
                    db.status = DatabaseStatus.RUNNING
                    db.health_status = f"Last update failed: {failure_reason}. Database is running with previous configuration."
                    await db.save()
                else:
                    # PRODUCT PERSPECTIVE: Only set to FAILED if not in any operation state
                    # If we're in operation state, keep it (user can retry)
                    # FAILED should only be for actual database failures, not operation failures
                    if db.status in [
                        DatabaseStatus.UPDATING,
                        DatabaseStatus.SCALING,
                        DatabaseStatus.RESUMING,
                        DatabaseStatus.PAUSING,
                    ]:
                        logger.warning(
                            "ops_request_failed_during_operation_preserving_status",
                            database_id=db.id,
                            ops_request_name=ops_request_name,
                            reason=failure_reason,
                            current_status=db.status.value,
                            message="OpsRequest failed during operation - preserving operation status, NOT setting to FAILED",
                        )
                        # Keep current status - don't change to FAILED
                        db.health_status = f"Last operation failed: {failure_reason}. Status preserved for retry."
                        await db.save()
                    else:
                        # Not in operation state - but check if database is actually healthy
                        # ABSOLUTE SAFEGUARD: If database is healthy, NEVER set to FAILED
                        if (db.health_status and db.health_status.lower() in ["ready", "running"]) or phase.lower() in ["ready", "running"]:
                            logger.warning(
                                "opsrequest_failed_but_database_healthy_preserving_running",
                                database_id=db.id,
                                ops_request_name=ops_request_name,
                                failure_reason=failure_reason,
                                health_status=db.health_status,
                                kubedb_phase=phase,
                                message="OpsRequest failed but database is healthy - preserving RUNNING, NOT setting to FAILED",
                            )
                            db.status = DatabaseStatus.RUNNING
                            db.health_status = f"OpsRequest failed but database is healthy: {failure_reason}. Status preserved."
                        else:
                            # Database is not healthy - might be actual failure
                            # But still check if it was SCALING - preserve SCALING if so
                            if db.status == DatabaseStatus.SCALING:
                                logger.warning(
                                    "opsrequest_failed_during_scaling_preserving_scaling",
                                    database_id=db.id,
                                    ops_request_name=ops_request_name,
                                    failure_reason=failure_reason,
                                    message="OpsRequest failed during SCALING - preserving SCALING status, NOT setting to FAILED",
                                )
                                # Keep SCALING status - don't change to FAILED
                                db.health_status = f"Last operation failed: {failure_reason}. Status preserved for retry."
                            else:
                                # Not SCALING and not healthy - might be actual failure
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

    async def _has_active_ops_request(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> bool:
        """
        Check if there's an active (non-terminal) OpsRequest for this database.
        
        Returns:
            True if there's an active OpsRequest, False otherwise
        """
        try:
            client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)

            # List all OpsRequests in namespace
            ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=db.namespace,
                plural=kubedb_service._get_ops_request_plural(db.engine),
            )

            # Filter OpsRequests that target this database and are not in terminal states
            all_items = ops_requests.get("items", [])
            active_ops = [
                item for item in all_items
                if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
                and item.get("status", {}).get("phase") not in ["Successful", "Failed", "Skipped"]
            ]

            return len(active_ops) > 0
        except Exception as e:
            logger.debug(
                "failed_to_check_active_ops_request",
                database_id=db.id,
                error=str(e),
            )
            # On error, assume no active OpsRequest (safer to update current_replicas)
            return False

    async def _is_database_truly_ready(
        self,
        db: Database,
        detailed_status: Optional[Dict[str, Any]],
        provider_id: str,
        kubeconfig_content: str,
    ) -> Dict[str, Any]:
        """
        COMPREHENSIVE CHECK: Determine if database is truly ready (all operations complete).
        
        This is the single source of truth for whether status should be RUNNING.
        Checks ALL conditions to ensure no background processes are running.
        
        Args:
            db: Database document
            detailed_status: Detailed status from KubeDB (phase, ready, etc.)
            provider_id: Provider ID
            kubeconfig_content: Provider's kubeconfig content
            
        Returns:
            Dict with:
            - is_ready: bool - True if database is truly ready
            - reasons: List[str] - Reasons why not ready (if any)
            - checks: Dict - Details of all checks performed
        """
        reasons = []
        checks = {}
        
        # Check 1: K8s Ready condition
        if not detailed_status:
            reasons.append("detailed_status not available")
            checks["k8s_ready"] = False
            return {"is_ready": False, "reasons": reasons, "checks": checks}
        
        phase = detailed_status.get("phase", "Unknown")
        is_ready_k8s = detailed_status.get("ready", False)
        ready_replicas = detailed_status.get("ready_replicas", 0)
        desired_replicas = detailed_status.get("replicas", db.replicas)
        
        checks["k8s_phase"] = phase
        checks["k8s_ready"] = is_ready_k8s
        checks["ready_replicas"] = ready_replicas
        checks["desired_replicas"] = desired_replicas
        
        if phase not in ["Ready", "Running"]:
            reasons.append(f"K8s phase is {phase}, not Ready/Running")
        if not is_ready_k8s:
            reasons.append("K8s Ready condition is False")
        if ready_replicas < desired_replicas:
            reasons.append(f"Not all replicas ready: {ready_replicas}/{desired_replicas}")
        
        # Check 2: No active OpsRequests
        has_active_ops_request = await self._has_active_ops_request(db, provider_id, kubeconfig_content)
        checks["has_active_ops_request"] = has_active_ops_request
        if has_active_ops_request:
            reasons.append("Active OpsRequest exists")
        
        # Check 3: No active operations in queue
        from app.models.operation import Operation, OperationStatus
        active_operation = await Operation.find_one({
            "database_id": db.id,
            "status": {"$in": [
                OperationStatus.QUEUED.value,
                OperationStatus.IN_PROGRESS.value,
            ]},
        })
        checks["has_active_operation"] = bool(active_operation)
        if active_operation:
            reasons.append(f"Active operation exists: {str(active_operation.id)}")
        
        # Check 4: No terminating pods
        has_terminating_pods = False
        non_running_pods = []
        try:
            client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
            label_selector = f"app.kubernetes.io/instance={db.kubedb_resource_name}"
            pods = await client_set.core_api.list_namespaced_pod(
                namespace=db.namespace,
                label_selector=label_selector
            )
            
            for pod in pods.items:
                pod_phase = pod.status.phase
                deletion_timestamp = pod.metadata.deletion_timestamp
                pod_name = pod.metadata.name
                
                if deletion_timestamp is not None or pod_phase == "Terminating":
                    has_terminating_pods = True
                    non_running_pods.append(f"{pod_name}(terminating)")
                elif pod_phase not in ["Running"]:
                    has_terminating_pods = True
                    non_running_pods.append(f"{pod_name}({pod_phase})")
            
            checks["has_terminating_pods"] = has_terminating_pods
            checks["non_running_pods"] = non_running_pods
            checks["total_pods"] = len(pods.items)
            checks["running_pods"] = len([p for p in pods.items if p.status.phase == "Running" and not p.metadata.deletion_timestamp])
            
            if has_terminating_pods:
                reasons.append(f"Pods not ready: {', '.join(non_running_pods)}")
        except Exception as e:
            logger.warning(
                "failed_to_check_pods_for_truly_ready",
                database_id=db.id,
                error=str(e)[:200],
            )
            # On error, don't block - but log it
            checks["pod_check_error"] = str(e)[:200]
        
        # Check 5: Verify OpsRequests are in terminal states (if any exist)
        # Even if no active ones, check if recent ones completed properly
        try:
            client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
            ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                group="ops.kubedb.com",
                version="v1alpha1",
                namespace=db.namespace,
                plural=kubedb_service._get_ops_request_plural(db.engine),
            )
            
            all_items = ops_requests.get("items", [])
            db_ops = [
                item for item in all_items
                if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
            ]
            
            # Check if any recent OpsRequest is still in non-terminal state
            recent_non_terminal = [
                item for item in db_ops
                if item.get("status", {}).get("phase") not in ["Successful", "Failed", "Skipped"]
            ]
            
            checks["recent_ops_requests"] = len(db_ops)
            checks["non_terminal_ops"] = len(recent_non_terminal)
            if recent_non_terminal:
                reasons.append(f"{len(recent_non_terminal)} OpsRequest(s) not in terminal state")
        except Exception as e:
            logger.debug(
                "failed_to_check_ops_requests_for_truly_ready",
                database_id=db.id,
                error=str(e)[:200],
            )
        
        is_ready = len(reasons) == 0
        
        return {
            "is_ready": is_ready,
            "reasons": reasons,
            "checks": checks
        }

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
                except (ValueError, AttributeError, TypeError) as e:
                    logger.warning(
                        "storage_parse_failed_in_status_sync",
                        storage_str=cr_storage,
                        error=str(e),
                    )
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

                # Check for active operations before setting status to RUNNING
                from app.models.operation import Operation, OperationStatus
                active_operation = await Operation.find_one({
                    "database_id": db.id,
                    "status": {"$in": [
                        OperationStatus.QUEUED.value,
                        OperationStatus.IN_PROGRESS.value,
                    ]},
                })

                if not active_operation:
                    # Before setting to RUNNING, check if replicas match (for scaling operations)
                    if db.status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING]:
                        # Check if ready_replicas matches desired replicas
                        # Use db.ready_replicas which is updated by status sync service
                        current_ready_replicas = db.ready_replicas or 0
                        if current_ready_replicas < db.replicas:
                            logger.info(
                                "database_still_scaling_replicas",
                                database_id=db.id,
                                ready_replicas=current_ready_replicas,
                                desired_replicas=db.replicas,
                                message="Preserving UPDATING/SCALING status until all replicas are ready",
                            )
                            await db.save()
                            return

                    # Use comprehensive check before setting to RUNNING
                    truly_ready_result = await self._is_database_truly_ready(
                        db=db,
                        detailed_status=detailed_status,
                        provider_id=provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    
                    if truly_ready_result["is_ready"]:
                        logger.info(
                            "database_up_to_date_setting_running",
                            database_id=db.id,
                            checks=truly_ready_result["checks"],
                        )
                        old_status_value = db.status
                        db.status = DatabaseStatus.RUNNING
                        await db.save()
                        
                        # Trigger initial backup if database just became RUNNING and backup is enabled
                        if old_status_value != DatabaseStatus.RUNNING and db.backup_enabled:
                            await self._trigger_initial_backup(db, provider_id, kubeconfig_content)
                    else:
                        logger.info(
                            "database_up_to_date_but_not_truly_ready",
                            database_id=db.id,
                            reasons=", ".join(truly_ready_result["reasons"]),
                            checks=truly_ready_result["checks"],
                            message="Database up to date but not truly ready - keeping current status",
                        )
                else:
                    logger.info(
                        "database_up_to_date_but_active_operation_exists",
                        database_id=db.id,
                        operation_id=str(active_operation.id),
                        keeping_status=db.status.value,
                    )
                
        except Exception as e:
            logger.error(
                "database_update_operation_failed",
                database_id=db.id,
                error=str(e),
                exc_info=True,
            )
            # PRODUCT PERSPECTIVE: Updates should NEVER result in FAILED status
            # If update operation fails, keep UPDATING status so user can retry
            # Don't set to FAILED - that's only for actual database failures, not operation errors
            if db.status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING]:
                logger.warning(
                    "update_operation_exception_preserving_updating_status",
                    database_id=db.id,
                    error=str(e)[:200],
                    message="Update operation exception occurred - preserving UPDATING/SCALING status, NOT setting to FAILED",
                )
                # Keep current status - don't change to FAILED
                await db.save()
            else:
                # If not in operation state, set to FAILED (actual failure)
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

    def _get_size_from_cr_resources(self, cr: Dict[str, Any], engine: str) -> Optional:
        """
        Determine DatabaseSize from CR resources.

        Converts CR resources back to DatabaseSize enum.
        """
        from app.models.database import DatabaseSize

        # Extract resources from CR
        cr_resources = None
        if "spec" in cr and "podTemplate" in cr["spec"]:
            containers = cr["spec"]["podTemplate"]["spec"].get("containers", [])
            for container in containers:
                # Match by engine name (mongodb, postgres, etc.)
                if container.get("name") in ["mongodb", "postgres", "mysql", engine]:
                    cr_resources = container.get("resources", {})
                    break

        if not cr_resources or "requests" not in cr_resources:
            return None

        requests = cr_resources["requests"]
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

    async def _cleanup_old_ops_requests(self, databases_by_provider: Dict[Optional[str], List[Database]]) -> None:
        """
        Clean up old OpsRequests for all databases.

        Keeps the 3 most recent OpsRequests per database and deletes older ones
        in terminal states (Successful, Failed, Skipped).

        This includes cleaning up OpsRequests for deleted databases.

        Args:
            databases_by_provider: Dictionary mapping provider_id to list of databases (from reconciliation)
        """
        terminal_phases = ["Successful", "Failed", "Skipped"]
        total_cleaned = 0

        # Get ALL providers to ensure we clean up orphaned OpsRequests
        all_providers = await Provider.find_all().to_list()

        for provider in all_providers:
            try:
                # Get ALL databases for this provider (including DELETED ones)
                provider_databases = await Database.find({"provider_id": provider.id}).to_list()

                if not provider_databases:
                    continue

                client_set = await kubedb_service.get_client_for_provider(
                    provider.id, provider.kubeconfig_content
                )

                for db in provider_databases:
                    try:
                        # Get all OpsRequests for this database
                        # First try with label selector for OpsRequests we created
                        try:
                            items_labeled = await client_set.custom_api.list_namespaced_custom_object(
                                group="ops.kubedb.com",
                                version="v1alpha1",
                                namespace=db.namespace,
                                plural=kubedb_service._get_ops_request_plural(db.engine),
                                label_selector="app.kubernetes.io/managed-by=kubedb-dbaas",
                            )
                            labeled_ops = items_labeled.get("items", [])
                        except Exception:
                            labeled_ops = []

                        # Also get all OpsRequests to filter by name (for legacy OpsRequests without labels)
                        items_all = await client_set.custom_api.list_namespaced_custom_object(
                            group="ops.kubedb.com",
                            version="v1alpha1",
                            namespace=db.namespace,
                            plural=kubedb_service._get_ops_request_plural(db.engine),
                        )

                        # Filter by name prefix for legacy OpsRequests
                        all_ops = items_all.get("items", [])
                        name_matched_ops = [
                            item for item in all_ops
                            if item.get("metadata", {}).get("name", "").startswith(f"{db.kubedb_resource_name}-")
                        ]

                        # Combine both: OpsRequests with our label OR matching name prefix
                        # Use set to avoid duplicates
                        ops_names = set()
                        ops_list = []

                        for item in labeled_ops + name_matched_ops:
                            name = item.get("metadata", {}).get("name")
                            if name and name not in ops_names:
                                ops_names.add(name)
                                ops_list.append(item)

                        # Sort by creation timestamp (newest first)
                        ops_list.sort(
                            key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""),
                            reverse=True,
                        )

                        # Keep the 3 most recent, cleanup the rest if they're terminal
                        if len(ops_list) > 3:
                            old_ops_requests = [
                                item for item in ops_list[3:]
                                if item.get("status", {}).get("phase") in terminal_phases
                            ]

                            for old_ops in old_ops_requests:
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
                                    logger.debug(
                                        "cleaned_up_old_ops_request",
                                        database_id=db.id,
                                        database_name=db.name,
                                        ops_request_name=ops_name,
                                        phase=old_ops.get("status", {}).get("phase"),
                                    )
                                except Exception as e:
                                    error_str = str(e).lower()
                                    if "404" not in error_str and "not found" not in error_str:
                                        logger.debug(
                                            "failed_to_delete_ops_request",
                                            ops_request_name=ops_name,
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

            except Exception as e:
                logger.debug(
                    "cleanup_failed_for_provider",
                    provider_id=provider.id,
                    error=str(e),
                )

        if total_cleaned > 0:
            logger.info(
                "ops_requests_cleanup_completed",
                total_cleaned=total_cleaned,
            )

    async def _ensure_backup_triggered(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Ensure backup is triggered for a RUNNING database (checks if backup job exists).
        
        Args:
            db: Database instance
            provider_id: Provider ID
            kubeconfig_content: Provider's kubeconfig content
        """
        from app.config.settings import settings
        
        if not settings.backup_enabled or not db.backup_enabled:
            return
        
        # CRITICAL: Check if database is RUNNING before triggering backup
        if db.status != DatabaseStatus.RUNNING:
            logger.debug(
                "database_not_running_skipping_backup",
                database_id=db.id,
                status=db.status.value,
            )
            return
        
        # Check if endpoint is available (database must be accessible)
        if not db.endpoint:
            logger.debug(
                "database_endpoint_not_available_skipping_backup",
                database_id=db.id,
            )
            return
        
        try:
            # Check if backup job already exists based on schedule frequency
            import base64
            decoded_content = base64.b64decode(kubeconfig_content).decode('utf-8')
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.yaml') as f:
                f.write(decoded_content)
                temp_kubeconfig = f.name
            
            try:
                from kubernetes_asyncio import client, config
                await config.load_kube_config(config_file=temp_kubeconfig)
                batch_api = client.BatchV1Api()
                
                # Get backup schedule (default to daily)
                backup_schedule = db.backup_schedule.value if db.backup_schedule else "daily"
                
                # Determine time window based on schedule
                from datetime import datetime, timedelta
                now = datetime.utcnow()
                
                if backup_schedule == "hourly":
                    # Check for backup in last hour
                    time_window = now - timedelta(hours=1)
                    period_name = "hour"
                elif backup_schedule == "daily":
                    # Check for backup today (last 24 hours)
                    time_window = now - timedelta(days=1)
                    period_name = "day"
                elif backup_schedule == "weekly":
                    # Check for backup this week (last 7 days)
                    time_window = now - timedelta(days=7)
                    period_name = "week"
                else:
                    # Default to daily
                    time_window = now - timedelta(days=1)
                    period_name = "day"
                
                # Check for existing backup jobs
                jobs = await batch_api.list_namespaced_job(
                    namespace=db.namespace,
                    label_selector=f"database={db.kubedb_resource_name},backup-type=direct",
                )
                
                has_running_job = False
                has_recent_job = False  # Any job (running, succeeded, or failed) in the period
                recent_backup_job = None
                most_recent_job_time = None
                
                logger.debug(
                    "checking_backup_jobs",
                    database_id=db.id,
                    schedule=backup_schedule,
                    total_jobs=len(jobs.items),
                    time_window=time_window.isoformat(),
                    now=now.isoformat(),
                )
                
                # Check ALL jobs - we want to prevent ANY job creation in the period, not just successful ones
                for job in jobs.items:
                    if not job.metadata.creation_timestamp:
                        continue
                    
                    # Handle timezone-aware and timezone-naive timestamps
                    created_time = job.metadata.creation_timestamp
                    if created_time.tzinfo is not None:
                        created_time = created_time.replace(tzinfo=None)
                    
                    time_diff = (now - created_time).total_seconds()
                    
                    # Check if job was created within the time window
                    if created_time > time_window:
                        # Job was created in current period
                        has_recent_job = True
                        recent_backup_job = job.metadata.name
                        most_recent_job_time = created_time
                        
                        # If job is running, definitely skip
                        if job.status.active:
                            has_running_job = True
                            logger.info(
                                "backup_job_running_in_period_skipping",
                                database_id=db.id,
                                job_name=job.metadata.name,
                                schedule=backup_schedule,
                                created_at=created_time.isoformat(),
                                time_diff_seconds=time_diff,
                            )
                            break
                        elif job.status.succeeded:
                            logger.info(
                                "backup_already_done_for_period",
                                database_id=db.id,
                                schedule=backup_schedule,
                                period=period_name,
                                job_name=job.metadata.name,
                                created_at=created_time.isoformat(),
                                time_diff_seconds=time_diff,
                            )
                            break
                        elif job.status.failed:
                            # Even if failed, we had a backup attempt in this period
                            # For daily/weekly, we don't retry immediately
                            logger.info(
                                "backup_attempted_in_period_skipping",
                                database_id=db.id,
                                schedule=backup_schedule,
                                period=period_name,
                                job_name=job.metadata.name,
                                status="failed",
                                created_at=created_time.isoformat(),
                                time_diff_seconds=time_diff,
                            )
                            break
                
                # If ANY job exists in current period (running, succeeded, or failed), skip
                if has_running_job or has_recent_job:
                    logger.info(
                        "backup_skipped_frequency_check",
                        database_id=db.id,
                        schedule=backup_schedule,
                        period=period_name,
                        has_running=has_running_job,
                        has_recent_job=has_recent_job,
                        recent_job=recent_backup_job,
                        recent_job_time=most_recent_job_time.isoformat() if most_recent_job_time else None,
                    )
                    return
                
                logger.info(
                    "triggering_scheduled_backup",
                    database_id=db.id,
                    schedule=backup_schedule,
                    period=period_name,
                )
                
            except Exception as check_error:
                logger.warning(
                    "failed_to_check_existing_backups",
                    database_id=db.id,
                    error=str(check_error),
                )
                # Continue to trigger backup if check fails (better to have backup than not)
            finally:
                import os
                if os.path.exists(temp_kubeconfig):
                    os.unlink(temp_kubeconfig)
            
            # No backup exists for current period, trigger backup
            await self._trigger_initial_backup(db, provider_id, kubeconfig_content)
            
        except Exception as e:
            logger.warning(
                "ensure_backup_triggered_failed",
                database_id=db.id,
                error=str(e),
            )

    async def _trigger_initial_backup(
        self,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
    ) -> None:
        """
        Trigger initial backup for a database that just became RUNNING.
        
        Args:
            db: Database instance
            provider_id: Provider ID
            kubeconfig_content: Provider's kubeconfig content
        """
        from app.config.settings import settings
        from app.services.database_service import DatabaseService
        
        if not settings.backup_enabled:
            return
        
        try:
            database_service = DatabaseService()
            
            # Get database credentials for backup
            try:
                db_credentials_obj = await database_service.get_credentials(db.id, db.domain, db.project)
                db_credentials = {
                    "username": db_credentials_obj.username,
                    "password": db_credentials_obj.password,
                    "database": db_credentials_obj.database or ("postgres" if db.engine.value == "postgres" else db.name),
                }
            except Exception as cred_error:
                logger.warning(
                    "failed_to_get_credentials_for_initial_backup",
                    database_id=db.id,
                    error=str(cred_error),
                )
                # Use default credentials
                default_db = "postgres" if db.engine.value == "postgres" else (db.name if db.engine.value == "mysql" else "admin")
                db_credentials = {
                    "username": "postgres" if db.engine.value == "postgres" else "root",
                    "password": "",
                    "database": default_db,
                }
            
            # For PostgreSQL, use "postgres" database for backup
            backup_db_name = "postgres" if db.engine.value == "postgres" else db_credentials.get("database", db.name)
            
            # Check if backup job already exists (avoid duplicate backups)
            from kubernetes_asyncio import client, config
            import base64
            decoded_content = base64.b64decode(kubeconfig_content).decode('utf-8')
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.yaml') as f:
                f.write(decoded_content)
                temp_kubeconfig = f.name
            
            try:
                await config.load_kube_config(config_file=temp_kubeconfig)
                batch_api = client.BatchV1Api()
                
                # Check for existing backup jobs
                jobs = await batch_api.list_namespaced_job(
                    namespace=db.namespace,
                    label_selector=f"database={db.kubedb_resource_name},backup-type=direct",
                )
                
                # If backup job already exists, skip
                if jobs.items:
                    logger.info(
                        "backup_job_already_exists_skipping",
                        database_id=db.id,
                        existing_jobs=len(jobs.items),
                    )
                    return
            except Exception as check_error:
                logger.debug("failed_to_check_existing_backups", error=str(check_error))
            finally:
                import os
                if os.path.exists(temp_kubeconfig):
                    os.unlink(temp_kubeconfig)
            
            # Trigger direct backup job
            await kubedb_service.create_backup_job_direct(
                database_name=db.kubedb_resource_name,
                database_engine=db.engine,
                namespace=db.namespace,
                database_host=db.endpoint or "localhost",
                database_port=db.port or 5432,
                database_user=db_credentials.get("username", "postgres"),
                database_password=db_credentials.get("password", ""),
                database_name_db=backup_db_name,
                bucket=settings.backup_s3_bucket,
                region=settings.backup_s3_region,
                endpoint=settings.backup_s3_endpoint,
                access_key_id=settings.backup_s3_access_key_id,
                secret_access_key=settings.backup_s3_secret_access_key,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
            )

            logger.info(
                "initial_backup_triggered_from_status_sync",
                database_id=db.id,
                database_name=db.kubedb_resource_name,
            )
        except Exception as backup_error:
            # Don't fail status sync if backup fails
            logger.warning(
                "initial_backup_failed_from_status_sync",
                database_id=db.id,
                error=str(backup_error),
            )


# Global reconciler instance (replaces StatusSyncService)
database_reconciler = DatabaseReconciler()

# Backward compatibility alias
status_sync_service = database_reconciler
