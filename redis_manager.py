"""
Redis Manager - Step 6
Pub/Sub system for real-time notifications across server instances
"""

import asyncio
import json
import logging
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass
from enum import Enum
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class RedisChannel(Enum):
    """Redis pub/sub channels"""
    NEW_EMAIL = "mailbox:new_email"
    EMAIL_DELETED = "mailbox:email_deleted"
    EMAIL_MOVED = "mailbox:email_moved"
    EMAIL_READ_STATUS = "mailbox:email_read_status"
    SYNC_COMPLETE = "mailbox:sync_complete"
    WORKER_STATUS = "mailbox:worker_status"
    SYSTEM_NOTIFICATIONS = "mailbox:system"


@dataclass
class RedisMessage:
    """Structured message for pub/sub"""
    channel: str
    event_type: str
    mailbox_id: str
    user_id: str
    data: Dict[str, Any]
    timestamp: float
    
    def to_json(self) -> str:
        return json.dumps({
            "channel": self.channel,
            "event_type": self.event_type,
            "mailbox_id": self.mailbox_id,
            "user_id": self.user_id,
            "data": self.data,
            "timestamp": self.timestamp
        })
    
    @classmethod
    def from_json(cls, json_str: str) -> 'RedisMessage':
        data = json.loads(json_str)
        return cls(
            channel=data["channel"],
            event_type=data["event_type"],
            mailbox_id=data["mailbox_id"],
            user_id=data["user_id"],
            data=data["data"],
            timestamp=data["timestamp"]
        )


class RedisManager:
    """
    Redis Manager for pub/sub messaging
    
    Features:
    - Connection pooling
    - Automatic reconnection
    - Channel subscriptions
    - Message serialization
    - Event callbacks
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        decode_responses: bool = True,
        max_connections: int = 20
    ):
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.decode_responses = decode_responses
        self.max_connections = max_connections
        
        # Redis connection pool
        self._pool: Optional[redis.ConnectionPool] = None
        self._client: Optional[redis.Redis] = None
        
        # Pub/Sub client (separate connection)
        self._pubsub_client: Optional[redis.Redis] = None
        
        # Subscription handlers
        self._subscriptions: Dict[str, List[Callable]] = {}
        
        # Manager state
        self._running = False
        self._connected = False
        self._listener_task: Optional[asyncio.Task] = None
        
        # Statistics
        self._stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "subscriptions": 0,
            "errors": 0
        }
    
    async def connect(self) -> bool:
        """Connect to Redis server"""
        try:
            # Create connection pool
            self._pool = redis.ConnectionPool(
                host=self.host,
                port=self.port,
                db=self.db,
                password=self.password,
                decode_responses=self.decode_responses,
                max_connections=self.max_connections,
                socket_keepalive=True,
                socket_connect_timeout=5
            )
            
            # Create main client
            self._client = redis.Redis(connection_pool=self._pool)
            
            # Test connection
            await self._client.ping()
            
            # Create pub/sub client (separate connection)
            self._pubsub_client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                password=self.password,
                decode_responses=self.decode_responses
            )
            
            self._connected = True
            logger.info(f"Redis connected: {self.host}:{self.port}")
            return True
            
        except Exception as e:
            self._connected = False
            self._client = None
            self._pubsub_client = None
            self._pool = None
            logger.error(f"Redis connection error: {e}")
            return False
    
    @property
    def is_connected(self) -> bool:
        """Whether Redis is available for pub/sub and cache."""
        return self._connected and self._client is not None

    async def disconnect(self):
        """Disconnect from Redis"""
        self._running = False
        self._connected = False
        
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        
        if self._pubsub_client:
            await self._pubsub_client.close()
        
        if self._client:
            await self._client.close()
        
        if self._pool:
            await self._pool.disconnect()
        
        logger.info("Redis disconnected")
    
    async def _ensure_connection(self) -> bool:
        """Ensure Redis connection is active"""
        if not self._connected or self._client is None:
            return await self.connect()
        
        try:
            await self._client.ping()
            return True
        except Exception:
            logger.warning("Redis connection lost, reconnecting...")
            self._connected = False
            return await self.connect()
    
    # ===========================================================================
    # PUBLISH
    # ===========================================================================
    
    async def publish(
        self,
        channel: RedisChannel,
        event_type: str,
        mailbox_id: str,
        user_id: str,
        data: Dict[str, Any]
    ) -> bool:
        """
        Publish a message to a channel
        
        Args:
            channel: Redis channel to publish to
            event_type: Type of event (new_email, deleted, etc.)
            mailbox_id: Mailbox identifier
            user_id: User identifier
            data: Event-specific data
        
        Returns:
            bool: Success status
        """
        if not await self._ensure_connection():
            self._stats["errors"] += 1
            return False
        
        try:
            import time
            message = RedisMessage(
                channel=channel.value,
                event_type=event_type,
                mailbox_id=mailbox_id,
                user_id=user_id,
                data=data,
                timestamp=time.time()
            )
            
            await self._client.publish(channel.value, message.to_json())
            self._stats["messages_sent"] += 1
            
            logger.debug(f"Published to {channel.value}: {event_type}")
            return True
            
        except Exception as e:
            logger.error(f"Publish error: {e}")
            self._stats["errors"] += 1
            return False
    
    async def publish_new_email(
        self,
        mailbox_id: str,
        user_id: str,
        email_data: Dict[str, Any]
    ):
        """Publish new email notification"""
        await self.publish(
            RedisChannel.NEW_EMAIL,
            "new_email",
            mailbox_id,
            user_id,
            email_data
        )
    
    async def publish_email_deleted(
        self,
        mailbox_id: str,
        user_id: str,
        email_uid: int,
        folder: str
    ):
        """Publish email deleted notification"""
        await self.publish(
            RedisChannel.EMAIL_DELETED,
            "email_deleted",
            mailbox_id,
            user_id,
            {"email_uid": email_uid, "folder": folder}
        )
    
    async def publish_email_moved(
        self,
        mailbox_id: str,
        user_id: str,
        email_uid: int,
        from_folder: str,
        to_folder: str
    ):
        """Publish email moved notification"""
        await self.publish(
            RedisChannel.EMAIL_MOVED,
            "email_moved",
            mailbox_id,
            user_id,
            {"email_uid": email_uid, "from_folder": from_folder, "to_folder": to_folder}
        )
    
    async def publish_sync_complete(
        self,
        mailbox_id: str,
        user_id: str,
        new_emails: int,
        folder: str
    ):
        """Publish sync complete notification"""
        await self.publish(
            RedisChannel.SYNC_COMPLETE,
            "sync_complete",
            mailbox_id,
            user_id,
            {"new_emails": new_emails, "folder": folder}
        )
    
    # ===========================================================================
    # SUBSCRIBE
    # ===========================================================================
    
    async def subscribe(
        self,
        channel: RedisChannel,
        callback: Callable[[RedisMessage], None]
    ):
        """
        Subscribe to a channel with a callback
        
        Args:
            channel: Channel to subscribe to
            callback: Function to call when message is received
        """
        if not self.is_connected:
            logger.debug(f"Redis unavailable; skipping subscribe to {channel.value}")
            return

        if channel.value not in self._subscriptions:
            self._subscriptions[channel.value] = []

            if not self._running:
                await self._start_listener()

        if callback not in self._subscriptions[channel.value]:
            self._subscriptions[channel.value].append(callback)
            self._stats["subscriptions"] += 1
        
        logger.info(f"Subscribed to channel: {channel.value}")
    
    async def unsubscribe(
        self,
        channel: RedisChannel,
        callback: Callable[[RedisMessage], None]
    ):
        """Unsubscribe from a channel"""
        if channel.value in self._subscriptions:
            if callback in self._subscriptions[channel.value]:
                self._subscriptions[channel.value].remove(callback)
            
            if not self._subscriptions[channel.value]:
                del self._subscriptions[channel.value]
    
    async def _start_listener(self):
        """Start the pub/sub listener"""
        if self._running or not self.is_connected:
            return
        
        self._running = True
        self._listener_task = asyncio.create_task(self._listener_loop())
    
    async def _listener_loop(self):
        """Main listener loop for subscribed channels"""
        if self._pubsub_client is None:
            logger.warning("Redis pub/sub client unavailable; listener not started")
            self._running = False
            return

        try:
            pubsub = self._pubsub_client.pubsub()
            
            # Subscribe to all channels
            channels = list(self._subscriptions.keys())
            if channels:
                await pubsub.subscribe(*channels)
            
            logger.info(f"Redis listener started, subscribed to {len(channels)} channels")
            
            while self._running:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    
                    if message and message["type"] == "message":
                        channel = message["channel"]
                        data = message["data"]
                        
                        # Parse message
                        redis_msg = RedisMessage.from_json(data)
                        
                        # Call all callbacks for this channel
                        if channel in self._subscriptions:
                            for callback in self._subscriptions[channel]:
                                try:
                                    await callback(redis_msg)
                                except Exception as e:
                                    logger.error(f"Callback error: {e}")
                        
                        self._stats["messages_received"] += 1
                
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Listener error: {e}")
                    await asyncio.sleep(1)
            
            await pubsub.unsubscribe()
            await pubsub.close()
            
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Listener loop error: {e}")
            self._running = False
    
    # ===========================================================================
    # CACHE OPERATIONS
    # ===========================================================================
    
    async def set_cache(self, key: str, value: Any, expire: int = 3600) -> bool:
        """Set a cache value with expiration"""
        if not await self._ensure_connection():
            return False
        
        try:
            await self._client.set(key, json.dumps(value), ex=expire)
            return True
        except Exception as e:
            logger.error(f"Cache set error: {e}")
            return False
    
    async def get_cache(self, key: str) -> Optional[Any]:
        """Get a cache value"""
        if not await self._ensure_connection():
            return None
        
        try:
            value = await self._client.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error(f"Cache get error: {e}")
            return None
    
    async def delete_cache(self, key: str) -> bool:
        """Delete a cache value"""
        if not await self._ensure_connection():
            return False
        
        try:
            await self._client.delete(key)
            return True
        except Exception as e:
            logger.error(f"Cache delete error: {e}")
            return False
    
    async def set_mailbox_state(
        self,
        mailbox_id: str,
        state: Dict[str, Any],
        expire: int = 86400
    ) -> bool:
        """Cache mailbox state"""
        key = f"mailbox:state:{mailbox_id}"
        return await self.set_cache(key, state, expire)
    
    async def get_mailbox_state(self, mailbox_id: str) -> Optional[Dict[str, Any]]:
        """Get cached mailbox state"""
        key = f"mailbox:state:{mailbox_id}"
        return await self.get_cache(key)
    
    async def set_user_session(
        self,
        user_id: str,
        session_data: Dict[str, Any],
        expire: int = 3600
    ) -> bool:
        """Cache user session data"""
        key = f"user:session:{user_id}"
        return await self.set_cache(key, session_data, expire)
    
    async def get_user_session(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get cached user session"""
        key = f"user:session:{user_id}"
        return await self.get_cache(key)
    
    # ===========================================================================
    # STATISTICS
    # ===========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get Redis manager statistics"""
        return {
            **self._stats,
            "subscriptions": len(self._subscriptions),
            "connected": self.is_connected
        }


# =============================================================================
# GLOBAL REDIS MANAGER INSTANCE
# =============================================================================

_redis_manager: Optional[RedisManager] = None


async def get_redis_manager(
    host: str = "localhost",
    port: int = 6379,
    db: int = 0,
    password: Optional[str] = None
) -> RedisManager:
    """
    Get or create the global Redis manager
    
    Args:
        host: Redis server host
        port: Redis server port
        db: Redis database number
        password: Redis password (optional)
    
    Returns:
        RedisManager: The global Redis manager instance
    """
    global _redis_manager
    
    if _redis_manager is None:
        _redis_manager = RedisManager(
            host=host,
            port=port,
            db=db,
            password=password
        )
        
        # Try to connect
        connected = await _redis_manager.connect()
        if not connected:
            logger.warning("Redis connection failed, running in fallback mode")
    
    return _redis_manager


async def shutdown_redis_manager():
    """Shutdown the global Redis manager"""
    global _redis_manager
    
    if _redis_manager:
        await _redis_manager.disconnect()
        _redis_manager = None
        logger.info("Redis Manager shutdown complete")


# =============================================================================
# EVENT BRIDGE - Connect WebSocket to Redis
# =============================================================================

class EventBridge:
    """
    Bridge between Redis pub/sub and WebSocket manager
    Broadcasts Redis events to connected WebSocket clients
    """
    
    def __init__(self):
        self._redis: Optional[RedisManager] = None
        self._ws_manager = None
    
    async def initialize(self, redis_manager: RedisManager, ws_manager):
        """Initialize the event bridge"""
        self._redis = redis_manager
        self._ws_manager = ws_manager

        if not redis_manager.is_connected:
            logger.info("Event bridge skipped (Redis unavailable)")
            return

        for channel in RedisChannel:
            await redis_manager.subscribe(channel, self._handle_redis_message)

        logger.info("Event bridge initialized")
    
    async def _handle_redis_message(self, message: RedisMessage):
        """Handle Redis message and broadcast to WebSocket"""
        if self._ws_manager is None:
            return
        
        try:
            # Prepare WebSocket message
            ws_message = {
                "type": message.event_type,
                "mailbox_id": message.mailbox_id,
                "user_id": message.user_id,
                "data": message.data,
                "timestamp": message.timestamp
            }
            
            # Broadcast to user
            await self._ws_manager.broadcast_to_user(
                message.user_id,
                ws_message
            )
            
            logger.debug(f"Bridged Redis message to WebSocket: {message.event_type}")
            
        except Exception as e:
            logger.error(f"Event bridge error: {e}")


_event_bridge: Optional[EventBridge] = None


async def get_event_bridge() -> EventBridge:
    """Get or create the global event bridge"""
    global _event_bridge
    
    if _event_bridge is None:
        _event_bridge = EventBridge()
    
    return _event_bridge


async def initialize_event_bridge(ws_manager):
    """Initialize the event bridge with dependencies"""
    bridge = await get_event_bridge()
    redis_mgr = await get_redis_manager()
    await bridge.initialize(redis_mgr, ws_manager)
    return bridge