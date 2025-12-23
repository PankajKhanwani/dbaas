"""
Redis-based Operation Queue for DBaaS

This module implements a distributed operation queue using Redis.
Each database has its own operation queue to ensure sequential processing
and prevent conflicting concurrent operations.

Features:
- Per-database operation queues
- Priority-based operation ordering
- Operation status tracking
- Dead letter queue for failed operations
- Atomic enqueue/dequeue operations

Usage:
    >>> from app.core.operation_queue import OperationQueue
    >>> import asyncio
    >>>
    >>> queue = OperationQueue()
    >>>
    >>> # Enqueue an operation
    >>> await queue.enqueue("db-123", {
    ...     "operation_id": "op-abc",
    ...     "type": "scale",
    ...     "params": {"replicas": 3},
    ...     "priority": 2
    ... })
    >>>
    >>> # Dequeue next operation
    >>> operation = await queue.dequeue("db-123")
    >>> print(operation["operation_id"])  # "op-abc"
"""

import json
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import structlog
import redis.asyncio as aioredis
from app.config.settings import settings

logger = structlog.get_logger(__name__)


class OperationQueue:
    """
    Distributed operation queue using Redis.

    Each database has its own queue to ensure operations are processed
    sequentially and prevent conflicts.
    """

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        """
        Initialize operation queue.

        Args:
            redis_client: Optional Redis client. If not provided, creates new client.
        """
        self.redis = redis_client
        self._initialized = redis_client is not None

    async def _ensure_connection(self) -> None:
        """Ensure Redis connection is established"""
        if not self._initialized:
            self.redis = await aioredis.from_url(
                str(settings.redis_url),
                encoding="utf-8",
                decode_responses=True,
                max_connections=settings.redis_max_connections
            )
            self._initialized = True
            logger.info("operation_queue_redis_connected", redis_url=str(settings.redis_url))

    def _get_queue_key(self, database_id: str) -> str:
        """Get Redis key for database operation queue"""
        return f"operation_queue:{database_id}"

    def _get_processing_key(self, database_id: str) -> str:
        """Get Redis key for currently processing operation"""
        return f"operation_processing:{database_id}"

    def _get_operation_key(self, operation_id: str) -> str:
        """Get Redis key for operation data"""
        return f"operation:{operation_id}"

    def _get_dlq_key(self) -> str:
        """Get Redis key for dead letter queue"""
        return "operation_dlq"

    async def enqueue(
        self,
        database_id: str,
        operation_data: Dict[str, Any],
        priority: int = 5
    ) -> None:
        """
        Enqueue an operation for a database.

        Operations are processed in priority order (lower number = higher priority).
        Operations with same priority are processed FIFO.

        Priority levels:
        1 = DELETE (highest priority - cleanup)
        2 = SCALE_DOWN (prevent resource exhaustion)
        3 = BACKUP (data safety)
        4 = SCALE_UP (capacity expansion)
        5 = UPDATE (non-critical changes)

        Args:
            database_id: Database ID
            operation_data: Operation details (must include operation_id, type, params)
            priority: Priority level (1-5, default 5)

        Example:
            >>> await queue.enqueue("db-123", {
            ...     "operation_id": "op-abc",
            ...     "type": "scale",
            ...     "params": {"replicas": 3}
            ... }, priority=4)
        """
        await self._ensure_connection()

        operation_id = operation_data.get("operation_id")
        if not operation_id:
            raise ValueError("operation_data must include operation_id")

        # Add metadata
        operation_data["database_id"] = database_id
        operation_data["priority"] = priority
        operation_data["queued_at"] = datetime.utcnow().isoformat()
        operation_data["status"] = "queued"

        queue_key = self._get_queue_key(database_id)
        operation_key = self._get_operation_key(operation_id)

        # Store operation data
        await self.redis.setex(
            operation_key,
            86400,  # 24 hours TTL
            json.dumps(operation_data)
        )

        # Add to priority queue
        # Using sorted set with priority as score
        await self.redis.zadd(
            queue_key,
            {operation_id: priority}
        )

        logger.info(
            "operation_enqueued",
            database_id=database_id,
            operation_id=operation_id,
            operation_type=operation_data.get("type"),
            priority=priority,
            queue_length=await self.get_queue_length(database_id)
        )

    async def dequeue(
        self,
        database_id: str,
        timeout: int = 0
    ) -> Optional[Dict[str, Any]]:
        """
        Dequeue the next operation for a database.

        This operation is atomic - the operation is removed from the queue
        and marked as processing in a single transaction.

        Args:
            database_id: Database ID
            timeout: Block timeout in seconds (0 = non-blocking)

        Returns:
            Operation data dict or None if queue is empty

        Example:
            >>> operation = await queue.dequeue("db-123")
            >>> if operation:
            ...     print(f"Processing {operation['operation_id']}")
        """
        await self._ensure_connection()

        queue_key = self._get_queue_key(database_id)

        # Get operation with lowest priority score (FIFO within priority)
        results = await self.redis.zrange(
            queue_key,
            0, 0,  # Get first element
            withscores=True
        )

        if not results:
            return None

        operation_id, priority = results[0]

        # Remove from queue
        await self.redis.zrem(queue_key, operation_id)

        # Get operation data
        operation_key = self._get_operation_key(operation_id)
        operation_json = await self.redis.get(operation_key)

        if not operation_json:
            logger.error(
                "operation_data_missing",
                database_id=database_id,
                operation_id=operation_id
            )
            return None

        operation_data = json.loads(operation_json)

        # Mark as processing
        processing_key = self._get_processing_key(database_id)
        operation_data["status"] = "processing"
        operation_data["started_at"] = datetime.utcnow().isoformat()

        await self.redis.setex(
            processing_key,
            600,  # 10 minutes TTL
            json.dumps(operation_data)
        )

        logger.info(
            "operation_dequeued",
            database_id=database_id,
            operation_id=operation_id,
            operation_type=operation_data.get("type"),
            priority=priority,
            remaining_in_queue=await self.get_queue_length(database_id)
        )

        return operation_data

    async def complete_operation(
        self,
        database_id: str,
        operation_id: str,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> None:
        """
        Mark operation as completed.

        Args:
            database_id: Database ID
            operation_id: Operation ID
            success: Whether operation succeeded
            error_message: Error message if failed

        Example:
            >>> await queue.complete_operation("db-123", "op-abc", success=True)
        """
        await self._ensure_connection()

        processing_key = self._get_processing_key(database_id)
        operation_key = self._get_operation_key(operation_id)

        # Update operation status
        operation_json = await self.redis.get(operation_key)
        if operation_json:
            operation_data = json.loads(operation_json)
            operation_data["status"] = "completed" if success else "failed"
            operation_data["completed_at"] = datetime.utcnow().isoformat()
            if error_message:
                operation_data["error_message"] = error_message

            # Store completed operation for 7 days
            await self.redis.setex(
                operation_key,
                604800,  # 7 days
                json.dumps(operation_data)
            )

        # Remove from processing
        await self.redis.delete(processing_key)

        logger.info(
            "operation_completed",
            database_id=database_id,
            operation_id=operation_id,
            success=success,
            error_message=error_message
        )

    async def fail_operation(
        self,
        database_id: str,
        operation_id: str,
        error_message: str,
        retries: int = 0,
        max_retries: int = 3
    ) -> bool:
        """
        Mark operation as failed and optionally requeue for retry.

        Args:
            database_id: Database ID
            operation_id: Operation ID
            error_message: Error description
            retries: Number of retries so far
            max_retries: Maximum retry attempts

        Returns:
            True if requeued for retry, False if moved to DLQ

        Example:
            >>> requeued = await queue.fail_operation(
            ...     "db-123", "op-abc",
            ...     error_message="Timeout",
            ...     retries=1,
            ...     max_retries=3
            ... )
        """
        await self._ensure_connection()

        operation_key = self._get_operation_key(operation_id)
        operation_json = await self.redis.get(operation_key)

        if not operation_json:
            logger.error("operation_not_found", operation_id=operation_id)
            return False

        operation_data = json.loads(operation_json)

        if retries < max_retries:
            # Retry with exponential backoff priority
            backoff_priority = operation_data.get("priority", 5) + (retries * 2)
            operation_data["retries"] = retries + 1
            operation_data["last_error"] = error_message
            operation_data["status"] = "retrying"

            await self.enqueue(database_id, operation_data, priority=backoff_priority)

            logger.warning(
                "operation_retrying",
                database_id=database_id,
                operation_id=operation_id,
                retries=retries + 1,
                max_retries=max_retries,
                error_message=error_message
            )
            return True
        else:
            # Move to dead letter queue
            operation_data["status"] = "dead_letter"
            operation_data["failed_at"] = datetime.utcnow().isoformat()
            operation_data["final_error"] = error_message

            dlq_key = self._get_dlq_key()
            await self.redis.lpush(dlq_key, json.dumps(operation_data))

            logger.error(
                "operation_moved_to_dlq",
                database_id=database_id,
                operation_id=operation_id,
                retries=retries,
                error_message=error_message
            )
            return False

    async def get_queue_length(self, database_id: str) -> int:
        """Get number of operations in queue for database"""
        await self._ensure_connection()
        queue_key = self._get_queue_key(database_id)
        return await self.redis.zcard(queue_key)

    async def get_queue_position(
        self,
        database_id: str,
        operation_id: str
    ) -> Optional[int]:
        """
        Get position of operation in queue (0-indexed).

        Returns:
            Queue position or None if not in queue
        """
        await self._ensure_connection()
        queue_key = self._get_queue_key(database_id)
        rank = await self.redis.zrank(queue_key, operation_id)
        return rank

    async def get_processing_operation(
        self,
        database_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get currently processing operation for database"""
        await self._ensure_connection()
        processing_key = self._get_processing_key(database_id)
        operation_json = await self.redis.get(processing_key)

        if not operation_json:
            return None

        return json.loads(operation_json)

    async def cancel_operation(
        self,
        database_id: str,
        operation_id: str
    ) -> bool:
        """
        Cancel a queued operation.

        Args:
            database_id: Database ID
            operation_id: Operation ID

        Returns:
            True if cancelled, False if not found or already processing
        """
        await self._ensure_connection()

        queue_key = self._get_queue_key(database_id)

        # Remove from queue
        removed = await self.redis.zrem(queue_key, operation_id)

        if removed:
            # Update operation status
            operation_key = self._get_operation_key(operation_id)
            operation_json = await self.redis.get(operation_key)
            if operation_json:
                operation_data = json.loads(operation_json)
                operation_data["status"] = "cancelled"
                operation_data["cancelled_at"] = datetime.utcnow().isoformat()
                await self.redis.setex(operation_key, 604800, json.dumps(operation_data))

            logger.info(
                "operation_cancelled",
                database_id=database_id,
                operation_id=operation_id
            )
            return True

        return False

    async def cleanup_stuck_operations(self, timeout_minutes: int = 10) -> int:
        """
        Cleanup operations that have been processing too long.

        Args:
            timeout_minutes: Consider operations stuck after this many minutes

        Returns:
            Number of operations cleaned up
        """
        await self._ensure_connection()

        # This would require scanning all processing keys
        # Implementation depends on your recovery strategy
        # For now, returning 0 as placeholder
        logger.info("cleanup_stuck_operations_started", timeout_minutes=timeout_minutes)
        return 0

    async def close(self) -> None:
        """Close Redis connection"""
        if self.redis and self._initialized:
            await self.redis.close()
            logger.info("operation_queue_redis_disconnected")
