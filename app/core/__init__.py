"""
Core functionality for DBaaS operation management.

This package provides the foundation for production-grade database operations:
- State machine for database lifecycle management
- Operation queue for concurrent operation handling
- Distributed locking to prevent conflicts
- Operation orchestration and tracking
"""

# Import lazily to avoid circular dependencies at module load time
# Users should import directly from submodules:
# from app.core.state_machine import DatabaseState, DatabaseStateMachine
# from app.core.operation_queue import OperationQueue
# from app.core.lock_manager import LockManager
# from app.core.operation_manager import OperationManager

__all__ = [
    "DatabaseState",
    "DatabaseStateMachine",
    "OperationQueue",
    "LockManager",
    "OperationManager",
]
