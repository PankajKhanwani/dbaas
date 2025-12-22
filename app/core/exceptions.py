"""
Custom exception classes for provider and resource management.
"""
from typing import Optional, Dict, Any
from fastapi import status
from app.exceptions import DBaaSException


class ResourceAllocationError(DBaaSException):
    """Raised when resource allocation fails."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details,
        )


class ProviderNotFoundError(DBaaSException):
    """Raised when a provider is not found."""

    def __init__(self, provider_id: str, details: Optional[Dict[str, Any]] = None):
        message = f"Provider '{provider_id}' not found"
        super().__init__(
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
            details=details or {"provider_id": provider_id},
        )


class InvalidProviderError(DBaaSException):
    """Raised when provider configuration is invalid."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details,
        )
