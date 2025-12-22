"""
Provider management API endpoints.
CRUD operations for Kubernetes cluster providers in multi-provider setup.

URL Pattern: /api/v1/providers
"""
from typing import Optional
from fastapi import APIRouter, Query, status, Path, Header, Request

from app.schemas.provider import (
    ProviderCreate,
    ProviderUpdate,
    ProviderResponse,
    ProviderListResponse,
    ProviderResources,
    ResourceInfo,
    ResourceAllocationRequest,
    ResourceAllocationResponse,
)
from app.config.logging import get_logger
from app.services.provider_service import provider_service
from app.services.provider_selector import provider_selector
from app.services.resource_allocation import resource_allocation_service
from app.services.kubedb_service import kubedb_service
from app.models.database import DatabaseEngine

router = APIRouter()
logger = get_logger(__name__)


def _build_provider_response(provider) -> ProviderResponse:
    """Convert Provider model to ProviderResponse schema."""
    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        region=provider.region,
        availability_zone=provider.availability_zone,
        cloud_provider=provider.cloud_provider,
        resources=ProviderResources(
            cpu=ResourceInfo(
                total_cores=provider.cpu_total_cores,
                allocated_cores=provider.cpu_allocated_cores,
                available_cores=provider.get_available_cpu(),
                reservation_percent=provider.cpu_reservation_percent,
            ),
            memory=ResourceInfo(
                total_gb=provider.memory_total_gb,
                allocated_gb=provider.memory_allocated_gb,
                available_gb=provider.get_available_memory(),
                reservation_percent=provider.memory_reservation_percent,
            ),
            storage=ResourceInfo(
                total_gb=provider.storage_total_gb,
                allocated_gb=provider.storage_allocated_gb,
                available_gb=provider.get_available_storage(),
                reservation_percent=provider.storage_reservation_percent,
            ),
        ),
        is_active=provider.is_active,
        is_maintenance=provider.is_maintenance,
        priority=provider.priority,
        tags=provider.tags,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


@router.post("/", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(provider_data: ProviderCreate):
    """
    Onboard a new provider (Kubernetes cluster).

    This endpoint allows administrators to register a new Kubernetes cluster
    that can host databases.

    **Resource Configuration:**
    - Specify total CPU, memory, and storage capacity
    - Set reservation percentages (default 80%) to reserve capacity for scaling
    - Example: 80% reservation means only 80% of total resources can be allocated

    **Priority:**
    - Higher priority providers are selected first (default: 100)
    - Range: 0-1000

    **Kubeconfig:**
    - Provide either kubeconfig_path or kubeconfig_content
    - Used for connecting to the Kubernetes cluster
    """
    logger.info(
        "provider_onboarding_requested",
        name=provider_data.name,
        region=provider_data.region,
        az=provider_data.availability_zone,
    )

    provider = await provider_service.create_provider(provider_data)
    return _build_provider_response(provider)


@router.get("/", response_model=ProviderListResponse)
async def list_providers(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
    region: Optional[str] = Query(None, description="Filter by region"),
    availability_zone: Optional[str] = Query(None, description="Filter by AZ"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
):
    """
    List all providers with optional filters.

    Returns providers in descending order by priority.

    **Filters:**
    - `region`: Filter by cloud region (e.g., us-east-1)
    - `availability_zone`: Filter by AZ (e.g., us-east-1a)
    - `is_active`: Filter by active status
    """
    skip = (page - 1) * page_size
    providers = await provider_service.list_providers(
        region=region,
        availability_zone=availability_zone,
        is_active=is_active,
        skip=skip,
        limit=page_size,
    )

    total = await provider_service.count_providers()

    return ProviderListResponse(
        providers=[_build_provider_response(p) for p in providers],
        total=total,
    )


@router.get("/engines")
async def get_available_engines(
    region: Optional[str] = Query(None, description="Filter by region"),
    availability_zone: Optional[str] = Query(None, description="Filter by AZ"),
):
    """
    Get all available database engines and their versions from providers in the specified region/AZ.

    This endpoint connects to a provider's Kubernetes cluster and queries
    all available KubeDB version CRDs to show what database engines and versions can be deployed.

    **Parameters:**
    - `region`: Optional region filter
    - `availability_zone`: Optional AZ filter

    **Behavior:**
    - If region/AZ specified: finds matching providers and queries their clusters
    - If no region/AZ: uses the default provider/cluster
    - Returns dictionary of engines with their available versions

    **Returns:**
    - Dictionary with engine names as keys and version lists as values
    - Empty dict if no providers found
    """
    try:
        # Find providers matching region/AZ
        providers = await provider_service.list_providers(
            region=region,
            availability_zone=availability_zone,
            is_active=True,
            skip=0,
            limit=1,  # Just need one provider to query versions
        )

        if not providers:
            logger.warning(
                "no_providers_found_for_region_az",
                region=region,
                az=availability_zone,
            )
            return {
                "engines": {},
                "error": "No active providers found for specified region/AZ"
            }

        # Use the first provider
        provider = providers[0]

        logger.info(
            "fetching_all_engines_from_provider",
            provider_id=provider.id,
            provider_name=provider.name,
            region=provider.region,
        )

        # Fetch versions for all supported engines
        engines = {}
        for engine in DatabaseEngine:
            try:
                logger.info(
                    "fetching_engine_versions",
                    engine=engine.value,
                    provider_id=provider.id,
                )
                result = await kubedb_service.get_available_versions(
                    engine=engine,
                    provider_id=provider.id,
                )

                logger.info(
                    "engine_versions_result",
                    engine=engine.value,
                    versions_count=len(result.get("versions", [])),
                )

                # Only include engines that have versions available
                if result.get("versions"):
                    engines[engine.value] = result.get("versions", [])

            except Exception as e:
                logger.error(
                    "failed_to_fetch_engine_versions",
                    engine=engine.value,
                    error=str(e),
                    exc_info=True,
                )
                # Continue to next engine instead of failing completely
                continue

        logger.info(
            "fetched_all_engines",
            provider_id=provider.id,
            engines_count=len(engines),
            engines=list(engines.keys()),
        )

        return {
            "region": provider.region,
            "availability_zone": provider.availability_zone,
            "provider_id": provider.id,
            "provider_name": provider.name,
            "engines": engines,
        }

    except Exception as e:
        logger.error(
            "failed_to_fetch_engines",
            error=str(e),
            exc_info=True,
        )
        return {
            "engines": {},
            "error": f"Failed to fetch engines: {str(e)}"
        }

@router.get("/{provider_id}", response_model=ProviderResponse)
async def get_provider(
    provider_id: str = Path(..., description="Provider ID"),
):
    """
    Get provider details by ID.

    Returns comprehensive information including:
    - Configuration (region, AZ, cloud provider)
    - Resource capacity and current allocation
    - Available resources for new databases
    - Status (active, maintenance mode)
    - Priority and metadata
    """
    provider = await provider_service.get_provider(provider_id)
    return _build_provider_response(provider)


@router.patch("/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: str = Path(..., description="Provider ID"),
    update_data: ProviderUpdate = None,
):
    """
    Update provider configuration.

    **Supported updates:**
    - Resource capacity (total CPU, memory, storage)
    - Reservation percentages
    - Active/maintenance status
    - Priority
    - Metadata and tags

    **Notes:**
    - Cannot update allocated resources directly (use allocation endpoints)
    - Changing total capacity while databases are running requires careful planning
    - Setting is_maintenance=True prevents new database allocations
    """
    provider = await provider_service.update_provider(provider_id, update_data)
    return _build_provider_response(provider)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str = Path(..., description="Provider ID"),
):
    """
    Delete a provider.

    **WARNING**: This is a destructive operation!

    Only delete providers that:
    - Have no allocated resources (cpu_allocated_cores = 0, etc.)
    - Are not hosting any databases

    The operation will fail if resources are currently allocated.
    """
    await provider_service.delete_provider(provider_id)


@router.post("/select", response_model=Optional[ProviderResponse])
async def select_provider(
    request: Request,
    allocation_request: ResourceAllocationRequest,
    x_region: Optional[str] = Header(None, description="Target region"),
    x_availability_zone: Optional[str] = Header(None, description="Target AZ"),
):
    """
    Select a provider based on resource requirements and headers.

    This endpoint demonstrates the provider selection logic without
    actually allocating resources.

    **Headers:**
    - `X-Region`: Target cloud region (e.g., us-east-1)
    - `X-Availability-Zone`: Target AZ (e.g., us-east-1a)

    **Selection Logic:**
    - Filters providers by region/AZ from headers
    - Only considers active providers not in maintenance mode
    - Checks available capacity (respecting reservation limits)
    - Selects highest priority provider that can accommodate resources

    Returns the selected provider or null if no suitable provider found.
    """
    headers = dict(request.headers)

    logger.info(
        "provider_selection_test",
        cpu=allocation_request.cpu_cores,
        memory=allocation_request.memory_gb,
        storage=allocation_request.storage_gb,
        region=x_region,
        az=x_availability_zone,
    )

    provider = await provider_selector.select_provider(
        cpu_cores=allocation_request.cpu_cores,
        memory_gb=allocation_request.memory_gb,
        storage_gb=allocation_request.storage_gb,
        headers=headers,
    )

    if provider:
        return _build_provider_response(provider)
    return None


@router.get("/available/list", response_model=ProviderListResponse)
async def get_available_providers(
    cpu_cores: float = Query(..., gt=0, description="Required CPU cores"),
    memory_gb: float = Query(..., gt=0, description="Required memory in GB"),
    storage_gb: float = Query(..., gt=0, description="Required storage in GB"),
    region: Optional[str] = Query(None, description="Filter by region"),
    availability_zone: Optional[str] = Query(None, description="Filter by AZ"),
):
    """
    Get all providers that can accommodate the specified resources.

    Useful for showing users which clusters have capacity for their database.

    **Parameters:**
    - `cpu_cores`: Required CPU cores
    - `memory_gb`: Required memory in GB
    - `storage_gb`: Required storage in GB
    - `region`: Optional region filter
    - `availability_zone`: Optional AZ filter

    Returns all providers with sufficient available capacity, ordered by priority.
    """
    providers = await provider_selector.get_available_providers(
        cpu_cores=cpu_cores,
        memory_gb=memory_gb,
        storage_gb=storage_gb,
        region=region,
        az=availability_zone,
    )

    return ProviderListResponse(
        providers=[_build_provider_response(p) for p in providers],
        total=len(providers),
    )


@router.post("/{provider_id}/allocate", response_model=ResourceAllocationResponse)
async def allocate_resources(
    provider_id: str = Path(..., description="Provider ID"),
    allocation_request: ResourceAllocationRequest = None,
    database_id: str = Query(..., description="Database ID requesting resources"),
):
    """
    Allocate resources on a provider.

    **INTERNAL USE ONLY** - This endpoint is typically called by the database
    creation service, not directly by users.

    Uses optimistic locking with automatic retries to handle concurrent
    allocation requests safely.

    **Allocation Rules:**
    - Provider must be active and not in maintenance mode
    - Requested resources must fit within available capacity
    - Available capacity = (total * reservation_percent / 100) - allocated

    Returns updated provider state after successful allocation.
    """
    logger.info(
        "resource_allocation_requested",
        provider_id=provider_id,
        database_id=database_id,
        cpu=allocation_request.cpu_cores,
        memory=allocation_request.memory_gb,
        storage=allocation_request.storage_gb,
    )

    provider = await resource_allocation_service.allocate_resources(
        provider_id=provider_id,
        cpu_cores=allocation_request.cpu_cores,
        memory_gb=allocation_request.memory_gb,
        storage_gb=allocation_request.storage_gb,
        database_id=database_id,
    )

    return ResourceAllocationResponse(
        provider_id=provider.id,
        allocated=True,
        message=f"Successfully allocated resources on provider {provider.name}",
        resources=ProviderResources(
            cpu=ResourceInfo(
                total_cores=provider.cpu_total_cores,
                allocated_cores=provider.cpu_allocated_cores,
                available_cores=provider.get_available_cpu(),
                reservation_percent=provider.cpu_reservation_percent,
            ),
            memory=ResourceInfo(
                total_gb=provider.memory_total_gb,
                allocated_gb=provider.memory_allocated_gb,
                available_gb=provider.get_available_memory(),
                reservation_percent=provider.memory_reservation_percent,
            ),
            storage=ResourceInfo(
                total_gb=provider.storage_total_gb,
                allocated_gb=provider.storage_allocated_gb,
                available_gb=provider.get_available_storage(),
                reservation_percent=provider.storage_reservation_percent,
            ),
        ),
    )


@router.post("/{provider_id}/deallocate", response_model=ResourceAllocationResponse)
async def deallocate_resources(
    provider_id: str = Path(..., description="Provider ID"),
    allocation_request: ResourceAllocationRequest = None,
    database_id: str = Query(..., description="Database ID releasing resources"),
):
    """
    Deallocate resources from a provider.

    **INTERNAL USE ONLY** - Called when databases are deleted or scaled down.

    Uses optimistic locking with automatic retries to handle concurrent
    deallocation requests safely.

    Returns updated provider state after successful deallocation.
    """
    logger.info(
        "resource_deallocation_requested",
        provider_id=provider_id,
        database_id=database_id,
        cpu=allocation_request.cpu_cores,
        memory=allocation_request.memory_gb,
        storage=allocation_request.storage_gb,
    )

    provider = await resource_allocation_service.deallocate_resources(
        provider_id=provider_id,
        cpu_cores=allocation_request.cpu_cores,
        memory_gb=allocation_request.memory_gb,
        storage_gb=allocation_request.storage_gb,
        database_id=database_id,
    )

    return ResourceAllocationResponse(
        provider_id=provider.id,
        allocated=False,
        message=f"Successfully deallocated resources from provider {provider.name}",
        resources=ProviderResources(
            cpu=ResourceInfo(
                total_cores=provider.cpu_total_cores,
                allocated_cores=provider.cpu_allocated_cores,
                available_cores=provider.get_available_cpu(),
                reservation_percent=provider.cpu_reservation_percent,
            ),
            memory=ResourceInfo(
                total_gb=provider.memory_total_gb,
                allocated_gb=provider.memory_allocated_gb,
                available_gb=provider.get_available_memory(),
                reservation_percent=provider.memory_reservation_percent,
            ),
            storage=ResourceInfo(
                total_gb=provider.storage_total_gb,
                allocated_gb=provider.storage_allocated_gb,
                available_gb=provider.get_available_storage(),
                reservation_percent=provider.storage_reservation_percent,
            ),
        ),
    )


@router.get("/versions/{engine}")
async def get_available_versions(
    engine: str = Path(..., description="Database engine (mongodb, postgres, mysql, redis, elasticsearch)"),
    region: Optional[str] = Query(None, description="Filter by region"),
    availability_zone: Optional[str] = Query(None, description="Filter by AZ"),
):
    """
    Get available database versions from providers in the specified region/AZ.

    This endpoint connects to the provider's Kubernetes cluster and queries
    the available KubeDB version CRDs to show what database versions can be deployed.

    **Parameters:**
    - `engine`: Database engine type (mongodb, postgres, mysql, redis, elasticsearch)
    - `region`: Optional region filter
    - `availability_zone`: Optional AZ filter

    **Behavior:**
    - If region/AZ specified: finds matching providers and queries their clusters
    - If no region/AZ: uses the default provider/cluster
    - Returns list of available versions from that cluster

    **Returns:**
    - List of available versions with metadata
    - Empty list if no providers found or KubeDB CRD not installed
    """
    try:
        # Parse engine
        try:
            db_engine = DatabaseEngine(engine.lower())
        except ValueError:
            return {
                "engine": engine,
                "versions": [],
                "error": f"Invalid engine: {engine}. Must be one of: mongodb, postgres, mysql, redis, elasticsearch"
            }

        # Find providers matching region/AZ
        providers = await provider_service.list_providers(
            region=region,
            availability_zone=availability_zone,
            is_active=True,
            skip=0,
            limit=1,  # Just need one provider to query versions
        )

        if not providers:
            logger.warning(
                "no_providers_found_for_region_az",
                region=region,
                az=availability_zone,
            )
            return {
                "engine": engine,
                "versions": [],
                "error": "No active providers found for specified region/AZ"
            }

        # Use the first provider
        provider = providers[0]

        logger.info(
            "fetching_versions_from_provider",
            engine=engine,
            provider_id=provider.id,
            provider_name=provider.name,
            region=provider.region,
        )

        # Fetch versions from the provider's cluster
        result = await kubedb_service.get_available_versions(
            engine=db_engine,
            provider_id=provider.id,
            kubeconfig_content=provider.kubeconfig_content,
        )

        return result

    except Exception as e:
        logger.error(
            "failed_to_fetch_versions",
            engine=engine,
            error=str(e),
            exc_info=True,
        )
        return {
            "engine": engine,
            "versions": [],
            "error": f"Failed to fetch versions: {str(e)}"
        }
