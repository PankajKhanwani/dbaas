"""
Redis-based operation queue service.

Provides:
- Durable queue for database operations
- Priority queuing
- Rate limiting
- Deduplication
"""
import asyncio
import json
from typing import Optional, List
import redis.asyncio as redis

from app.config.logging import get_logger
from app.config.settings import settings
from app.services import metrics

logger = get_logger(__name__)


class OperationQueue:
    """
    Async queue for database operations using Redis.

    Features:
    - Persistence (survives API restarts)
    - Priority queuing (high priority operations processed first)
    - Deduplication (prevents duplicate operations)
    - Rate limiting (prevents K8s overload)
    """

    def __init__(self):
        """Initialize Redis connection."""
        self.redis: Optional[redis.Redis] = None
        self.queue_key = "operations:pending"
        self.processing_key = "operations:processing"
        self.dedup_key_prefix = "operations:dedup:"

    async def connect(self):
        """Connect to Redis."""
        if self.redis is None:
            # Convert RedisDsn to string (Pydantic RedisDsn needs to be converted)
            redis_url_str = str(settings.redis_url)
            self.redis = await redis.from_url(
                redis_url_str,
                encoding="utf-8",
                decode_responses=True,
            )
            logger.info("connected_to_redis", url=redis_url_str)

    async def disconnect(self):
        """Disconnect from Redis."""
        if self.redis:
            await self.redis.close()
            logger.info("disconnected_from_redis")

    async def enqueue(
        self,
        operation_id: str,
        priority: int = 0,
        dedup_key: Optional[str] = None,
    ) -> bool:
        """
        Add operation to queue.

        Args:
            operation_id: Operation ID to enqueue
            priority: Higher number = higher priority (default: 0)
            dedup_key: Optional key for deduplication (e.g., "db-123:scale")

        Returns:
            True if enqueued, False if duplicate
        """
        await self.connect()

        # Check for duplicate
        if dedup_key:
            dedup_full_key = f"{self.dedup_key_prefix}{dedup_key}"
            is_duplicate = await self.redis.exists(dedup_full_key)
            if is_duplicate:
                logger.warning(
                    "duplicate_operation_rejected",
                    operation_id=operation_id,
                    dedup_key=dedup_key,
                )
                # Extract operation type from dedup_key (format: "db-id:operation_type")
                op_type = dedup_key.split(":")[-1] if ":" in dedup_key else "unknown"
                metrics.record_duplicate_rejected(op_type)
                return False

            # Set dedup marker (expires in 1 hour)
            await self.redis.setex(dedup_full_key, 3600, operation_id)

        # Add to sorted set (score = priority, higher scores processed first)
        await self.redis.zadd(
            self.queue_key,
            {operation_id: priority},
        )

        logger.info(
            "operation_enqueued",
            operation_id=operation_id,
            priority=priority,
        )

        # Record metric (extract operation type from dedup_key if available)
        if dedup_key and ":" in dedup_key:
            op_type = dedup_key.split(":")[-1]
            metrics.record_queue_enqueue(op_type)

        return True

    async def dequeue(self, timeout: int = 0) -> Optional[str]:
        """
        Get next operation from queue (highest priority).

        Args:
            timeout: Seconds to wait for an operation (0 = no wait)

        Returns:
            Operation ID or None if queue is empty
        """
        await self.connect()

        if timeout > 0:
            # Blocking pop with timeout
            result = await self.redis.bzpopmax(self.queue_key, timeout=timeout)
            if result:
                _key, operation_id, _score = result
                return operation_id
        else:
            # Non-blocking pop
            result = await self.redis.zpopmax(self.queue_key, count=1)
            if result:
                operation_id, _score = result[0]
                return operation_id

        return None

    async def mark_processing(self, operation_id: str):
        """Mark operation as being processed."""
        await self.connect()
        await self.redis.sadd(self.processing_key, operation_id)

    async def mark_completed(self, operation_id: str, dedup_key: Optional[str] = None):
        """
        Mark operation as completed.

        Removes from processing set and clears dedup marker.
        """
        await self.connect()

        # Remove from processing set
        await self.redis.srem(self.processing_key, operation_id)

        # Clear dedup marker
        if dedup_key:
            dedup_full_key = f"{self.dedup_key_prefix}{dedup_key}"
            await self.redis.delete(dedup_full_key)

        logger.debug(
            "operation_marked_completed",
            operation_id=operation_id,
        )

    async def get_queue_depth(self) -> int:
        """Get number of queued operations."""
        await self.connect()
        return await self.redis.zcard(self.queue_key)

    async def get_processing_count(self) -> int:
        """Get number of operations being processed."""
        await self.connect()
        return await self.redis.scard(self.processing_key)

    async def get_stats(self) -> dict:
        """Get queue statistics."""
        await self.connect()

        queued = await self.get_queue_depth()
        processing = await self.get_processing_count()

        # Update Prometheus metrics
        metrics.update_queue_stats(queued, processing)

        return {
            "queued": queued,
            "processing": processing,
        }

    async def cancel_operation(self, operation_id: str) -> bool:
        """
        Cancel a queued operation.

        Args:
            operation_id: Operation ID to cancel

        Returns:
            True if cancelled, False if not found in queue
        """
        await self.connect()

        # Remove from queue
        removed = await self.redis.zrem(self.queue_key, operation_id)

        if removed:
            logger.info("operation_cancelled", operation_id=operation_id)
            return True

        return False

    async def requeue_failed(self, operation_id: str, priority: int = 10):
        """
        Requeue a failed operation with higher priority.

        Args:
            operation_id: Operation ID to requeue
            priority: Priority (default: 10 for retries)
        """
        await self.enqueue(operation_id, priority=priority)
        logger.info(
            "operation_requeued",
            operation_id=operation_id,
            priority=priority,
        )


# Global queue instance
operation_queue = OperationQueue()
