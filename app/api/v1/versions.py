"""
Database versions API endpoints.
Provides information about available database engine versions.
"""
from typing import Optional, Dict, Any
from fastapi import APIRouter, Query

from app.models.database import DatabaseEngine
from app.config.logging import get_logger
from app.services.kubedb_service import kubedb_service

router = APIRouter()
logger = get_logger(__name__)


@router.get("/")
async def get_available_versions(
    engine: Optional[str] = Query(None, description="Filter by engine (mongodb, postgres, mysql, mariadb, redis, elasticsearch)")
) -> Dict[str, Any]:
    """
    Get available database versions from KubeDB.

    Returns all supported database engines and their available versions.
    Optionally filter by a specific engine.

    **Response Format:**
    ```json
    {
        "mongodb": [
            {
                "version": "8.0.10",
                "name": "8.0.10",
                "deprecated": false
            },
            {
                "version": "7.0.21",
                "name": "7.0.21",
                "deprecated": false
            }
        ],
        "postgres": [...]
    }
    ```

    **Query Parameters:**
    - **engine** (optional): Filter by specific engine (mongodb, postgres, mysql, mariadb, redis, elasticsearch)

    **Example Usage:**
    - Get all versions: `GET /api/v1/versions/`
    - Get MongoDB versions only: `GET /api/v1/versions/?engine=mongodb`
    - Get MariaDB versions only: `GET /api/v1/versions/?engine=mariadb`
    """
    # Convert string engine to enum if provided
    engine_enum = None
    if engine:
        try:
            engine_enum = DatabaseEngine(engine.lower())
        except ValueError:
            # Invalid engine, will return empty result
            return {"error": f"Invalid engine: {engine}. Valid options: mongodb, postgres, mysql, mariadb, redis, elasticsearch"}

    versions = await kubedb_service.get_available_versions(engine=engine_enum)

    return versions
