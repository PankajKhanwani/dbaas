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


def _availability_score(provider: Provider, cpu_cores: float, memory_gb: float, storage_gb: float) -> float:
    """
    Compute a normalized availability score for a provider.

    Score = min(avail_cpu / req_cpu, avail_mem / req_mem, avail_storage / req_storage)

    This represents the minimum "headroom multiplier" across all resource dimensions.
    A score of 2.0 means the provider can serve this request twice over on every dimension.
    Higher is better — indicates more available capacity relative to the request.
    """
    avail_cpu = provider.get_available_cpu()
    avail_mem = provider.get_available_memory()
    avail_storage = provider.get_available_storage()

    # Avoid division by zero (should not happen due to can_accommodate guard, but defensive)
    if cpu_cores <= 0 or memory_gb <= 0 or storage_gb <= 0:
        return 0.0

    return min(
        avail_cpu / cpu_cores,
        avail_mem / memory_gb,
        avail_storage / storage_gb,
    )


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
        domain: Optional[str] = None,
        **kwargs
    ) -> Optional[Provider]:
        """
        Select a provider based on the strategy.

        Args:
            cpu_cores: Required CPU cores
            memory_gb: Required memory in GB
            storage_gb: Required storage in GB
            headers: Request headers containing az, region, etc.
            domain: Tenant domain for the database being created
            **kwargs: Additional criteria

        Returns:
            Selected Provider or None if no suitable provider found
        """
        pass


class DedicatedProviderStrategy(ProviderSelectionStrategy):
    """
    Strategy that prioritizes dedicated (domain-bound) providers.

    Behavior:
    - Phase 1: Look for providers dedicated to the requesting domain (provider.domain == domain).
      Among those, pick the one with the highest availability score.
      If dedicated providers exist but none can accommodate → ERROR (raise, no fallback).
    - Phase 2 (fallback): If NO dedicated providers exist for this domain at all,
      fall back to shared providers (provider.domain is None).
      Among shared providers, pick the one with the highest availability score.

    Availability Score: min(avail_cpu/req_cpu, avail_mem/req_mem, avail_storage/req_storage)
    Higher score = more headroom = preferred.

    Region/AZ headers are applied as additional filters in both phases.
    """

    async def select_provider(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        headers: Dict[str, str],
        domain: Optional[str] = None,
        **kwargs
    ) -> Optional[Provider]:
        region = headers.get("x-region") or headers.get("X-Region")
        az = headers.get("x-availability-zone") or headers.get("X-Availability-Zone")

        logger.info(
            "dedicated_provider_selection",
            domain=domain,
            region=region,
            az=az,
            cpu=cpu_cores,
            memory=memory_gb,
            storage=storage_gb,
        )

        # --- Phase 1: Dedicated providers for this domain ---
        if domain:
            dedicated_providers = await self._query_providers(
                domain=domain, region=region, az=az
            )

            if dedicated_providers:
                # Dedicated providers exist — MUST use one of them
                candidate = self._pick_best(dedicated_providers, cpu_cores, memory_gb, storage_gb)

                if candidate:
                    logger.info(
                        "dedicated_provider_selected",
                        provider_id=candidate.id,
                        provider_name=candidate.name,
                        domain=domain,
                        score=_availability_score(candidate, cpu_cores, memory_gb, storage_gb),
                    )
                    return candidate

                # Dedicated providers exist but none can accommodate → error out
                logger.warning(
                    "dedicated_providers_at_capacity",
                    domain=domain,
                    dedicated_count=len(dedicated_providers),
                    cpu=cpu_cores,
                    memory=memory_gb,
                    storage=storage_gb,
                )
                return None  # Caller (database_service) will raise ResourceAllocationError

        # --- Phase 2: Fall back to shared providers (domain=None) ---
        logger.info(
            "falling_back_to_shared_providers",
            domain=domain,
            reason="No dedicated providers found for this domain",
        )

        shared_providers = await self._query_providers(
            domain=None, region=region, az=az
        )

        candidate = self._pick_best(shared_providers, cpu_cores, memory_gb, storage_gb)

        if candidate:
            logger.info(
                "shared_provider_selected",
                provider_id=candidate.id,
                provider_name=candidate.name,
                score=_availability_score(candidate, cpu_cores, memory_gb, storage_gb),
            )
            return candidate

        logger.warning(
            "no_provider_found",
            domain=domain,
            region=region,
            az=az,
            cpu=cpu_cores,
            memory=memory_gb,
            storage=storage_gb,
        )
        return None

    async def _query_providers(
        self,
        domain: Optional[str],
        region: Optional[str],
        az: Optional[str],
    ) -> List[Provider]:
        """Query providers filtered by domain, region, and AZ."""
        query = Provider.find(
            Provider.is_active == True,
            Provider.is_maintenance == False,
        )

        # Domain filter: exact match for dedicated, explicit None for shared
        if domain is not None:
            query = query.find(Provider.domain == domain)
        else:
            query = query.find(Provider.domain == None)  # noqa: E711

        if region:
            query = query.find(Provider.region == region)
        if az:
            query = query.find(Provider.availability_zone == az)

        return await query.to_list()

    def _pick_best(
        self,
        providers: List[Provider],
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
    ) -> Optional[Provider]:
        """
        Among providers that can accommodate the request, pick the one
        with the highest normalized availability score.
        """
        candidates = []
        for provider in providers:
            if provider.can_accommodate(cpu_cores, memory_gb, storage_gb):
                score = _availability_score(provider, cpu_cores, memory_gb, storage_gb)
                candidates.append((provider, score))

        if not candidates:
            return None

        # Sort descending by score, then by priority as tiebreaker
        candidates.sort(key=lambda x: (x[1], x[0].priority), reverse=True)
        return candidates[0][0]


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
        domain: Optional[str] = None,
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
        domain: Optional[str] = None,
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
        domain: Optional[str] = None,
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
        selector = ProviderSelector(strategy=DedicatedProviderStrategy())
        provider = await selector.select_provider(cpu, memory, storage, headers, domain=domain)
    """

    def __init__(self, strategy: Optional[ProviderSelectionStrategy] = None):
        """
        Initialize with a strategy.

        Args:
            strategy: Provider selection strategy (defaults to DedicatedProviderStrategy)
        """
        self.strategy = strategy or DedicatedProviderStrategy()

    def set_strategy(self, strategy: ProviderSelectionStrategy):
        """Change the selection strategy at runtime."""
        self.strategy = strategy

    async def select_provider(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        headers: Dict[str, str],
        domain: Optional[str] = None,
        **kwargs
    ) -> Optional[Provider]:
        """
        Select a provider using the current strategy.

        Args:
            cpu_cores: Required CPU cores
            memory_gb: Required memory in GB
            storage_gb: Required storage in GB
            headers: Request headers
            domain: Tenant domain for dedicated provider lookup
            **kwargs: Additional criteria

        Returns:
            Selected Provider or None
        """
        return await self.strategy.select_provider(
            cpu_cores, memory_gb, storage_gb, headers, domain=domain, **kwargs
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