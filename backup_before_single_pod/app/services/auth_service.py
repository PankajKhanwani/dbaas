"""
Authentication service.
Handles user registration, login, and token management.
"""
from datetime import datetime
from typing import Optional

from app.config.logging import get_logger
from app.exceptions import AuthenticationError, ConflictError, ValidationError
from app.models.auth import UserLogin, UserRegister, TokenResponse, UserRole
from app.models.tenant import TenantPlan, TenantStatus, ResourceQuota
from app.repositories.models import User, Tenant
from app.utils.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
)
from app.config.settings import settings

logger = get_logger(__name__)


class AuthService:
    """Service for authentication operations."""

    async def register_user(self, user_data: UserRegister) -> TokenResponse:
        """
        Register a new user and create a tenant.

        Args:
            user_data: User registration data

        Returns:
            Token response with access and refresh tokens

        Raises:
            ConflictError: If user already exists
        """
        # Check if user already exists
        existing_user = await User.find_one(User.email == user_data.email)
        if existing_user:
            logger.warning("registration_failed_user_exists", email=user_data.email)
            raise ConflictError("User with this email already exists")

        # Create tenant
        tenant_slug = user_data.email.split("@")[0].lower().replace(".", "-")
        existing_tenant = await Tenant.find_one(Tenant.slug == tenant_slug)

        # Make slug unique if it exists
        if existing_tenant:
            tenant_slug = f"{tenant_slug}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        tenant = Tenant(
            name=user_data.organization or user_data.full_name,
            slug=tenant_slug,
            organization=user_data.organization,
            plan=TenantPlan.FREE,
            status=TenantStatus.ACTIVE,
            contact_email=user_data.email,
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

        # Create user
        user = User(
            email=user_data.email,
            password_hash=hash_password(user_data.password),
            full_name=user_data.full_name,
            role=UserRole.ADMIN,  # First user is admin
            tenant_id=tenant.id,
            is_active=True,
        )
        await user.save()

        logger.info(
            "user_registered",
            user_id=user.id,
            email=user.email,
            tenant_id=tenant.id,
        )

        # Generate tokens
        access_token = create_access_token(
            user_id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role,
        )

        refresh_token = create_refresh_token(
            user_id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    async def login_user(self, credentials: UserLogin) -> TokenResponse:
        """
        Authenticate user and generate tokens.

        Args:
            credentials: User login credentials

        Returns:
            Token response with access and refresh tokens

        Raises:
            AuthenticationError: If credentials are invalid
        """
        # Find user by email
        user = await User.find_one(User.email == credentials.email)

        if not user:
            logger.warning("login_failed_user_not_found", email=credentials.email)
            raise AuthenticationError("Invalid email or password")

        # Verify password
        if not verify_password(credentials.password, user.password_hash):
            logger.warning("login_failed_invalid_password", email=credentials.email)
            raise AuthenticationError("Invalid email or password")

        # Check if user is active
        if not user.is_active:
            logger.warning("login_failed_inactive_user", email=credentials.email)
            raise AuthenticationError("User account is inactive")

        # Update last login
        user.last_login_at = datetime.utcnow()
        await user.save()

        logger.info(
            "user_logged_in",
            user_id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
        )

        # Generate tokens
        access_token = create_access_token(
            user_id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role,
        )

        refresh_token = create_refresh_token(
            user_id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    async def refresh_access_token(self, refresh_token: str) -> TokenResponse:
        """
        Refresh access token using refresh token.

        Args:
            refresh_token: Refresh token

        Returns:
            New token response

        Raises:
            AuthenticationError: If refresh token is invalid
        """
        from app.utils.security import decode_token
        from app.models.auth import TokenType

        # Decode refresh token
        token_payload = decode_token(refresh_token)

        # Verify it's a refresh token
        if token_payload.type != TokenType.REFRESH:
            raise AuthenticationError("Invalid token type")

        # Get user
        user = await User.find_one(User.id == token_payload.sub)

        if not user or not user.is_active:
            raise AuthenticationError("User not found or inactive")

        # Generate new tokens
        access_token = create_access_token(
            user_id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role,
        )

        new_refresh_token = create_refresh_token(
            user_id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role,
        )

        logger.info("token_refreshed", user_id=user.id)

        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            token_type="bearer",
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )


# Global instance
auth_service = AuthService()
