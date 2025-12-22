"""
Tenant service.
Handles tenant management and quota operations.
"""
from typing import Optional

from app.config.logging import get_logger
from app.exceptions import NotFoundError, ConflictError, QuotaExceededError
from app.models.tenant import TenantCreate, TenantUpdate, TenantResponse, ResourceQuota
from app.repositories.models import Tenant, Database

logger = get_logger(__name__)


class TenantService:
    """Service for tenant operations."""

    async def create_tenant(self, tenant_data: TenantCreate) -> TenantResponse:
        """
        Create a new tenant.

        Args:
            tenant_data: Tenant creation data

        Returns:
            Created tenant

        Raises:
            ConflictError: If tenant slug already exists
        """
        # Check if slug already exists
        existing = await Tenant.find_one(Tenant.slug == tenant_data.slug)
        if existing:
            raise ConflictError(f"Tenant with slug '{tenant_data.slug}' already exists")

        # Create tenant with default quota based on plan
        from app.config.settings import settings

        tenant = Tenant(
            name=tenant_data.name,
            slug=tenant_data.slug,
            organization=tenant_data.organization,
            plan=tenant_data.plan,
            contact_email=tenant_data.contact_email,
            metadata=tenant_data.metadata or {},
            quota=ResourceQuota(
                max_cpu_cores=settings.default_tenant_quota_cpu,
                max_memory_gb=settings.default_tenant_quota_memory_gb,
                max_storage_gb=settings.default_tenant_quota_storage_gb,
                max_databases=settings.default_tenant_max_databases,
                max_backups=50,
            ),
        )

        await tenant.save()

        logger.info("tenant_created", tenant_id=tenant.id, slug=tenant.slug)

        return self._to_response(tenant)

    async def get_tenant(self, tenant_id: str) -> TenantResponse:
        """
        Get tenant by ID.

        Args:
            tenant_id: Tenant ID

        Returns:
            Tenant details

        Raises:
            NotFoundError: If tenant not found
        """
        tenant = await Tenant.find_one(Tenant.id == tenant_id)

        if not tenant:
            raise NotFoundError(f"Tenant {tenant_id} not found")

        # Update quota usage
        await self._update_quota_usage(tenant)

        return self._to_response(tenant)

    async def update_tenant(
        self, tenant_id: str, tenant_data: TenantUpdate
    ) -> TenantResponse:
        """
        Update tenant.

        Args:
            tenant_id: Tenant ID
            tenant_data: Update data

        Returns:
            Updated tenant

        Raises:
            NotFoundError: If tenant not found
        """
        tenant = await Tenant.find_one(Tenant.id == tenant_id)

        if not tenant:
            raise NotFoundError(f"Tenant {tenant_id} not found")

        # Update fields
        if tenant_data.name is not None:
            tenant.name = tenant_data.name
        if tenant_data.plan is not None:
            tenant.plan = tenant_data.plan
        if tenant_data.status is not None:
            tenant.status = tenant_data.status
        if tenant_data.contact_email is not None:
            tenant.contact_email = tenant_data.contact_email
        if tenant_data.metadata is not None:
            tenant.metadata.update(tenant_data.metadata)

        tenant.updated_at = None  # Will be set by Beanie

        await tenant.save()

        logger.info("tenant_updated", tenant_id=tenant.id)

        return self._to_response(tenant)

    async def get_quota(self, tenant_id: str) -> ResourceQuota:
        """
        Get tenant quota and usage.

        Args:
            tenant_id: Tenant ID

        Returns:
            Resource quota with usage

        Raises:
            NotFoundError: If tenant not found
        """
        tenant = await Tenant.find_one(Tenant.id == tenant_id)

        if not tenant:
            raise NotFoundError(f"Tenant {tenant_id} not found")

        await self._update_quota_usage(tenant)

        return tenant.quota

    async def check_quota(
        self,
        tenant_id: str,
        cpu: float = 0,
        memory: float = 0,
        storage: float = 0,
        databases: int = 0,
    ) -> bool:
        """
        Check if tenant has available quota.

        Args:
            tenant_id: Tenant ID
            cpu: Required CPU cores
            memory: Required memory GB
            storage: Required storage GB
            databases: Required database count

        Returns:
            True if quota is available

        Raises:
            QuotaExceededError: If quota exceeded
        """
        tenant = await Tenant.find_one(Tenant.id == tenant_id)

        if not tenant:
            raise NotFoundError(f"Tenant {tenant_id} not found")

        await self._update_quota_usage(tenant)

        quota = tenant.quota

        # Check each resource
        if cpu > 0 and quota.used_cpu_cores + cpu > quota.max_cpu_cores:
            raise QuotaExceededError(
                f"CPU quota exceeded. Available: {quota.cpu_available}, Required: {cpu}"
            )

        if memory > 0 and quota.used_memory_gb + memory > quota.max_memory_gb:
            raise QuotaExceededError(
                f"Memory quota exceeded. Available: {quota.memory_available}GB, Required: {memory}GB"
            )

        if storage > 0 and quota.used_storage_gb + storage > quota.max_storage_gb:
            raise QuotaExceededError(
                f"Storage quota exceeded. Available: {quota.storage_available}GB, Required: {storage}GB"
            )

        if databases > 0 and quota.used_databases + databases > quota.max_databases:
            raise QuotaExceededError(
                f"Database quota exceeded. Available: {quota.databases_available}, Required: {databases}"
            )

        return True

    async def _update_quota_usage(self, tenant: Tenant) -> None:
        """Update tenant quota usage from actual database usage."""
        # Get all databases for tenant
        databases = await Database.find(
            Database.tenant_id == tenant.id,
            Database.status.nin(["deleted", "failed"]),
        ).to_list()

        # Calculate usage
        total_cpu = 0.0
        total_memory = 0.0
        total_storage = 0.0

        for db in databases:
            # Parse size to get resources
            size_config = self._get_size_resources(db.size)
            total_cpu += float(size_config["cpu"].rstrip("m")) / 1000 * db.replicas
            total_memory += float(size_config["memory"].rstrip("Gi")) * db.replicas
            total_storage += db.storage_gb

        # Update quota
        tenant.quota.used_cpu_cores = total_cpu
        tenant.quota.used_memory_gb = total_memory
        tenant.quota.used_storage_gb = total_storage
        tenant.quota.used_databases = len(databases)

        await tenant.save()

    def _get_size_resources(self, size: str) -> dict:
        """Get resource specs for database size."""
        from app.models.database import DatabaseSize

        size_configs = {
            DatabaseSize.MICRO: {"cpu": "500m", "memory": "1Gi"},
            DatabaseSize.SMALL: {"cpu": "1000m", "memory": "2Gi"},
            DatabaseSize.MEDIUM: {"cpu": "2000m", "memory": "4Gi"},
            DatabaseSize.LARGE: {"cpu": "2000m", "memory": "8Gi"},
            DatabaseSize.XLARGE: {"cpu": "4000m", "memory": "16Gi"},
            DatabaseSize.XXLARGE: {"cpu": "8000m", "memory": "32Gi"},
        }
        return size_configs.get(size, size_configs[DatabaseSize.SMALL])

    def _to_response(self, tenant: Tenant) -> TenantResponse:
        """Convert tenant document to response model."""
        return TenantResponse(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            organization=tenant.organization,
            plan=tenant.plan,
            status=tenant.status,
            contact_email=tenant.contact_email,
            quota=tenant.quota,
            metadata=tenant.metadata,
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
        )


# Global instance
tenant_service = TenantService()
