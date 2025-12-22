"""
Custom exceptions for the KubeDB DBaaS platform.

This module defines all custom exceptions used throughout the application
for consistent error handling and reporting.
"""
from typing import Optional, Dict, Any
from fastapi import status


class DBaaSException(Exception):
    """
    Base exception for all DBaaS platform errors.

    All custom exceptions should inherit from this base class.
    """

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(DBaaSException):
    """
    Raised when request validation fails.

    Used for invalid input data, schema validation errors, etc.
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            details=details,
        )


class AuthenticationError(DBaaSException):
    """
    Raised when authentication fails.

    Used for invalid credentials, expired tokens, etc.
    """

    def __init__(self, message: str = "Authentication failed", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            details=details,
        )


class AuthorizationError(DBaaSException):
    """
    Raised when user lacks permission for an action.

    Used for RBAC violations, tenant isolation violations, etc.
    """

    def __init__(self, message: str = "Access denied", details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_403_FORBIDDEN,
            details=details,
        )


class NotFoundError(DBaaSException):
    """
    Raised when a requested resource is not found.

    Used for database not found, tenant not found, etc.
    """

    def __init__(self, resource: str, resource_id: str, details: Optional[Dict[str, Any]] = None):
        message = f"{resource} with ID '{resource_id}' not found"
        super().__init__(
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
            details=details or {"resource": resource, "resource_id": resource_id},
        )


class ConflictError(DBaaSException):
    """
    Raised when a resource conflict occurs.

    Used for duplicate names, conflicting operations, etc.
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_409_CONFLICT,
            details=details,
        )


class QuotaExceededError(DBaaSException):
    """
    Raised when tenant quota is exceeded.

    Used when attempting to create resources beyond tenant limits.
    """

    def __init__(
        self,
        resource: str,
        requested: float,
        available: float,
        limit: float,
        details: Optional[Dict[str, Any]] = None,
    ):
        message = f"Quota exceeded for {resource}: requested {requested}, available {available}, limit {limit}"
        super().__init__(
            message=message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details=details or {
                "resource": resource,
                "requested": requested,
                "available": available,
                "limit": limit,
            },
        )


class KubernetesError(DBaaSException):
    """
    Raised when Kubernetes API operations fail.

    Used for K8s API errors, connection issues, etc.
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=f"Kubernetes error: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class KubeDBError(DBaaSException):
    """
    Raised when KubeDB operations fail.

    Used for KubeDB custom resource creation/deletion errors.
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=f"KubeDB error: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class DatabaseOperationError(DBaaSException):
    """
    Raised when database operations fail.

    Used for creation, deletion, scaling failures.
    """

    def __init__(self, operation: str, database_id: str, reason: str, details: Optional[Dict[str, Any]] = None):
        message = f"Database {operation} failed for '{database_id}': {reason}"
        super().__init__(
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details or {
                "operation": operation,
                "database_id": database_id,
                "reason": reason,
            },
        )


class BackupError(DBaaSException):
    """
    Raised when backup operations fail.

    Used for backup creation, listing, or retrieval failures.
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=f"Backup error: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class RestoreError(DBaaSException):
    """
    Raised when restore operations fail.

    Used for restore from backup failures.
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=f"Restore error: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class VaultError(DBaaSException):
    """
    Raised when HashiCorp Vault operations fail.

    Used for secrets management errors.
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=f"Vault error: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class RateLimitExceeded(DBaaSException):
    """
    Raised when API rate limit is exceeded.

    Used by rate limiting middleware.
    """

    def __init__(self, message: str = "Rate limit exceeded", retry_after: Optional[int] = None):
        details = {"retry_after": retry_after} if retry_after else {}
        super().__init__(
            message=message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details=details,
        )


# Export all exceptions
__all__ = [
    "DBaaSException",
    "ValidationError",
    "AuthenticationError",
    "AuthorizationError",
    "NotFoundError",
    "ConflictError",
    "QuotaExceededError",
    "KubernetesError",
    "KubeDBError",
    "DatabaseOperationError",
    "BackupError",
    "RestoreError",
    "VaultError",
    "RateLimitExceeded",
]
