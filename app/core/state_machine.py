"""
Database State Machine for DBaaS

This module implements a strict state machine for database lifecycle management.
It prevents invalid state transitions and ensures operation consistency.

States:
- CREATING: Initial creation in progress
- AVAILABLE: Ready for use
- MODIFYING: Update operation in progress (scale, update config, etc.)
- BACKING_UP: Backup in progress
- DELETING: Deletion in progress
- DELETED: Soft deleted (can be restored within retention period)
- FAILED: Operation failed, requires intervention
- MAINTENANCE: Automated maintenance in progress

Usage:
    >>> from app.core.state_machine import DatabaseState, DatabaseStateMachine
    >>>
    >>> # Check if transition is valid
    >>> DatabaseStateMachine.can_transition(
    ...     DatabaseState.CREATING,
    ...     DatabaseState.AVAILABLE
    ... )
    True
    >>>
    >>> # Check if operation is allowed in current state
    >>> DatabaseStateMachine.can_perform_operation(
    ...     DatabaseState.AVAILABLE,
    ...     "scale"
    ... )
    True
"""

from enum import Enum
from typing import Dict, Set, Optional
import structlog

logger = structlog.get_logger(__name__)


class DatabaseState(str, Enum):
    """Database lifecycle states"""
    CREATING = "creating"
    AVAILABLE = "available"
    MODIFYING = "modifying"
    BACKING_UP = "backing_up"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"
    MAINTENANCE = "maintenance"


class OperationType(str, Enum):
    """Types of database operations"""
    CREATE = "create"
    SCALE = "scale"
    UPDATE = "update"
    DELETE = "delete"
    BACKUP = "backup"
    RESTORE = "restore"
    RESTART = "restart"
    MAINTENANCE = "maintenance"


class DatabaseStateMachine:
    """
    State machine for database lifecycle management.

    Enforces strict state transitions and operation permissions to prevent
    race conditions and invalid states.
    """

    # Define allowed state transitions
    TRANSITIONS: Dict[DatabaseState, Set[DatabaseState]] = {
        DatabaseState.CREATING: {
            DatabaseState.AVAILABLE,  # Successful creation
            DatabaseState.FAILED,      # Creation failed
        },
        DatabaseState.AVAILABLE: {
            DatabaseState.MODIFYING,   # Scale, update, restart
            DatabaseState.BACKING_UP,  # Backup operation
            DatabaseState.DELETING,    # User deletion
            DatabaseState.MAINTENANCE, # Automated maintenance
        },
        DatabaseState.MODIFYING: {
            DatabaseState.AVAILABLE,   # Modification successful
            DatabaseState.FAILED,      # Modification failed
        },
        DatabaseState.BACKING_UP: {
            DatabaseState.AVAILABLE,   # Backup successful
            DatabaseState.FAILED,      # Backup failed
        },
        DatabaseState.DELETING: {
            DatabaseState.DELETED,     # Deletion successful
            DatabaseState.FAILED,      # Deletion failed (requires cleanup)
        },
        DatabaseState.MAINTENANCE: {
            DatabaseState.AVAILABLE,   # Maintenance completed
            DatabaseState.FAILED,      # Maintenance failed
        },
        DatabaseState.FAILED: {
            DatabaseState.AVAILABLE,   # Manual recovery successful
            DatabaseState.DELETING,    # Give up, delete failed database
        },
        DatabaseState.DELETED: set(),  # Terminal state, no transitions
    }

    # Define which operations are allowed in each state
    ALLOWED_OPERATIONS: Dict[DatabaseState, Set[OperationType]] = {
        DatabaseState.CREATING: set(),  # Wait for creation to complete
        DatabaseState.AVAILABLE: {
            OperationType.SCALE,
            OperationType.UPDATE,
            OperationType.DELETE,
            OperationType.BACKUP,
            OperationType.RESTORE,
            OperationType.RESTART,
        },
        DatabaseState.MODIFYING: set(),  # Wait for modification to complete
        DatabaseState.BACKING_UP: {
            OperationType.BACKUP,  # Can queue another backup
        },
        DatabaseState.DELETING: set(),  # Irreversible, wait for deletion
        DatabaseState.DELETED: set(),   # Terminal state
        DatabaseState.FAILED: {
            OperationType.DELETE,  # Can delete failed database
        },
        DatabaseState.MAINTENANCE: set(),  # Wait for maintenance to complete
    }

    @classmethod
    def can_transition(
        cls,
        from_state: DatabaseState,
        to_state: DatabaseState
    ) -> bool:
        """
        Check if state transition is valid.

        Args:
            from_state: Current database state
            to_state: Target state

        Returns:
            True if transition is allowed, False otherwise

        Example:
            >>> DatabaseStateMachine.can_transition(
            ...     DatabaseState.CREATING,
            ...     DatabaseState.AVAILABLE
            ... )
            True
            >>> DatabaseStateMachine.can_transition(
            ...     DatabaseState.AVAILABLE,
            ...     DatabaseState.CREATING
            ... )
            False
        """
        allowed_states = cls.TRANSITIONS.get(from_state, set())
        return to_state in allowed_states

    @classmethod
    def validate_transition(
        cls,
        from_state: DatabaseState,
        to_state: DatabaseState,
        database_id: Optional[str] = None
    ) -> None:
        """
        Validate state transition and raise exception if invalid.

        Args:
            from_state: Current database state
            to_state: Target state
            database_id: Optional database ID for logging

        Raises:
            ValueError: If transition is not allowed

        Example:
            >>> DatabaseStateMachine.validate_transition(
            ...     DatabaseState.CREATING,
            ...     DatabaseState.AVAILABLE,
            ...     "db-123"
            ... )
            # No exception raised
            >>> DatabaseStateMachine.validate_transition(
            ...     DatabaseState.DELETED,
            ...     DatabaseState.AVAILABLE
            ... )
            Traceback (most recent call last):
                ...
            ValueError: Invalid state transition from deleted to available
        """
        if not cls.can_transition(from_state, to_state):
            error_msg = (
                f"Invalid state transition from {from_state.value} "
                f"to {to_state.value}"
            )
            if database_id:
                error_msg += f" for database {database_id}"

            logger.error(
                "invalid_state_transition",
                database_id=database_id,
                from_state=from_state.value,
                to_state=to_state.value,
                allowed_states=[s.value for s in cls.TRANSITIONS.get(from_state, set())]
            )
            raise ValueError(error_msg)

        logger.info(
            "state_transition_validated",
            database_id=database_id,
            from_state=from_state.value,
            to_state=to_state.value
        )

    @classmethod
    def can_perform_operation(
        cls,
        current_state: DatabaseState,
        operation: OperationType
    ) -> bool:
        """
        Check if operation is allowed in current state.

        Args:
            current_state: Current database state
            operation: Operation to perform

        Returns:
            True if operation is allowed, False otherwise

        Example:
            >>> DatabaseStateMachine.can_perform_operation(
            ...     DatabaseState.AVAILABLE,
            ...     OperationType.SCALE
            ... )
            True
            >>> DatabaseStateMachine.can_perform_operation(
            ...     DatabaseState.CREATING,
            ...     OperationType.SCALE
            ... )
            False
        """
        allowed_ops = cls.ALLOWED_OPERATIONS.get(current_state, set())
        return operation in allowed_ops

    @classmethod
    def validate_operation(
        cls,
        current_state: DatabaseState,
        operation: OperationType,
        database_id: Optional[str] = None
    ) -> None:
        """
        Validate operation and raise exception if not allowed.

        Args:
            current_state: Current database state
            operation: Operation to perform
            database_id: Optional database ID for logging

        Raises:
            ValueError: If operation is not allowed in current state

        Example:
            >>> DatabaseStateMachine.validate_operation(
            ...     DatabaseState.AVAILABLE,
            ...     OperationType.SCALE,
            ...     "db-123"
            ... )
            # No exception raised
        """
        if not cls.can_perform_operation(current_state, operation):
            error_msg = (
                f"Operation {operation.value} not allowed in state "
                f"{current_state.value}"
            )
            if database_id:
                error_msg += f" for database {database_id}"

            # Add helpful message
            if current_state == DatabaseState.CREATING:
                error_msg += " (wait for database creation to complete)"
            elif current_state == DatabaseState.MODIFYING:
                error_msg += " (wait for current operation to complete)"
            elif current_state == DatabaseState.DELETING:
                error_msg += " (database is being deleted)"
            elif current_state == DatabaseState.DELETED:
                error_msg += " (database has been deleted)"
            elif current_state == DatabaseState.FAILED:
                error_msg += " (database is in failed state, requires manual intervention)"

            logger.error(
                "operation_not_allowed",
                database_id=database_id,
                current_state=current_state.value,
                operation=operation.value,
                allowed_operations=[op.value for op in cls.ALLOWED_OPERATIONS.get(current_state, set())]
            )
            raise ValueError(error_msg)

        logger.info(
            "operation_validated",
            database_id=database_id,
            current_state=current_state.value,
            operation=operation.value
        )

    @classmethod
    def get_state_for_operation(cls, operation: OperationType) -> DatabaseState:
        """
        Get the intermediate state for an operation.

        Args:
            operation: Operation type

        Returns:
            Database state during operation

        Example:
            >>> DatabaseStateMachine.get_state_for_operation(OperationType.SCALE)
            <DatabaseState.MODIFYING: 'modifying'>
            >>> DatabaseStateMachine.get_state_for_operation(OperationType.BACKUP)
            <DatabaseState.BACKING_UP: 'backing_up'>
        """
        operation_state_map = {
            OperationType.CREATE: DatabaseState.CREATING,
            OperationType.SCALE: DatabaseState.MODIFYING,
            OperationType.UPDATE: DatabaseState.MODIFYING,
            OperationType.RESTART: DatabaseState.MODIFYING,
            OperationType.DELETE: DatabaseState.DELETING,
            OperationType.BACKUP: DatabaseState.BACKING_UP,
            OperationType.RESTORE: DatabaseState.MODIFYING,
            OperationType.MAINTENANCE: DatabaseState.MAINTENANCE,
        }
        return operation_state_map.get(operation, DatabaseState.MODIFYING)
