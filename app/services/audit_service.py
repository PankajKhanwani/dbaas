"""
Audit logging service.
Comprehensive audit trail for compliance and security.
"""
from typing import Dict, Any, Optional
from datetime import datetime

from app.config.logging import get_logger
from app.repositories.models import AuditLog

logger = get_logger(__name__)


class AuditService:
    """Service for audit logging."""

    async def log_action(
        self,
        action: str,
        resource_type: str,
        resource_id: str,
        domain: str,
        project: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_method: Optional[str] = None,
        request_path: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """
        Log an audit event.

        Args:
            action: Action performed (e.g., "database.create")
            resource_type: Type of resource (e.g., "database")
            resource_id: Resource identifier
            domain: Domain name
            project: Project name
            user_id: User ID (optional)
            user_email: User email (optional)
            ip_address: Client IP address (optional)
            user_agent: User agent (optional)
            request_method: HTTP method (optional)
            request_path: Request path (optional)
            status_code: HTTP status code (optional)
            details: Additional details (optional)

        Returns:
            Created audit log entry
        """
        audit_log = AuditLog(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            domain=domain,
            project=project,
            user_id=user_id,
            user_email=user_email,
            ip_address=ip_address,
            user_agent=user_agent,
            request_method=request_method,
            request_path=request_path,
            status_code=status_code,
            details=details or {},
            timestamp=datetime.utcnow(),
        )

        await audit_log.save()

        logger.info(
            "audit_log_created",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            domain=domain,
            project=project,
            user_id=user_id,
        )

        return audit_log

    async def get_logs(
        self,
        domain: str,
        project: str,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        """
        Get audit logs for a domain/project.

        Args:
            domain: Domain name
            project: Project name
            action: Filter by action (optional)
            resource_type: Filter by resource type (optional)
            limit: Maximum number of logs to return

        Returns:
            List of audit logs
        """
        filters = [AuditLog.domain == domain, AuditLog.project == project]

        if action:
            filters.append(AuditLog.action == action)

        if resource_type:
            filters.append(AuditLog.resource_type == resource_type)

        logs = (
            await AuditLog.find(*filters)
            .sort(-AuditLog.timestamp)
            .limit(limit)
            .to_list()
        )

        return logs


# Global instance
audit_service = AuditService()
