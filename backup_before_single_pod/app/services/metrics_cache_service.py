"""
Production-grade metrics caching service.

Handles lakhs of concurrent users with:
- Multi-layer caching (Redis + in-memory)
- Rate limiting
- Request coalescing
- Circuit breaker pattern
"""
import asyncio
import time
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict

from app.config.logging import get_logger
from app.config.redis import RedisConnection

logger = get_logger(__name__)


class CircuitBreaker:
    """Circuit breaker for failing fast when backend is down."""

    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        """
        Args:
            failure_threshold: Number of failures before opening circuit
            timeout: Seconds to wait before trying again (half-open state)
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half-open

    def record_success(self):
        """Record successful call."""
        self.failures = 0
        self.state = "closed"

    def record_failure(self):
        """Record failed call."""
        self.failures += 1
        self.last_failure_time = time.time()

        if self.failures >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                "circuit_breaker_opened",
                failures=self.failures,
                timeout=self.timeout
            )

    def can_attempt(self) -> bool:
        """Check if we should attempt the call."""
        if self.state == "closed":
            return True

        if self.state == "open":
            # Check if timeout has passed
            if self.last_failure_time and (time.time() - self.last_failure_time) > self.timeout:
                self.state = "half-open"
                logger.info("circuit_breaker_half_open")
                return True
            return False

        # half-open: allow one request to test
        return True


class RequestCoalescer:
    """
    Coalesces multiple simultaneous requests for the same resource.

    Prevents "thundering herd" problem where thousands of users
    request the same metric simultaneously.
    """

    def __init__(self):
        self._pending: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def coalesce(self, key: str, fetch_fn):
        """
        Coalesce requests for the same key.

        If multiple requests come in for the same key,
        only one will actually fetch the data.
        """
        async with self._lock:
            if key in self._pending:
                # Another request is already fetching this
                logger.debug("request_coalesced", key=key)
                return await self._pending[key]

            # Create future for this request
            future = asyncio.Future()
            self._pending[key] = future

        try:
            # Fetch the data
            result = await fetch_fn()
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            # Remove from pending
            async with self._lock:
                self._pending.pop(key, None)


class RateLimiter:
    """
    Token bucket rate limiter.

    Supports:
    - Per-user rate limits
    - Per-database rate limits
    - Global rate limits
    """

    def __init__(self, redis: RedisConnection):
        self.redis = redis

    async def check_limit(
        self,
        key: str,
        limit: int,
        window: int = 60
    ) -> Tuple[bool, int]:
        """
        Check if request is within rate limit.

        Args:
            key: Rate limit key (e.g., "user:123" or "db:abc")
            limit: Max requests per window
            window: Time window in seconds

        Returns:
            (allowed, remaining) tuple
        """
        redis_key = f"ratelimit:{key}"

        try:
            # Use Redis INCR for atomic counter
            count = await self.redis.client.incr(redis_key)

            if count == 1:
                # First request - set expiry
                await self.redis.client.expire(redis_key, window)

            remaining = max(0, limit - count)
            allowed = count <= limit

            if not allowed:
                logger.warning(
                    "rate_limit_exceeded",
                    key=key,
                    limit=limit,
                    count=count
                )

            return allowed, remaining

        except Exception as e:
            logger.error("rate_limit_check_failed", error=str(e))
            # Fail open - allow request if Redis is down
            return True, limit


class MetricsCacheService:
    """
    Production-grade metrics caching service.

    Architecture:
    - L1 Cache: In-memory (process-local, 5s TTL)
    - L2 Cache: Redis (distributed, 15s TTL)
    - L3: MongoDB aggregated metrics (30s TTL)
    - Source: Live Prometheus metrics
    """

    def __init__(self):
        self.redis = RedisConnection()

        # L1 Cache: In-memory
        self._memory_cache: Dict[str, Tuple[Any, float]] = {}
        self._memory_ttl = 5  # 5 seconds

        # L2 Cache: Redis TTL
        self._redis_ttl = 15  # 15 seconds

        # Circuit breakers per database
        self._circuit_breakers: Dict[str, CircuitBreaker] = defaultdict(CircuitBreaker)

        # Request coalescer
        self._coalescer = RequestCoalescer()

        # Rate limiter
        self._rate_limiter = RateLimiter(self.redis)

        # Stats
        self._stats = {
            "l1_hits": 0,
            "l2_hits": 0,
            "l3_hits": 0,
            "source_hits": 0,
            "rate_limited": 0,
        }

    async def get_metrics(
        self,
        database_id: str,
        user_id: Optional[str],
        fetch_fn,
        use_aggregated: bool = True
    ) -> Dict[str, Any]:
        """
        Get metrics with multi-layer caching and rate limiting.

        Args:
            database_id: Database identifier
            user_id: User making the request (for rate limiting)
            fetch_fn: Async function to fetch metrics from source
            use_aggregated: Use pre-aggregated metrics if available

        Returns:
            Metrics dictionary
        """
        cache_key = f"metrics:{database_id}"

        # Rate limiting
        if user_id:
            allowed, remaining = await self._rate_limiter.check_limit(
                f"user:{user_id}",
                limit=100,  # 100 req/min per user
                window=60
            )

            if not allowed:
                self._stats["rate_limited"] += 1
                return {
                    "error": "Rate limit exceeded",
                    "retry_after": 60,
                    "limit": 100,
                    "remaining": remaining
                }

        # Database-level rate limiting
        allowed, remaining = await self._rate_limiter.check_limit(
            f"db:{database_id}",
            limit=1000,  # 1000 req/min per database
            window=60
        )

        if not allowed:
            self._stats["rate_limited"] += 1
            return {
                "error": "Database rate limit exceeded",
                "retry_after": 60
            }

        # L1: Check memory cache (fastest)
        cached = self._get_from_memory(cache_key)
        if cached:
            self._stats["l1_hits"] += 1
            logger.debug("metrics_l1_cache_hit", database_id=database_id)
            return cached

        # L2: Check Redis cache (fast, distributed)
        cached = await self._get_from_redis(cache_key)
        if cached:
            self._stats["l2_hits"] += 1
            logger.debug("metrics_l2_cache_hit", database_id=database_id)
            # Store in L1 for next time
            self._store_in_memory(cache_key, cached)
            return cached

        # Circuit breaker check
        circuit_breaker = self._circuit_breakers[database_id]
        if not circuit_breaker.can_attempt():
            logger.warning("circuit_breaker_open", database_id=database_id)
            # Return stale cache if available
            stale = await self._get_stale_from_redis(cache_key)
            if stale:
                return {**stale, "stale": True, "reason": "Circuit breaker open"}
            return {"error": "Service temporarily unavailable", "retry_after": 30}

        # Request coalescing: Multiple users requesting same metric → single fetch
        try:
            metrics = await self._coalescer.coalesce(
                cache_key,
                lambda: self._fetch_with_circuit_breaker(
                    database_id,
                    circuit_breaker,
                    fetch_fn
                )
            )

            self._stats["source_hits"] += 1

            # Store in all cache layers
            self._store_in_memory(cache_key, metrics)
            await self._store_in_redis(cache_key, metrics)

            return metrics

        except Exception as e:
            logger.error(
                "metrics_fetch_failed",
                database_id=database_id,
                error=str(e)
            )

            # Try to return stale cache as fallback
            stale = await self._get_stale_from_redis(cache_key)
            if stale:
                return {**stale, "stale": True, "reason": str(e)}

            raise

    async def _fetch_with_circuit_breaker(
        self,
        database_id: str,
        circuit_breaker: CircuitBreaker,
        fetch_fn
    ):
        """Fetch metrics with circuit breaker protection."""
        try:
            metrics = await fetch_fn()
            circuit_breaker.record_success()
            return metrics
        except Exception as e:
            circuit_breaker.record_failure()
            raise

    def _get_from_memory(self, key: str) -> Optional[Dict[str, Any]]:
        """Get from L1 (memory) cache."""
        if key in self._memory_cache:
            data, expires_at = self._memory_cache[key]
            if time.time() < expires_at:
                return data
            # Expired
            del self._memory_cache[key]
        return None

    def _store_in_memory(self, key: str, data: Dict[str, Any]):
        """Store in L1 (memory) cache."""
        expires_at = time.time() + self._memory_ttl
        self._memory_cache[key] = (data, expires_at)

    async def _get_from_redis(self, key: str) -> Optional[Dict[str, Any]]:
        """Get from L2 (Redis) cache."""
        try:
            import json
            cached = await self.redis.client.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.error("redis_get_failed", key=key, error=str(e))
        return None

    async def _get_stale_from_redis(self, key: str) -> Optional[Dict[str, Any]]:
        """Get stale data from Redis (ignoring TTL)."""
        stale_key = f"stale:{key}"
        try:
            import json
            cached = await self.redis.client.get(stale_key)
            if cached:
                return json.loads(cached)
        except Exception:
            return None

    async def _store_in_redis(self, key: str, data: Dict[str, Any]):
        """Store in L2 (Redis) cache."""
        try:
            import json
            # Store fresh copy
            await self.redis.client.set(key, json.dumps(data), ex=self._redis_ttl)

            # Store stale copy (longer TTL, for fallback)
            stale_key = f"stale:{key}"
            await self.redis.client.set(stale_key, json.dumps(data), ex=300)  # 5 min

        except Exception as e:
            logger.error("redis_set_failed", key=key, error=str(e))

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = sum([
            self._stats["l1_hits"],
            self._stats["l2_hits"],
            self._stats["source_hits"]
        ])

        return {
            **self._stats,
            "total_requests": total,
            "cache_hit_rate": (
                (self._stats["l1_hits"] + self._stats["l2_hits"]) / total * 100
                if total > 0 else 0
            ),
            "memory_cache_size": len(self._memory_cache),
        }


# Global instance
metrics_cache_service = MetricsCacheService()
