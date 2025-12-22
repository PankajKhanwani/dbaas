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
        """Allocate resources (increase allocated counters)."""
        self.cpu_allocated_cores += cpu_cores
        self.memory_allocated_gb += memory_gb
        self.storage_allocated_gb += storage_gb
        self.updated_at = datetime.utcnow()

    def deallocate_resources(self, cpu_cores: float, memory_gb: float, storage_gb: float) -> None:
        """Deallocate resources (decrease allocated counters)."""
        self.cpu_allocated_cores = max(0, self.cpu_allocated_cores - cpu_cores)
        self.memory_allocated_gb = max(0, self.memory_allocated_gb - memory_gb)
        self.storage_allocated_gb = max(0, self.storage_allocated_gb - storage_gb)
        self.updated_at = datetime.utcnow()

    class Settings:
        name = "providers"
        indexes = ["name", "region", "availability_zone", "is_active", "priority"]
