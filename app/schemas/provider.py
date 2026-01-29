"""
Pydantic schemas for Provider API requests and responses.
"""
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, validator
from datetime import datetime


class ResourceConfig(BaseModel):
    """Resource configuration for a provider."""
    total_cores: float = Field(..., gt=0, description="Total CPU cores")
    total_gb: float = Field(..., gt=0, description="Total capacity in GB")
    reservation_percent: int = Field(80, ge=0, le=100, description="Percentage that can be allocated (0-100)")


class ProviderResourcesBase(BaseModel):
    """Base schema for provider resources."""
    cpu_total_cores: float = Field(..., gt=0, description="Total CPU cores available")
    memory_total_gb: float = Field(..., gt=0, description="Total memory in GB")
    storage_total_gb: float = Field(..., gt=0, description="Total storage in GB")

    cpu_reservation_percent: int = Field(80, ge=0, le=100)
    memory_reservation_percent: int = Field(80, ge=0, le=100)
    storage_reservation_percent: int = Field(80, ge=0, le=100)


class ProviderCreate(ProviderResourcesBase):
    """Schema for creating a new provider."""
    name: str = Field(..., min_length=1, max_length=100)
    region: str = Field(..., min_length=1, max_length=50)
    availability_zone: Optional[str] = Field(None, max_length=50)
    cloud_provider: Optional[str] = Field(None, max_length=50)

    # Domain binding — set to bind this provider as dedicated to a specific tenant domain.
    # Providers without a domain are treated as shared/general providers.
    domain: Optional[str] = Field(None, max_length=100, description="Tenant domain this provider is dedicated to. Leave None for shared providers.")

    kubeconfig_path: Optional[str] = None
    kubeconfig_content: Optional[str] = None
    api_endpoint: Optional[str] = None

    is_active: bool = True
    priority: int = Field(100, ge=0, le=1000)
    verify_ssl: bool = Field(
        default=True,
        description="Verify SSL certificates (disable only for clusters with certificate issues)"
    )
    tags: Optional[Dict[str, Any]] = Field(default_factory=dict)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ProviderUpdate(BaseModel):
    """Schema for updating a provider."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    region: Optional[str] = None
    availability_zone: Optional[str] = None
    cloud_provider: Optional[str] = None
    domain: Optional[str] = Field(None, max_length=100, description="Tenant domain this provider is dedicated to. Set to None to make shared.")

    cpu_total_cores: Optional[float] = Field(None, gt=0)
    memory_total_gb: Optional[float] = Field(None, gt=0)
    storage_total_gb: Optional[float] = Field(None, gt=0)

    cpu_reservation_percent: Optional[int] = Field(None, ge=0, le=100)
    memory_reservation_percent: Optional[int] = Field(None, ge=0, le=100)
    storage_reservation_percent: Optional[int] = Field(None, ge=0, le=100)

    kubeconfig_path: Optional[str] = None
    kubeconfig_content: Optional[str] = None
    api_endpoint: Optional[str] = None

    is_active: Optional[bool] = None
    is_maintenance: Optional[bool] = None
    priority: Optional[int] = Field(None, ge=0, le=1000)
    tags: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class ResourceInfo(BaseModel):
    """Resource information with allocation details."""
    total_cores: Optional[float] = None
    total_gb: Optional[float] = None
    allocated_cores: Optional[float] = None
    allocated_gb: Optional[float] = None
    available_cores: Optional[float] = None
    available_gb: Optional[float] = None
    reservation_percent: int


class ProviderResources(BaseModel):
    """Detailed resource information for a provider."""
    cpu: ResourceInfo
    memory: ResourceInfo
    storage: ResourceInfo


class ProviderResponse(BaseModel):
    """Response schema for provider."""
    id: str
    name: str
    region: str
    availability_zone: Optional[str]
    cloud_provider: Optional[str]
    domain: Optional[str] = None
    resources: ProviderResources
    is_active: bool
    is_maintenance: bool
    priority: int
    tags: Dict[str, Any]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class ProviderListResponse(BaseModel):
    """Response schema for listing providers."""
    providers: list[ProviderResponse]
    total: int


class ProviderSelectionCriteria(BaseModel):
    """Criteria for selecting a provider."""
    region: Optional[str] = None
    availability_zone: Optional[str] = None
    required_cpu_cores: float = Field(..., gt=0)
    required_memory_gb: float = Field(..., gt=0)
    required_storage_gb: float = Field(..., gt=0)
    tags: Optional[Dict[str, Any]] = None


class ResourceAllocationRequest(BaseModel):
    """Request to allocate resources."""
    cpu_cores: float = Field(..., gt=0)
    memory_gb: float = Field(..., gt=0)
    storage_gb: float = Field(..., gt=0)


class ResourceAllocationResponse(BaseModel):
    """Response for resource allocation."""
    provider_id: str
    allocated: bool
    message: str
    resources: Optional[ProviderResources] = None
