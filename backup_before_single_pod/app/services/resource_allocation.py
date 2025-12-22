"""
Resource Allocation Service with Concurrency Handling for MongoDB

Handles resource allocation/deallocation with optimistic locking
to prevent race conditions in concurrent requests.
"""
from typing import Optional
import structlog
from datetime import datetime

from app.repositories.models import Provider
from app.core.exceptions import ResourceAllocationError, ProviderNotFoundError

logger = structlog.get_logger()


class ResourceAllocationService:
    """
    Service for managing resource allocation with concurrency safety.

    Uses optimistic locking (version field) to prevent
    race conditions when multiple requests try to allocate resources simultaneously.
    """

    MAX_RETRIES = 5  # Maximum retry attempts for optimistic locking

    @staticmethod
    async def allocate_resources(
        provider_id: str,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        database_id: str,
    ) -> Provider:
        """
        Allocate resources on a provider with concurrency safety.

        Uses optimistic locking with retries to handle concurrent allocation requests.

        Args:
            provider_id: Provider ID
            cpu_cores: CPU cores to allocate
            memory_gb: Memory in GB to allocate
            storage_gb: Storage in GB to allocate
            database_id: Database ID requesting resources

        Returns:
            Updated Provider object

        Raises:
            ProviderNotFoundError: If provider doesn't exist
            ResourceAllocationError: If resources cannot be allocated
        """
        for attempt in range(ResourceAllocationService.MAX_RETRIES):
            # Fetch provider
            provider = await Provider.find_one(Provider.id == provider_id)

            if not provider:
                raise ProviderNotFoundError(f"Provider {provider_id} not found")

            # Check if provider is active
            if not provider.is_active or provider.is_maintenance:
                raise ResourceAllocationError(
                    f"Provider {provider_id} is not available (active={provider.is_active}, maintenance={provider.is_maintenance})"
                )

            # Check if resources are available
            if not provider.can_accommodate(cpu_cores, memory_gb, storage_gb):
                available_cpu = provider.get_available_cpu()
                available_memory = provider.get_available_memory()
                available_storage = provider.get_available_storage()

                raise ResourceAllocationError(
                    f"Insufficient resources on provider {provider_id}. "
                    f"Required: CPU={cpu_cores}, Memory={memory_gb}GB, Storage={storage_gb}GB. "
                    f"Available: CPU={available_cpu}, Memory={available_memory}GB, Storage={available_storage}GB"
                )

            # Store current version for optimistic locking
            current_version = provider.version

            # Allocate resources
            provider.allocate_resources(cpu_cores, memory_gb, storage_gb)
            provider.version += 1  # Increment version

            try:
                # Try to save with version check (optimistic locking)
                # This will fail if another request modified the provider in the meantime
                result = await Provider.find_one(
                    Provider.id == provider_id,
                    Provider.version == current_version
                ).update(
                    {
                        "$set": {
                            "cpu_allocated_cores": provider.cpu_allocated_cores,
                            "memory_allocated_gb": provider.memory_allocated_gb,
                            "storage_allocated_gb": provider.storage_allocated_gb,
                            "updated_at": provider.updated_at,
                            "version": provider.version,
                        }
                    }
                )

                if result.modified_count == 0:
                    # Version mismatch - another request modified the provider
                    logger.warning(
                        "resource_allocation_conflict_retry",
                        provider_id=provider_id,
                        attempt=attempt + 1,
                        max_retries=ResourceAllocationService.MAX_RETRIES,
                    )
                    continue  # Retry

                # Success!
                logger.info(
                    "resources_allocated",
                    provider_id=provider_id,
                    database_id=database_id,
                    cpu_cores=cpu_cores,
                    memory_gb=memory_gb,
                    storage_gb=storage_gb,
                    cpu_allocated=provider.cpu_allocated_cores,
                    memory_allocated=provider.memory_allocated_gb,
                    storage_allocated=provider.storage_allocated_gb,
                    attempt=attempt + 1,
                )

                # Fetch and return updated provider
                updated_provider = await Provider.find_one(Provider.id == provider_id)
                return updated_provider

            except Exception as e:
                logger.error("resource_allocation_error", error=str(e), attempt=attempt + 1)
                raise ResourceAllocationError(f"Failed to allocate resources: {str(e)}")

        # Max retries exceeded
        raise ResourceAllocationError(
            f"Failed to allocate resources after {ResourceAllocationService.MAX_RETRIES} attempts due to concurrent modifications"
        )

    @staticmethod
    async def deallocate_resources(
        provider_id: str,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        database_id: str,
    ) -> Provider:
        """
        Deallocate resources from a provider with concurrency safety.

        Uses optimistic locking with retries.

        Args:
            provider_id: Provider ID
            cpu_cores: CPU cores to deallocate
            memory_gb: Memory in GB to deallocate
            storage_gb: Storage in GB to deallocate
            database_id: Database ID releasing resources

        Returns:
            Updated Provider object

        Raises:
            ProviderNotFoundError: If provider doesn't exist
        """
        for attempt in range(ResourceAllocationService.MAX_RETRIES):
            provider = await Provider.find_one(Provider.id == provider_id)

            if not provider:
                raise ProviderNotFoundError(f"Provider {provider_id} not found")

            current_version = provider.version

            # Deallocate resources
            provider.deallocate_resources(cpu_cores, memory_gb, storage_gb)
            provider.version += 1

            try:
                result = await Provider.find_one(
                    Provider.id == provider_id,
                    Provider.version == current_version
                ).update(
                    {
                        "$set": {
                            "cpu_allocated_cores": provider.cpu_allocated_cores,
                            "memory_allocated_gb": provider.memory_allocated_gb,
                            "storage_allocated_gb": provider.storage_allocated_gb,
                            "updated_at": provider.updated_at,
                            "version": provider.version,
                        }
                    }
                )

                if result.modified_count == 0:
                    logger.warning(
                        "resource_deallocation_conflict_retry",
                        provider_id=provider_id,
                        attempt=attempt + 1,
                    )
                    continue

                logger.info(
                    "resources_deallocated",
                    provider_id=provider_id,
                    database_id=database_id,
                    cpu_cores=cpu_cores,
                    memory_gb=memory_gb,
                    storage_gb=storage_gb,
                    cpu_allocated=provider.cpu_allocated_cores,
                    memory_allocated=provider.memory_allocated_gb,
                    storage_allocated=provider.storage_allocated_gb,
                )

                updated_provider = await Provider.find_one(Provider.id == provider_id)
                return updated_provider

            except Exception as e:
                logger.error("resource_deallocation_error", error=str(e))
                raise ResourceAllocationError(f"Failed to deallocate resources: {str(e)}")

        raise ResourceAllocationError(
            f"Failed to deallocate resources after {ResourceAllocationService.MAX_RETRIES} attempts"
        )

    @staticmethod
    async def update_allocation(
        provider_id: str,
        old_cpu_cores: float,
        old_memory_gb: float,
        old_storage_gb: float,
        new_cpu_cores: float,
        new_memory_gb: float,
        new_storage_gb: float,
        database_id: str,
    ) -> Provider:
        """
        Update resource allocation (e.g., during database scaling).

        Args:
            provider_id: Provider ID
            old_cpu_cores: Previously allocated CPU cores
            old_memory_gb: Previously allocated memory
            old_storage_gb: Previously allocated storage
            new_cpu_cores: New CPU cores requirement
            new_memory_gb: New memory requirement
            new_storage_gb: New storage requirement
            database_id: Database ID

        Returns:
            Updated Provider object

        Raises:
            ResourceAllocationError: If new resources cannot be allocated
        """
        for attempt in range(ResourceAllocationService.MAX_RETRIES):
            provider = await Provider.find_one(Provider.id == provider_id)

            if not provider:
                raise ProviderNotFoundError(f"Provider {provider_id} not found")

            # Calculate delta
            delta_cpu = new_cpu_cores - old_cpu_cores
            delta_memory = new_memory_gb - old_memory_gb
            delta_storage = new_storage_gb - old_storage_gb

            # If scaling up, check if resources are available
            if delta_cpu > 0 or delta_memory > 0 or delta_storage > 0:
                if not provider.can_accommodate(
                    max(0, delta_cpu),
                    max(0, delta_memory),
                    max(0, delta_storage),
                ):
                    raise ResourceAllocationError(
                        f"Insufficient resources for scaling on provider {provider_id}"
                    )

            current_version = provider.version

            # Update allocation
            provider.cpu_allocated_cores += delta_cpu
            provider.memory_allocated_gb += delta_memory
            provider.storage_allocated_gb += delta_storage

            # Ensure non-negative values
            provider.cpu_allocated_cores = max(0, provider.cpu_allocated_cores)
            provider.memory_allocated_gb = max(0, provider.memory_allocated_gb)
            provider.storage_allocated_gb = max(0, provider.storage_allocated_gb)

            provider.updated_at = datetime.utcnow()
            provider.version += 1

            try:
                result = await Provider.find_one(
                    Provider.id == provider_id,
                    Provider.version == current_version
                ).update(
                    {
                        "$set": {
                            "cpu_allocated_cores": provider.cpu_allocated_cores,
                            "memory_allocated_gb": provider.memory_allocated_gb,
                            "storage_allocated_gb": provider.storage_allocated_gb,
                            "updated_at": provider.updated_at,
                            "version": provider.version,
                        }
                    }
                )

                if result.modified_count == 0:
                    logger.warning(
                        "resource_update_conflict_retry",
                        provider_id=provider_id,
                        attempt=attempt + 1,
                    )
                    continue

                logger.info(
                    "resource_allocation_updated",
                    provider_id=provider_id,
                    database_id=database_id,
                    delta_cpu=delta_cpu,
                    delta_memory=delta_memory,
                    delta_storage=delta_storage,
                    new_cpu_allocated=provider.cpu_allocated_cores,
                    new_memory_allocated=provider.memory_allocated_gb,
                    new_storage_allocated=provider.storage_allocated_gb,
                )

                updated_provider = await Provider.find_one(Provider.id == provider_id)
                return updated_provider

            except Exception as e:
                logger.error("resource_update_error", error=str(e))
                raise ResourceAllocationError(f"Failed to update allocation: {str(e)}")

        raise ResourceAllocationError(
            f"Failed to update allocation after {ResourceAllocationService.MAX_RETRIES} attempts"
        )

    @staticmethod
    async def check_scaling_capacity(
        provider_id: str,
        additional_cpu_cores: float,
        additional_memory_gb: float,
        additional_storage_gb: float,
    ) -> bool:
        """
        Check if provider has capacity for scaling (non-blocking check).

        Args:
            provider_id: Provider ID
            additional_cpu_cores: Additional CPU required
            additional_memory_gb: Additional memory required
            additional_storage_gb: Additional storage required

        Returns:
            True if scaling is possible, False otherwise
        """
        provider = await Provider.find_one(Provider.id == provider_id)

        if not provider or not provider.is_active or provider.is_maintenance:
            return False

        return provider.can_accommodate(
            additional_cpu_cores,
            additional_memory_gb,
            additional_storage_gb,
        )


# Global instance
resource_allocation_service = ResourceAllocationService()
