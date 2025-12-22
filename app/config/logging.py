"""
Structured logging configuration using structlog.
Provides JSON logging for production and human-readable logging for development.
"""
import logging
import sys
from typing import Any, Dict

import structlog
from structlog.types import EventDict, Processor

from app.config.settings import settings


def add_app_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add application context to log events."""
    event_dict["app"] = settings.app_name
    event_dict["version"] = settings.app_version
    event_dict["environment"] = settings.environment
    return event_dict


def add_severity_level(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add severity level for Google Cloud Logging compatibility."""
    if "level" in event_dict:
        event_dict["severity"] = event_dict["level"].upper()
    return event_dict


def configure_logging() -> None:
    """Configure structured logging for the application."""

    # Shared processors for all log records
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_app_context,
        add_severity_level,
    ]

    # Configure structlog
    if settings.is_production:
        # Production: JSON logging
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
        renderer = structlog.processors.JSONRenderer()
    else:
        # Development: Human-readable colored logging
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=True),
        ]
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=processors,  # type: ignore
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
    )

    # Set log levels for third-party libraries
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("fastapi").setLevel(logging.INFO)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a configured structlog logger.

    Args:
        name: Logger name (typically __name__ of the module)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)
