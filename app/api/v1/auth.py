"""
Authentication and authorization API endpoints.
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.models.auth import (
    UserLogin,
    UserRegister,
    TokenResponse,
    APIKeyCreate,
    APIKeyResponse,
    UserResponse,
)
from app.config.logging import get_logger
from app.services.auth_service import auth_service
from app.middleware.auth import get_current_user
from app.repositories.models import User, APIKey
from app.utils.security import generate_api_key, hash_api_key

router = APIRouter()
security = HTTPBearer()
logger = get_logger(__name__)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserRegister):
    """
    Register a new user account.
    Creates a new tenant and user.

    - **email**: User email address
    - **password**: User password (min 8 characters)
    - **full_name**: User's full name
    - **organization**: Organization name (optional)
    """
    return await auth_service.register_user(user_data)


@router.post("/login", response_model=TokenResponse)
async def login(credentials: UserLogin):
    """
    Authenticate user and return JWT tokens.

    - **email**: User email
    - **password**: User password

    Returns access token and refresh token.
    """
    return await auth_service.login_user(credentials)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Refresh access token using refresh token.

    Pass refresh token in Authorization header as Bearer token.
    Returns new access token and refresh token.
    """
    return await auth_service.refresh_access_token(credentials.credentials)


@router.post("/api-keys", response_model=APIKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    api_key_data: APIKeyCreate,
    current_user: User = Depends(get_current_user),
):
    """
    Create a new API key for programmatic access.

    Requires authentication. Returns API key which should be stored securely.
    """
    # Generate API key
    plain_key = generate_api_key()
    key_hash = hash_api_key(plain_key)

    # Calculate expiration
    expires_at = None
    if api_key_data.expires_in_days:
        from datetime import timedelta

        expires_at = datetime.utcnow() + timedelta(days=api_key_data.expires_in_days)

    # Create API key document
    api_key = APIKey(
        name=api_key_data.name,
        key_hash=key_hash,
        description=api_key_data.description,
        scopes=api_key_data.scopes,
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expires_at=expires_at,
    )

    await api_key.save()

    logger.info(
        "api_key_created",
        api_key_id=api_key.id,
        user_id=current_user.id,
        name=api_key.name,
    )

    return APIKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key=plain_key,  # Only shown once
        description=api_key.description,
        scopes=api_key.scopes,
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """
    Get current authenticated user information.

    Returns user profile including tenant and role information.
    """
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        tenant_id=current_user.tenant_id,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        last_login_at=current_user.last_login_at,
    )
