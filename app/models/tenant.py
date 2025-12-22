"""
Pydantic models for multi-tenancy.
"""
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel, Field


class TenantStatus(str, Enum):
    """Tenant account status."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    TRIAL = "trial"
    DELETED = "deleted"


class TenantPlan(str, Enum):
    """Tenant subscription plans."""

    FREE = "free"
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class ResourceQuota(BaseModel):
    """Resource quota for a tenant."""

    max_cpu_cores: int = Field(..., ge=1, description="Maximum CPU cores")
    max_memory_gb: int = Field(..., ge=1, description="Maximum memory in GB")
    max_storage_gb: int = Field(..., ge=10, description="Maximum storage in GB")
    max_databases: int = Field(..., ge=1, description="Maximum number of databases")
    max_backups: int = Field(..., ge=0, description="Maximum number of backups")

    # Used resources (tracked)
    used_cpu_cores: float = Field(default=0.0, description="Used CPU cores")
    used_memory_gb: float = Field(default=0.0, description="Used memory in GB")
    used_storage_gb: float = Field(default=0.0, description="Used storage in GB")
    used_databases: int = Field(default=0, description="Number of databases in use")
    used_backups: int = Field(default=0, description="Number of backups")

    @property
    def cpu_available(self) -> float:
        """Get available CPU cores."""
        return self.max_cpu_cores - self.used_cpu_cores

    @property
    def memory_available(self) -> float:
        """Get available memory in GB."""
        return self.max_memory_gb - self.used_memory_gb

    @property
    def storage_available(self) -> float:
        """Get available storage in GB."""
        return self.max_storage_gb - self.used_storage_gb

    @property
    def databases_available(self) -> int:
        """Get available database slots."""
        return self.max_databases - self.used_databases


class TenantCreate(BaseModel):
    """Request model for creating a tenant."""

    name: str = Field(..., min_length=3, max_length=100, description="Tenant name")
    slug: str = Field(
        ...,
        min_length=3,
        max_length=63,
        pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description="Tenant slug (URL-safe)",
    )
    organization: Optional[str] = Field(default=None, description="Organization name")
    plan: TenantPlan = Field(default=TenantPlan.FREE, description="Subscription plan")
    contact_email: Optional[str] = Field(default=None, description="Contact email")
    metadata: Optional[Dict[str, str]] = Field(default=None, description="Custom metadata")


class TenantUpdate(BaseModel):
    """Request model for updating a tenant."""

    name: Optional[str] = Field(default=None, min_length=3, max_length=100, description="Tenant name")
    plan: Optional[TenantPlan] = Field(default=None, description="Subscription plan")
    status: Optional[TenantStatus] = Field(default=None, description="Tenant status")
    contact_email: Optional[str] = Field(default=None, description="Contact email")
    metadata: Optional[Dict[str, str]] = Field(default=None, description="Custom metadata")


class TenantResponse(BaseModel):
    """Response model for tenant."""

    id: str = Field(..., description="Tenant ID")
    name: str = Field(..., description="Tenant name")
    slug: str = Field(..., description="Tenant slug")
    organization: Optional[str] = Field(default=None, description="Organization name")
    plan: TenantPlan = Field(..., description="Subscription plan")
    status: TenantStatus = Field(..., description="Tenant status")
    contact_email: Optional[str] = Field(default=None, description="Contact email")
    quota: ResourceQuota = Field(..., description="Resource quota")
    metadata: Dict[str, str] = Field(default_factory=dict, description="Custom metadata")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "tenant-xyz",
                "name": "Acme Corporation",
                "slug": "acme-corp",
                "organization": "Acme Inc",
                "plan": "professional",
                "status": "active",
                "contact_email": "admin@acme.com",
                "quota": {
                    "max_cpu_cores": 20,
                    "max_memory_gb": 40,
                    "max_storage_gb": 200,
                    "max_databases": 20,
                    "max_backups": 100,
                    "used_cpu_cores": 8.0,
                    "used_memory_gb": 16.0,
                    "used_storage_gb": 80.0,
                    "used_databases": 5,
                    "used_backups": 15,
                },
                "metadata": {"department": "engineering"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-15T10:30:00Z",
            }
        }
