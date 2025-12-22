"""
Redis connection and initialization with retry logic.
"""
import asyncio
from typing import Optional

import redis.asyncio as redis

from app.config.settings import settings
from app.config.logging import get_logger

logger = get_logger(__name__)


class RedisConnection:
    """Redis connection manager with retry logic."""

    client: Optional[redis.Redis] = None

    @classmethod
    async def connect(cls) -> None:
        """
        Connect to Redis with retry logic.

        Retries up to 10 times with exponential backoff (2s, 4s, 8s, 16s, 30s max).
        Can be interrupted with Ctrl+C at any time.
        """
        max_attempts = 10
        base_delay = 2
        max_delay = 30

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "connecting_to_redis",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    url=str(settings.redis_url).split("@")[-1],  # Log without credentials
                )

                # Create Redis client with production-grade connection pooling
                cls.client = redis.Redis.from_url(
                    str(settings.redis_url),
                    max_connections=settings.redis_max_connections,  # Max 20 connections
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                    socket_keepalive=True,              # Keep connections alive
                    retry_on_timeout=True,              # Retry on timeout
                    health_check_interval=30,           # Check connection health every 30s
                )

                # Test connection
                await cls.client.ping()

                logger.info("redis_connected")
                return  # Success!

            except (KeyboardInterrupt, asyncio.CancelledError):
                # User interrupted - exit immediately
                logger.info("redis_connection_interrupted_by_user")
                raise
            except (redis.ConnectionError, redis.TimeoutError) as e:
                logger.error(
                    "redis_connection_failed",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                    url=str(settings.redis_url).split("@")[-1],
                )

                if attempt >= max_attempts:
                    logger.error("redis_max_retries_exceeded")
                    raise

                # Calculate exponential backoff delay
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.info(f"retrying_redis_in_{delay}s")

                # Use asyncio.sleep which respects cancellation/interrupts
                await asyncio.sleep(delay)
            except Exception as e:
                logger.info("redis_connection_interrupted")
                raise

    @classmethod
    async def close(cls) -> None:
        """Close Redis connection."""
        if cls.client:
            logger.info("closing_redis_connection")
            await cls.client.close()
            logger.info("redis_connection_closed")

    @classmethod
    async def get_client(cls) -> redis.Redis:
        """
        Get Redis client instance.

        Returns:
            Redis client instance

        Raises:
            RuntimeError: If Redis is not connected
        """
        if cls.client is None:
            raise RuntimeError("Redis is not connected. Call connect() first.")
        return cls.client

    @classmethod
    async def ping(cls) -> bool:
        """
        Check Redis connectivity.

        Returns:
            True if connected, False otherwise
        """
        try:
            if cls.client:
                await cls.client.ping()
                return True
            return False
        except Exception as e:
            logger.error("redis_ping_failed", error=str(e))
            return False
