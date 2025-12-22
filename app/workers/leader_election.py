"""
Leader election using Redis for reconciliation worker.
Ensures only ONE pod runs reconciliation at a time.
"""
import asyncio
from typing import Optional

from app.config.redis import RedisConnection
from app.config.logging import get_logger

logger = get_logger(__name__)


class LeaderElection:
    """
    Simple leader election using Redis SET with NX and EX.
    
    Ensures only ONE pod runs reconciliation even with multiple replicas.
    """
    
    def __init__(self, instance_id: str, lease_duration: int = 30):
        """
        Initialize leader election.
        
        Args:
            instance_id: Unique instance identifier
            lease_duration: Lease duration in seconds
        """
        self.instance_id = instance_id
        self.lease_duration = lease_duration
        self.leader_key = "kubedb:leader:reconciler"
        self.is_leader = False
    
    async def acquire_leadership(self) -> bool:
        """Try to acquire leadership."""
        redis = await RedisConnection.get_client()
        
        # Try to set leader key (only if not exists)
        acquired = await redis.set(
            self.leader_key,
            self.instance_id,
            nx=True,
            ex=self.lease_duration,
        )
        
        if acquired:
            if not self.is_leader:
                logger.info("leadership_acquired", instance_id=self.instance_id)
            self.is_leader = True
            return True
        
        # Check if we're already the leader
        current_leader = await redis.get(self.leader_key)
        
        if current_leader == self.instance_id:
            self.is_leader = True
            return True
        
        if self.is_leader:
            logger.info("leadership_lost", instance_id=self.instance_id)
        
        self.is_leader = False
        return False
    
    async def renew_lease(self) -> bool:
        """Renew leadership lease."""
        if not self.is_leader:
            return False
        
        redis = await RedisConnection.get_client()
        current_leader = await redis.get(self.leader_key)
        
        if current_leader == self.instance_id:
            await redis.expire(self.leader_key, self.lease_duration)
            logger.debug("leadership_lease_renewed", instance_id=self.instance_id)
            return True
        else:
            self.is_leader = False
            return False
    
    async def release_leadership(self):
        """Release leadership (on shutdown)."""
        if not self.is_leader:
            return
        
        redis = await RedisConnection.get_client()
        current_leader = await redis.get(self.leader_key)
        
        if current_leader == self.instance_id:
            await redis.delete(self.leader_key)
            logger.info("leadership_released", instance_id=self.instance_id)
        
        self.is_leader = False
