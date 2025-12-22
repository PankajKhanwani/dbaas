"""
Provider Selection Service with Pluggable Strategy Pattern (Async MongoDB)

Allows different provider selection strategies based on headers or business logic.
Can be easily extended to add new selection strategies.
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
import structlog

from app.repositories.models import Provider

logger = structlog.get_logger()


class ProviderSelectionStrategy(ABC):
    """
    Abstract base class for provider selection strategies.

    Implement this interface to create custom provider selection logic.
    """

    @abstractmethod
    async def select_provider(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        headers: Dict[str, str],
        **kwargs
    ) -> Optional[Provider]:
        """
        Select a provider based on the strategy.

        Args:
            cpu_cores: Required CPU cores
            memory_gb: Required memory in GB
            storage_gb: Required storage in GB
            headers: Request headers containing az, region, etc.
            **kwargs: Additional criteria

        Returns:
            Selected Provider or None if no suitable provider found
        """
        pass


class RegionAZStrategy(ProviderSelectionStrategy):
    """
    Strategy that selects provider based on Region and Availability Zone from headers.

    Headers:
        - X-Region: Target region (e.g., us-east-1)
        - X-Availability-Zone: Target AZ (e.g., us-east-1a)
    """

    async def select_provider(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        headers: Dict[str, str],
        **kwargs
    ) -> Optional[Provider]:
        """Select provider based on region and AZ from headers."""
        region = headers.get("x-region") or headers.get("X-Region")
        az = headers.get("x-availability-zone") or headers.get("X-Availability-Zone")

        logger.info(
            "selecting_provider_by_region_az",
            region=region,
            az=az,
            cpu=cpu_cores,
            memory=memory_gb,
            storage=storage_gb,
        )

        # Build query
        query = Provider.find(
            Provider.is_active == True,
            Provider.is_maintenance == False,
        )

        # Filter by region if provided
        if region:
            query = query.find(Provider.region == region)

        # Filter by AZ if provided
        if az:
            query = query.find(Provider.availability_zone == az)

        # Order by priority (higher first)
        providers = await query.sort(-Provider.priority).to_list()

        # Find first provider that can accommodate resources
        for provider in providers:
            if provider.can_accommodate(cpu_cores, memory_gb, storage_gb):
                logger.info(
                    "provider_selected",
                    provider_id=provider.id,
                    provider_name=provider.name,
                )
                return provider

        logger.warning(
            "no_provider_found",
            region=region,
            az=az,
            cpu=cpu_cores,
            memory=memory_gb,
            storage=storage_gb,
        )
        return None


class BestFitStrategy(ProviderSelectionStrategy):
    """
    Strategy that selects provider with the best fit (least waste of resources).

    Useful for optimizing resource utilization.
    """

    async def select_provider(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        headers: Dict[str, str],
        **kwargs
    ) -> Optional[Provider]:
        """Select provider with best resource fit."""
        region = headers.get("x-region") or headers.get("X-Region")
        az = headers.get("x-availability-zone") or headers.get("X-Availability-Zone")

        # Build query
        query = Provider.find(
            Provider.is_active == True,
            Provider.is_maintenance == False,
        )

        if region:
            query = query.find(Provider.region == region)
        if az:
            query = query.find(Provider.availability_zone == az)

        providers = await query.to_list()

        # Filter providers that can accommodate and score them
        suitable_providers = []
        for provider in providers:
            if provider.can_accommodate(cpu_cores, memory_gb, storage_gb):
                # Calculate "waste" score (lower is better)
                cpu_waste = provider.get_available_cpu() - cpu_cores
                memory_waste = provider.get_available_memory() - memory_gb
                storage_waste = provider.get_available_storage() - storage_gb

                # Normalize by total capacity
                waste_score = (
                    (cpu_waste / provider.cpu_total_cores) +
                    (memory_waste / provider.memory_total_gb) +
                    (storage_waste / provider.storage_total_gb)
                ) / 3

                suitable_providers.append((provider, waste_score))

        if not suitable_providers:
            return None

        # Sort by waste score (ascending) and priority (descending)
        suitable_providers.sort(key=lambda x: (x[1], -x[0].priority))

        selected_provider = suitable_providers[0][0]
        logger.info(
            "provider_selected_best_fit",
            provider_id=selected_provider.id,
            waste_score=suitable_providers[0][1],
        )
        return selected_provider


class RoundRobinStrategy(ProviderSelectionStrategy):
    """
    Strategy that distributes databases across providers in a round-robin fashion.

    Useful for load balancing across multiple clusters.
    """

    def __init__(self):
        self.last_provider_index = -1

    async def select_provider(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        headers: Dict[str, str],
        **kwargs
    ) -> Optional[Provider]:
        """Select provider in round-robin fashion."""
        region = headers.get("x-region") or headers.get("X-Region")

        query = Provider.find(
            Provider.is_active == True,
            Provider.is_maintenance == False,
        )

        if region:
            query = query.find(Provider.region == region)

        providers = await query.sort(+Provider.id).to_list()

        if not providers:
            return None

        # Try providers in round-robin order
        for i in range(len(providers)):
            self.last_provider_index = (self.last_provider_index + 1) % len(providers)
            provider = providers[self.last_provider_index]

            if provider.can_accommodate(cpu_cores, memory_gb, storage_gb):
                logger.info(
                    "provider_selected_round_robin",
                    provider_id=provider.id,
                    index=self.last_provider_index,
                )
                return provider

        return None


class ProviderSelector:
    """
    Provider Selector with pluggable strategies.

    Usage:
        selector = ProviderSelector(strategy=RegionAZStrategy())
        provider = await selector.select_provider(cpu, memory, storage, headers)
    """

    def __init__(self, strategy: Optional[ProviderSelectionStrategy] = None):
        """
        Initialize with a strategy.

        Args:
            strategy: Provider selection strategy (defaults to RegionAZStrategy)
        """
        self.strategy = strategy or RegionAZStrategy()

    def set_strategy(self, strategy: ProviderSelectionStrategy):
        """Change the selection strategy at runtime."""
        self.strategy = strategy

    async def select_provider(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        headers: Dict[str, str],
        **kwargs
    ) -> Optional[Provider]:
        """
        Select a provider using the current strategy.

        Args:
            cpu_cores: Required CPU cores
            memory_gb: Required memory in GB
            storage_gb: Required storage in GB
            headers: Request headers
            **kwargs: Additional criteria

        Returns:
            Selected Provider or None
        """
        return await self.strategy.select_provider(
            cpu_cores, memory_gb, storage_gb, headers, **kwargs
        )

    async def get_available_providers(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        region: Optional[str] = None,
        az: Optional[str] = None,
    ) -> List[Provider]:
        """
        Get all providers that can accommodate the requested resources.

        Useful for showing available options to users.
        """
        query = Provider.find(
            Provider.is_active == True,
            Provider.is_maintenance == False,
        )

        if region:
            query = query.find(Provider.region == region)
        if az:
            query = query.find(Provider.availability_zone == az)

        providers = await query.sort(-Provider.priority).to_list()

        return [
            p for p in providers
            if p.can_accommodate(cpu_cores, memory_gb, storage_gb)
        ]


# Global selector instance (can be configured at startup)
provider_selector = ProviderSelector()
