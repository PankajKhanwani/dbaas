"""
MongoDB database connection and initialization using Motor and Beanie ODM.
"""
import asyncio
from typing import List

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.config.settings import settings
from app.config.logging import get_logger

logger = get_logger(__name__)


class Database:
    """MongoDB database connection manager."""

    client: AsyncIOMotorClient = None  # type: ignore
    database = None

    @classmethod
    async def connect_db(cls, document_models: List[type]) -> None:
        """
        Connect to MongoDB and initialize Beanie ODM with retry logic.

        Retries up to 10 times with exponential backoff (2s, 4s, 8s, 16s, 30s max).
        Can be interrupted with Ctrl+C at any time.

        Args:
            document_models: List of Beanie document models to initialize
        """
        max_attempts = 10
        base_delay = 2
        max_delay = 30

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "connecting_to_mongodb",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    url=settings.mongodb_url.split("@")[-1],  # Log without credentials
                    database=settings.mongodb_database,
                )

                # Create Motor client with production-grade connection pooling
                cls.client = AsyncIOMotorClient(
                    str(settings.mongodb_url),
                    maxPoolSize=settings.mongodb_max_pool_size,  # Max 20 connections
                    minPoolSize=settings.mongodb_min_pool_size,  # Min 2 connections
                    maxIdleTimeMS=settings.mongodb_max_idle_time_ms,  # Close idle after 30s
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=10000,  # 10s connection timeout
                    socketTimeoutMS=20000,   # 20s socket timeout
                    retryWrites=True,        # Retry failed writes
                )

                # Get database
                cls.database = cls.client[settings.mongodb_database]

                # Initialize Beanie with document models
                await init_beanie(
                    database=cls.database,
                    document_models=document_models,  # type: ignore
                )

                # Test connection
                await cls.client.admin.command("ping")

                logger.info(
                    "mongodb_connected",
                    database=settings.mongodb_database,
                    models_count=len(document_models),
                )
                return  # Success!

            except (KeyboardInterrupt, asyncio.CancelledError):
                # User interrupted - exit immediately
                logger.info("mongodb_connection_interrupted_by_user")
                raise
            except (ConnectionFailure, ServerSelectionTimeoutError) as e:
                logger.error(
                    "mongodb_connection_failed",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                    url=settings.mongodb_url.split("@")[-1],
                )

                if attempt >= max_attempts:
                    logger.error("mongodb_max_retries_exceeded")
                    raise

                # Calculate exponential backoff delay
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.info(f"retrying_mongodb_in_{delay}s")

                # Wait with shutdown check
                from app.utils.shutdown import shutdown_handler
                shutdown_requested = await shutdown_handler.wait_or_shutdown(delay)
                if shutdown_requested:
                    logger.info("mongodb_connection_cancelled_by_shutdown")
                    raise KeyboardInterrupt("Shutdown requested")

    @classmethod
    async def close_db(cls) -> None:
        """Close MongoDB connection."""
        if cls.client:
            logger.info("closing_mongodb_connection")
            cls.client.close()
            logger.info("mongodb_connection_closed")

    @classmethod
    async def get_database(cls):
        """Get database instance."""
        return cls.database

    @classmethod
    async def ping(cls) -> bool:
        """
        Check database connectivity.

        Returns:
            True if connected, False otherwise
        """
        try:
            await cls.client.admin.command("ping")
            return True
        except Exception as e:
            logger.error("database_ping_failed", error=str(e))
            return False
