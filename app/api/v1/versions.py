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
    # Return static versions (no provider needed for version listing)
    static_versions = {
        "mongodb": [
            {"version": "8.0.4", "name": "8.0.4", "deprecated": False},
            {"version": "8.0.3", "name": "8.0.3", "deprecated": False},
            {"version": "7.0.21", "name": "7.0.21", "deprecated": False},
        ],
        "postgres": [
            {"version": "16.4", "name": "16.4", "deprecated": False},
            {"version": "16.2", "name": "16.2", "deprecated": False},
            {"version": "15.8", "name": "15.8", "deprecated": False},
        ],
        "mysql": [
            {"version": "8.0.40", "name": "8.0.40", "deprecated": False},
            {"version": "8.0.35", "name": "8.0.35", "deprecated": False},
        ],
        "redis": [
            {"version": "7.2", "name": "7.2", "deprecated": False},
            {"version": "7.0", "name": "7.0", "deprecated": False},
        ],
        "elasticsearch": [
            {"version": "8.11.0", "name": "8.11.0", "deprecated": False},
        ],
    }
    
    # Filter by engine if provided
    if engine:
        try:
            engine_enum = DatabaseEngine(engine.lower())
            return {engine_enum.value: static_versions.get(engine_enum.value, [])}
        except ValueError:
            return {"error": f"Invalid engine: {engine}. Valid options: mongodb, postgres, mysql, mariadb, redis, elasticsearch"}
    
    return static_versions
