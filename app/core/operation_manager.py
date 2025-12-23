"""
Operation Manager for DBaaS

This module orchestrates database operations using the state machine,
operation queue, and distributed locks to ensure consistent and
concurrent-safe operation execution.

This is the main entry point for all database operations from the API.

Features:
- Validates operations against current database state
- Enqueues operations with proper priority
- Tracks operation lifecycle
- Provides operation status and progress
- Handles operation cancellation

Usage:
    >>> from app.core.operation_manager import OperationManager, operation_manager
    >>>
    >>> # Enqueue a scale operation
    >>> operation_id = await operation_manager.enqueue_operation(
    ...     database_id="db-123",
    ...     operation_type="scale",
    ...     params={"replicas": 3},
    ...     user_id="user-456"
    ... )
    >>>
    >>> # Check operation status
    >>> status = await operation_manager.get_operation_status(operation_id)
    >>> print(status["status"])  # "queued" or "processing" or "completed"
"""

import uuid
from typing import Dict, List, Optional, Any
from datetime import datetime
import structlog
from beanie import PydanticObjectId

from app.core.state_machine import (
    DatabaseState,
    DatabaseStateMachine,
    OperationType
)
from app.core.operation_queue import OperationQueue
from app.core.lock_manager import LockManager
from app.repositories.models import Database
from app.models.operation import DatabaseOperation, OperationStatus
from app.models.database import DatabaseStatus

logger = structlog.get_logger(__name__)


def map_status_to_state(status: DatabaseStatus) -> DatabaseState:
    """
    Map DatabaseStatus (API/model enum) to DatabaseState (state machine enum).

    Args:
        status: DatabaseStatus enum value

    Returns:
        Corresponding DatabaseState enum value
    """
    mapping = {
        DatabaseStatus.PENDING: DatabaseState.CREATING,
        DatabaseStatus.PROVISIONING: DatabaseState.CREATING,
        DatabaseStatus.RUNNING: DatabaseState.AVAILABLE,
        DatabaseStatus.UPDATING: DatabaseState.MODIFYING,
        DatabaseStatus.SCALING: DatabaseState.MODIFYING,
        DatabaseStatus.PAUSED: DatabaseState.AVAILABLE,
        DatabaseStatus.FAILED: DatabaseState.FAILED,
        DatabaseStatus.DELETING: DatabaseState.DELETING,
        DatabaseStatus.DELETED: DatabaseState.DELETED,
    }
    return mapping.get(status, DatabaseState.AVAILABLE)


class OperationManager:
    """
    Central manager for database operations.

    Coordinates state machine, operation queue, and distributed locks
    to provide reliable concurrent operation execution.
    """

    # Priority mapping for operation types
    OPERATION_PRIORITIES = {
        OperationType.DELETE: 1,      # Highest priority - cleanup
        OperationType.SCALE: 2,        # High priority - capacity management
        OperationType.BACKUP: 3,       # Medium-high priority - data safety
        OperationType.UPDATE: 4,       # Medium priority
        OperationType.RESTART: 4,      # Medium priority
        OperationType.RESTORE: 5,      # Lower priority
        OperationType.MAINTENANCE: 5,  # Lower priority
    }

    def __init__(self):
        """Initialize operation manager"""
        self.queue = OperationQueue()
        self.lock_manager = LockManager()

    async def enqueue_operation(
        self,
        database_id: str,
        operation_type: str,
        params: Dict[str, Any],
        user_id: Optional[str] = None,
        priority: Optional[int] = None
    ) -> str:
        """
        Enqueue a new operation for a database.

        This method:
        1. Loads current database state
        2. Validates operation is allowed in current state
        3. Creates operation record in database
        4. Enqueues operation in Redis queue
        5. Returns operation ID for tracking

        Args:
            database_id: Database ID
            operation_type: Type of operation (scale, update, delete, etc.)
            params: Operation parameters
            user_id: Optional user ID who requested operation
            priority: Optional custom priority (1-5)

        Returns:
            Operation ID for tracking

        Raises:
            ValueError: If operation not allowed in current state
            Exception: If database not found

        Example:
            >>> operation_id = await operation_manager.enqueue_operation(
            ...     database_id="db-123",
            ...     operation_type="scale",
            ...     params={"replicas": 3},
            ...     user_id="user-456"
            ... )
            >>> print(f"Operation queued: {operation_id}")
        """
        # Load database
        database = await Database.get(database_id)
        if not database:
            raise ValueError(f"Database {database_id} not found")

        # Convert operation type string to enum
        try:
            op_type = OperationType(operation_type.lower())
        except ValueError:
            raise ValueError(
                f"Invalid operation type: {operation_type}. "
                f"Allowed: {[t.value for t in OperationType]}"
            )

        # Get current state (map DatabaseStatus to DatabaseState)
        current_state = map_status_to_state(database.status)

        # Validate operation is allowed in current state
        DatabaseStateMachine.validate_operation(
            current_state,
            op_type,
            database_id
        )

        # Generate operation ID
        operation_id = f"op-{uuid.uuid4().hex[:12]}"

        # Determine priority
        if priority is None:
            priority = self.OPERATION_PRIORITIES.get(op_type, 5)

        # Create operation record in database
        operation = DatabaseOperation(
            id=operation_id,
            database_id=database_id,
            operation_type=operation_type,
            status=OperationStatus.QUEUED,
            params=params,
            priority=priority,
            user_id=user_id,
            created_at=datetime.utcnow(),
        )
        await operation.insert()

        # Enqueue in Redis
        operation_data = {
            "operation_id": operation_id,
            "type": operation_type,
            "params": params,
            "user_id": user_id,
        }

        await self.queue.enqueue(
            database_id,
            operation_data,
            priority=priority
        )

        logger.info(
            "operation_enqueued",
            database_id=database_id,
            operation_id=operation_id,
            operation_type=operation_type,
            priority=priority,
            current_state=current_state.value,
            queue_position=await self.queue.get_queue_position(database_id, operation_id)
        )

        return operation_id

    async def get_operation_status(
        self,
        operation_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get current status of an operation.

        Args:
            operation_id: Operation ID

        Returns:
            Operation status dict or None if not found

        Example:
            >>> status = await operation_manager.get_operation_status("op-abc123")
            >>> print(status["status"])  # "queued", "processing", "completed", "failed"
        """
        # Load from database
        operation = await DatabaseOperation.get(operation_id)
        if not operation:
            return None

        result = {
            "operation_id": operation.id,
            "database_id": operation.database_id,
            "operation_type": operation.operation_type,
            "status": operation.status,
            "created_at": operation.created_at.isoformat() if operation.created_at else None,
            "started_at": operation.started_at.isoformat() if operation.started_at else None,
            "completed_at": operation.completed_at.isoformat() if operation.completed_at else None,
            "params": operation.params,
            "error_message": operation.error_message,
        }

        # Add queue position if queued
        if operation.status == OperationStatus.QUEUED:
            position = await self.queue.get_queue_position(
                operation.database_id,
                operation_id
            )
            result["queue_position"] = position

        # Add progress if available
        if operation.progress:
            result["progress"] = operation.progress

        return result

    async def list_operations(
        self,
        database_id: str,
        limit: int = 10,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        List operations for a database.

        Args:
            database_id: Database ID
            limit: Maximum number of operations to return
            status: Optional status filter

        Returns:
            List of operation dicts

        Example:
            >>> operations = await operation_manager.list_operations(
            ...     "db-123",
            ...     limit=5,
            ...     status="completed"
            ... )
        """
        query = {"database_id": database_id}
        if status:
            query["status"] = status

        operations = await DatabaseOperation.find(
            query
        ).sort("-created_at").limit(limit).to_list()

        return [
            {
                "operation_id": op.id,
                "operation_type": op.operation_type,
                "status": op.status,
                "created_at": op.created_at.isoformat() if op.created_at else None,
                "completed_at": op.completed_at.isoformat() if op.completed_at else None,
                "error_message": op.error_message,
            }
            for op in operations
        ]

    async def cancel_operation(
        self,
        operation_id: str,
        user_id: Optional[str] = None
    ) -> bool:
        """
        Cancel a queued operation.

        Args:
            operation_id: Operation ID
            user_id: Optional user ID for authorization

        Returns:
            True if cancelled, False if not found or already processing

        Example:
            >>> cancelled = await operation_manager.cancel_operation("op-abc123")
            >>> if cancelled:
            ...     print("Operation cancelled successfully")
        """
        # Load operation
        operation = await DatabaseOperation.get(operation_id)
        if not operation:
            logger.warning("operation_not_found_for_cancel", operation_id=operation_id)
            return False

        # Can only cancel queued operations
        if operation.status != OperationStatus.QUEUED:
            logger.warning(
                "operation_cannot_cancel_not_queued",
                operation_id=operation_id,
                status=operation.status
            )
            return False

        # Remove from queue
        cancelled = await self.queue.cancel_operation(
            operation.database_id,
            operation_id
        )

        if cancelled:
            # Update operation status
            operation.status = OperationStatus.CANCELLED
            operation.completed_at = datetime.utcnow()
            await operation.save()

            logger.info(
                "operation_cancelled",
                operation_id=operation_id,
                database_id=operation.database_id,
                user_id=user_id
            )
            return True

        return False

    async def get_queue_status(
        self,
        database_id: str
    ) -> Dict[str, Any]:
        """
        Get current queue status for a database.

        Args:
            database_id: Database ID

        Returns:
            Queue status dict with length, processing operation, etc.

        Example:
            >>> queue_status = await operation_manager.get_queue_status("db-123")
            >>> print(f"Queue length: {queue_status['length']}")
        """
        queue_length = await self.queue.get_queue_length(database_id)
        processing_op = await self.queue.get_processing_operation(database_id)
        is_locked = await self.lock_manager.is_locked(database_id)
        lock_info = await self.lock_manager.get_lock_info(database_id) if is_locked else None

        return {
            "database_id": database_id,
            "queue_length": queue_length,
            "is_locked": is_locked,
            "lock_info": lock_info,
            "processing_operation": processing_op,
        }

    async def update_operation_progress(
        self,
        operation_id: str,
        progress: int,
        message: Optional[str] = None
    ) -> None:
        """
        Update operation progress (used by workers).

        Args:
            operation_id: Operation ID
            progress: Progress percentage (0-100)
            message: Optional progress message

        Example:
            >>> await operation_manager.update_operation_progress(
            ...     "op-abc123",
            ...     progress=50,
            ...     message="Creating PVC"
            ... )
        """
        operation = await DatabaseOperation.get(operation_id)
        if operation:
            operation.progress = {
                "percent": progress,
                "message": message,
                "updated_at": datetime.utcnow().isoformat()
            }
            await operation.save()

            logger.debug(
                "operation_progress_updated",
                operation_id=operation_id,
                progress=progress,
                message=message
            )

    async def mark_operation_started(
        self,
        operation_id: str,
        worker_id: str
    ) -> None:
        """
        Mark operation as started (used by workers).

        Args:
            operation_id: Operation ID
            worker_id: Worker ID processing operation

        Example:
            >>> await operation_manager.mark_operation_started(
            ...     "op-abc123",
            ...     "worker-1"
            ... )
        """
        operation = await DatabaseOperation.get(operation_id)
        if operation:
            operation.status = OperationStatus.IN_PROGRESS
            operation.started_at = datetime.utcnow()
            await operation.save()

            logger.info(
                "operation_started",
                operation_id=operation_id,
                database_id=operation.database_id,
                worker_id=worker_id
            )

    async def mark_operation_completed(
        self,
        operation_id: str,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> None:
        """
        Mark operation as completed (used by workers).

        Args:
            operation_id: Operation ID
            success: Whether operation succeeded
            error_message: Optional error message if failed

        Example:
            >>> await operation_manager.mark_operation_completed(
            ...     "op-abc123",
            ...     success=True
            ... )
        """
        operation = await DatabaseOperation.get(operation_id)
        if operation:
            operation.status = OperationStatus.COMPLETED if success else OperationStatus.FAILED
            operation.completed_at = datetime.utcnow()
            if error_message:
                operation.error_message = error_message
            await operation.save()

            # Mark as completed in queue
            await self.queue.complete_operation(
                operation.database_id,
                operation_id,
                success=success,
                error_message=error_message
            )

            logger.info(
                "operation_completed",
                operation_id=operation_id,
                database_id=operation.database_id,
                success=success,
                error_message=error_message
            )

    async def cleanup_stuck_operations(
        self,
        timeout_minutes: int = 10
    ) -> int:
        """
        Cleanup operations stuck in processing state.

        Args:
            timeout_minutes: Consider operations stuck after this many minutes

        Returns:
            Number of operations cleaned up

        Example:
            >>> cleaned = await operation_manager.cleanup_stuck_operations(timeout_minutes=15)
            >>> print(f"Cleaned {cleaned} stuck operations")
        """
        from datetime import timedelta

        cutoff_time = datetime.utcnow() - timedelta(minutes=timeout_minutes)

        # Find stuck operations
        stuck_operations = await DatabaseOperation.find({
            "status": OperationStatus.IN_PROGRESS,
            "started_at": {"$lt": cutoff_time}
        }).to_list()

        cleaned = 0
        for operation in stuck_operations:
            # Force release lock
            await self.lock_manager.force_release_lock(operation.database_id)

            # Retry operation
            await self.queue.fail_operation(
                operation.database_id,
                operation.id,
                error_message=f"Operation stuck for {timeout_minutes} minutes",
                retries=operation.retry_count or 0,
                max_retries=3
            )

            operation.retry_count = (operation.retry_count or 0) + 1
            await operation.save()

            cleaned += 1

        if cleaned > 0:
            logger.warning(
                "stuck_operations_cleaned",
                count=cleaned,
                timeout_minutes=timeout_minutes
            )

        return cleaned

    async def close(self) -> None:
        """Close connections"""
        await self.queue.close()
        await self.lock_manager.close()


# Global singleton instance
operation_manager = OperationManager()
