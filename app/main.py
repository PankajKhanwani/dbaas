"""
Main FastAPI application entry point.
Production-grade DBaaS platform powered by KubeDB.
"""
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.config.database import Database
from app.config.redis import RedisConnection
from app.config.logging import configure_logging, get_logger
from app.config.settings import settings
from app.exceptions import DBaaSException
from app.api.v1 import health, databases, auth, versions, providers, operations

# Configure logging
configure_logging()
logger = get_logger(__name__)

# Initialize Sentry for error tracking (production)
if settings.sentry_dsn and settings.is_production:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        environment=settings.environment,
        release=settings.app_version,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """
    Application lifespan manager.
    Handles startup and shutdown events.

    Starts ALL components in ONE process:
    1. API server (FastAPI)
    2. Reconciliation worker (with leader election)
    3. Operation workers (3 concurrent)
    4. Status sync worker
    """
    # Startup
    logger.info(
        "application_starting",
        version=settings.app_version,
        environment=settings.environment,
    )

    background_tasks = []

    try:
        # Initialize database connection
        from app.repositories.models import Database as DatabaseModel
        from app.repositories.models import AuditLog
        from app.repositories.models import Provider
        from app.models.operation import Operation

        document_models = [DatabaseModel, AuditLog, Provider, Operation]

        # Connect to MongoDB with retry logic
        logger.info("initializing_mongodb_connection")
        await Database.connect_db(document_models)
        logger.info("mongodb_initialized_successfully")

        # Connect to Redis with retry logic
        logger.info("initializing_redis_connection")
        await RedisConnection.connect()
        logger.info("redis_initialized_successfully")

        # Start ALL background workers in this process
        logger.info("starting_background_workers")
        import asyncio
        import socket
        import uuid
        from app.workers.leader_election import LeaderElection
        from app.workers.reconciliation_worker import ReconciliationWorker
        from app.workers.operation_worker import OperationWorker
        from app.services.status_sync_service import database_reconciler

        # Generate unique instance ID
        hostname = socket.gethostname()
        instance_id = f"{hostname}-{uuid.uuid4().hex[:8]}"

        # 1. Reconciliation Worker with Leader Election
        leader_election = LeaderElection(instance_id=instance_id, lease_duration=30)
        reconciler = ReconciliationWorker(reconcile_interval=settings.reconcile_interval)

        async def run_reconciler_with_leader_election():
            """Run reconciler only if leader."""
            while True:
                try:
                    is_leader = await leader_election.acquire_leadership()

                    if is_leader:
                        if not reconciler.running:
                            logger.info("became_leader_starting_reconciler", instance_id=instance_id)
                            asyncio.create_task(reconciler.start())
                        await asyncio.sleep(10)
                        await leader_election.renew_lease()
                    else:
                        if reconciler.running:
                            logger.info("lost_leadership_stopping_reconciler", instance_id=instance_id)
                            await reconciler.stop()
                        await asyncio.sleep(5)
                except asyncio.CancelledError:
                    if reconciler.running:
                        await reconciler.stop()
                    await leader_election.release_leadership()
                    break
                except Exception as e:
                    logger.error("leader_election_error", error=str(e))
                    await asyncio.sleep(10)

        background_tasks.append(asyncio.create_task(run_reconciler_with_leader_election()))

        # 2. Operation Workers (3 concurrent)
        num_workers = getattr(settings, 'num_operation_workers', 3)
        for worker_id in range(1, num_workers + 1):
            worker = OperationWorker(worker_id=worker_id)
            background_tasks.append(asyncio.create_task(worker.start()))

        logger.info("operation_workers_started", count=num_workers)

        # 3. Status Sync (runs in all pods)
        async def run_status_sync():
            """Run status sync periodically."""
            sync_interval = getattr(settings, 'status_sync_interval', 30)
            while True:
                try:
                    await database_reconciler.reconcile_all_databases()
                    await asyncio.sleep(sync_interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("status_sync_error", error=str(e))
                    await asyncio.sleep(60)

        background_tasks.append(asyncio.create_task(run_status_sync()))

        logger.info(
            "application_started",
            version=settings.app_version,
            instance_id=instance_id,
            background_tasks=len(background_tasks)
        )

    except KeyboardInterrupt:
        logger.info("application_startup_interrupted")
        raise
    except Exception as e:
        logger.error("application_startup_failed", error=str(e))
        raise

    yield

    # Shutdown
    logger.info("application_shutting_down")

    # Cancel all background tasks
    if background_tasks:
        logger.info("stopping_background_tasks", count=len(background_tasks))
        for task in background_tasks:
            task.cancel()

        # Wait for tasks to finish (with timeout)
        try:
            import asyncio
            await asyncio.wait_for(
                asyncio.gather(*background_tasks, return_exceptions=True),
                timeout=30.0
            )
            logger.info("background_tasks_stopped")
        except asyncio.TimeoutError:
            logger.warning("background_tasks_shutdown_timeout")

    # Close connections gracefully
    try:
        await Database.close_db()
        logger.info("mongodb_connection_closed")
    except Exception as e:
        logger.error("mongodb_close_error", error=str(e))

    try:
        await RedisConnection.close()
        logger.info("redis_connection_closed")
    except Exception as e:
        logger.error("redis_close_error", error=str(e))

    logger.info("application_shutdown_complete")


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Production-grade Database as a Service platform powered by KubeDB",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    openapi_url="/openapi.json" if not settings.is_production else None,
    lifespan=lifespan,
)

# Add CORS middleware for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)


# Exception handlers
@app.exception_handler(DBaaSException)
async def dbaas_exception_handler(request: Request, exc: DBaaSException) -> JSONResponse:
    """Handle custom DBaaS exceptions."""
    logger.error(
        "dbaas_exception",
        path=request.url.path,
        method=request.method,
        error=exc.message,
        status_code=exc.status_code,
        details=exc.details,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "details": exc.details,
                "status_code": exc.status_code,
            }
        },
    )


def _sanitize_errors(errors):
    """Sanitize Pydantic validation errors to be JSON serializable."""
    sanitized = []
    for error in errors:
        sanitized_error = {}
        for key, value in error.items():
            if key == 'ctx' and isinstance(value, dict):
                # Convert any non-serializable objects in ctx to strings
                sanitized_error[key] = {k: str(v) for k, v in value.items()}
            elif isinstance(value, (str, int, float, bool, type(None))):
                sanitized_error[key] = value
            elif isinstance(value, (list, tuple)):
                sanitized_error[key] = list(value)
            else:
                sanitized_error[key] = str(value)
        sanitized.append(sanitized_error)
    return sanitized


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors."""
    errors = _sanitize_errors(exc.errors())

    logger.warning(
        "validation_error",
        path=request.url.path,
        method=request.method,
        errors=errors,
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "message": "Validation error",
                "details": errors,
                "status_code": 422,
            }
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle all other exceptions."""
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=True,
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "message": "Internal server error",
                "details": {} if settings.is_production else {"error": str(exc)},
                "status_code": 500,
            }
        },
    )


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all HTTP requests."""
    logger.info(
        "request_started",
        method=request.method,
        path=request.url.path,
        client=request.client.host if request.client else None,
    )

    response = await call_next(request)

    logger.info(
        "request_completed",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
    )

    return response


# Initialize Prometheus metrics
if settings.prometheus_enabled:
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# Include routers
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(versions.router, prefix="/api/v1/versions", tags=["Versions"])
app.include_router(providers.router, prefix="/api/v1/providers", tags=["Providers"])
app.include_router(operations.router, prefix="/api/v1/operations", tags=["Operations"])
app.include_router(
    databases.router,
    prefix="/api/v1/domain/{domain_name}/project/{project_name}/databases",
    tags=["Databases"]
)


@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "status": "running",
        "docs": "/docs" if not settings.is_production else "disabled",
    }


if __name__ == "__main__":
    import uvicorn
    from app.utils.shutdown import shutdown_handler

    # Setup shutdown handler before starting server
    shutdown_handler.setup()

    try:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=settings.reload,
            log_level=settings.log_level.lower(),
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("application_stopped")
    finally:
        shutdown_handler.restore()
        sys.exit(0)
