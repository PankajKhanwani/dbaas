"""
Retry utilities for Kubernetes API calls.

Provides decorators and functions to handle transient failures in Kubernetes API calls
with exponential backoff and configurable retry strategies.
"""
import asyncio
import functools
from typing import Callable, TypeVar, Optional, Tuple, Type
from kubernetes.client.rest import ApiException

from app.config.logging import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


def is_retryable_k8s_error(exception: Exception) -> bool:
    """
    Determine if a Kubernetes API exception should trigger a retry.

    Args:
        exception: The exception to check

    Returns:
        True if the exception is retryable, False otherwise
    """
    if not isinstance(exception, ApiException):
        # Non-API exceptions are generally not retryable
        return False

    # HTTP status codes that are retryable
    retryable_status_codes = {
        408,  # Request Timeout
        429,  # Too Many Requests (rate limiting)
        500,  # Internal Server Error
        502,  # Bad Gateway
        503,  # Service Unavailable
        504,  # Gateway Timeout
    }

    return exception.status in retryable_status_codes


def retry_on_k8s_error(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    retry_on: Optional[Tuple[Type[Exception], ...]] = None,
) -> Callable:
    """
    Decorator to retry Kubernetes API calls with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay between retries in seconds (default: 1.0)
        max_delay: Maximum delay between retries in seconds (default: 30.0)
        exponential_base: Base for exponential backoff calculation (default: 2.0)
        retry_on: Additional exception types to retry on (default: None)

    Returns:
        Decorated function with retry logic

    Example:
        @retry_on_k8s_error(max_retries=5, initial_delay=2.0)
        async def create_database(name: str):
            # ... Kubernetes API call ...
            pass
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    result = await func(*args, **kwargs)

                    # Log success after retry
                    if attempt > 0:
                        logger.info(
                            "k8s_api_call_succeeded_after_retry",
                            function=func.__name__,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                        )

                    return result

                except Exception as e:
                    last_exception = e

                    # Check if this exception is retryable
                    should_retry = is_retryable_k8s_error(e)

                    # Also check custom retry exceptions if provided
                    if retry_on and isinstance(e, retry_on):
                        should_retry = True

                    # Don't retry on the last attempt or non-retryable errors
                    if attempt >= max_retries or not should_retry:
                        if should_retry:
                            logger.error(
                                "k8s_api_call_failed_max_retries",
                                function=func.__name__,
                                attempt=attempt + 1,
                                max_retries=max_retries,
                                error_type=type(e).__name__,
                                error=str(e),
                            )
                        else:
                            logger.debug(
                                "k8s_api_call_failed_non_retryable",
                                function=func.__name__,
                                error_type=type(e).__name__,
                                error=str(e),
                            )
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(
                        initial_delay * (exponential_base ** attempt),
                        max_delay
                    )

                    # Log retry attempt
                    status_code = getattr(e, 'status', None)
                    logger.warning(
                        "k8s_api_call_failed_retrying",
                        function=func.__name__,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay_seconds=delay,
                        error_type=type(e).__name__,
                        error=str(e),
                        status_code=status_code,
                    )

                    # Wait before retrying
                    await asyncio.sleep(delay)

            # Should never reach here, but just in case
            raise last_exception

        return wrapper
    return decorator


def retry_on_connection_error(
    max_retries: int = 5,
    initial_delay: float = 0.5,
    max_delay: float = 10.0,
) -> Callable:
    """
    Decorator for retrying on connection errors (more aggressive for transient network issues).

    Args:
        max_retries: Maximum number of retry attempts (default: 5)
        initial_delay: Initial delay between retries in seconds (default: 0.5)
        max_delay: Maximum delay between retries in seconds (default: 10.0)

    Returns:
        Decorated function with retry logic

    Example:
        @retry_on_connection_error(max_retries=10)
        async def check_cluster_health():
            # ... Kubernetes API call ...
            pass
    """
    return retry_on_k8s_error(
        max_retries=max_retries,
        initial_delay=initial_delay,
        max_delay=max_delay,
        exponential_base=2.0,
        retry_on=(ConnectionError, TimeoutError),
    )


async def retry_async(
    func: Callable[..., T],
    *args,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    **kwargs
) -> T:
    """
    Retry an async function with exponential backoff (functional interface).

    Use this when you can't use the decorator (e.g., for lambda functions or dynamic calls).

    Args:
        func: Async function to retry
        *args: Positional arguments to pass to func
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay between retries in seconds
        max_delay: Maximum delay between retries in seconds
        **kwargs: Keyword arguments to pass to func

    Returns:
        Result of the function call

    Example:
        result = await retry_async(
            client.read_namespaced_pod,
            name="my-pod",
            namespace="default",
            max_retries=5
        )
    """
    decorated = retry_on_k8s_error(
        max_retries=max_retries,
        initial_delay=initial_delay,
        max_delay=max_delay,
    )(func)

    return await decorated(*args, **kwargs)
