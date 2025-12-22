"""
Pydantic models for database resources.
"""
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class DatabaseEngine(str, Enum):
    """Supported database engines."""

    POSTGRES = "postgres"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    MONGODB = "mongodb"
    REDIS = "redis"
    ELASTICSEARCH = "elasticsearch"


class DatabaseStatus(str, Enum):
    """Database instance status."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    UPDATING = "updating"
    SCALING = "scaling"
    PAUSED = "paused"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class DatabaseSize(str, Enum):
    """Database instance sizes (similar to AWS RDS)."""

    MICRO = "db.t3.micro"  # 1 vCPU, 1GB RAM
    SMALL = "db.t3.small"  # 1 vCPU, 2GB RAM
    MEDIUM = "db.t3.medium"  # 2 vCPU, 4GB RAM
    LARGE = "db.t3.large"  # 2 vCPU, 8GB RAM
    XLARGE = "db.t3.xlarge"  # 4 vCPU, 16GB RAM
    XXLARGE = "db.t3.2xlarge"  # 8 vCPU, 32GB RAM


class BackupSchedule(str, Enum):
    """Backup schedule frequency."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


class UpgradeStrategy(str, Enum):
    """Version upgrade strategy."""

    DISABLED = "disabled"  # No automatic upgrades
    LATEST_PATCH = "latest_patch"  # Auto-upgrade to latest patch version (e.g., 8.0.35 -> 8.0.36)
    LATEST_MINOR = "latest_minor"  # Auto-upgrade to latest minor version (e.g., 8.0.x -> 8.2.x)
    SPECIFIC_VERSION = "specific_version"  # Upgrade to a specific target version


class UpgradePolicy(BaseModel):
    """Version upgrade policy configuration."""

    strategy: UpgradeStrategy = Field(
        default=UpgradeStrategy.DISABLED, description="Upgrade strategy"
    )
    target_version: Optional[str] = Field(
        default=None, description="Target version (required if strategy is SPECIFIC_VERSION)"
    )
    maintenance_window: Optional[str] = Field(
        default=None,
        description="Maintenance window for upgrades (cron format, e.g., '0 2 * * 0' for Sundays 2 AM)",
    )
    auto_approve: bool = Field(
        default=False,
        description="Auto-approve upgrades without manual confirmation",
    )
    pre_upgrade_backup: bool = Field(
        default=True, description="Create backup before upgrading"
    )


class VersionUpgradeRequest(BaseModel):
    """Request model for upgrading database version."""

    target_version: str = Field(..., description="Target version to upgrade to")
    skip_backup: bool = Field(
        default=False, description="Skip pre-upgrade backup (not recommended)"
    )
    apply_immediately: bool = Field(
        default=False,
        description="Apply upgrade immediately, ignore maintenance window",
    )


class DatabaseCreateRequest(BaseModel):
    """Request model for creating a database."""

    name: str = Field(
        ...,
        min_length=3,
        max_length=63,
        pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description="Database name (DNS-1123 compliant)",
    )
    engine: DatabaseEngine = Field(..., description="Database engine type")
    version: str = Field(..., description="Database version (e.g., '15.0', '8.0', '6.0')")
    size: DatabaseSize = Field(default=DatabaseSize.MICRO, description="Instance size")
    storage_gb: int = Field(default=20, ge=10, le=10000, description="Storage size in GB")
    replicas: int = Field(default=1, ge=1, le=9, description="Number of replicas")
    backup_enabled: bool = Field(default=True, description="Enable automated backups")
    backup_schedule: Optional[BackupSchedule] = Field(
        default=BackupSchedule.DAILY, description="Backup schedule"
    )
    backup_retention_days: int = Field(default=7, ge=1, le=365, description="Backup retention days")
    high_availability: bool = Field(default=False, description="Enable high availability")
    monitoring_enabled: bool = Field(default=True, description="Enable monitoring")
    username: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=63,
        description="Database username (optional, auto-generated if not provided)"
    )
    password: Optional[str] = Field(
        default=None,
        min_length=8,
        description="Database password (optional, auto-generated if not provided)"
    )
    upgrade_policy: Optional[UpgradePolicy] = Field(
        default=None, description="Automated version upgrade policy"
    )
    labels: Optional[Dict[str, str]] = Field(default=None, description="Custom labels")
    annotations: Optional[Dict[str, str]] = Field(default=None, description="Custom annotations")

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        """Validate version format."""
        if not v or len(v) < 3:
            raise ValueError("Version must be specified (e.g., '15.0', '8.0')")
        return v


class DatabaseUpdateRequest(BaseModel):
    """Request model for updating a database."""

    size: Optional[DatabaseSize] = Field(default=None, description="New instance size")
    storage_gb: Optional[int] = Field(default=None, ge=10, le=10000, description="New storage size")
    replicas: Optional[int] = Field(default=None, ge=1, le=9, description="New replica count")
    backup_enabled: Optional[bool] = Field(default=None, description="Enable/disable backups")
    backup_schedule: Optional[BackupSchedule] = Field(default=None, description="Backup schedule")
    monitoring_enabled: Optional[bool] = Field(default=None, description="Enable/disable monitoring")
    upgrade_policy: Optional[UpgradePolicy] = Field(
        default=None, description="Update automated version upgrade policy"
    )
    labels: Optional[Dict[str, str]] = Field(default=None, description="Update labels")
    annotations: Optional[Dict[str, str]] = Field(default=None, description="Update annotations")


class DatabaseScaleRequest(BaseModel):
    """Request model for scaling a database."""

    replicas: Optional[int] = Field(default=None, ge=1, le=9, description="New replica count")
    storage_gb: Optional[int] = Field(default=None, ge=10, le=10000, description="New storage size")
    size: Optional[DatabaseSize] = Field(default=None, description="New instance size")


class DatabaseCredentials(BaseModel):
    """Database connection credentials."""

    username: str = Field(..., description="Database username")
    password: str = Field(..., description="Database password")
    host: str = Field(..., description="Database host")
    port: int = Field(..., description="Database port")
    database: str = Field(..., description="Database name")
    connection_string: str = Field(..., description="Full connection string")


class DatabaseResponse(BaseModel):
    """Response model for database resource."""

    id: str = Field(..., description="Database unique identifier")
    name: str = Field(..., description="Database name")
    domain: str = Field(..., description="Domain name")
    project: str = Field(..., description="Project name")
    engine: DatabaseEngine = Field(..., description="Database engine")
    version: str = Field(..., description="Database version")
    size: DatabaseSize = Field(..., description="Instance size")
    storage_gb: int = Field(..., description="Storage size in GB")
    replicas: int = Field(..., description="Desired number of replicas")
    ready_replicas: int = Field(default=0, description="Number of ready replicas")
    status: DatabaseStatus = Field(..., description="Current status")
    backup_enabled: bool = Field(..., description="Backup status")
    backup_schedule: Optional[BackupSchedule] = Field(default=None, description="Backup schedule")
    high_availability: bool = Field(..., description="HA enabled")
    monitoring_enabled: bool = Field(..., description="Monitoring enabled")
    endpoint: Optional[str] = Field(default=None, description="Connection endpoint")
    port: Optional[int] = Field(default=None, description="Connection port")
    labels: Dict[str, str] = Field(default_factory=dict, description="Labels")
    annotations: Dict[str, str] = Field(default_factory=dict, description="Annotations")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    health_status: Optional[str] = Field(default=None, description="Health status")
    upgrade_policy: Optional[UpgradePolicy] = Field(
        default=None, description="Automated version upgrade policy"
    )
    available_upgrades: List[str] = Field(
        default_factory=list, description="List of available upgrade versions"
    )
    operation_id: Optional[str] = Field(default=None, description="Active operation ID (if any)")
    poll_url: Optional[str] = Field(default=None, description="URL to poll operation status")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "db-abc123",
                "name": "my-postgres-db",
                "domain": "example-domain",
                "project": "production-project",
                "engine": "postgres",
                "version": "15.0",
                "size": "db.t3.medium",
                "storage_gb": 50,
                "replicas": 3,
                "status": "running",
                "backup_enabled": True,
                "backup_schedule": "daily",
                "high_availability": True,
                "monitoring_enabled": True,
                "endpoint": "my-postgres-db.default.svc.cluster.local",
                "port": 5432,
                "labels": {"environment": "production"},
                "annotations": {},
                "created_at": "2024-01-15T10:30:00Z",
                "updated_at": "2024-01-15T10:30:00Z",
                "health_status": "healthy",
            }
        }


class DatabaseListResponse(BaseModel):
    """Response model for list of databases."""

    databases: List[DatabaseResponse] = Field(..., description="List of databases")
    total: int = Field(..., description="Total count")
    page: int = Field(default=1, description="Current page")
    page_size: int = Field(default=10, description="Page size")


class DatabaseMetrics(BaseModel):
    """Database performance metrics."""

    cpu_usage_percent: float = Field(..., description="CPU usage percentage")
    memory_usage_percent: float = Field(..., description="Memory usage percentage")
    storage_usage_percent: float = Field(..., description="Storage usage percentage")
    connections_active: int = Field(..., description="Active connections")
    connections_max: int = Field(..., description="Maximum connections")
    iops_read: float = Field(..., description="Read IOPS")
    iops_write: float = Field(..., description="Write IOPS")
    network_in_mbps: float = Field(..., description="Network in (Mbps)")
    network_out_mbps: float = Field(..., description="Network out (Mbps)")
    timestamp: datetime = Field(..., description="Metrics timestamp")
