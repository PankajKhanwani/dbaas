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
            
            db.ready_replicas = ready_replicas
            
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

            # Handle DELETING status - perform actual deletion
            # The DELETE endpoint sets status to DELETING, and we need to actually delete the resource
            if db.status == DatabaseStatus.DELETING:
                logger.info(
                    "database_status_deleting_performing_deletion",
                    database_id=db.id,
                    message="Database status is DELETING - performing actual deletion"
                )
                # Call deletion handler to delete KubeDB resource and clean up
                await self._handle_database_deletion(
                    db=db,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )
                return  # Exit after deletion is handled

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
                    # Resume completed - update to RUNNING
                    logger.info(
                        "resume_completed_updating_to_running",
                        database_id=db.id,
                        kubedb_phase=phase,
                    )
                    db.status = DatabaseStatus.RUNNING
                    await db.save()
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
            # If K8s says Ready and no active OpsRequest, set to RUNNING
            # Only keep UPDATING if there's an active OpsRequest, recent OpsRequest, or upgrade operation
            # IMPORTANT: Don't override UPDATING status if it was just set (recent OpsRequest or status was UPDATING)
            if has_active_ops_request or has_recent_ops_request or active_operation:
                # Keep status as UPDATING if there's an active/recent OpsRequest or upgrade operation
                if db.status != DatabaseStatus.UPDATING:
                    logger.info(
                        "setting_status_to_updating_due_to_active_operation",
                        database_id=db.id,
                        has_active_ops_request=has_active_ops_request,
                        has_recent_ops_request=has_recent_ops_request,
                        has_active_operation=bool(active_operation),
                        operation_id=str(active_operation.id) if active_operation else None,
                    )
                    db.status = DatabaseStatus.UPDATING
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

            # Only update status from KubeDB phase if no operation is in progress
            # This preserves operation status (UPDATING, SCALING, RESUMING, etc.) until operation completes
            if not operation_performed:
                mapped_status = self._map_kubedb_phase_to_status(phase, is_ready)
                # Only update if not in an operation state
                # IMPORTANT: Don't override RESUMING or PAUSING status here - they're handled above
                if old_status not in [
                    DatabaseStatus.UPDATING,
                    DatabaseStatus.SCALING,
                    DatabaseStatus.DELETING,
                    DatabaseStatus.PAUSED,
                    DatabaseStatus.RESUMING,  # Preserve RESUMING status
                    DatabaseStatus.PAUSING,     # Preserve PAUSING status
                ]:
                    db.status = mapped_status
                # If operation completed (status changed from operation state), update ONLY if no active OpsRequest or operation
                elif old_status in [DatabaseStatus.UPDATING, DatabaseStatus.SCALING] and mapped_status == DatabaseStatus.RUNNING:
                    # Double-check there's no active OpsRequest or upgrade operation before changing to RUNNING
                    logger.info(
                        "status_sync_operation_completion_check",
                        database_id=db.id,
                        old_status=old_status.value,
                        mapped_status=mapped_status.value,
                        has_active_ops_request=has_active_ops_request,
                        has_active_operation=bool(active_operation),
                    )
                    if not has_active_ops_request and not active_operation:
                        logger.info(
                            "changing_status_to_running_no_active_operation",
                            database_id=db.id,
                        )
                        db.status = mapped_status
                    else:
                        logger.info(
                            "keeping_updating_status_active_operation_exists",
                            database_id=db.id,
                            has_active_ops_request=has_active_ops_request,
                            operation_id=str(active_operation.id) if active_operation else None,
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
                
                # Update status to RUNNING if database is ready
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
                    
                    # Update status to RUNNING if database is ready and phase is Ready/Running
                    if db_is_ready and db_phase.lower() in ["ready", "running"] and db.status == DatabaseStatus.UPDATING:
                        db.status = DatabaseStatus.RUNNING
                        logger.info(
                            "database_status_updated_to_running_after_opsrequest",
                            database_id=db.id,
                            ops_request_name=ops_request_name,
                            phase=db_phase,
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
                        if ready_replicas < db.replicas:
                            logger.info(
                                "database_still_scaling_replicas",
                                database_id=db.id,
                                ready_replicas=ready_replicas,
                                desired_replicas=db.replicas,
                                message="Preserving UPDATING/SCALING status until all replicas are ready",
                            )
                            await db.save()
                            return

                    # Only update status to RUNNING if no active operations and replicas match
                    logger.info(
                        "database_up_to_date_setting_running",
                        database_id=db.id,
                    )
                    db.status = DatabaseStatus.RUNNING
                    await db.save()
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


# Global reconciler instance (replaces StatusSyncService)
database_reconciler = DatabaseReconciler()

# Backward compatibility alias
status_sync_service = database_reconciler
