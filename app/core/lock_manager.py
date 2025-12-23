"""
Distributed Lock Manager for DBaaS

This module implements distributed locking using Redis to prevent
concurrent operations on the same database.

Features:
- Per-database locks
- Automatic lock expiration (prevents deadlocks)
- Lock ownership tracking
- Heartbeat mechanism for long operations
- Lock stealing detection

Usage:
    >>> from app.core.lock_manager import LockManager
    >>> import asyncio
    >>>
    >>> lock_mgr = LockManager()
    >>>
    >>> # Acquire lock
    >>> acquired = await lock_mgr.acquire_lock("db-123", "worker-1", "op-abc")
    >>> if acquired:
    ...     try:
    ...         # Perform operation
    ...         await do_something()
    ...     finally:
    ...         # Always release lock
    ...         await lock_mgr.release_lock("db-123", "worker-1")
"""

import json
import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import structlog
import redis.asyncio as aioredis
from app.config.settings import settings

logger = structlog.get_logger(__name__)


class LockManager:
    """
    Distributed lock manager using Redis.

    Prevents concurrent operations on the same database using
    distributed locks with automatic expiration.
    """

    DEFAULT_LOCK_TIMEOUT = 300  # 5 minutes
    HEARTBEAT_INTERVAL = 60  # 1 minute

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        """
        Initialize lock manager.

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
            logger.info("lock_manager_redis_connected", redis_url=str(settings.redis_url))

    def _get_lock_key(self, database_id: str) -> str:
        """Get Redis key for database lock"""
        return f"database_lock:{database_id}"

    async def acquire_lock(
        self,
        database_id: str,
        worker_id: str,
        operation_id: str,
        timeout: int = DEFAULT_LOCK_TIMEOUT
    ) -> bool:
        """
        Acquire exclusive lock on a database.

        The lock automatically expires after timeout seconds to prevent
        deadlocks in case worker crashes.

        Args:
            database_id: Database ID to lock
            worker_id: ID of worker acquiring lock
            operation_id: ID of operation requiring lock
            timeout: Lock timeout in seconds (default 300 = 5 minutes)

        Returns:
            True if lock acquired, False if database is already locked

        Example:
            >>> acquired = await lock_mgr.acquire_lock(
            ...     "db-123",
            ...     "worker-1",
            ...     "op-abc",
            ...     timeout=600
            ... )
            >>> if acquired:
            ...     print("Lock acquired!")
        """
        await self._ensure_connection()

        lock_key = self._get_lock_key(database_id)

        lock_data = {
            "worker_id": worker_id,
            "operation_id": operation_id,
            "acquired_at": datetime.utcnow().isoformat(),
            "timeout": timeout
        }

        # Try to set lock with NX (only if not exists)
        acquired = await self.redis.set(
            lock_key,
            json.dumps(lock_data),
            ex=timeout,
            nx=True  # Only set if key doesn't exist
        )

        if acquired:
            logger.info(
                "lock_acquired",
                database_id=database_id,
                worker_id=worker_id,
                operation_id=operation_id,
                timeout=timeout
            )
            return True
        else:
            # Lock already exists
            existing_lock = await self.get_lock_info(database_id)
            logger.warning(
                "lock_acquisition_failed",
                database_id=database_id,
                worker_id=worker_id,
                operation_id=operation_id,
                existing_lock=existing_lock
            )
            return False

    async def release_lock(
        self,
        database_id: str,
        worker_id: str
    ) -> bool:
        """
        Release lock on a database.

        Only the worker that acquired the lock can release it.

        Args:
            database_id: Database ID to unlock
            worker_id: ID of worker releasing lock

        Returns:
            True if lock released, False if lock not held by this worker

        Example:
            >>> released = await lock_mgr.release_lock("db-123", "worker-1")
            >>> if released:
            ...     print("Lock released!")
        """
        await self._ensure_connection()

        lock_key = self._get_lock_key(database_id)

        # Check current lock owner
        lock_json = await self.redis.get(lock_key)
        if not lock_json:
            # No lock exists
            logger.warning(
                "lock_release_no_lock",
                database_id=database_id,
                worker_id=worker_id
            )
            return False

        lock_data = json.loads(lock_json)

        # Verify ownership
        if lock_data.get("worker_id") != worker_id:
            logger.error(
                "lock_release_wrong_owner",
                database_id=database_id,
                worker_id=worker_id,
                actual_owner=lock_data.get("worker_id")
            )
            return False

        # Delete lock
        await self.redis.delete(lock_key)

        logger.info(
            "lock_released",
            database_id=database_id,
            worker_id=worker_id,
            operation_id=lock_data.get("operation_id")
        )
        return True

    async def extend_lock(
        self,
        database_id: str,
        worker_id: str,
        additional_timeout: int = 60
    ) -> bool:
        """
        Extend lock timeout (heartbeat).

        Used for long-running operations to prevent lock expiration.

        Args:
            database_id: Database ID
            worker_id: ID of worker extending lock
            additional_timeout: Additional seconds to add to timeout

        Returns:
            True if extended, False if lock not held by this worker

        Example:
            >>> # In long operation, periodically extend lock
            >>> extended = await lock_mgr.extend_lock("db-123", "worker-1", 60)
        """
        await self._ensure_connection()

        lock_key = self._get_lock_key(database_id)

        # Check current lock owner
        lock_json = await self.redis.get(lock_key)
        if not lock_json:
            logger.warning(
                "lock_extend_no_lock",
                database_id=database_id,
                worker_id=worker_id
            )
            return False

        lock_data = json.loads(lock_json)

        # Verify ownership
        if lock_data.get("worker_id") != worker_id:
            logger.error(
                "lock_extend_wrong_owner",
                database_id=database_id,
                worker_id=worker_id,
                actual_owner=lock_data.get("worker_id")
            )
            return False

        # Extend TTL
        await self.redis.expire(lock_key, additional_timeout)

        logger.debug(
            "lock_extended",
            database_id=database_id,
            worker_id=worker_id,
            additional_timeout=additional_timeout
        )
        return True

    async def get_lock_info(self, database_id: str) -> Optional[Dict[str, Any]]:
        """
        Get information about current lock on database.

        Args:
            database_id: Database ID

        Returns:
            Lock information dict or None if not locked

        Example:
            >>> lock_info = await lock_mgr.get_lock_info("db-123")
            >>> if lock_info:
            ...     print(f"Locked by {lock_info['worker_id']}")
        """
        await self._ensure_connection()

        lock_key = self._get_lock_key(database_id)
        lock_json = await self.redis.get(lock_key)

        if not lock_json:
            return None

        lock_data = json.loads(lock_json)

        # Add TTL info
        ttl = await self.redis.ttl(lock_key)
        lock_data["ttl_seconds"] = ttl

        return lock_data

    async def is_locked(self, database_id: str) -> bool:
        """
        Check if database is currently locked.

        Args:
            database_id: Database ID

        Returns:
            True if locked, False otherwise

        Example:
            >>> if await lock_mgr.is_locked("db-123"):
            ...     print("Database is locked")
        """
        await self._ensure_connection()

        lock_key = self._get_lock_key(database_id)
        return await self.redis.exists(lock_key) > 0

    async def force_release_lock(self, database_id: str) -> bool:
        """
        Force release lock on a database (admin operation).

        WARNING: Use only for stuck operations or emergency cleanup.
        This can cause race conditions if used incorrectly.

        Args:
            database_id: Database ID

        Returns:
            True if lock was released, False if no lock existed

        Example:
            >>> # Emergency: force release stuck lock
            >>> await lock_mgr.force_release_lock("db-123")
        """
        await self._ensure_connection()

        lock_key = self._get_lock_key(database_id)

        lock_info = await self.get_lock_info(database_id)
        if not lock_info:
            return False

        await self.redis.delete(lock_key)

        logger.warning(
            "lock_force_released",
            database_id=database_id,
            previous_owner=lock_info.get("worker_id"),
            operation_id=lock_info.get("operation_id")
        )
        return True

    async def cleanup_expired_locks(self) -> int:
        """
        Cleanup expired locks.

        Redis automatically expires locks, but this method can be used
        to manually check and report on expired locks.

        Returns:
            Number of locks cleaned up
        """
        await self._ensure_connection()

        # Redis automatically handles expiration via TTL
        # This is just a reporting/monitoring function
        logger.info("cleanup_expired_locks_completed", cleaned=0)
        return 0

    async def wait_for_lock(
        self,
        database_id: str,
        worker_id: str,
        operation_id: str,
        timeout: int = DEFAULT_LOCK_TIMEOUT,
        max_wait_seconds: int = 300
    ) -> bool:
        """
        Wait for lock to become available and acquire it.

        Args:
            database_id: Database ID
            worker_id: Worker ID
            operation_id: Operation ID
            timeout: Lock timeout once acquired
            max_wait_seconds: Maximum time to wait (default 5 minutes)

        Returns:
            True if lock acquired, False if timeout waiting

        Example:
            >>> # Wait up to 5 minutes for lock
            >>> acquired = await lock_mgr.wait_for_lock(
            ...     "db-123", "worker-1", "op-abc",
            ...     max_wait_seconds=300
            ... )
        """
        import asyncio

        await self._ensure_connection()

        start_time = datetime.utcnow()
        attempt = 0

        while True:
            # Try to acquire lock
            acquired = await self.acquire_lock(
                database_id,
                worker_id,
                operation_id,
                timeout
            )

            if acquired:
                logger.info(
                    "lock_acquired_after_wait",
                    database_id=database_id,
                    worker_id=worker_id,
                    operation_id=operation_id,
                    attempts=attempt,
                    wait_seconds=(datetime.utcnow() - start_time).total_seconds()
                )
                return True

            # Check if max wait time exceeded
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed >= max_wait_seconds:
                logger.error(
                    "lock_wait_timeout",
                    database_id=database_id,
                    worker_id=worker_id,
                    operation_id=operation_id,
                    attempts=attempt,
                    max_wait_seconds=max_wait_seconds
                )
                return False

            # Wait before retry (exponential backoff, max 30 seconds)
            wait_time = min(2 ** min(attempt, 5), 30)
            await asyncio.sleep(wait_time)
            attempt += 1

    async def close(self) -> None:
        """Close Redis connection"""
        if self.redis and self._initialized:
            await self.redis.close()
            logger.info("lock_manager_redis_disconnected")
