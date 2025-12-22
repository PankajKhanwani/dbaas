"""
Database management API endpoints.
Core CRUD operations for managed database instances via KubeDB.

URL Pattern: /api/v1/domain/{domain_name}/project/{project_name}/databases
"""
from typing import Optional

from fastapi import APIRouter, Query, status, Path, Request

from app.models.database import (
    DatabaseCreateRequest,
    DatabaseUpdateRequest,
    DatabaseScaleRequest,
    DatabaseResponse,
    DatabaseListResponse,
    DatabaseCredentials,
    DatabaseEngine,
)
from app.config.logging import get_logger
from app.services.database_service import database_service
from app.services.kubedb_service import kubedb_service

router = APIRouter()
logger = get_logger(__name__)


@router.post("/{database_id}/refresh-endpoint", status_code=status.HTTP_200_OK)
async def refresh_endpoint(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Manually refresh the database endpoint to fetch the latest IP address.

    This endpoint is useful for:
    - Updating legacy databases to use IP addresses instead of DNS
    - Refreshing endpoint after LoadBalancer changes
    - Testing the IP endpoint feature
    """
    logger.info(
        "refreshing_database_endpoint",
        database_id=database_id,
        domain=domain_name,
        project=project_name
    )

    result = await database_service.refresh_database_endpoint(
        domain_name=domain_name,
        project_name=project_name,
        database_id=database_id
    )

    return result


@router.post("/", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_database(
    request: Request,
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    db_request: DatabaseCreateRequest = None,
):
    """
    Create a new managed database instance - ASYNC OPERATION.

    **Pre-checks performed:**
    - Validates database name doesn't exist
    - Selects appropriate provider based on region/AZ headers
    - Validates resource availability

    **Returns immediately (202 Accepted)** after pre-checks pass.
    All heavy operations (resource allocation, KubeDB provisioning) are handled
    asynchronously by background handlers. The reconciler will sync status periodically.

    **Check status:** Use GET /{database_id}/status to monitor provisioning progress.

    **Provider Selection Headers:**
    - X-Region: Target region for database deployment (e.g., "us-east-1")
    - X-Availability-Zone: Target AZ (e.g., "us-east-1a")

    **Supported Engines:**
    - postgres (PostgreSQL)
    - mysql (MySQL)
    - mongodb (MongoDB)
    - redis (Redis)
    - elasticsearch (Elasticsearch)

    **Instance Sizes:**
    - db.t3.micro: 0.5 vCPU, 1GB RAM
    - db.t3.small: 1 vCPU, 2GB RAM
    - db.t3.medium: 2 vCPU, 4GB RAM
    - db.t3.large: 2 vCPU, 8GB RAM
    - db.t3.xlarge: 4 vCPU, 16GB RAM
    - db.t3.2xlarge: 8 vCPU, 32GB RAM
    """
    # Extract headers for provider selection
    headers = dict(request.headers)

    logger.info(
        "database_creation_requested",
        name=db_request.name,
        engine=db_request.engine,
        version=db_request.version,
        domain=domain_name,
        project=project_name,
        region=headers.get("x-region"),
        az=headers.get("x-availability-zone"),
    )

    # Pre-checks only - returns immediately
    # Heavy work handled by async handler
    return await database_service.create_database(
        db_request, domain_name, project_name, headers=headers
    )


@router.get("/", response_model=DatabaseListResponse)
async def list_databases(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
    engine: Optional[str] = Query(None, description="Filter by engine type"),
    status: Optional[str] = Query(None, description="Filter by status"),
):
    """
    List all databases for the specified domain and project.

    Supports pagination and filtering by engine type and status.

    Returns databases in descending order by creation date.
    """
    return await database_service.list_databases(
        domain=domain_name,
        project=project_name,
        page=page,
        page_size=page_size,
        engine=engine,
        status=status,
    )


@router.get("/{database_id}", response_model=DatabaseResponse)
async def get_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Get database details by ID.

    Returns comprehensive information including:
    - Configuration (engine, version, size, replicas)
    - Status and health
    - Connection endpoint
    - Resource usage
    - Labels and annotations
    """
    return await database_service.get_database(database_id, domain_name, project_name)


@router.patch("/{database_id}", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def update_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
    update_data: DatabaseUpdateRequest = None,
):
    """
    Update database configuration.

    Supported updates:
    - **Instance size** (vertical scaling) - Changes CPU and memory allocation
    - **Storage size** (can only increase) - Expands disk space
    - **Replica count** (horizontal scaling) - Adds or removes replicas
    - **Backup settings** - Enable/disable backups, change schedule
    - **Monitoring settings** - Enable/disable monitoring
    - **Labels and annotations** - Update metadata

    **Notes:**
    - Storage can only be increased, never decreased
    - Scaling operations may cause brief interruption
    - Backup/monitoring changes are metadata-only (no restart)
    - Updates are applied asynchronously via KubeDB
    """
    return await database_service.update_database(
        database_id, update_data, domain_name, project_name
    )


@router.delete("/{database_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Delete a database instance.

    **WARNING**: This is a destructive operation!

    The database and all its data will be permanently deleted.
    Backups will be retained according to the backup retention policy.

    The operation is async - the database status will change to 'deleting'
    and the resource will be removed from Kubernetes.
    """
    await database_service.delete_database(database_id, domain_name, project_name)


@router.post("/{database_id}/scale", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def scale_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
    scale_request: DatabaseScaleRequest = None,
):
    """
    Scale database resources.

    Supports:
    - **Horizontal scaling**: Increase/decrease replica count
    - **Vertical scaling**: Change instance size (CPU/memory)
    - **Storage expansion**: Increase storage size (cannot decrease)

    Scaling is performed with minimal downtime where possible.
    """
    return await database_service.scale_database(database_id, scale_request, domain_name, project_name)


@router.post("/{database_id}/pause", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def pause_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Pause a running database instance.

    Paused databases:
    - Stop consuming compute resources
    - Still consume storage resources
    - Can be resumed at any time
    - Retain all data

    Use this to save costs for non-production databases.
    """
    return await database_service.pause_database(database_id, domain_name, project_name)


@router.post("/{database_id}/resume", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def resume_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Resume a paused database instance.

    The database will be started and become available within a few minutes.
    """
    return await database_service.resume_database(database_id, domain_name, project_name)


@router.get("/{database_id}/status")
async def get_database_status(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Get real-time database status from KubeDB.

    Fetches the current status directly from the Kubernetes resource,
    not just from the cached database record.

    Returns:
    - KubeDB phase (Provisioning, Ready, Running, Failed, etc.)
    - Application status (mapped from KubeDB phase)
    - Ready status (boolean)
    - Health status
    - Endpoint information
    - Conditions (detailed status conditions from Kubernetes)

    This endpoint is useful for monitoring database provisioning progress.
    """
    return await database_service.get_database_status_realtime(
        database_id, domain_name, project_name
    )


@router.get("/{database_id}/credentials", response_model=DatabaseCredentials)
async def get_database_credentials(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Get database connection credentials.

    **IMPORTANT**: Handle these credentials securely!

    Returns:
    - Username
    - Password
    - Host (connection endpoint)
    - Port
    - Database name
    - Full connection string

    Credentials are retrieved from Kubernetes secrets created by KubeDB.
    """
    return await database_service.get_credentials(database_id, domain_name, project_name)


@router.get("/{database_id}/metrics")
async def get_database_metrics(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Get database metrics from monitoring endpoint.

    Fetches real-time metrics from the database's Prometheus exporter.
    Only available if monitoring is enabled for the database.

    Returns:
    - Performance metrics (operations/sec, latency, etc.)
    - Resource usage (CPU, memory, storage)
    - Connection metrics
    - Database-specific metrics based on engine type

    Returns 404 if monitoring is not enabled.
    """
    return await database_service.get_metrics(database_id, domain_name, project_name)


@router.post("/{database_id}/backup", status_code=status.HTTP_202_ACCEPTED)
async def trigger_backup(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Trigger an immediate backup.

    Creates a snapshot of the database that can be used for restore.
    Backup is performed asynchronously.

    Returns a job ID to track backup progress.
    """
    # TODO: Implement backup via KubeDB Stash
    return {
        "message": "Backup initiated",
        "database_id": database_id,
        "domain": domain_name,
        "project": project_name,
        "job_id": f"backup-{database_id}-pending",
    }


@router.post("/{database_id}/restore", status_code=status.HTTP_202_ACCEPTED)
async def restore_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
    backup_id: str = Query(..., description="Backup ID to restore from"),
):
    """
    Restore database from a backup.

    **WARNING**: This will restore the database to the state at backup time.
    Recent data may be lost.

    The restore operation is performed asynchronously.
    Returns a job ID to track restore progress.
    """
    # TODO: Implement restore via KubeDB Stash
    return {
        "message": "Restore initiated",
        "database_id": database_id,
        "domain": domain_name,
        "project": project_name,
        "backup_id": backup_id,
        "job_id": f"restore-{database_id}-pending",
    }
