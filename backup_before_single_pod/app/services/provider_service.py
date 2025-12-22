"""
Provider Service for CRUD operations with MongoDB/Beanie.
"""
from typing import List, Optional
from datetime import datetime
import structlog

from app.repositories.models import Provider
from app.schemas.provider import ProviderCreate, ProviderUpdate
from app.core.exceptions import ProviderNotFoundError, InvalidProviderError

logger = structlog.get_logger()


async def _validate_kubernetes_connection(
    kubeconfig_content: Optional[str],
    kubeconfig_path: Optional[str],
    verify_ssl: bool = True
) -> None:
    """
    Validate that we can connect to the Kubernetes cluster.

    Args:
        kubeconfig_content: Kubeconfig content
        kubeconfig_path: Kubeconfig file path
        verify_ssl: Whether to verify SSL certificates

    Raises:
        InvalidProviderError: If connection fails
    """
    from app.services.kubedb_service import kubedb_service

    try:
        # Create a temporary client directly from kubeconfig for validation
        # Use a temporary provider_id for validation (won't be cached)
        temp_provider_id = "validation-temp"
        client_set = await kubedb_service._load_kubeconfig_from_provider(
            provider_id=temp_provider_id,
            kubeconfig_content=kubeconfig_content,
            verify_ssl=verify_ssl
        )

        logger.info("attempting_namespace_list_for_validation", verify_ssl=verify_ssl)

        # Try to list namespaces as a connection test
        await client_set.core_api.list_namespace()

        logger.info("kubernetes_connection_validated", verify_ssl=verify_ssl)

        # Clean up the temporary client
        await client_set.close()

    except Exception as e:
        logger.error(
            "kubernetes_connection_validation_failed",
            error=str(e),
            exc_info=True
        )
        raise InvalidProviderError(
            f"Failed to connect to Kubernetes cluster: {str(e)}. "
            "Please verify your kubeconfig is valid and the cluster is accessible."
        )


class ProviderService:
    """Service for managing providers (async MongoDB operations)."""

    @staticmethod
    async def create_provider(provider_data: ProviderCreate) -> Provider:
        """
        Create a new provider.

        Args:
            provider_data: Provider creation data

        Returns:
            Created Provider

        Raises:
            InvalidProviderError: If provider data is invalid, name exists, or connection fails
        """
        try:
            # Check if provider with same name already exists
            existing = await Provider.find_one(Provider.name == provider_data.name)
            if existing:
                raise InvalidProviderError(f"Provider with name '{provider_data.name}' already exists")

            # Validate Kubernetes connection before creating provider
            logger.info("validating_kubernetes_connection", name=provider_data.name, verify_ssl=provider_data.verify_ssl)
            await _validate_kubernetes_connection(
                kubeconfig_content=provider_data.kubeconfig_content,
                kubeconfig_path=provider_data.kubeconfig_path,
                verify_ssl=provider_data.verify_ssl
            )

            # Ensure kubeconfig is base64 encoded before storing
            import base64
            kubeconfig_to_store = provider_data.kubeconfig_content
            if kubeconfig_to_store:
                # Check if already base64 encoded by trying to decode
                try:
                    decoded = base64.b64decode(kubeconfig_to_store).decode('utf-8')
                    # If it decodes successfully and looks like YAML, it's already encoded
                    if 'apiVersion' not in decoded and 'clusters' not in decoded:
                        # Doesn't look like kubeconfig, probably wasn't encoded
                        kubeconfig_to_store = base64.b64encode(kubeconfig_to_store.encode('utf-8')).decode('utf-8')
                except Exception:
                    # Not base64 encoded, encode it
                    kubeconfig_to_store = base64.b64encode(kubeconfig_to_store.encode('utf-8')).decode('utf-8')

            provider = Provider(
                name=provider_data.name,
                region=provider_data.region,
                availability_zone=provider_data.availability_zone,
                cloud_provider=provider_data.cloud_provider,
                cpu_total_cores=provider_data.cpu_total_cores,
                memory_total_gb=provider_data.memory_total_gb,
                storage_total_gb=provider_data.storage_total_gb,
                cpu_reservation_percent=provider_data.cpu_reservation_percent,
                memory_reservation_percent=provider_data.memory_reservation_percent,
                storage_reservation_percent=provider_data.storage_reservation_percent,
                kubeconfig_path=provider_data.kubeconfig_path,
                kubeconfig_content=kubeconfig_to_store,
                api_endpoint=provider_data.api_endpoint,
                verify_ssl=provider_data.verify_ssl,
                is_active=provider_data.is_active,
                priority=provider_data.priority,
                tags=provider_data.tags or {},
                metadata=provider_data.metadata or {},
            )

            await provider.insert()

            logger.info("provider_created", provider_id=provider.id, name=provider.name)
            return provider

        except InvalidProviderError:
            raise
        except Exception as e:
            logger.error("provider_creation_failed", error=str(e))
            raise InvalidProviderError(f"Failed to create provider: {str(e)}")

    @staticmethod
    async def get_provider(provider_id: str) -> Provider:
        """
        Get a provider by ID.

        Args:
            provider_id: Provider ID

        Returns:
            Provider

        Raises:
            ProviderNotFoundError: If provider not found
        """
        provider = await Provider.find_one(Provider.id == provider_id)
        if not provider:
            raise ProviderNotFoundError(f"Provider {provider_id} not found")
        return provider

    @staticmethod
    async def list_providers(
        region: Optional[str] = None,
        availability_zone: Optional[str] = None,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Provider]:
        """
        List providers with optional filters.

        Args:
            region: Filter by region
            availability_zone: Filter by AZ
            is_active: Filter by active status
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of providers
        """
        query = Provider.find()

        if region:
            query = query.find(Provider.region == region)
        if availability_zone:
            query = query.find(Provider.availability_zone == availability_zone)
        if is_active is not None:
            query = query.find(Provider.is_active == is_active)

        providers = await query.skip(skip).limit(limit).to_list()
        return providers

    @staticmethod
    async def update_provider(
        provider_id: str, provider_data: ProviderUpdate
    ) -> Provider:
        """
        Update a provider.

        Args:
            provider_id: Provider ID
            provider_data: Update data

        Returns:
            Updated Provider

        Raises:
            ProviderNotFoundError: If provider not found
        """
        provider = await ProviderService.get_provider(provider_id)

        # Update fields if provided
        update_data = provider_data.dict(exclude_unset=True)

        # Ensure kubeconfig is base64 encoded before storing
        if 'kubeconfig_content' in update_data and update_data['kubeconfig_content']:
            import base64
            kubeconfig_to_store = update_data['kubeconfig_content']
            try:
                decoded = base64.b64decode(kubeconfig_to_store).decode('utf-8')
                # If it decodes successfully and looks like YAML, it's already encoded
                if 'apiVersion' not in decoded and 'clusters' not in decoded:
                    # Doesn't look like kubeconfig, probably wasn't encoded
                    kubeconfig_to_store = base64.b64encode(kubeconfig_to_store.encode('utf-8')).decode('utf-8')
            except Exception:
                # Not base64 encoded, encode it
                kubeconfig_to_store = base64.b64encode(kubeconfig_to_store.encode('utf-8')).decode('utf-8')

            update_data['kubeconfig_content'] = kubeconfig_to_store

        for field, value in update_data.items():
            setattr(provider, field, value)

        provider.updated_at = datetime.utcnow()
        await provider.save()

        # Invalidate cached client for this provider
        from app.services.kubedb_service import kubedb_service
        kubedb_service.invalidate_provider_cache(provider_id)

        logger.info("provider_updated", provider_id=provider.id)
        return provider

    @staticmethod
    async def delete_provider(provider_id: str) -> None:
        """
        Delete a provider.

        Args:
            provider_id: Provider ID

        Raises:
            ProviderNotFoundError: If provider not found
            ConflictError: If provider has active databases
        """
        from app.repositories.models import Database
        from app.exceptions import ConflictError

        provider = await ProviderService.get_provider(provider_id)

        # Check if any databases are using this provider
        database_count = await Database.find(
            Database.provider_id == provider_id
        ).count()

        if database_count > 0:
            raise ConflictError(
                f"Cannot delete provider '{provider.name}': {database_count} database(s) are still using this provider",
                details={
                    "provider_id": provider_id,
                    "provider_name": provider.name,
                    "database_count": database_count,
                },
            )

        await provider.delete()

        logger.info("provider_deleted", provider_id=provider_id)

    @staticmethod
    async def count_providers() -> int:
        """Count total providers."""
        return await Provider.count()


# Global instance
provider_service = ProviderService()
