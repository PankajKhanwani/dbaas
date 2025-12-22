"""
Security utilities for password hashing and JWT token management.
"""
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config.settings import settings
from app.config.logging import get_logger
from app.models.auth import TokenPayload, UserRole, TokenType
from app.exceptions import AuthenticationError

logger = get_logger(__name__)

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Hashed password
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a hash.

    Args:
        plain_password: Plain text password
        hashed_password: Hashed password

    Returns:
        True if password matches, False otherwise
    """
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    user_id: str,
    email: str,
    tenant_id: str,
    role: UserRole,
) -> str:
    """
    Create JWT access token.

    Args:
        user_id: User ID
        email: User email
        tenant_id: Tenant ID
        role: User role

    Returns:
        JWT access token
    """
    now = datetime.utcnow()
    expires = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)

    payload = {
        "sub": user_id,
        "email": email,
        "tenant_id": tenant_id,
        "role": role.value,
        "type": TokenType.ACCESS.value,
        "exp": expires,
        "iat": now,
    }

    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

    logger.info(
        "access_token_created",
        user_id=user_id,
        tenant_id=tenant_id,
        expires_at=expires.isoformat(),
    )

    return token


def create_refresh_token(
    user_id: str,
    email: str,
    tenant_id: str,
    role: UserRole,
) -> str:
    """
    Create JWT refresh token.

    Args:
        user_id: User ID
        email: User email
        tenant_id: Tenant ID
        role: User role

    Returns:
        JWT refresh token
    """
    now = datetime.utcnow()
    expires = now + timedelta(days=settings.jwt_refresh_token_expire_days)

    payload = {
        "sub": user_id,
        "email": email,
        "tenant_id": tenant_id,
        "role": role.value,
        "type": TokenType.REFRESH.value,
        "exp": expires,
        "iat": now,
    }

    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

    logger.info(
        "refresh_token_created",
        user_id=user_id,
        tenant_id=tenant_id,
        expires_at=expires.isoformat(),
    )

    return token


def decode_token(token: str) -> TokenPayload:
    """
    Decode and validate JWT token.

    Args:
        token: JWT token

    Returns:
        Token payload

    Raises:
        AuthenticationError: If token is invalid or expired
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
        )

        return TokenPayload(
            sub=payload["sub"],
            email=payload["email"],
            tenant_id=payload["tenant_id"],
            role=UserRole(payload["role"]),
            type=TokenType(payload["type"]),
            exp=datetime.fromtimestamp(payload["exp"]),
            iat=datetime.fromtimestamp(payload["iat"]),
        )

    except JWTError as e:
        logger.warning("token_decode_failed", error=str(e))
        raise AuthenticationError("Invalid or expired token")
    except Exception as e:
        logger.error("token_decode_error", error=str(e))
        raise AuthenticationError("Token validation failed")


def generate_api_key() -> str:
    """
    Generate a random API key.

    Returns:
        Random API key
    """
    import secrets
    return f"sk_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """
    Hash an API key for storage.

    Args:
        api_key: Plain API key

    Returns:
        Hashed API key
    """
    return hash_password(api_key)


def verify_api_key(plain_key: str, hashed_key: str) -> bool:
    """
    Verify an API key against a hash.

    Args:
        plain_key: Plain API key
        hashed_key: Hashed API key

    Returns:
        True if key matches, False otherwise
    """
    return verify_password(plain_key, hashed_key)
