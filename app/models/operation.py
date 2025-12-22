"""
Operation model for tracking database operations.

Enables:
- Async operation tracking
- Status polling
- Audit trail
- Retry logic
"""
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional
from uuid import uuid4

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


class OperationType(str, Enum):
    """Type of database operation."""
    SCALE_VERTICAL = "scale_vertical"
    SCALE_HORIZONTAL = "scale_horizontal"
    EXPAND_STORAGE = "expand_storage"
    UPGRADE_VERSION = "upgrade_version"
    BACKUP = "backup"
    RESTORE = "restore"
    PAUSE = "pause"
    RESUME = "resume"


class OperationStatus(str, Enum):
    """Status of database operation."""
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class Operation(Document):
    """
    Tracks database operations (scale, upgrade, backup, etc.)

    This enables async operations with status tracking and history.
    """

    class Settings:
        name = "operations"
        indexes = [
            IndexModel([("database_id", 1), ("created_at", -1)]),
            IndexModel([("status", 1)]),
            IndexModel([("created_at", -1)]),
            # Compound index for operation deduplication (reconciliation worker)
            # Used by: reconciliation_worker._create_reconciliation_operation()
            # Query: {database_id: X, type: Y, status: {$in: [...]}}
            IndexModel([("database_id", 1), ("type", 1), ("status", 1)]),
        ]

    # Identity
    id: str = Field(default_factory=lambda: f"op-{uuid4().hex[:12]}")
    database_id: str

    # Operation details
    type: OperationType
    desired_state: Dict[str, Any]  # What we're trying to achieve

    # Status tracking
    status: OperationStatus = OperationStatus.QUEUED
    progress: int = 0  # 0-100
    message: str = ""  # Current phase message

    # K8s integration
    ops_request_name: Optional[str] = None
    ops_request_uid: Optional[str] = None
    ops_request_phase: Optional[str] = None

    # Timing
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    estimated_completion_at: Optional[datetime] = None

    # Error handling
    error_message: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3

    # Audit
    created_by: str = "system"
    domain: str
    project: str

    def get_duration_seconds(self) -> Optional[float]:
        """Get operation duration in seconds."""
        if not self.started_at:
            return None
        end = self.completed_at or datetime.utcnow()
        return (end - self.started_at).total_seconds()

    def is_terminal(self) -> bool:
        """Check if operation is in terminal state."""
        return self.status in [
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.TIMEOUT,
        ]

    def can_retry(self) -> bool:
        """Check if operation can be retried."""
        return (
            self.status == OperationStatus.FAILED
            and self.retry_count < self.max_retries
        )
