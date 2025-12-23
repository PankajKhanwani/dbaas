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
    
    **Immediate Feedback:**
    - If scaling is needed, status changes to "UPDATING" immediately
    - OpsRequest is created immediately (fire-and-forget)
    - Background worker monitors and updates status back to "RUNNING" when complete
    """
    from app.repositories.models import Database, Provider
    from app.models.database import DatabaseStatus, DatabaseSize
    from app.services.kubedb_service import kubedb_service
    
    # Get database
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    if not db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database {database_id} not found"
        )
    
    # Update desired state first
    old_size = db.size
    old_replicas = db.replicas
    old_storage = db.storage_gb
    
    if update_data:
        if update_data.size is not None:
            db.size = update_data.size
        if update_data.replicas is not None:
            db.replicas = update_data.replicas
        if update_data.storage_gb is not None:
            db.storage_gb = update_data.storage_gb
        if update_data.backup_enabled is not None:
            db.backup_enabled = update_data.backup_enabled
        if update_data.backup_schedule is not None:
            db.backup_schedule = update_data.backup_schedule
        if update_data.monitoring_enabled is not None:
            db.monitoring_enabled = update_data.monitoring_enabled
        if update_data.labels is not None:
            db.labels = update_data.labels
        if update_data.annotations is not None:
            db.annotations = update_data.annotations
    
    # Check if scaling parameters changed
    size_changed = old_size != db.size
    replicas_changed = old_replicas != db.replicas
    storage_changed = old_storage != db.storage_gb
    
    # If scaling parameters changed, set status to UPDATING immediately and create OpsRequest in background
    if size_changed or replicas_changed or storage_changed:
        # Set status to UPDATING immediately for instant feedback
        db.status = DatabaseStatus.UPDATING
        await db.save()
        
        logger.info(
            "database_status_set_to_updating_immediate",
            database_id=db.id,
            size_changed=size_changed,
            replicas_changed=replicas_changed,
            storage_changed=storage_changed,
            message="Status set to UPDATING immediately - OpsRequest will be created in background",
        )
        
        # Create OpsRequest immediately in background (fire-and-forget)
        # Check for active OpsRequests to prevent duplicates
        async def create_ops_requests_background():
            """Create OpsRequests in background - check for active ones first to prevent duplicates."""
            try:
                # Get provider for kubeconfig
                provider = await Provider.find_one({"_id": db.provider_id})
                if not provider:
                    logger.error(
                        "provider_not_found_for_opsrequest",
                        database_id=db.id,
                        provider_id=db.provider_id,
                    )
                    return
                
                kubeconfig_content = provider.kubeconfig_content
                client_set = await kubedb_service.get_client_for_provider(provider.id, kubeconfig_content)
                
                # CRITICAL: Check for active OpsRequests before creating new ones
                # KubeDB can only process one OpsRequest at a time
                # This prevents duplicate OpsRequests that cause pods to restart multiple times
                try:
                    existing_ops_requests = await client_set.custom_api.list_namespaced_custom_object(
                        group="ops.kubedb.com",
                        version="v1alpha1",
                        namespace=db.namespace,
                        plural=kubedb_service._get_ops_request_plural(db.engine),
                    )
                    
                    # Filter OpsRequests that target this database
                    all_items = existing_ops_requests.get("items", [])
                    db_ops = [
                        item for item in all_items
                        if item.get("spec", {}).get("databaseRef", {}).get("name") == db.kubedb_resource_name
                    ]
                    
                    # Check for active (non-terminal) OpsRequests
                    active_ops = [
                        item for item in db_ops
                        if item.get("status", {}).get("phase") not in ["Successful", "Failed", "Skipped"]
                    ]
                    
                    if active_ops:
                        # There's already an active OpsRequest - skip creating new ones
                        active_ops_names = [op.get("metadata", {}).get("name", "Unknown") for op in active_ops]
                        active_phases = [op.get("status", {}).get("phase", "Unknown") for op in active_ops]
                        logger.warning(
                            "active_ops_request_exists_skipping_creation",
                            database_id=db.id,
                            active_ops_requests=active_ops_names,
                            active_phases=active_phases,
                            message="Active OpsRequest(s) already exist - skipping to prevent duplicate operations and multiple pod restarts",
                        )
                        return  # Skip - let existing OpsRequest complete
                except Exception as e:
                    logger.warning(
                        "failed_to_check_active_ops_requests",
                        database_id=db.id,
                        error=str(e),
                        message="Proceeding with OpsRequest creation despite check failure",
                    )
                    # Continue - if check fails, proceed (better to create than miss an update)
                
                ops_requests_created = []
                
                # Create OpsRequest(s) only if no active ones exist
                if size_changed and db.size:
                    try:
                        ops_request = await kubedb_service.create_vertical_scaling_ops_request(
                            engine=db.engine,
                            name=db.kubedb_resource_name,
                            namespace=db.namespace,
                            size=db.size,
                            provider_id=provider.id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        ops_requests_created.append(ops_request.get("metadata", {}).get("name") if isinstance(ops_request, dict) else None)
                        logger.info(
                            "vertical_scaling_opsrequest_created_background",
                            database_id=db.id,
                            size=db.size.value,
                        )
                    except Exception as e:
                        logger.error(
                            "failed_to_create_vertical_scaling_opsrequest",
                            database_id=db.id,
                            error=str(e),
                        )
                
                if replicas_changed:
                    try:
                        ops_request = await kubedb_service.create_horizontal_scaling_ops_request(
                            engine=db.engine,
                            name=db.kubedb_resource_name,
                            namespace=db.namespace,
                            replicas=db.replicas,
                            provider_id=provider.id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        ops_requests_created.append(ops_request.get("metadata", {}).get("name") if isinstance(ops_request, dict) else None)
                        logger.info(
                            "horizontal_scaling_opsrequest_created_background",
                            database_id=db.id,
                            replicas=db.replicas,
                        )
                    except Exception as e:
                        logger.error(
                            "failed_to_create_horizontal_scaling_opsrequest",
                            database_id=db.id,
                            error=str(e),
                        )
                
                if storage_changed and db.storage_gb:
                    try:
                        await kubedb_service.patch_database(
                            engine=db.engine,
                            name=db.kubedb_resource_name,
                            namespace=db.namespace,
                            storage_gb=db.storage_gb,
                            provider_id=provider.id,
                            kubeconfig_content=kubeconfig_content,
                        )
                        logger.info(
                            "storage_expansion_applied_background",
                            database_id=db.id,
                            storage_gb=db.storage_gb,
                        )
                    except Exception as e:
                        logger.error(
                            "failed_to_expand_storage",
                            database_id=db.id,
                            error=str(e),
                        )
                
                if ops_requests_created:
                    logger.info(
                        "database_opsrequests_created_background",
                        database_id=db.id,
                        ops_requests=ops_requests_created,
                        message="OpsRequest(s) created in background",
                    )
            
            except Exception as e:
                logger.error(
                    "failed_to_create_opsrequest_background",
                    database_id=db.id,
                    error=str(e),
                    exc_info=True,
                )
        
        # Start background task (fire-and-forget) - runs immediately, doesn't block response
        import asyncio
        asyncio.create_task(create_ops_requests_background())
    
    # Save database (for non-scaling updates like backup settings, labels, etc.)
    if not (size_changed or replicas_changed or storage_changed):
        await db.save()
    
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
    
    # Return database response directly from db object we already have
    # Don't call get_database() as it syncs status from KubeDB and would overwrite UPDATING status
    # Reload db to ensure we have the latest state (including the UPDATING status we just set)
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    return database_service._to_response(db)


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
    
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    if not db:
        return {
            "error": "Database not found",
            "database_id": database_id,
        }

    # Update desired state via service (reconciler will handle OpsRequest creation)
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

    # Update database status to UPDATING (reconciler will create OpsRequest)
    db.status = DatabaseStatus.UPDATING
    await db.save()
    
    logger.info(
        "database_desired_state_updated",
        database_id=database_id,
        message="Desired state updated. Reconciler will create OpsRequest if needed.",
        scale_request=scale_request.model_dump(exclude_none=True) if scale_request else {},
    )

    # Return result
    if hasattr(result, 'model_dump'):
        result_dict = result.model_dump()
    elif hasattr(result, 'dict'):
        result_dict = result.dict()
    else:
        result_dict = dict(result)
    
    result_dict["message"] = "Desired state updated. Reconciler will reconcile and create OpsRequest if needed."
    
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
    realtime: bool = Query(False, description="Fetch real-time status from Kubernetes (slower)"),
):
    """
    Get database status.

    By default, returns cached status from MongoDB (updated every 30 seconds by status_sync).
    This is fast and sufficient for most use cases.

    Set realtime=true to fetch directly from Kubernetes (slower but most up-to-date).

    Returns:
    - KubeDB phase (Provisioning, Ready, Running, Failed, etc.)
    - Application status (mapped from KubeDB phase)
    - Ready status (boolean)
    - Health status
    - Endpoint information
    - Conditions (detailed status conditions from Kubernetes) - only with realtime=true

    This endpoint is useful for monitoring database provisioning progress.
    """
    if realtime:
        # Slow path - fetch from Kubernetes
        return await database_service.get_database_status_realtime(
            database_id, domain_name, project_name
        )
    else:
        # Fast path - return cached status from MongoDB
        db = await database_service.get_database(database_id, domain_name, project_name)
        return {
            "database_id": db.id,
            "name": db.name,
            "status": db.status,
            "health_status": db.health_status,
            "endpoint": db.endpoint,
            "port": db.port,
            "replicas": {
                "desired": db.replicas,
                "ready": db.ready_replicas,
                "status": f"{db.ready_replicas}/{db.replicas}",
            },
            "updated_at": db.updated_at.isoformat() if db.updated_at else None,
            "cached": True,
            "message": "Cached status (updated every 30s). Use ?realtime=true for real-time K8s status.",
        }


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
    
    **Optimized Performance:**
    - Uses Redis cache for version data (2-hour TTL)
    - Parallel database and provider fetching
    - Direct provider_id usage (no region/AZ lookup)
    """
    logger.info(
        "fetching_available_upgrades",
        database_id=database_id,
        domain=domain_name,
        project=project_name,
    )

    from app.repositories.models import Database, Provider
    from app.services.version_cache_service import version_cache_service

    # Optimize: Fetch database first (needed to get provider_id)
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    
    if not db:
        return {"available_upgrades": [], "message": "Database not found"}

    current_version = db.version
    engine = db.engine
    provider_id = db.provider_id

    # Fetch provider (needed for kubeconfig if cache miss)
    # Note: We fetch provider after db because we need provider_id from db
    # But this is still fast since it's just one MongoDB query
    provider = await Provider.find_one({"_id": provider_id})
    if not provider:
        return {"available_upgrades": [], "message": "Provider not found"}

    engine_str = engine.value if hasattr(engine, 'value') else str(engine)

    logger.info(
        "fetching_available_versions_for_engine",
        engine=engine_str,
        current_version=current_version,
        provider_id=provider_id,
    )

    # Get all available versions using cached service
    try:
        # Use cache service - it will fetch from K8s API only if cache miss
        # Disable cache in get_available_versions to avoid double caching
        async def fetch_versions():
            """Fetch versions from Kubernetes API."""
            return await kubedb_service.get_available_versions(
                engine=engine,
                provider_id=provider_id,
                kubeconfig_content=provider.kubeconfig_content,
                use_cache=False,  # Disable internal cache, version_cache_service handles it
            )

        all_versions = await version_cache_service.get_versions(
            engine=engine_str,
            provider_id=provider_id,
            fetch_fn=fetch_versions,
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
            "engine": engine_str,
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
