"""
Database management API endpoints.
Core CRUD operations for managed database instances via KubeDB.

URL Pattern: /api/v1/domain/{domain_name}/project/{project_name}/databases
"""
from typing import Optional, Dict, Any
from fastapi import APIRouter, Query, status, Path, Request

from app.models.database import (
    DatabaseCreateRequest,
    DatabaseUpdateRequest,
    DatabaseScaleRequest,
    DatabaseResponse,
    DatabaseListResponse,
    DatabaseCredentials,
    DatabaseEngine,
    VersionUpgradeRequest,
    UpgradePolicy,
)
from app.config.logging import get_logger
from app.services.database_service import database_service
from app.services.kubedb_service import kubedb_service
from app.services.audit_service import audit_service
from app.utils.version import is_upgrade

router = APIRouter()
logger = get_logger(__name__)


async def _get_audit_info(request: Request) -> Dict[str, Any]:
    """
    Extract audit information from FastAPI request.
    
    Returns:
        Dictionary with user info, IP address, user agent, etc.
    """
    # Try to get user from request state (set by auth middleware if available)
    user_id = getattr(request.state, "user_id", None)
    user_email = getattr(request.state, "user_email", None)
    
    # Get client IP
    client_ip = request.client.host if request.client else None
    # Check for forwarded IP (if behind proxy)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    
    return {
        "user_id": user_id,
        "user_email": user_email,
        "ip_address": client_ip,
        "user_agent": request.headers.get("User-Agent"),
        "request_method": request.method,
        "request_path": str(request.url.path),
    }


@router.post("/{database_id}/refresh-endpoint", status_code=status.HTTP_200_OK)
async def refresh_endpoint(
    request: Request,
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

    # Audit log
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.refresh_endpoint",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
        details={"endpoint": result.get("endpoint")},
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
    request: Request,
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
    result = await database_service.update_database(
        database_id, update_data, domain_name, project_name
    )
    
    # Audit log
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.update",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
        details=update_data.model_dump(exclude_none=True) if update_data else {},
    )
    
    return result


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
    request: Request,
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
    Returns operation IDs to track scaling progress.
    """
    logger.info(
        "initiating_database_scale",
        database_id=database_id,
        scale_request=scale_request.model_dump(exclude_none=True) if scale_request else {},
    )

    # Get the database first
    from app.repositories.models import Database
    from app.models.database import DatabaseStatus
    from app.models.operation import Operation, OperationType, OperationStatus
    
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    if not db:
        return {
            "error": "Database not found",
            "database_id": database_id,
        }

    # Update desired state via service
    result = await database_service.scale_database(database_id, scale_request, domain_name, project_name)
    
    # Audit log
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.scale",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
        details=scale_request.model_dump(exclude_none=True) if scale_request else {},
    )

    # Create operations for each scaling type and enqueue them
    from app.services.operation_queue import operation_queue
    
    operations_created = []
    
    if scale_request and scale_request.replicas:
        # Horizontal scaling operation
        operation = Operation(
            database_id=database_id,
            type=OperationType.SCALE_HORIZONTAL,
            desired_state={"replicas": scale_request.replicas},
            status=OperationStatus.QUEUED,
            domain=domain_name,
            project=project_name,
            created_by="api",
        )
        await operation.insert()
        operations_created.append(operation.id)
        
        # Enqueue operation
        await operation_queue.enqueue(
            operation.id,
            priority=5,
            dedup_key=f"{database_id}:scale_horizontal"
        )
        logger.info(
            "horizontal_scaling_operation_created",
            operation_id=operation.id,
            database_id=database_id,
            target_replicas=scale_request.replicas,
        )

    if scale_request and scale_request.size:
        # Vertical scaling operation
        operation = Operation(
            database_id=database_id,
            type=OperationType.SCALE_VERTICAL,
            desired_state={"size": scale_request.size.value},
            status=OperationStatus.QUEUED,
            domain=domain_name,
            project=project_name,
            created_by="api",
        )
        await operation.insert()
        operations_created.append(operation.id)
        
        # Enqueue operation
        await operation_queue.enqueue(
            operation.id,
            priority=5,
            dedup_key=f"{database_id}:scale_vertical"
        )
        logger.info(
            "vertical_scaling_operation_created",
            operation_id=operation.id,
            database_id=database_id,
            target_size=scale_request.size.value,
        )

    if scale_request and scale_request.storage_gb:
        # Storage expansion operation
        operation = Operation(
            database_id=database_id,
            type=OperationType.EXPAND_STORAGE,
            desired_state={"storage_gb": scale_request.storage_gb},
            status=OperationStatus.QUEUED,
            domain=domain_name,
            project=project_name,
            created_by="api",
        )
        await operation.insert()
        operations_created.append(operation.id)
        
        # Enqueue operation
        await operation_queue.enqueue(
            operation.id,
            priority=5,
            dedup_key=f"{database_id}:expand_storage"
        )
        logger.info(
            "storage_expansion_operation_created",
            operation_id=operation.id,
            database_id=database_id,
            target_storage_gb=scale_request.storage_gb,
        )

    # Update database status to UPDATING if any operations were created
    if operations_created:
        db.status = DatabaseStatus.UPDATING
        await db.save()
        logger.info(
            "database_scaling_initiated",
            database_id=database_id,
            operation_count=len(operations_created),
            operation_ids=operations_created,
        )

    # Return result with operation IDs
    # Convert DatabaseResponse to dict and add operation info
    if hasattr(result, 'model_dump'):
        result_dict = result.model_dump()
    elif hasattr(result, 'dict'):
        result_dict = result.dict()
    else:
        result_dict = dict(result)
    
    result_dict["operation_ids"] = operations_created
    if operations_created:
        result_dict["message"] = f"Scaling initiated. {len(operations_created)} operation(s) created and queued."
    else:
        result_dict["message"] = "Database desired state updated. No operations required."
    
    return result_dict


@router.post("/{database_id}/pause", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def pause_database(
    request: Request,
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
    result = await database_service.pause_database(database_id, domain_name, project_name)
    
    # Audit log
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.pause",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
    )
    
    return result


@router.post("/{database_id}/resume", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def resume_database(
    request: Request,
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Resume a paused database instance.

    The database will be started and become available within a few minutes.
    """
    result = await database_service.resume_database(database_id, domain_name, project_name)
    
    # Audit log
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.resume",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
    )
    
    return result


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
    request: Request,
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
    result = await database_service.get_credentials(database_id, domain_name, project_name)
    
    # Audit log (sensitive operation - credentials access)
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.get_credentials",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
        details={"host": result.host, "port": result.port, "database": result.database},
    )
    
    return result


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
    request: Request,
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
    result = {
        "message": "Backup initiated",
        "database_id": database_id,
        "domain": domain_name,
        "project": project_name,
        "job_id": f"backup-{database_id}-pending",
    }
    
    # Audit log
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.backup",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
        details={"job_id": result["job_id"]},
    )
    
    return result


@router.post("/{database_id}/restore", status_code=status.HTTP_202_ACCEPTED)
async def restore_database(
    request: Request,
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
    result = {
        "message": "Restore initiated",
        "database_id": database_id,
        "domain": domain_name,
        "project": project_name,
        "backup_id": backup_id,
        "job_id": f"restore-{database_id}-pending",
    }
    
    # Audit log (critical operation)
    audit_info = await _get_audit_info(request)
    await audit_service.log_action(
        action="database.restore",
        resource_type="database",
        resource_id=database_id,
        domain=domain_name,
        project=project_name,
        **audit_info,
        details={"backup_id": backup_id, "job_id": result["job_id"]},
    )
    
    return result


# ===============================================================================
# VERSION UPGRADE ENDPOINTS
# ===============================================================================


@router.get("/{database_id}/available-upgrades")
async def get_available_upgrades(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Get available upgrade versions for a database.

    Returns a list of versions that the database can be upgraded to,
    based on the current version and upgrade compatibility.
    """
    logger.info(
        "fetching_available_upgrades",
        database_id=database_id,
        domain=domain_name,
        project=project_name,
    )

    # Get the database to know its current version and engine
    db_doc = await database_service.get_database(database_id, domain_name, project_name)
    if not db_doc:
        return {"available_upgrades": [], "message": "Database not found"}

    current_version = db_doc.version
    engine = db_doc.engine

    # Get provider info for region/AZ
    from app.repositories.models import Database, Provider
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    if not db:
        return {"available_upgrades": [], "message": "Database not found"}

    provider = await Provider.find_one({"_id": db.provider_id})
    if not provider:
        return {"available_upgrades": [], "message": "Provider not found"}

    region = provider.region
    availability_zone = provider.availability_zone

    logger.info(
        "fetching_available_versions_for_engine",
        engine=engine.value if hasattr(engine, 'value') else engine,
        current_version=current_version,
        region=region,
        az=availability_zone,
    )

    # Get all available versions for this engine from the provider
    try:
        all_versions = await kubedb_service.get_versions_by_region(
            engine=engine.value if hasattr(engine, 'value') else engine,
            region=region,
            availability_zone=availability_zone
        )

        # Filter to only show versions newer than current version using semantic versioning
        upgradeable_versions = []
        for v in all_versions:
            version_str = v.get("version", "")
            if version_str and is_upgrade(current_version, version_str):
                upgradeable_versions.append(v)

        logger.info(
            "found_upgradeable_versions",
            current_version=current_version,
            count=len(upgradeable_versions),
        )

        return {
            "database_id": database_id,
            "current_version": current_version,
            "engine": engine.value if hasattr(engine, 'value') else engine,
            "available_upgrades": [v.get("version") for v in upgradeable_versions],
            "upgrade_details": upgradeable_versions,
        }

    except Exception as e:
        logger.error(
            "error_fetching_available_upgrades",
            database_id=database_id,
            error=str(e),
        )
        return {
            "database_id": database_id,
            "current_version": current_version,
            "available_upgrades": [],
            "error": str(e),
        }


@router.post("/{database_id}/upgrade", status_code=status.HTTP_202_ACCEPTED)
async def upgrade_database_version(
    request: Request,
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
    upgrade_request: VersionUpgradeRequest = ...,
):
    """
    Upgrade database to a new version.

    **Process:**
    1. Validates target version is available
    2. Creates pre-upgrade backup (unless skipped)
    3. Creates KubeDB OpsRequest for version update
    4. Monitors upgrade progress

    **WARNING**: Version upgrades may cause downtime and are irreversible.
    Always backup before upgrading.

    Returns an operation ID to track upgrade progress.
    """
    logger.info(
        "initiating_database_upgrade",
        database_id=database_id,
        target_version=upgrade_request.target_version,
        skip_backup=upgrade_request.skip_backup,
    )

    # Get the database
    db_doc = await database_service.get_database(database_id, domain_name, project_name)
    if not db_doc:
        return {
            "error": "Database not found",
            "database_id": database_id,
        }

    current_version = db_doc.version
    engine = db_doc.engine

    # TODO: Validate target version is available and compatible
    # TODO: Create pre-upgrade backup if not skipped
    # TODO: Create KubeDB OpsRequest for version update

    try:
        # Initiate the upgrade via KubeDB service
        operation_id = await kubedb_service.upgrade_database_version(
            domain=domain_name,
            project=project_name,
            database_id=database_id,
            target_version=upgrade_request.target_version,
            skip_backup=upgrade_request.skip_backup,
        )

        logger.info(
            "database_upgrade_initiated",
            database_id=database_id,
            current_version=current_version,
            target_version=upgrade_request.target_version,
            ops_request_name=operation_id,
        )
        
        # Audit log (critical operation)
        audit_info = await _get_audit_info(request)
        await audit_service.log_action(
            action="database.upgrade_version",
            resource_type="database",
            resource_id=database_id,
            domain=domain_name,
            project=project_name,
            **audit_info,
            details={
                "current_version": current_version,
                "target_version": upgrade_request.target_version,
                "skip_backup": upgrade_request.skip_backup,
                "operation_id": operation_id,
            },
        )

        # Create Operation record to track the upgrade
        from app.models.operation import Operation, OperationType, OperationStatus
        from app.repositories.models import Database
        from app.models.database import DatabaseStatus
        
        db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
        if not db:
            raise ValueError(f"Database {database_id} not found")
        
        # Create operation record
        operation = Operation(
            database_id=database_id,
            type=OperationType.UPGRADE_VERSION,
            desired_state={
                "target_version": upgrade_request.target_version,
                "current_version": current_version,
                "skip_backup": upgrade_request.skip_backup,
            },
            status=OperationStatus.QUEUED,
            ops_request_name=operation_id,  # This is the OpsRequest name
            domain=domain_name,
            project=project_name,
            created_by="api",
        )
        await operation.insert()
        
        # Enqueue operation for monitoring
        from app.services.operation_queue import operation_queue
        await operation_queue.enqueue(operation.id, priority=5)
        
        # Update database status to UPDATING
        db.status = DatabaseStatus.UPDATING
        await db.save()

        return {
            "message": "Database version upgrade initiated",
            "database_id": database_id,
            "current_version": current_version,
            "target_version": upgrade_request.target_version,
            "operation_id": operation.id,
            "ops_request_name": operation_id,  # KubeDB OpsRequest name
            "poll_url": f"/api/v1/domain/{domain_name}/project/{project_name}/databases/{database_id}",
        }

    except Exception as e:
        logger.error(
            "error_initiating_upgrade",
            database_id=database_id,
            error=str(e),
        )
        return {
            "error": f"Failed to initiate upgrade: {str(e)}",
            "database_id": database_id,
        }


@router.put("/{database_id}/upgrade-policy")
async def update_upgrade_policy(
    request: Request,
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
    upgrade_policy: UpgradePolicy = ...,
):
    """
    Update automated version upgrade policy for a database.

    **Upgrade Strategies:**
    - `disabled`: No automatic upgrades
    - `latest_patch`: Auto-upgrade to latest patch version (e.g., 8.0.35 -> 8.0.36)
    - `latest_minor`: Auto-upgrade to latest minor version (e.g., 8.0.x -> 8.2.x)
    - `specific_version`: Upgrade to a specific target version

    **Maintenance Window:**
    Use cron format to specify when upgrades can occur (e.g., "0 2 * * 0" for Sundays at 2 AM)
    """
    logger.info(
        "updating_upgrade_policy",
        database_id=database_id,
        strategy=upgrade_policy.strategy,
    )

    try:
        # Update the database document with new upgrade policy
        await database_service.update_database_upgrade_policy(
            domain_name, project_name, database_id, upgrade_policy
        )

        logger.info(
            "upgrade_policy_updated",
            database_id=database_id,
            strategy=upgrade_policy.strategy,
        )
        
        # Audit log
        audit_info = await _get_audit_info(request)
        await audit_service.log_action(
            action="database.update_upgrade_policy",
            resource_type="database",
            resource_id=database_id,
            domain=domain_name,
            project=project_name,
            **audit_info,
            details=upgrade_policy.model_dump(exclude_none=True),
        )

        return {
            "message": "Upgrade policy updated successfully",
            "database_id": database_id,
            "upgrade_policy": upgrade_policy.model_dump(),
        }

    except Exception as e:
        logger.error(
            "error_updating_upgrade_policy",
            database_id=database_id,
            error=str(e),
        )
        return {
            "error": f"Failed to update upgrade policy: {str(e)}",
            "database_id": database_id,
        }
