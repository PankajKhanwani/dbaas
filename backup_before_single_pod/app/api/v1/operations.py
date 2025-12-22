"""
Operation status API endpoints.
Provides status polling for async database operations.

URL Pattern: /api/v1/operations
"""
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from app.models.operation import Operation, OperationType, OperationStatus
from app.config.logging import get_logger
from app.exceptions import NotFoundError
from app.services.operation_queue import operation_queue

router = APIRouter()
logger = get_logger(__name__)


class OperationResponse(BaseModel):
    """Response model for operation status."""

    id: str = Field(..., description="Operation ID")
    database_id: str = Field(..., description="Database ID")
    type: OperationType = Field(..., description="Operation type")
    status: OperationStatus = Field(..., description="Current status")
    progress: int = Field(..., ge=0, le=100, description="Progress percentage (0-100)")
    message: str = Field(default="", description="Current status message")

    # Timing
    created_at: datetime = Field(..., description="When operation was created")
    started_at: Optional[datetime] = Field(default=None, description="When operation started")
    completed_at: Optional[datetime] = Field(default=None, description="When operation completed")
    estimated_completion_at: Optional[datetime] = Field(
        default=None, description="Estimated completion time"
    )

    # Details
    desired_state: dict = Field(..., description="Target state (e.g., {size: 'db.t3.large'})")
    ops_request_name: Optional[str] = Field(default=None, description="KubeDB OpsRequest name")
    ops_request_phase: Optional[str] = Field(default=None, description="KubeDB OpsRequest phase")

    # Error handling
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
    retry_count: int = Field(default=0, description="Number of retries")

    # Metadata
    domain: str = Field(..., description="Domain name")
    project: str = Field(..., description="Project name")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "op-abc123",
                "database_id": "db-xyz789",
                "type": "scale_vertical",
                "status": "in_progress",
                "progress": 65,
                "message": "Updating pods (rolling restart)",
                "created_at": "2025-11-28T10:00:00Z",
                "started_at": "2025-11-28T10:00:05Z",
                "estimated_completion_at": "2025-11-28T10:05:00Z",
                "desired_state": {"size": "db.t3.large"},
                "ops_request_name": "mongodb-scale-abc123",
                "ops_request_phase": "Progressing",
                "domain": "example-domain",
                "project": "production-project",
            }
        }


class OperationListResponse(BaseModel):
    """Response model for operation list."""

    operations: list[OperationResponse]
    total: int
    page: int
    page_size: int


class QueueStatsResponse(BaseModel):
    """Response model for queue statistics."""

    queued: int = Field(..., description="Number of operations in queue")
    processing: int = Field(..., description="Number of operations being processed")


@router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str = Path(..., description="Operation ID"),
):
    """
    Get operation status by ID.

    Returns real-time status of an async database operation.

    Use this endpoint to poll for operation progress after creating/scaling a database.

    **Response Fields:**
    - `status`: Current status (queued, in_progress, completed, failed, cancelled)
    - `progress`: Percentage complete (0-100)
    - `message`: Human-readable status message
    - `estimated_completion_at`: When operation is expected to finish
    - `error_message`: Error details if operation failed

    **Polling Recommendations:**
    - Poll every 3-5 seconds during operation
    - Stop polling when status is terminal (completed, failed, cancelled, timeout)
    - Check `progress` field for UI progress bars
    """
    operation = await Operation.get(operation_id)

    if not operation:
        raise NotFoundError("Operation", operation_id)

    logger.debug("operation_status_polled", operation_id=operation_id, status=operation.status)

    return OperationResponse(
        id=operation.id,
        database_id=operation.database_id,
        type=operation.type,
        status=operation.status,
        progress=operation.progress,
        message=operation.message,
        created_at=operation.created_at,
        started_at=operation.started_at,
        completed_at=operation.completed_at,
        estimated_completion_at=operation.estimated_completion_at,
        desired_state=operation.desired_state,
        ops_request_name=operation.ops_request_name,
        ops_request_phase=operation.ops_request_phase,
        error_message=operation.error_message,
        retry_count=operation.retry_count,
        domain=operation.domain,
        project=operation.project,
    )


@router.get("/", response_model=OperationListResponse)
async def list_operations(
    database_id: Optional[str] = Query(None, description="Filter by database ID"),
    status: Optional[OperationStatus] = Query(None, description="Filter by status"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
):
    """
    List operations with optional filtering.

    Supports filtering by database ID and status.
    Returns operations in descending order by creation date (newest first).

    **Common Use Cases:**
    - Get all operations for a specific database: `?database_id=db-abc123`
    - Get all in-progress operations: `?status=in_progress`
    - Get recent failed operations: `?status=failed&page=1&page_size=20`
    """
    # Build query
    filters = []
    if database_id:
        filters.append(Operation.database_id == database_id)
    if status:
        filters.append(Operation.status == status)

    # Get total count
    if filters:
        total = await Operation.find(*filters).count()
    else:
        total = await Operation.find_all().count()

    # Get paginated results
    skip = (page - 1) * page_size

    if filters:
        operations = (
            await Operation.find(*filters)
            .sort("-created_at")
            .skip(skip)
            .limit(page_size)
            .to_list()
        )
    else:
        operations = (
            await Operation.find_all().sort("-created_at").skip(skip).limit(page_size).to_list()
        )

    return OperationListResponse(
        operations=[
            OperationResponse(
                id=op.id,
                database_id=op.database_id,
                type=op.type,
                status=op.status,
                progress=op.progress,
                message=op.message,
                created_at=op.created_at,
                started_at=op.started_at,
                completed_at=op.completed_at,
                estimated_completion_at=op.estimated_completion_at,
                desired_state=op.desired_state,
                ops_request_name=op.ops_request_name,
                ops_request_phase=op.ops_request_phase,
                error_message=op.error_message,
                retry_count=op.retry_count,
                domain=op.domain,
                project=op.project,
            )
            for op in operations
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/queue/stats", response_model=QueueStatsResponse)
async def get_queue_stats():
    """
    Get operation queue statistics.

    Returns:
    - Number of operations waiting in queue
    - Number of operations currently being processed

    Useful for monitoring system load and operation backlog.
    """
    stats = await operation_queue.get_stats()

    logger.debug("queue_stats_requested", stats=stats)

    return QueueStatsResponse(
        queued=stats["queued"],
        processing=stats["processing"],
    )


@router.post("/{operation_id}/cancel")
async def cancel_operation(
    operation_id: str = Path(..., description="Operation ID"),
):
    """
    Cancel a queued operation.

    **Important:**
    - Only works for operations in QUEUED status
    - Cannot cancel operations that are already IN_PROGRESS
    - Returns 404 if operation not found in queue

    **Use Cases:**
    - User changed their mind before operation started
    - Submitted wrong scaling parameters
    - Emergency cancellation needed
    """
    operation = await Operation.get(operation_id)

    if not operation:
        raise NotFoundError("Operation", operation_id)

    if operation.status != OperationStatus.QUEUED:
        return {
            "success": False,
            "message": f"Cannot cancel operation in {operation.status.value} status",
            "operation_id": operation_id,
        }

    # Try to cancel from queue
    cancelled = await operation_queue.cancel_operation(operation_id)

    if cancelled:
        # Update operation record
        operation.status = OperationStatus.CANCELLED
        operation.completed_at = datetime.utcnow()
        await operation.save()

        logger.info("operation_cancelled", operation_id=operation_id)

        return {
            "success": True,
            "message": "Operation cancelled successfully",
            "operation_id": operation_id,
        }

    return {
        "success": False,
        "message": "Operation not found in queue (may have already started)",
        "operation_id": operation_id,
    }
