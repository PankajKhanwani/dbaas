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
        self.reconcile_interval = 10
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
                # Wait a bit before retrying
                await asyncio.sleep(60)

        logger.info("reconciliation_worker_stopped")

    async def stop(self):
        """Stop reconciliation worker gracefully."""
        logger.info("reconciliation_worker_stopping")
        self.running = False
        # Cancel any ongoing sleep to exit immediately
        if self._sleep_task and not self._sleep_task.done():
            self._sleep_task.cancel()
            try:
                await self._sleep_task
            except asyncio.CancelledError:
                pass

    async def reconcile_all_databases(self):
        """
        Reconcile all databases across all providers.

        Checks each database's KubeDB CR against its database record.
        Creates operations when drift is detected.
        """
        try:
            # Get all active providers
            providers = await Provider.find_all().to_list()

            logger.info(
                "reconciliation_started",
                provider_count=len(providers),
            )

            for provider in providers:
                try:
                    await self._reconcile_provider_databases(provider)
                except Exception as e:
                    logger.error(
                        "provider_reconciliation_failed",
                        provider_id=provider.id,
                        error=str(e),
                    )
                    continue

            logger.info("reconciliation_completed")

        except Exception as e:
            logger.error(
                "reconciliation_failed",
                error=str(e),
                exc_info=True,
            )

    async def _reconcile_provider_databases(self, provider: Provider):
        """Reconcile all databases for a specific provider."""
        # Get all databases for this provider that need reconciliation
        databases = await Database.find(
            {"provider_id": provider.id, "status": {"$in": [
                DatabaseStatus.RUNNING.value,
                DatabaseStatus.UPDATING.value,
                DatabaseStatus.SCALING.value,
            ]}}
        ).to_list()

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
                    "database_reconciliation_failed",
                    database_id=db.id,
                    provider_id=provider.id,
                    error=str(e),
                )
                continue

    async def _reconcile_database(self, db: Database, provider: Provider):
        """
        Reconcile a single database.

        Steps:
        1. Fetch current state from KubeDB CR
        2. Update current_* fields in database record
        3. Compare desired state (size, replicas, storage_gb) vs current state
        4. Create operations if drift detected
        """
        try:
            # Get kubeconfig
            kubeconfig_content = provider.kubeconfig_content

            # Get current KubeDB CR
            client_set = await kubedb_service.get_client_for_provider(
                provider.id, kubeconfig_content
            )

            current_cr = await client_set.custom_api.get_namespaced_custom_object(
                group=kubedb_service._get_kubedb_group(db.engine),
                version=kubedb_service._get_kubedb_version(db.engine),
                namespace=db.namespace,
                plural=kubedb_service._get_kubedb_plural(db.engine),
                name=db.kubedb_resource_name,
            )

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

            # STEP 2: Compare desired vs current state
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
                # Only allow storage expansion
                if db.storage_gb > (db.current_storage_gb or 0):
                    operations_to_create.append({
                        "type": OperationType.EXPAND_STORAGE,
                        "desired_state": {"storage_gb": db.storage_gb},
                    })
                    drift_detected = True

            # Check size drift (desired vs current)
            if db.size != db.current_size:
                logger.info(
                    "size_drift_detected",
                    database_id=db.id,
                    desired=db.size.value if db.size else None,
                    current=db.current_size.value if db.current_size else None,
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
    ):
        """
        Create an operation to fix drift.

        Uses the operation queue so it goes through the same
        flow as user-initiated operations.
        """
        try:
            # Create operation record
            operation = Operation(
                database_id=db.id,
                type=operation_type,
                desired_state=desired_state,
                status=OperationStatus.QUEUED,
                domain=db.domain,
                project=db.project,
                created_by="reconciliation_worker",
            )
            await operation.insert()

            # Enqueue operation (with deduplication)
            dedup_key = f"{db.id}:{operation_type.value}"
            enqueued = await operation_queue.enqueue(
                operation.id,
                priority=3,  # Lower priority than user operations (which use 5)
                dedup_key=dedup_key,
            )

            if enqueued:
                logger.info(
                    "reconciliation_operation_created",
                    database_id=db.id,
                    operation_id=operation.id,
                    operation_type=operation_type.value,
                    desired_state=desired_state,
                )
            else:
                logger.info(
                    "reconciliation_operation_skipped_duplicate",
                    database_id=db.id,
                    operation_type=operation_type.value,
                )

        except Exception as e:
            logger.error(
                "failed_to_create_reconciliation_operation",
                database_id=db.id,
                operation_type=operation_type.value,
                error=str(e),
            )

    def _parse_storage(self, storage_str: str) -> Optional[int]:
        """Parse storage string (e.g., '20Gi') to GB integer."""
        if not storage_str:
            return None
        try:
            return int(storage_str.replace("Gi", "").replace("G", ""))
        except:
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
        # This is reverse-engineering from kubedb_service._get_resource_limits()
        resource_map = {
            ("500m", "1Gi"): DatabaseSize.DB_T3_MICRO,
            ("1", "2Gi"): DatabaseSize.DB_T3_SMALL,
            ("2", "4Gi"): DatabaseSize.DB_T3_MEDIUM,
            ("2", "8Gi"): DatabaseSize.DB_T3_LARGE,
            ("4", "16Gi"): DatabaseSize.DB_T3_XLARGE,
            ("8", "32Gi"): DatabaseSize.DB_T3_2XLARGE,
        }

        for (expected_cpu, expected_mem), size in resource_map.items():
            if cpu == expected_cpu and memory == expected_mem:
                return size

        # If no exact match, return None (will be detected as drift)
        return None


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

    def signal_handler(sig):
        logger.info("signal_received", signal=sig)
        shutdown_event.set()

    # Register signal handlers
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: signal_handler("SIGTERM"))
    loop.add_signal_handler(signal.SIGINT, lambda: signal_handler("SIGINT"))

    # Monitor shutdown event
    async def monitor_shutdown():
        await shutdown_event.wait()
        await worker.stop()

    # Start shutdown monitor
    shutdown_task = asyncio.create_task(monitor_shutdown())

    # Run worker
    try:
        await asyncio.gather(worker.start(), shutdown_task, return_exceptions=True)
    finally:
        # Cleanup
        shutdown_task.cancel()
        try:
            await shutdown_task
        except asyncio.CancelledError:
            pass
        await operation_queue.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("reconciliation_worker_interrupted")
        sys.exit(0)
