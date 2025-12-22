"""
Pydantic models for authentication and authorization.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


class UserRole(str, Enum):
    """User roles for RBAC."""

    ADMIN = "admin"
    USER = "user"
    VIEWER = "viewer"


class TokenType(str, Enum):
    """Token types."""

    ACCESS = "access"
    REFRESH = "refresh"


class UserLogin(BaseModel):
    """User login request."""

    email: EmailStr = Field(..., description="User email")
    password: str = Field(..., min_length=8, description="User password")


class UserRegister(BaseModel):
    """User registration request."""

    email: EmailStr = Field(..., description="User email")
    password: str = Field(..., min_length=8, description="User password")
    full_name: str = Field(..., min_length=2, max_length=100, description="Full name")
    organization: Optional[str] = Field(default=None, description="Organization name")


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str = Field(..., description="JWT access token")
    refresh_token: str = Field(..., description="JWT refresh token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiration in seconds")


class TokenPayload(BaseModel):
    """JWT token payload."""

    sub: str = Field(..., description="Subject (user ID)")
    email: str = Field(..., description="User email")
    tenant_id: str = Field(..., description="Tenant ID")
    role: UserRole = Field(..., description="User role")
    type: TokenType = Field(..., description="Token type")
    exp: datetime = Field(..., description="Expiration time")
    iat: datetime = Field(..., description="Issued at time")


class APIKeyCreate(BaseModel):
    """API key creation request."""

    name: str = Field(..., min_length=3, max_length=100, description="API key name")
    description: Optional[str] = Field(default=None, description="API key description")
    expires_in_days: Optional[int] = Field(
        default=None, ge=1, le=365, description="Expiration in days"
    )
    scopes: List[str] = Field(default=["read"], description="API key scopes")


class APIKeyResponse(BaseModel):
    """API key response."""

    id: str = Field(..., description="API key ID")
    name: str = Field(..., description="API key name")
    key: str = Field(..., description="API key (only shown once)")
    description: Optional[str] = Field(default=None, description="Description")
    scopes: List[str] = Field(..., description="Scopes")
    created_at: datetime = Field(..., description="Creation timestamp")
    expires_at: Optional[datetime] = Field(default=None, description="Expiration timestamp")
    last_used_at: Optional[datetime] = Field(default=None, description="Last used timestamp")


class UserResponse(BaseModel):
    """User response model."""

    id: str = Field(..., description="User ID")
    email: EmailStr = Field(..., description="User email")
    full_name: str = Field(..., description="Full name")
    role: UserRole = Field(..., description="User role")
    tenant_id: str = Field(..., description="Tenant ID")
    is_active: bool = Field(..., description="Account active status")
    created_at: datetime = Field(..., description="Creation timestamp")
    last_login_at: Optional[datetime] = Field(default=None, description="Last login timestamp")
