"""
Shutdown signal handler for graceful application termination.
"""
import asyncio
import signal
import sys
from typing import Optional

from app.config.logging import get_logger

logger = get_logger(__name__)


class ShutdownHandler:
    """Handles graceful shutdown on SIGINT/SIGTERM signals."""

    def __init__(self):
        """Initialize shutdown handler."""
        self.shutdown_event: Optional[asyncio.Event] = None
        self._original_sigint = None
        self._original_sigterm = None

    def setup(self):
        """Setup signal handlers and shutdown event."""
        # Create shutdown event
        try:
            self.shutdown_event = asyncio.Event()
        except RuntimeError:
            # No event loop yet, will be created later
            pass

        # Install signal handlers
        self._original_sigint = signal.signal(signal.SIGINT, self._signal_handler)
        self._original_sigterm = signal.signal(signal.SIGTERM, self._signal_handler)
        logger.info("shutdown_handler_installed")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        sig_name = signal.Signals(signum).name
        logger.info(f"received_signal_{sig_name}_initiating_shutdown")

        # Set shutdown event if available
        if self.shutdown_event:
            self.shutdown_event.set()

        # Exit immediately
        sys.exit(0)

    def is_shutting_down(self) -> bool:
        """Check if shutdown has been requested."""
        return self.shutdown_event and self.shutdown_event.is_set()

    async def wait_or_shutdown(self, delay: float) -> bool:
        """
        Wait for delay seconds or until shutdown is requested.

        Args:
            delay: Time to wait in seconds

        Returns:
            True if shutdown was requested, False if wait completed normally
        """
        if not self.shutdown_event:
            self.shutdown_event = asyncio.Event()

        try:
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=delay)
            return True  # Shutdown requested
        except asyncio.TimeoutError:
            return False  # Wait completed normally

    def restore(self):
        """Restore original signal handlers."""
        if self._original_sigint:
            signal.signal(signal.SIGINT, self._original_sigint)
        if self._original_sigterm:
            signal.signal(signal.SIGTERM, self._original_sigterm)


# Global instance
shutdown_handler = ShutdownHandler()
