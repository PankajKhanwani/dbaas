"""
Async-first database API endpoints using operation queue.

This module provides updated versions of database endpoints that use
the operation_manager and queue architecture instead of direct K8s calls.

Usage:
  Replace imports in app/main.py:
  from app.api.v1 import databases_async as databases
"""
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Query, status, Path, HTTPException

from app.models.database import (
    DatabaseCreateRequest,
    DatabaseUpdateRequest,
    DatabaseScaleRequest,
    DatabaseResponse,
    DatabaseListResponse,
    DatabaseCredentials,
    DatabaseEngine,
    VersionUpgradeRequest,
)
from app.config.logging import get_logger
from app.services.database_service import database_service
from app.core.operation_manager import operation_manager
from app.repositories.models import Database
from app.models.database import DatabaseStatus

router = APIRouter()
logger = get_logger(__name__)


# ===============================================================================
# READ ENDPOINTS (GET) - These use existing database_service (no async queue)
# ===============================================================================


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
    from app.services.database_service import database_service
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
    from app.services.database_service import database_service
    return await database_service.get_database(database_id, domain_name, project_name)


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
    from app.services.database_service import database_service
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
    from app.services.database_service import database_service
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
    from app.services.database_service import database_service
    return await database_service.get_metrics(database_id, domain_name, project_name)


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
    from app.services.database_service import database_service

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

    # Get supported versions for this engine
    from app.services.kubedb_service import kubedb_service
    from app.repositories.models import Provider

    # Get provider for this database
    provider = await Provider.get(db_doc.provider_id)
    if not provider:
        return {"available_upgrades": [], "message": "Provider not found"}

    # Get supported versions from kubedb_service
    supported_versions = await kubedb_service.get_supported_versions(
        engine.value,
        provider.region,
        provider.availability_zone
    )

    # Filter to only show versions newer than current
    from app.utils.version import is_upgrade
    available_upgrades = [
        v for v in supported_versions
        if is_upgrade(current_version, v)
    ]

    return {
        "current_version": current_version,
        "available_upgrades": available_upgrades,
        "engine": engine.value,
    }


# ===============================================================================
# WRITE ENDPOINTS (POST/PATCH/DELETE) - These use async operation queue
# ===============================================================================


@router.post("/", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database: DatabaseCreateRequest = ...
):
    """
    Create a new database (async via operation queue).

    Returns immediately with operation_id. Use GET /operations/{operation_id}
    to track progress.
    """
    logger.info(
        "create_database_request_async",
        domain=domain_name,
        project=project_name,
        name=database.name,
        engine=database.engine.value,
        version=database.version,
    )

    # Validate domain and project exist (from existing service)
    await database_service._validate_domain_project(domain_name, project_name)

    # Create database record with "provisioning" status
    db = Database(
        name=database.name,
        domain=domain_name,
        project=project_name,
        engine=database.engine,
        version=database.version,
        size=database.size,
        replicas=database.replicas,
        storage_gb=database.storage_gb,
        status=DatabaseStatus.PROVISIONING,  # Start as provisioning
        region=database.region,
        availability_zone=database.availability_zone,
        backup_enabled=database.backup_enabled or False,
        backup_schedule=database.backup_schedule,
        high_availability=database.high_availability or False,
        monitoring_enabled=database.monitoring_enabled or True,
        labels=database.labels or {},
        annotations=database.annotations or {},
    )

    # Get provider for this region/AZ
    from app.repositories.models import Provider
    provider = await Provider.find_one({
        "region": database.region,
        "availability_zone": database.availability_zone
    })
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No provider found for region={database.region}, az={database.availability_zone}"
        )

    db.provider_id = str(provider.id)
    db.namespace = database.namespace or "default"

    # Save database record
    await db.insert()

    logger.info("database_record_created", database_id=db.id, status="queued")

    # Enqueue create operation
    operation_id = await operation_manager.enqueue_operation(
        database_id=str(db.id),
        operation_type="create",
        params={
            "name": database.name,
            "engine": database.engine.value,
            "version": database.version,
            "size": database.size.value,
            "replicas": database.replicas,
            "storage_gb": database.storage_gb,
            "region": database.region,
            "availability_zone": database.availability_zone,
            "namespace": db.namespace,
            "provider_id": db.provider_id,
            "backup_enabled": database.backup_enabled,
            "backup_schedule": database.backup_schedule,
            "high_availability": database.high_availability,
            "monitoring_enabled": database.monitoring_enabled,
            "labels": database.labels or {},
            "annotations": database.annotations or {},
        },
        priority=1,  # High priority for create operations
    )

    logger.info(
        "create_operation_enqueued",
        database_id=db.id,
        operation_id=operation_id,
    )

    # Return response with operation_id
    return DatabaseResponse(
        id=str(db.id),
        name=db.name,
        domain=db.domain,
        project=db.project,
        engine=db.engine,
        version=db.version,
        size=db.size,
        storage_gb=db.storage_gb,
        replicas=db.replicas,
        status=db.status,
        region=getattr(db, 'region', None),
        availability_zone=getattr(db, 'availability_zone', None),
        namespace=db.namespace,
        backup_enabled=db.backup_enabled,
        backup_schedule=db.backup_schedule,
        high_availability=db.high_availability,
        monitoring_enabled=db.monitoring_enabled,
        labels=db.labels,
        annotations=db.annotations,
        created_at=db.created_at,
        updated_at=db.updated_at,
        operation_id=operation_id,  # Include operation ID
        poll_url=f"/api/v1/operations/{operation_id}" if operation_id else None,  # Poll URL
    )


@router.post("/{database_id}/scale", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def scale_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
    scale_request: Optional[DatabaseScaleRequest] = None,
):
    """
    Scale a database (async via operation queue).

    Supports:
    - Horizontal scaling (replicas)
    - Vertical scaling (instance size)
    - Storage expansion

    Returns immediately with operation_id.
    """
    logger.info(
        "scale_database_request_async",
        database_id=database_id,
        scale_request=scale_request.model_dump(exclude_none=True) if scale_request else {},
    )

    # Load database
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    if not db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database {database_id} not found"
        )

    # Prepare scale parameters
    params = {}
    if scale_request:
        if scale_request.replicas is not None:
            params["replicas"] = scale_request.replicas
        if scale_request.size is not None:
            params["size"] = scale_request.size.value
        if scale_request.storage_gb is not None:
            params["storage_gb"] = scale_request.storage_gb

    if not params:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one scaling parameter required (replicas, size, or storage_gb)"
        )

    # Don't update status here - let the worker set it when processing
    # The operation_manager will validate current state before enqueueing

    # Enqueue scale operation
    operation_id = await operation_manager.enqueue_operation(
        database_id=str(db.id),
        operation_type="scale",
        params=params,
        priority=2,  # High priority for scale operations
    )

    logger.info(
        "scale_operation_enqueued",
        database_id=db.id,
        operation_id=operation_id,
        params=params,
    )

    # Return response
    return DatabaseResponse(
        id=str(db.id),
        name=db.name,
        domain=db.domain,
        project=db.project,
        engine=db.engine,
        version=db.version,
        size=db.size,
        storage_gb=db.storage_gb,
        replicas=db.replicas,
        status=db.status,
        region=getattr(db, 'region', None),
        availability_zone=getattr(db, 'availability_zone', None),
        namespace=db.namespace,
        backup_enabled=db.backup_enabled,
        backup_schedule=db.backup_schedule,
        high_availability=db.high_availability,
        monitoring_enabled=db.monitoring_enabled,
        labels=db.labels,
        annotations=db.annotations,
        created_at=db.created_at,
        updated_at=db.updated_at,
        operation_id=operation_id,
        poll_url=f"/api/v1/operations/{operation_id}" if operation_id else None,
    )


@router.delete("/{database_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
):
    """
    Delete a database (async via operation queue).

    Returns immediately with operation_id.
    """
    logger.info(
        "delete_database_request_async",
        database_id=database_id,
    )

    # Load database
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    if not db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database {database_id} not found"
        )

    # Update status to deleting
    db.status = DatabaseStatus.DELETING
    await db.save()

    # Enqueue delete operation
    operation_id = await operation_manager.enqueue_operation(
        database_id=str(db.id),
        operation_type="delete",
        params={
            "name": db.name,
            "namespace": db.namespace,
            "provider_id": db.provider_id,
            "engine": db.engine.value,
        },
        priority=1,  # Highest priority for delete operations
    )

    logger.info(
        "delete_operation_enqueued",
        database_id=db.id,
        operation_id=operation_id,
    )

    return {
        "message": "Database deletion initiated",
        "database_id": str(db.id),
        "operation_id": operation_id,
        "status": "deleting",
        "poll_url": f"/api/v1/operations/{operation_id}",
    }


@router.patch("/{database_id}", response_model=DatabaseResponse, status_code=status.HTTP_202_ACCEPTED)
async def update_database(
    domain_name: str = Path(..., description="Domain name"),
    project_name: str = Path(..., description="Project name"),
    database_id: str = Path(..., description="Database ID"),
    update_request: DatabaseUpdateRequest = ...,
):
    """
    Update database configuration (async via operation queue).

    Returns immediately with operation_id.
    """
    logger.info(
        "update_database_request_async",
        database_id=database_id,
        update_request=update_request.model_dump(exclude_none=True),
    )

    # Load database
    db = await Database.find_one({"_id": database_id, "domain": domain_name, "project": project_name})
    if not db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database {database_id} not found"
        )

    # Update desired state directly in database record
    # Reconciler worker will detect drift and apply changes
    updated = False

    # Update resource specs (desired state)
    if update_request.size is not None:
        db.size = update_request.size
        updated = True
    if update_request.replicas is not None:
        db.replicas = update_request.replicas
        updated = True
    if update_request.storage_gb is not None:
        db.storage_gb = update_request.storage_gb
        updated = True

    # Update configuration
    if update_request.labels is not None:
        db.labels = update_request.labels
        updated = True
    if update_request.annotations is not None:
        db.annotations = update_request.annotations
        updated = True
    if update_request.backup_enabled is not None:
        db.backup_enabled = update_request.backup_enabled
        updated = True
    if update_request.backup_schedule is not None:
        db.backup_schedule = update_request.backup_schedule
        updated = True
    if update_request.monitoring_enabled is not None:
        db.monitoring_enabled = update_request.monitoring_enabled
        updated = True

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No update parameters provided"
        )

    # Save updated desired state
    db.updated_at = datetime.utcnow()
    await db.save()

    logger.info(
        "database_desired_state_updated",
        database_id=db.id,
        size=db.size.value if update_request.size else None,
        replicas=db.replicas if update_request.replicas else None,
        storage_gb=db.storage_gb if update_request.storage_gb else None,
    )

    # Check for drift and create OpsRequests immediately
    from app.services.kubedb_service import kubedb_service
    from app.repositories.models import Provider

    # Get provider
    provider = await Provider.get(db.provider_id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider {db.provider_id} not found"
        )

    kubeconfig_content = provider.kubeconfig_content
    operation_id = None

    try:
        # Get actual resources from K8s to check for real drift
        if update_request.size:
            from app.services.kubedb_service import kubedb_service

            # Get current MongoDB CR from K8s
            client_set = await kubedb_service.get_client_for_provider(
                db.provider_id, kubeconfig_content
            )

            try:
                current_cr = await client_set.custom_api.get_namespaced_custom_object(
                    group=kubedb_service._get_kubedb_group(db.engine),
                    version=kubedb_service._get_kubedb_version(db.engine),
                    namespace=db.namespace,
                    plural=kubedb_service._get_kubedb_plural(db.engine),
                    name=db.kubedb_resource_name or db.name,
                )

                # Get current resources from K8s
                # Parse resources from CR
                def parse_size_from_cr(cr, engine):
                    """Parse DatabaseSize from MongoDB CR resources."""
                    from app.models.database import DatabaseSize

                    # Get resources based on engine type
                    if engine == "mongodb":
                        resources = cr.get("spec", {}).get("podTemplate", {}).get("spec", {}).get("containers", [{}])[0].get("resources", {})
                    else:
                        resources = cr.get("spec", {}).get("podTemplate", {}).get("spec", {}).get("resources", {})

                    requests = resources.get("requests", {})
                    cpu = requests.get("cpu", "")
                    memory = requests.get("memory", "")

                    # Map resources to DatabaseSize
                    resource_map = {
                        ("500m", "1Gi"): DatabaseSize.MICRO,
                        ("1", "2Gi"): DatabaseSize.SMALL,
                        ("2", "4Gi"): DatabaseSize.MEDIUM,
                        ("2", "8Gi"): DatabaseSize.LARGE,
                        ("4", "16Gi"): DatabaseSize.XLARGE,
                        ("8", "32Gi"): DatabaseSize.XXLARGE,
                    }

                    for (expected_cpu, expected_mem), size in resource_map.items():
                        if cpu == expected_cpu and memory == expected_mem:
                            return size
                    return None

                actual_size = parse_size_from_cr(current_cr, db.engine.value)

                # Only create OpsRequest if actual K8s resources differ from desired
                if actual_size != db.size:
                    logger.info(
                        "size_drift_detected_creating_opsrequest",
                        database_id=db.id,
                        desired=db.size.value,
                        actual_k8s=actual_size.value if actual_size else None,
                    )

                    result = await kubedb_service.create_vertical_scaling_ops_request(
                        engine=db.engine,
                        name=db.kubedb_resource_name or db.name,
                        namespace=db.namespace,
                        size=db.size,
                        provider_id=db.provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    logger.info("vertical_scaling_opsrequest_created", result=result)
                else:
                    logger.info(
                        "no_size_drift_skipping_opsrequest",
                        database_id=db.id,
                        size=db.size.value,
                    )
            except Exception as e:
                logger.warning("failed_to_get_k8s_resources", error=str(e))
                # Fall back to db.current_size comparison
                if db.size != db.current_size:
                    result = await kubedb_service.create_vertical_scaling_ops_request(
                        engine=db.engine,
                        name=db.kubedb_resource_name or db.name,
                        namespace=db.namespace,
                        size=db.size,
                        provider_id=db.provider_id,
                        kubeconfig_content=kubeconfig_content,
                    )
                    logger.info("vertical_scaling_opsrequest_created_fallback", result=result)

        # Check for replicas drift
        if update_request.replicas and db.replicas != db.current_replicas:
            logger.info(
                "replicas_drift_detected_creating_opsrequest",
                database_id=db.id,
                desired=db.replicas,
                current=db.current_replicas,
            )

            result = await kubedb_service.create_horizontal_scaling_ops_request(
                engine=db.engine,
                name=db.kubedb_resource_name or db.name,
                namespace=db.namespace,
                replicas=db.replicas,
                provider_id=db.provider_id,
                kubeconfig_content=kubeconfig_content,
            )
            logger.info("horizontal_scaling_opsrequest_created", result=result)

        # Check for storage drift (only expansion allowed)
        if update_request.storage_gb and db.storage_gb > (db.current_storage_gb or 0):
            logger.info(
                "storage_drift_detected_creating_opsrequest",
                database_id=db.id,
                desired=db.storage_gb,
                current=db.current_storage_gb,
            )

            result = await kubedb_service.patch_database(
                engine=db.engine,
                name=db.kubedb_resource_name or db.name,
                namespace=db.namespace,
                storage_gb=db.storage_gb,
                provider_id=db.provider_id,
                kubeconfig_content=kubeconfig_content,
            )
            logger.info("storage_expansion_applied", result=result)

    except Exception as e:
        logger.error(
            "failed_to_apply_changes",
            database_id=db.id,
            error=str(e),
        )
        # Don't fail the PATCH - desired state is already updated
        # Worker will retry on next reconciliation

    # Return response
    return DatabaseResponse(
        id=str(db.id),
        name=db.name,
        domain=db.domain,
        project=db.project,
        engine=db.engine,
        version=db.version,
        size=db.size,
        storage_gb=db.storage_gb,
        replicas=db.replicas,
        status=db.status,
        region=getattr(db, 'region', None),
        availability_zone=getattr(db, 'availability_zone', None),
        namespace=db.namespace,
        backup_enabled=db.backup_enabled,
        backup_schedule=db.backup_schedule,
        high_availability=db.high_availability,
        monitoring_enabled=db.monitoring_enabled,
        labels=db.labels,
        annotations=db.annotations,
        created_at=db.created_at,
        updated_at=db.updated_at,
        operation_id=operation_id,
        poll_url=f"/api/v1/operations/{operation_id}" if operation_id else None,
    )
