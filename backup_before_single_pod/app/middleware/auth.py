"""
Authentication middleware and dependencies.
"""
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config.logging import get_logger
from app.exceptions import AuthenticationError, AuthorizationError
from app.models.auth import TokenType, UserRole
from app.repositories.models import User, APIKey
from app.utils.security import decode_token, verify_api_key

logger = get_logger(__name__)

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    """
    Get current authenticated user from JWT token.

    Args:
        credentials: HTTP authorization credentials

    Returns:
        Current user

    Raises:
        HTTPException: If authentication fails
    """
    try:
        # Decode token
        token_payload = decode_token(credentials.credentials)

        # Verify it's an access token
        if token_payload.type != TokenType.ACCESS:
            raise AuthenticationError("Invalid token type")

        # Get user from database
        user = await User.find_one(User.id == token_payload.sub)

        if not user:
            raise AuthenticationError("User not found")

        if not user.is_active:
            raise AuthenticationError("User account is inactive")

        # Update last login
        user.last_login_at = None  # Will be updated in login endpoint

        logger.info(
            "user_authenticated",
            user_id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
        )

        return user

    except AuthenticationError as e:
        logger.warning("authentication_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        logger.error("authentication_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Get current active user (additional check).

    Args:
        current_user: Current user from token

    Returns:
        Current active user
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )
    return current_user


async def get_current_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Get current admin user.

    Args:
        current_user: Current user from token

    Returns:
        Current admin user

    Raises:
        HTTPException: If user is not admin
    """
    if current_user.role != UserRole.ADMIN:
        logger.warning(
            "unauthorized_admin_access",
            user_id=current_user.id,
            role=current_user.role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


class RoleChecker:
    """Dependency to check user role."""

    def __init__(self, allowed_roles: list[UserRole]):
        self.allowed_roles = allowed_roles

    async def __call__(self, current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in self.allowed_roles:
            logger.warning(
                "unauthorized_role_access",
                user_id=current_user.id,
                role=current_user.role,
                required_roles=[r.value for r in self.allowed_roles],
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required roles: {[r.value for r in self.allowed_roles]}",
            )
        return current_user
