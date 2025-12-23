"""
Version cache service for caching KubeDB version CRD data.

Caches version information to avoid expensive Kubernetes API calls.
Uses Redis for distributed caching with 2-hour TTL.
"""
import json
import time
from typing import Dict, List, Optional, Any
from collections import defaultdict

from app.config.redis import RedisConnection
from app.config.logging import get_logger

logger = get_logger(__name__)


class VersionCacheService:
    """
    Cache service for KubeDB version CRD data.
    
    Architecture:
    - L1 Cache: In-memory (process-local, 30s TTL)
    - L2 Cache: Redis (distributed, 2 hours TTL)
    - Source: Kubernetes API (KubeDB version CRDs)
    """

    def __init__(self):
        self.redis = RedisConnection()
        
        # L1 Cache: In-memory
        self._memory_cache: Dict[str, tuple[Any, float]] = {}
        self._memory_ttl = 30  # 30 seconds
        
        # L2 Cache: Redis TTL
        self._redis_ttl = 7200  # 2 hours
        
        # Stats
        self._stats = {
            "l1_hits": 0,
            "l2_hits": 0,
            "source_hits": 0,
            "cache_misses": 0,
        }

    def _get_cache_key(self, engine: str, provider_id: str) -> str:
        """Generate cache key for version data."""
        return f"versions:{engine}:{provider_id}"

    def _get_from_memory(self, key: str) -> Optional[List[Dict[str, Any]]]:
        """Get from L1 (memory) cache."""
        if key in self._memory_cache:
            data, expires_at = self._memory_cache[key]
            if time.time() < expires_at:
                self._stats["l1_hits"] += 1
                return data
            # Expired
            del self._memory_cache[key]
        return None

    def _store_in_memory(self, key: str, data: List[Dict[str, Any]]):
        """Store in L1 (memory) cache."""
        expires_at = time.time() + self._memory_ttl
        self._memory_cache[key] = (data, expires_at)

    async def _get_from_redis(self, key: str) -> Optional[List[Dict[str, Any]]]:
        """Get from L2 (Redis) cache."""
        try:
            cached = await self.redis.client.get(key)
            if cached:
                self._stats["l2_hits"] += 1
                return json.loads(cached)
        except Exception as e:
            logger.error("redis_get_failed", key=key, error=str(e))
        return None

    async def _store_in_redis(self, key: str, data: List[Dict[str, Any]]):
        """Store in L2 (Redis) cache."""
        try:
            await self.redis.client.set(
                key,
                json.dumps(data),
                ex=self._redis_ttl
            )
        except Exception as e:
            logger.error("redis_set_failed", key=key, error=str(e))

    async def get_versions(
        self,
        engine: str,
        provider_id: str,
        fetch_fn,
    ) -> List[Dict[str, Any]]:
        """
        Get versions with caching.
        
        Args:
            engine: Database engine (mongodb, postgres, mysql, etc.)
            provider_id: Provider ID
            fetch_fn: Async function to fetch versions from source (K8s API)
        
        Returns:
            List of version dictionaries
        """
        cache_key = self._get_cache_key(engine, provider_id)
        
        # Try L1 cache (memory)
        cached = self._get_from_memory(cache_key)
        if cached is not None:
            logger.debug("version_cache_l1_hit", engine=engine, provider_id=provider_id)
            return cached
        
        # Try L2 cache (Redis)
        cached = await self._get_from_redis(cache_key)
        if cached is not None:
            logger.debug("version_cache_l2_hit", engine=engine, provider_id=provider_id)
            # Store in L1 for faster subsequent access
            self._store_in_memory(cache_key, cached)
            return cached
        
        # Cache miss - fetch from source
        self._stats["cache_misses"] += 1
        logger.info("version_cache_miss", engine=engine, provider_id=provider_id)
        
        try:
            # Fetch from source (Kubernetes API)
            result = await fetch_fn()
            versions = result.get("versions", []) if isinstance(result, dict) else result
            
            # Store in both caches
            self._store_in_memory(cache_key, versions)
            await self._store_in_redis(cache_key, versions)
            
            self._stats["source_hits"] += 1
            return versions
            
        except Exception as e:
            logger.error(
                "version_fetch_failed",
                engine=engine,
                provider_id=provider_id,
                error=str(e),
            )
            raise

    async def invalidate_cache(self, engine: str, provider_id: str):
        """
        Invalidate cache for a specific engine and provider.
        
        Args:
            engine: Database engine
            provider_id: Provider ID
        """
        cache_key = self._get_cache_key(engine, provider_id)
        
        # Remove from L1 cache
        if cache_key in self._memory_cache:
            del self._memory_cache[cache_key]
        
        # Remove from L2 cache (Redis)
        try:
            await self.redis.client.delete(cache_key)
            logger.info("version_cache_invalidated", engine=engine, provider_id=provider_id)
        except Exception as e:
            logger.error("version_cache_invalidation_failed", key=cache_key, error=str(e))

    async def invalidate_provider_cache(self, provider_id: str):
        """
        Invalidate all version caches for a provider.
        
        Args:
            provider_id: Provider ID
        """
        engines = ["mongodb", "postgres", "mysql", "redis", "elasticsearch"]
        for engine in engines:
            await self.invalidate_cache(engine, provider_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = sum([
            self._stats["l1_hits"],
            self._stats["l2_hits"],
            self._stats["source_hits"],
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
version_cache_service = VersionCacheService()

