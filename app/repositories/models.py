"""
Beanie document models for MongoDB collections.
These are the ORM models that map to MongoDB collections.
"""
from datetime import datetime
from typing import Dict, List, Optional, Any
from uuid import uuid4

from beanie import Document, Indexed
from pydantic import Field

from app.models.auth import UserRole
from app.models.database import DatabaseEngine, DatabaseStatus, DatabaseSize, BackupSchedule
from app.models.tenant import TenantStatus, TenantPlan, ResourceQuota


class Tenant(Document):
    """Tenant document model."""

    id: Indexed(str) = Field(default_factory=lambda: f"tenant-{uuid4().hex[:12]}")  # type: ignore
    name: str
    slug: Indexed(str, unique=True)  # type: ignore
    organization: Optional[str] = None
    plan: TenantPlan = TenantPlan.FREE
    status: TenantStatus = TenantStatus.ACTIVE
    contact_email: Optional[str] = None
    quota: ResourceQuota = Field(default_factory=lambda: ResourceQuota(
        max_cpu_cores=10,
        max_memory_gb=20,
        max_storage_gb=100,
        max_databases=10,
        max_backups=50,
    ))
    metadata: Dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "tenants"
        indexes = ["slug", "status", "plan"]


class User(Document):
    """User document model."""

    id: Indexed(str) = Field(default_factory=lambda: f"user-{uuid4().hex[:12]}")  # type: ignore
    email: Indexed(str, unique=True)  # type: ignore
    password_hash: str
    full_name: str
    role: UserRole = UserRole.USER
    tenant_id: Indexed(str)  # type: ignore
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: Optional[datetime] = None

    class Settings:
        name = "users"
        indexes = ["email", "tenant_id", "role"]


class APIKey(Document):
    """API key document model."""

    id: Indexed(str) = Field(default_factory=lambda: f"key-{uuid4().hex[:12]}")  # type: ignore
    name: str
    key_hash: str
    description: Optional[str] = None
    scopes: List[str] = Field(default_factory=lambda: ["read"])
    user_id: Indexed(str)  # type: ignore
    tenant_id: Indexed(str)  # type: ignore
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    is_active: bool = True

    class Settings:
        name = "api_keys"
        indexes = ["user_id", "tenant_id", "is_active"]


class Database(Document):
    """Database instance document model."""

    id: Indexed(str) = Field(default_factory=lambda: f"db-{uuid4().hex[:12]}")  # type: ignore
    name: Indexed(str)  # type: ignore
    domain: Indexed(str)  # type: ignore
    project: Indexed(str)  # type: ignore
    engine: DatabaseEngine
    version: str

    # Desired state (what user wants)
    size: DatabaseSize
    storage_gb: int
    replicas: int

    # Current state (what's actually running in KubeDB)
    current_size: Optional[DatabaseSize] = None
    current_storage_gb: Optional[int] = None
    current_replicas: Optional[int] = None

    ready_replicas: int = 0  # Actual running replicas
    status: DatabaseStatus = DatabaseStatus.PENDING
    backup_enabled: bool = True
    backup_schedule: Optional[BackupSchedule] = BackupSchedule.DAILY
    backup_retention_days: int = 7
    high_availability: bool = False
    monitoring_enabled: bool = True
    endpoint: Optional[str] = None
    port: Optional[int] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    health_status: Optional[str] = None
    kubedb_resource_name: Optional[str] = None
    namespace: str = "default"

    # Cached metrics (updated by background sync service every 30-60 seconds)
    metrics: Optional[Dict[str, Any]] = None
    metrics_last_updated: Optional[datetime] = None

    # Multi-provider support
    provider_id: Optional[Indexed(str)] = None  # type: ignore

    # Resource allocation tracking
    allocated_cpu_cores: Optional[float] = None
    allocated_memory_gb: Optional[float] = None
    allocated_storage_gb: Optional[float] = None

    class Settings:
        name = "databases"
        indexes = ["name", "domain", "project", "status", "engine", "provider_id"]


class AuditLog(Document):
    """Audit log document model."""

    id: str = Field(default_factory=lambda: f"audit-{uuid4().hex[:12]}")
    action: Indexed(str)  # type: ignore
    resource_type: str
    resource_id: str
    domain: Indexed(str)  # type: ignore
    project: Indexed(str)  # type: ignore
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_method: Optional[str] = None
    request_path: Optional[str] = None
    status_code: Optional[int] = None
    details: Dict = Field(default_factory=dict)
    timestamp: Indexed(datetime) = Field(default_factory=datetime.utcnow)  # type: ignore

    class Settings:
        name = "audit_logs"
        indexes = ["action", "domain", "project", "timestamp", "resource_type"]


class Provider(Document):
    """Provider (Kubernetes cluster) document model for multi-provider support."""

    id: Indexed(str) = Field(default_factory=lambda: f"provider-{uuid4().hex[:12]}")  # type: ignore
    name: Indexed(str, unique=True)  # type: ignore

    # Location
    region: Indexed(str)  # type: ignore
    availability_zone: Optional[Indexed(str)] = None  # type: ignore
    cloud_provider: Optional[str] = None

    # Domain binding (dedicated provider for a specific tenant domain)
    domain: Optional[Indexed(str)] = None  # type: ignore

    # Resource capacity (total)
    cpu_total_cores: float
    memory_total_gb: float
    storage_total_gb: float

    # Resource allocation (currently allocated)
    cpu_allocated_cores: float = 0.0
    memory_allocated_gb: float = 0.0
    storage_allocated_gb: float = 0.0

    # Reservation limits (percentage that can be allocated)
    cpu_reservation_percent: int = 80
    memory_reservation_percent: int = 80
    storage_reservation_percent: int = 80

    # Kubernetes configuration
    kubeconfig_path: Optional[str] = None
    kubeconfig_content: Optional[str] = None
    api_endpoint: Optional[str] = None
    verify_ssl: bool = True  # Verify SSL certificates (disable only for clusters with cert issues)

    # Status
    is_active: bool = True
    is_maintenance: bool = False
    priority: int = 100  # Higher priority providers are selected first

    # Metadata
    tags: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Version for optimistic locking (concurrency control)
    version: int = 0

    def get_available_cpu(self) -> float:
        """Calculate available CPU based on reservation limit."""
        max_allocatable = (self.cpu_total_cores * self.cpu_reservation_percent) / 100
        return max(0, max_allocatable - self.cpu_allocated_cores)

    def get_available_memory(self) -> float:
        """Calculate available memory based on reservation limit."""
        max_allocatable = (self.memory_total_gb * self.memory_reservation_percent) / 100
        return max(0, max_allocatable - self.memory_allocated_gb)

    def get_available_storage(self) -> float:
        """Calculate available storage based on reservation limit."""
        max_allocatable = (self.storage_total_gb * self.storage_reservation_percent) / 100
        return max(0, max_allocatable - self.storage_allocated_gb)

    def can_accommodate(self, cpu_cores: float, memory_gb: float, storage_gb: float) -> bool:
        """Check if provider can accommodate the requested resources."""
        if not self.is_active or self.is_maintenance:
            return False

        return (
            self.get_available_cpu() >= cpu_cores and
            self.get_available_memory() >= memory_gb and
            self.get_available_storage() >= storage_gb
        )

    def allocate_resources(self, cpu_cores: float, memory_gb: float, storage_gb: float) -> None:
        """
        DEPRECATED: Use allocate_resources_atomic() instead.

        This method is kept for backward compatibility but has race conditions.
        """
        self.cpu_allocated_cores += cpu_cores
        self.memory_allocated_gb += memory_gb
        self.storage_allocated_gb += storage_gb
        self.updated_at = datetime.utcnow()

    def deallocate_resources(self, cpu_cores: float, memory_gb: float, storage_gb: float) -> None:
        """
        DEPRECATED: Use deallocate_resources_atomic() instead.

        This method is kept for backward compatibility but has race conditions.
        """
        self.cpu_allocated_cores = max(0, self.cpu_allocated_cores - cpu_cores)
        self.memory_allocated_gb = max(0, self.memory_allocated_gb - memory_gb)
        self.storage_allocated_gb = max(0, self.storage_allocated_gb - storage_gb)
        self.updated_at = datetime.utcnow()

    async def allocate_resources_atomic(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        max_retries: int = 3
    ) -> bool:
        """
        Atomically allocate resources with optimistic locking.

        Uses MongoDB atomic operations ($inc) with version checking to prevent
        race conditions during concurrent resource allocation.

        Args:
            cpu_cores: CPU cores to allocate
            memory_gb: Memory in GB to allocate
            storage_gb: Storage in GB to allocate
            max_retries: Maximum number of retry attempts on version conflict

        Returns:
            True if allocated successfully, False if failed (out of capacity or max retries)
        """
        import asyncio
        from datetime import datetime, timezone
        from app.config.logging import get_logger

        logger = get_logger(__name__)

        for attempt in range(max_retries):
            # Step 1: Check if we can allocate (capacity check with current values)
            if not self.can_accommodate(cpu_cores, memory_gb, storage_gb):
                logger.warning(
                    "provider_insufficient_capacity",
                    provider_id=self.id,
                    requested_cpu=cpu_cores,
                    available_cpu=self.get_available_cpu(),
                    requested_memory=memory_gb,
                    available_memory=self.get_available_memory(),
                    requested_storage=storage_gb,
                    available_storage=self.get_available_storage(),
                )
                return False

            # Step 2: Atomic update with version check (optimistic locking)
            result = await Provider.find_one({
                "_id": self.id,
                "version": self.version  # Optimistic lock - only update if version matches
            }).update({
                "$inc": {
                    "cpu_allocated_cores": cpu_cores,
                    "memory_allocated_gb": memory_gb,
                    "storage_allocated_gb": storage_gb,
                    "version": 1  # Increment version atomically
                },
                "$set": {
                    "updated_at": datetime.now(timezone.utc)
                }
            })

            if result.modified_count > 0:
                # Success! Update local instance to match database
                self.cpu_allocated_cores += cpu_cores
                self.memory_allocated_gb += memory_gb
                self.storage_allocated_gb += storage_gb
                self.version += 1
                self.updated_at = datetime.now(timezone.utc)

                logger.info(
                    "resources_allocated_successfully",
                    provider_id=self.id,
                    cpu=cpu_cores,
                    memory=memory_gb,
                    storage=storage_gb,
                    new_cpu_allocated=self.cpu_allocated_cores,
                    new_memory_allocated=self.memory_allocated_gb,
                    new_storage_allocated=self.storage_allocated_gb,
                    attempt=attempt + 1,
                )
                return True
            else:
                # Version conflict - someone else modified the provider between our read and update
                logger.warning(
                    "resource_allocation_version_conflict",
                    provider_id=self.id,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    message="Another process modified provider, retrying with fresh data"
                )

                # Reload provider to get latest values
                updated_provider = await Provider.get(self.id)
                if not updated_provider:
                    logger.error("provider_disappeared", provider_id=self.id)
                    return False

                # Update local instance with fresh data from database
                self.cpu_allocated_cores = updated_provider.cpu_allocated_cores
                self.memory_allocated_gb = updated_provider.memory_allocated_gb
                self.storage_allocated_gb = updated_provider.storage_allocated_gb
                self.version = updated_provider.version
                self.updated_at = updated_provider.updated_at

                # Retry after exponential backoff delay
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))  # 100ms, 200ms, 300ms

        logger.error(
            "resource_allocation_failed_after_retries",
            provider_id=self.id,
            max_retries=max_retries,
            cpu_requested=cpu_cores,
            memory_requested=memory_gb,
            storage_requested=storage_gb,
        )
        return False

    async def deallocate_resources_atomic(
        self,
        cpu_cores: float,
        memory_gb: float,
        storage_gb: float,
        max_retries: int = 3
    ) -> bool:
        """
        Atomically deallocate resources with optimistic locking.

        Uses MongoDB atomic operations ($inc with negative values) with version
        checking to prevent race conditions during concurrent resource deallocation.

        Args:
            cpu_cores: CPU cores to deallocate
            memory_gb: Memory in GB to deallocate
            storage_gb: Storage in GB to deallocate
            max_retries: Maximum number of retry attempts on version conflict

        Returns:
            True if deallocated successfully, False if failed
        """
        import asyncio
        from datetime import datetime, timezone
        from app.config.logging import get_logger

        logger = get_logger(__name__)

        for attempt in range(max_retries):
            # Atomic update with version check
            result = await Provider.find_one({
                "_id": self.id,
                "version": self.version
            }).update({
                "$inc": {
                    "cpu_allocated_cores": -cpu_cores,
                    "memory_allocated_gb": -memory_gb,
                    "storage_allocated_gb": -storage_gb,
                    "version": 1
                },
                "$set": {
                    "updated_at": datetime.now(timezone.utc)
                }
            })

            if result.modified_count > 0:
                # Success! Update local instance
                self.cpu_allocated_cores = max(0, self.cpu_allocated_cores - cpu_cores)
                self.memory_allocated_gb = max(0, self.memory_allocated_gb - memory_gb)
                self.storage_allocated_gb = max(0, self.storage_allocated_gb - storage_gb)
                self.version += 1
                self.updated_at = datetime.now(timezone.utc)

                logger.info(
                    "resources_deallocated_successfully",
                    provider_id=self.id,
                    cpu=cpu_cores,
                    memory=memory_gb,
                    storage=storage_gb,
                    new_cpu_allocated=self.cpu_allocated_cores,
                    new_memory_allocated=self.memory_allocated_gb,
                    new_storage_allocated=self.storage_allocated_gb,
                )

                # Fix any negative values that might have occurred
                await self._fix_negative_allocations()
                return True
            else:
                # Version conflict - reload and retry
                logger.warning(
                    "resource_deallocation_version_conflict",
                    provider_id=self.id,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                )

                updated_provider = await Provider.get(self.id)
                if not updated_provider:
                    logger.error("provider_disappeared_during_deallocation", provider_id=self.id)
                    return False

                self.cpu_allocated_cores = updated_provider.cpu_allocated_cores
                self.memory_allocated_gb = updated_provider.memory_allocated_gb
                self.storage_allocated_gb = updated_provider.storage_allocated_gb
                self.version = updated_provider.version
                self.updated_at = updated_provider.updated_at

                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))

        logger.error(
            "resource_deallocation_failed_after_retries",
            provider_id=self.id,
            max_retries=max_retries,
        )
        return False

    async def _fix_negative_allocations(self) -> None:
        """
        Fix any negative allocation values that might have occurred.

        This can happen if deallocation is called with values larger than
        what's currently allocated (e.g., due to manual DB edits or bugs).
        """
        from app.config.logging import get_logger

        logger = get_logger(__name__)
        update_needed = False
        update_dict = {}

        if self.cpu_allocated_cores < 0:
            update_dict["cpu_allocated_cores"] = 0
            update_needed = True

        if self.memory_allocated_gb < 0:
            update_dict["memory_allocated_gb"] = 0
            update_needed = True

        if self.storage_allocated_gb < 0:
            update_dict["storage_allocated_gb"] = 0
            update_needed = True

        if update_needed:
            await Provider.find_one({"_id": self.id}).update({"$set": update_dict})
            logger.warning(
                "fixed_negative_allocations",
                provider_id=self.id,
                updates=update_dict,
                message="Negative resource allocations detected and fixed to zero"
            )

    class Settings:
        name = "providers"
        indexes = ["name", "region", "availability_zone", "domain", "is_active", "priority"]
