"""
Operation worker for processing database operations asynchronously.

Runs as a separate process/container from the API.
Processes operations from Redis queue.
"""
import asyncio
import signal
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config.database import Database as DatabaseConnection
from app.config.logging import get_logger
from app.models.operation import Operation, OperationStatus
from app.core.state_machine import OperationType
from app.repositories.models import Database
from app.models.database import DatabaseStatus
from app.services.operation_queue import operation_queue
from app.services.kubedb_service import kubedb_service
from app.services.database_service import DatabaseService
from app.services import metrics
from app.exceptions import KubeDBError

logger = get_logger(__name__)


class OperationWorker:
    """
    Processes queued database operations asynchronously.

    Features:
    - Async processing (non-blocking)
    - Progress tracking
    - Retry logic
    - Timeout handling
    - Graceful shutdown
    """

    def __init__(self, worker_id: int = 1):
        """Initialize worker."""
        self.worker_id = worker_id
        self.running = False
        self.current_operation_id: Optional[str] = None
        self._current_task: Optional[asyncio.Task] = None
        self._shutdown_timeout = 5  # Force shutdown after 5 seconds

    async def start(self):
        """Start worker (runs until stopped)."""
        self.running = True

        logger.info(
            "worker_started",
            worker_id=self.worker_id,
        )

        while self.running:
            try:
                # Get next operation (blocks for 5 seconds, but check running flag)
                # Use shorter timeout to allow checking running flag more frequently
                operation_id = await operation_queue.dequeue(timeout=1)

                if not operation_id:
                    # No operations, wait a bit
                    await asyncio.sleep(1)
                    continue

                # Process it
                self.current_operation_id = operation_id
                self._current_task = asyncio.create_task(self.process_operation(operation_id))
                try:
                    await self._current_task
                except asyncio.CancelledError:
                    logger.warning(
                        "operation_cancelled_during_shutdown",
                        operation_id=operation_id,
                    )
                    # Mark operation as failed due to shutdown
                    try:
                        operation = await Operation.get(operation_id)
                        if operation:
                            operation.status = OperationStatus.FAILED
                            operation.error_message = "Operation cancelled due to worker shutdown"
                            await operation.save()
                    except Exception as e:
                        logger.error(
                            "failed_to_mark_operation_cancelled",
                            operation_id=operation_id,
                            error=str(e),
                        )
                finally:
                    self.current_operation_id = None
                    self._current_task = None

            except asyncio.CancelledError:
                logger.info("worker_cancelled", worker_id=self.worker_id)
                break
            except Exception as e:
                logger.error(
                    "worker_error",
                    worker_id=self.worker_id,
                    error=str(e),
                    exc_info=True,
                )
                await asyncio.sleep(5)

        logger.info("worker_stopped", worker_id=self.worker_id)

    async def stop(self):
        """Stop worker gracefully."""
        logger.info("worker_stopping", worker_id=self.worker_id)
        self.running = False

        # Cancel current operation task if running
        if self._current_task and not self._current_task.done():
            logger.info(
                "worker_cancelling_current_operation",
                operation_id=self.current_operation_id,
            )
            self._current_task.cancel()
            try:
                await asyncio.wait_for(self._current_task, timeout=self._shutdown_timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.warning(
                    "worker_operation_cancelled_or_timeout",
                    operation_id=self.current_operation_id,
                )
            except Exception as e:
                logger.error(
                    "worker_error_cancelling_operation",
                    operation_id=self.current_operation_id,
                    error=str(e),
                )

    async def process_operation(self, operation_id: str):
        """
        Process a single operation.

        Flow:
        1. Load operation from DB
        2. Mark as IN_PROGRESS
        3. Execute operation (create OpsRequest, monitor it)
        4. Update status (COMPLETED or FAILED)
        """
        logger.info(
            "processing_operation",
            worker_id=self.worker_id,
            operation_id=operation_id,
        )

        # Mark as processing in queue
        await operation_queue.mark_processing(operation_id)

        # Set worker busy
        metrics.set_worker_busy(self.worker_id, True)

        try:
            # Load operation
            operation = await Operation.get(operation_id)
            if not operation:
                logger.error("operation_not_found", operation_id=operation_id)
                metrics.set_worker_busy(self.worker_id, False)
                return

            # Load database
            db = await Database.get(operation.database_id)
            if not db:
                logger.error(
                    "database_not_found",
                    operation_id=operation_id,
                    database_id=operation.database_id,
                )
                operation.status = OperationStatus.FAILED
                operation.error_message = "Database not found"
                await operation.save()
                metrics.set_worker_busy(self.worker_id, False)
                return

            # Record operation start
            metrics.record_operation_start(operation.type.value)

            # Update status to IN_PROGRESS
            operation.status = OperationStatus.IN_PROGRESS
            operation.started_at = datetime.now(timezone.utc)
            operation.estimated_completion_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            await operation.save()

            # Set database status to UPDATING
            db.status = DatabaseStatus.UPDATING
            await db.save()

            logger.info(
                "database_status_set_to_updating",
                operation_id=operation_id,
                database_id=db.id,
            )

            # Process based on type
            if operation.type == OperationType.SCALE_VERTICAL:
                await self.process_scale_vertical(operation, db)
            elif operation.type == OperationType.SCALE_HORIZONTAL:
                await self.process_scale_horizontal(operation, db)
            elif operation.type == OperationType.EXPAND_STORAGE:
                await self.process_expand_storage(operation, db)
            elif operation.type == OperationType.UPGRADE_VERSION:
                await self.process_upgrade_version(operation, db)
            else:
                raise ValueError(f"Unsupported operation type: {operation.type}")

            # Mark as completed for operations that don't require async monitoring
            # ASYNC OPERATIONS (monitored by status sync service):
            # - SCALE_VERTICAL: Status sync monitors OpsRequest until complete
            # - SCALE_HORIZONTAL: Status sync monitors OpsRequest until complete
            # - UPGRADE_VERSION: Status sync monitors OpsRequest until complete
            #
            # SYNC OPERATIONS (completed immediately):
            # - EXPAND_STORAGE: Direct patch, completes immediately
            async_operation_types = [
                OperationType.SCALE_VERTICAL,
                OperationType.SCALE_HORIZONTAL,
                OperationType.UPGRADE_VERSION
            ]

            if operation.type not in async_operation_types:
                # Sync operation - mark as completed now
                operation.status = OperationStatus.COMPLETED
                operation.progress = 100
                operation.completed_at = datetime.now(timezone.utc)
                operation.message = "Operation completed successfully"
                await operation.save()

                # Update current state to match desired state
                await self._update_current_state_after_operation(db, operation)
            else:
                # Async operation - status sync service will mark as completed
                logger.info(
                    "async_operation_queued_for_monitoring",
                    operation_id=operation.id,
                    operation_type=operation.type.value,
                    message="Status sync service will monitor and complete",
                )

            # Mark completed in queue
            dedup_key = f"{db.id}:{operation.type.value}"
            await operation_queue.mark_completed(operation_id, dedup_key=dedup_key)

            # Record metrics
            duration = operation.get_duration_seconds() or 0
            metrics.record_operation_complete(
                operation.type.value, OperationStatus.COMPLETED.value, duration
            )
            metrics.record_worker_operation_processed(self.worker_id)
            metrics.set_worker_busy(self.worker_id, False)

            logger.info(
                "operation_completed",
                worker_id=self.worker_id,
                operation_id=operation_id,
                duration_seconds=duration,
            )

        except Exception as e:
            logger.error(
                "operation_failed",
                worker_id=self.worker_id,
                operation_id=operation_id,
                error=str(e),
                exc_info=True,
            )

            # Record error metric
            metrics.record_worker_error(self.worker_id)

            # Mark as failed
            operation = await Operation.get(operation_id)
            if operation:
                operation.status = OperationStatus.FAILED
                operation.error_message = str(e)
                operation.completed_at = datetime.now(timezone.utc)
                await operation.save()

                # Check if can retry
                if operation.can_retry():
                    logger.info(
                        "operation_retry_scheduled",
                        operation_id=operation_id,
                        retry_count=operation.retry_count + 1,
                    )
                    # Record retry metric
                    metrics.record_operation_retry(operation.type.value)

                    # Requeue with higher priority
                    await operation_queue.requeue_failed(operation_id, priority=10)
                else:
                    # Update database status
                    db = await Database.get(operation.database_id)
                    if db:
                        db.status = DatabaseStatus.FAILED
                        await db.save()

                    # Mark completed (failed) in queue
                    dedup_key = f"{db.id}:{operation.type.value}"
                    await operation_queue.mark_completed(operation_id, dedup_key=dedup_key)

                    # Record failed operation metric
                    duration = operation.get_duration_seconds() or 0
                    metrics.record_operation_complete(
                        operation.type.value, OperationStatus.FAILED.value, duration
                    )

            # Set worker not busy
            metrics.set_worker_busy(self.worker_id, False)

    async def process_scale_vertical(self, operation: Operation, db: Database):
        """Process vertical scaling operation."""
        logger.info(
            "processing_vertical_scale",
            operation_id=operation.id,
            database_id=db.id,
            target_size=operation.desired_state.get("size"),
        )

        # Get provider credentials
        db_service = DatabaseService()
        provider_id, kubeconfig_content = await db_service._get_provider_kubeconfig(db)

        # Update progress
        operation.progress = 10
        operation.message = "Creating OpsRequest"
        await operation.save()

        # Create OpsRequest
        from app.models.database import DatabaseSize
        target_size = DatabaseSize(operation.desired_state["size"])

        ops_request = await kubedb_service.create_vertical_scaling_ops_request(
            engine=db.engine,
            name=db.kubedb_resource_name,
            namespace=db.namespace,
            size=target_size,
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )

        # Store OpsRequest details
        operation.ops_request_name = ops_request["metadata"]["name"]
        operation.ops_request_uid = ops_request["metadata"]["uid"]
        operation.status = OperationStatus.IN_PROGRESS
        operation.progress = 20
        operation.message = "VerticalScaling OpsRequest created - monitoring via status sync"
        await operation.save()

        # Record OpsRequest created metric
        metrics.record_ops_request_created(db.engine.value, "vertical_scaling")

        logger.info(
            "ops_request_created_async",
            operation_id=operation.id,
            ops_request_name=operation.ops_request_name,
            message="Status sync service will monitor completion",
        )

        # NOTE: Don't wait for OpsRequest completion!
        # The status sync service will monitor the OpsRequest and update:
        # - operation.status when complete/failed
        # - operation.progress based on phase
        # - operation.ops_request_phase from K8s
        # - db.status when complete
        # This allows the worker to process other operations immediately.

    async def process_scale_horizontal(self, operation: Operation, db: Database):
        """Process horizontal scaling (replicas) operation."""
        logger.info(
            "processing_horizontal_scale",
            operation_id=operation.id,
            database_id=db.id,
            target_replicas=operation.desired_state.get("replicas"),
        )

        # Get provider credentials
        db_service = DatabaseService()
        provider_id, kubeconfig_content = await db_service._get_provider_kubeconfig(db)

        # Update progress
        operation.progress = 10
        operation.message = "Creating horizontal scaling OpsRequest"
        await operation.save()

        # Create OpsRequest for horizontal scaling
        result = await kubedb_service.patch_database(
            engine=db.engine,
            name=db.kubedb_resource_name,
            namespace=db.namespace,
            replicas=operation.desired_state["replicas"],
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )

        # Extract OpsRequest name from result
        ops_request_name = result.get("ops_request_name") if isinstance(result, dict) else None
        if ops_request_name:
            operation.ops_request_name = ops_request_name
            operation.message = f"Monitoring OpsRequest: {ops_request_name}"
            await operation.save()

            # Monitor OpsRequest progress (like vertical scaling)
            ops_start_time = datetime.now(timezone.utc)
            await self.monitor_ops_request(
                operation=operation,
                db=db,
                provider_id=provider_id,
                kubeconfig_content=kubeconfig_content,
                timeout=600,  # 10 minutes
            )

            # Record OpsRequest completion metric
            ops_duration = (datetime.now(timezone.utc) - ops_start_time).total_seconds()
            final_phase = operation.ops_request_phase or "Successful"
            metrics.record_ops_request_complete(
                db.engine.value, "horizontal_scaling", final_phase, ops_duration
            )

            # After OpsRequest completes, update current_replicas to match spec.replicas
            # This ensures reconciler won't detect false drift
            if operation.ops_request_phase in ["Successful", "Skipped"]:
                # Fetch the actual CR to get the final spec.replicas value
                client_set = await kubedb_service.get_client_for_provider(provider_id, kubeconfig_content)
                try:
                    current_cr = await client_set.custom_api.get_namespaced_custom_object(
                        group=kubedb_service._get_kubedb_group(db.engine),
                        version=kubedb_service._get_kubedb_version(db.engine),
                        namespace=db.namespace,
                        plural=kubedb_service._get_kubedb_plural(db.engine),
                        name=db.kubedb_resource_name,
                    )
                    cr_replicas = current_cr.get("spec", {}).get("replicas", db.replicas)
                    db.current_replicas = cr_replicas
                    await db.save()
                    logger.info(
                        "current_replicas_updated_after_horizontal_scaling",
                        database_id=db.id,
                        current_replicas=cr_replicas,
                        desired_replicas=db.replicas,
                    )
                except Exception as e:
                    logger.warning(
                        "failed_to_update_current_replicas_after_scaling",
                        database_id=db.id,
                        error=str(e),
                    )
        else:
            # Fallback if OpsRequest name not returned
            operation.progress = 100
            operation.message = "HorizontalScaling OpsRequest created"
            await operation.save()

    async def process_expand_storage(self, operation: Operation, db: Database):
        """Process storage expansion operation."""
        logger.info(
            "processing_storage_expansion",
            operation_id=operation.id,
            database_id=db.id,
            target_storage=operation.desired_state.get("storage_gb"),
        )

        # Get provider credentials
        db_service = DatabaseService()
        provider_id, kubeconfig_content = await db_service._get_provider_kubeconfig(db)

        # Update progress
        operation.progress = 20
        operation.message = "Expanding storage"
        await operation.save()

        # Patch database with new storage
        await kubedb_service.patch_database(
            engine=db.engine,
            name=db.kubedb_resource_name,
            namespace=db.namespace,
            storage_gb=operation.desired_state["storage_gb"],
            provider_id=provider_id,
            kubeconfig_content=kubeconfig_content,
        )

        # Update database record
        db.storage_gb = operation.desired_state["storage_gb"]
        await db.save()

        operation.progress = 100
        operation.message = "Storage expanded"
        await operation.save()

    async def process_upgrade_version(self, operation: Operation, db: Database):
        """Process version upgrade operation."""
        logger.info(
            "processing_version_upgrade",
            operation_id=operation.id,
            database_id=db.id,
            target_version=operation.desired_state.get("target_version"),
        )

        # Get provider credentials
        db_service = DatabaseService()
        provider_id, kubeconfig_content = await db_service._get_provider_kubeconfig(db)

        # OpsRequest should already be created by the API endpoint
        if not operation.ops_request_name:
            raise ValueError("OpsRequest name not found in operation. Upgrade may not have been initiated correctly.")

        # Update progress and status
        operation.status = OperationStatus.IN_PROGRESS
        operation.progress = 20
        operation.message = "VersionUpgrade OpsRequest created - monitoring via status sync"
        await operation.save()

        # Record OpsRequest created metric
        metrics.record_ops_request_created(db.engine.value, "version_upgrade")

        logger.info(
            "ops_request_created_async",
            operation_id=operation.id,
            ops_request_name=operation.ops_request_name,
            message="Status sync service will monitor completion",
        )

        # NOTE: Don't wait for OpsRequest completion!
        # The status sync service will monitor the OpsRequest and update:
        # - operation.status when complete/failed
        # - operation.progress based on phase
        # - operation.ops_request_phase from K8s
        # - db.version when complete
        # This allows the worker to process other operations immediately.

    async def monitor_ops_request(
        self,
        operation: Operation,
        db: Database,
        provider_id: str,
        kubeconfig_content: str,
        timeout: int,
    ):
        """
        Monitor OpsRequest until completion.

        Updates operation progress based on OpsRequest phase.
        """
        start_time = datetime.now(timezone.utc)

        while self.running:  # Check running flag to allow cancellation
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

            if elapsed > timeout:
                raise TimeoutError(f"Operation timed out after {timeout}s")

            # Check OpsRequest status
            try:
                status = await kubedb_service.get_ops_request_status(
                    engine=db.engine,
                    ops_request_name=operation.ops_request_name,
                    namespace=db.namespace,
                    provider_id=provider_id,
                    kubeconfig_content=kubeconfig_content,
                )

                phase = status["phase"]
                operation.ops_request_phase = phase

                # Update progress based on phase
                if phase == "Pending":
                    operation.progress = 30
                    operation.message = "OpsRequest pending"
                elif phase == "Progressing":
                    # Estimate progress: 30% to 90%
                    progress = 30 + int((elapsed / timeout) * 60)
                    operation.progress = min(progress, 90)
                    operation.message = "Updating pods (rolling restart)"
                elif phase == "Successful":
                    operation.progress = 100
                    operation.message = "OpsRequest successful"
                    await operation.save()

                    # Update database status
                    db.status = DatabaseStatus.RUNNING
                    await db.save()

                    logger.info(
                        "ops_request_successful",
                        operation_id=operation.id,
                        ops_request_name=operation.ops_request_name,
                        duration=elapsed,
                    )

                    # Clean up: Delete successful OpsRequest
                    try:
                        await kubedb_service.delete_ops_request(
                            engine=db.engine,
                            ops_request_name=operation.ops_request_name,
                            namespace=db.namespace,
                            provider_id=provider_id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        logger.info(
                            "ops_request_cleaned_up",
                            operation_id=operation.id,
                            ops_request_name=operation.ops_request_name,
                        )
                    except Exception as e:
                        # Log but don't fail the operation if cleanup fails
                        logger.warning(
                            "ops_request_cleanup_failed",
                            operation_id=operation.id,
                            ops_request_name=operation.ops_request_name,
                            error=str(e),
                        )

                    return

                elif phase == "Failed":
                    failure_reason = self.get_failure_reason(status)

                    # Clean up: Delete failed OpsRequest
                    try:
                        await kubedb_service.delete_ops_request(
                            engine=db.engine,
                            ops_request_name=operation.ops_request_name,
                            namespace=db.namespace,
                            provider_id=provider_id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        logger.info(
                            "failed_ops_request_cleaned_up",
                            operation_id=operation.id,
                            ops_request_name=operation.ops_request_name,
                        )
                    except Exception as e:
                        logger.warning(
                            "failed_ops_request_cleanup_failed",
                            operation_id=operation.id,
                            ops_request_name=operation.ops_request_name,
                            error=str(e),
                        )

                    raise KubeDBError(f"OpsRequest failed: {failure_reason}")

                await operation.save()

            except KubeDBError:
                raise
            except Exception as e:
                logger.warning(
                    "error_checking_ops_request_status",
                    operation_id=operation.id,
                    error=str(e),
                )

            # Wait before next check (with cancellation support)
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                logger.info("ops_request_monitoring_cancelled", operation_id=operation.id)
                raise

    def get_failure_reason(self, status: dict) -> str:
        """Extract failure reason from OpsRequest status."""
        conditions = status.get("conditions", [])
        for condition in conditions:
            if condition.get("type") == "Failed" or condition.get("reason") == "Failed":
                return condition.get("message", "Unknown error")
        return "Unknown error"

    async def _update_current_state_after_operation(
        self,
        db: Database,
        operation: Operation,
    ):
        """
        Update current_* fields after successful operation.

        After an operation completes successfully, the current state
        should match the desired state that was just applied.

        IMPORTANT: Uses targeted update to avoid overwriting desired state.
        """
        from app.models.database import DatabaseSize

        try:
            # Build update dict for ONLY current_* fields
            # Do NOT use db.save() as it overwrites desired state!
            update_dict = {}

            if operation.type == OperationType.SCALE_VERTICAL:
                # Update current size
                size_str = operation.desired_state.get("size")
                if size_str:
                    update_dict["current_size"] = DatabaseSize(size_str)

            elif operation.type == OperationType.SCALE_HORIZONTAL:
                # Update current replicas
                replicas = operation.desired_state.get("replicas")
                if replicas is not None:
                    update_dict["current_replicas"] = replicas

            elif operation.type == OperationType.EXPAND_STORAGE:
                # Update current storage
                storage_gb = operation.desired_state.get("storage_gb")
                if storage_gb is not None:
                    update_dict["current_storage_gb"] = storage_gb

            # Update ONLY current_* fields using targeted update
            # This preserves desired state fields (replicas, size, storage_gb)
            if update_dict:
                await Database.find_one({"_id": db.id}).update({"$set": update_dict})

            logger.info(
                "current_state_updated_after_operation",
                database_id=db.id,
                operation_type=operation.type.value,
                desired_state=operation.desired_state,
            )

        except Exception as e:
            logger.error(
                "failed_to_update_current_state",
                database_id=db.id,
                operation_id=operation.id,
                error=str(e),
            )


# Main entry point
async def main():
    """Run operation worker."""
    # Initialize database connection
    from app.repositories.models import Database as DatabaseModel
    from app.repositories.models import Provider as ProviderModel
    from app.repositories.models import AuditLog
    from app.models.operation import Operation as OperationModel

    document_models = [DatabaseModel, ProviderModel, AuditLog, OperationModel]
    await DatabaseConnection.connect_db(document_models)

    # Create worker
    worker = OperationWorker(worker_id=1)

    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()

    def signal_handler(sig):
        logger.info("signal_received", signal=sig)
        asyncio.create_task(worker.stop())

    loop.add_signal_handler(signal.SIGTERM, lambda: signal_handler("SIGTERM"))
    loop.add_signal_handler(signal.SIGINT, lambda: signal_handler("SIGINT"))

    # Run worker
    try:
        await worker.start()
    finally:
        # Cleanup
        await operation_queue.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("worker_interrupted")
        sys.exit(0)

