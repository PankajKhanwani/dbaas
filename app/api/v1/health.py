"""
Health check endpoints for monitoring and orchestration.
Provides liveness, readiness, and startup probes.
"""
from datetime import datetime

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.config.database import Database
from app.config.redis import RedisConnection
from app.config.settings import settings

router = APIRouter()


@router.get("/")
async def health_check():
    """
    Basic health check endpoint.
    Returns current status and version.
    """
    return {
        "status": "healthy",
        "version": settings.app_version,
        "environment": settings.environment,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/live")
async def liveness():
    """
    Kubernetes liveness probe.
    Indicates whether the application should be restarted.
    """
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}


@router.get("/ready")
async def readiness():
    """
    Kubernetes readiness probe.
    Indicates whether the application is ready to serve traffic.
    Checks MongoDB and Redis connectivity.
    """
    db_healthy = await Database.ping()
    redis_healthy = await RedisConnection.ping()

    if not db_healthy or not redis_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "mongodb": "healthy" if db_healthy else "unhealthy",
                "redis": "healthy" if redis_healthy else "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    return {
        "status": "ready",
        "mongodb": "healthy",
        "redis": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/startup")
async def startup():
    """
    Kubernetes startup probe.
    Indicates whether the application has started successfully.
    """
    try:
        db_healthy = await Database.ping()

        return {
            "status": "started" if db_healthy else "starting",
            "database": "connected" if db_healthy else "connecting",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "starting",
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
