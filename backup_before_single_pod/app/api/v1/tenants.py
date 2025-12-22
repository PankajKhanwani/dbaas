"""
Tenant management API endpoints.
"""
from fastapi import APIRouter, Depends, status

from app.models.tenant import TenantCreate, TenantUpdate, TenantResponse, ResourceQuota
from app.models.auth import UserRole
from app.config.logging import get_logger
from app.services.tenant_service import tenant_service
from app.middleware.auth import get_current_user, RoleChecker
from app.repositories.models import User
from app.exceptions import AuthorizationError

router = APIRouter()
logger = get_logger(__name__)


@router.post("/", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    tenant_data: TenantCreate,
    current_user: User = Depends(RoleChecker([UserRole.ADMIN])),
):
    """
    Create a new tenant (admin only).

    Only admin users can create new tenants.
    Regular users get a tenant created automatically during registration.
    """
    return await tenant_service.create_tenant(tenant_data)


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Get tenant by ID.

    Users can only view their own tenant unless they are admin.
    """
    # Check access
    if current_user.tenant_id != tenant_id and current_user.role != UserRole.ADMIN:
        raise AuthorizationError("Access denied to this tenant")

    return await tenant_service.get_tenant(tenant_id)


@router.patch("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: str,
    tenant_data: TenantUpdate,
    current_user: User = Depends(get_current_user),
):
    """
    Update tenant information.

    Users can update their own tenant. Admins can update any tenant.
    """
    # Check access
    if current_user.tenant_id != tenant_id and current_user.role != UserRole.ADMIN:
        raise AuthorizationError("Access denied to this tenant")

    return await tenant_service.update_tenant(tenant_id, tenant_data)


@router.get("/{tenant_id}/quota", response_model=ResourceQuota)
async def get_tenant_quota(
    tenant_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Get tenant resource quota and usage.

    Shows maximum allowed resources and current usage.
    Includes available capacity for each resource type.
    """
    # Check access
    if current_user.tenant_id != tenant_id and current_user.role != UserRole.ADMIN:
        raise AuthorizationError("Access denied to this tenant")

    return await tenant_service.get_quota(tenant_id)
