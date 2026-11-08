"""
Webhook Manager - Step 7
Outbound webhook system for email event notifications
"""

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from enum import Enum
import aiohttp

logger = logging.getLogger(__name__)


class WebhookEventType(Enum):
    """Types of webhook events"""
    NEW_EMAIL = "new_email"
    EMAIL_DELETED = "email_deleted"
    EMAIL_MOVED = "email_moved"
    EMAIL_READ = "email_read"
    EMAIL_UNREAD = "email_unread"
    SYNC_COMPLETE = "sync_complete"
    WORKER_STARTED = "worker_started"
    WORKER_STOPPED = "worker_stopped"
    WORKER_ERROR = "worker_error"


@dataclass
class WebhookConfig:
    """Webhook configuration"""
    webhook_id: str
    user_id: str
    mailbox_id: Optional[str] = None
    url: str = ""
    secret: Optional[str] = None
    events: List[WebhookEventType] = field(default_factory=list)
    enabled: bool = True
    retry_count: int = 3
    retry_delay: int = 5
    timeout: int = 30
    headers: Dict[str, str] = field(default_factory=dict)
    
    def should_handle_event(self, event_type: WebhookEventType) -> bool:
        """Check if this webhook should handle the given event type"""
        return event_type in self.events


@dataclass
class WebhookDelivery:
    """Webhook delivery record"""
    delivery_id: str
    webhook_id: str
    event_type: str
    payload: Dict[str, Any]
    timestamp: float
    attempts: int = 0
    last_attempt: Optional[float] = None
    status: str = "pending"  # pending, delivered, failed
    response_code: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None


class WebhookManager:
    """
    Webhook Manager for outbound event notifications
    
    Features:
    - Configurable webhooks per mailbox/user
    - HMAC signature verification
    - Automatic retries with exponential backoff
    - Event filtering
    - Delivery tracking
    - Timeout handling
    """
    
    def __init__(
        self,
        max_concurrent_deliveries: int = 20,
        default_timeout: int = 30,
        max_retries: int = 3
    ):
        self.max_concurrent_deliveries = max_concurrent_deliveries
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        
        # Webhook configurations
        self._webhooks: Dict[str, WebhookConfig] = {}
        
        # Delivery queue
        self._delivery_queue: asyncio.Queue = asyncio.Queue()
        
        # Active deliveries
        self._active_deliveries: Dict[str, asyncio.Task] = {}
        
        # Delivery history
        self._delivery_history: List[WebhookDelivery] = []
        self._max_history = 1000
        
        # Statistics
        self._stats = {
            "total_deliveries": 0,
            "successful_deliveries": 0,
            "failed_deliveries": 0,
            "retries": 0
        }
        
        # Running state
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        
        # HTTP session
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def start(self):
        """Start the webhook manager"""
        if self._running:
            return
        
        self._running = True
        
        # Create HTTP session
        timeout = aiohttp.ClientTimeout(total=self.default_timeout)
        self._session = aiohttp.ClientSession(timeout=timeout)
        
        # Start worker
        self._worker_task = asyncio.create_task(self._worker_loop())
        
        logger.info("Webhook Manager started")
    
    async def stop(self):
        """Stop the webhook manager"""
        self._running = False
        
        # Cancel active deliveries
        for task in self._active_deliveries.values():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Close HTTP session
        if self._session:
            await self._session.close()
        
        # Wait for worker to finish
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Webhook Manager stopped")
    
    # ===========================================================================
    # WEBHOOK MANAGEMENT
    # ===========================================================================
    
    async def register_webhook(
        self,
        webhook_id: str,
        user_id: str,
        url: str,
        events: List[WebhookEventType],
        secret: Optional[str] = None,
        mailbox_id: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> bool:
        """
        Register a new webhook
        
        Args:
            webhook_id: Unique webhook identifier
            user_id: User identifier
            url: Webhook endpoint URL
            events: List of event types to subscribe to
            secret: Optional HMAC secret for signature
            mailbox_id: Optional mailbox ID filter
            headers: Optional custom headers
        
        Returns:
            bool: Success status
        """
        config = WebhookConfig(
            webhook_id=webhook_id,
            user_id=user_id,
            mailbox_id=mailbox_id,
            url=url,
            secret=secret,
            events=events,
            headers=headers or {}
        )
        
        self._webhooks[webhook_id] = config
        logger.info(f"Registered webhook: {webhook_id} for user {user_id}")
        
        return True
    
    async def unregister_webhook(self, webhook_id: str) -> bool:
        """Unregister a webhook"""
        if webhook_id in self._webhooks:
            del self._webhooks[webhook_id]
            logger.info(f"Unregistered webhook: {webhook_id}")
            return True
        return False
    
    async def update_webhook(
        self,
        webhook_id: str,
        updates: Dict[str, Any]
    ) -> bool:
        """Update webhook configuration"""
        if webhook_id not in self._webhooks:
            return False
        
        config = self._webhooks[webhook_id]
        
        for key, value in updates.items():
            if hasattr(config, key):
                setattr(config, key, value)
        
        return True
    
    def get_webhook(self, webhook_id: str) -> Optional[WebhookConfig]:
        """Get webhook configuration"""
        return self._webhooks.get(webhook_id)
    
    def get_user_webhooks(self, user_id: str) -> List[WebhookConfig]:
        """Get all webhooks for a user"""
        return [
            w for w in self._webhooks.values()
            if w.user_id == user_id
        ]
    
    def get_mailbox_webhooks(self, mailbox_id: str) -> List[WebhookConfig]:
        """Get all webhooks for a mailbox"""
        return [
            w for w in self._webhooks.values()
            if w.mailbox_id == mailbox_id
        ]
    
    # ===========================================================================
    # EVENT DISPATCH
    # ===========================================================================
    
    async def dispatch_event(
        self,
        event_type: WebhookEventType,
        mailbox_id: str,
        user_id: str,
        data: Dict[str, Any]
    ):
        """
        Dispatch an event to all matching webhooks
        
        Args:
            event_type: Type of event
            mailbox_id: Mailbox identifier
            user_id: User identifier
            data: Event data
        """
        # Find matching webhooks
        matching_webhooks = []
        
        for webhook in self._webhooks.values():
            if not webhook.enabled:
                continue
            
            # Check event type match
            if not webhook.should_handle_event(event_type):
                continue
            
            # Check mailbox filter
            if webhook.mailbox_id and webhook.mailbox_id != mailbox_id:
                continue
            
            matching_webhooks.append(webhook)
        
        if not matching_webhooks:
            return
        
        # Create payload
        payload = {
            "event": event_type.value,
            "mailbox_id": mailbox_id,
            "user_id": user_id,
            "timestamp": time.time(),
            "data": data
        }
        
        # Queue deliveries
        for webhook in matching_webhooks:
            delivery = WebhookDelivery(
                delivery_id=f"{webhook.webhook_id}_{int(time.time() * 1000)}",
                webhook_id=webhook.webhook_id,
                event_type=event_type.value,
                payload=payload
            )
            
            await self._delivery_queue.put(delivery)
        
        logger.info(f"Dispatched {event_type.value} to {len(matching_webhooks)} webhooks")
    
    # ===========================================================================
    # DELIVERY WORKER
    # ===========================================================================
    
    async def _worker_loop(self):
        """Main worker loop for processing deliveries"""
        while self._running:
            try:
                # Get delivery from queue
                delivery = await asyncio.wait_for(
                    self._delivery_queue.get(),
                    timeout=1.0
                )
                
                # Process delivery
                asyncio.create_task(self._process_delivery(delivery))
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
    
    async def _process_delivery(self, delivery: WebhookDelivery):
        """Process a single webhook delivery"""
        webhook = self._webhooks.get(delivery.webhook_id)
        if not webhook:
            logger.warning(f"Webhook not found: {delivery.webhook_id}")
            return
        
        # Track in active deliveries
        self._active_deliveries[delivery.delivery_id] = asyncio.current_task()
        
        try:
            await self._deliver_webhook(webhook, delivery)
        except Exception as e:
            logger.error(f"Delivery error: {e}")
            delivery.error = str(e)
            delivery.status = "failed"
        finally:
            # Remove from active deliveries
            if delivery.delivery_id in self._active_deliveries:
                del self._active_deliveries[delivery.delivery_id]
            
            # Add to history
            self._add_to_history(delivery)
            
            # Update stats
            self._stats["total_deliveries"] += 1
            if delivery.status == "delivered":
                self._stats["successful_deliveries"] += 1
            else:
                self._stats["failed_deliveries"] += 1
    
    async def _deliver_webhook(
        self,
        webhook: WebhookConfig,
        delivery: WebhookDelivery
    ):
        """Deliver webhook with retries"""
        url = webhook.url
        payload = delivery.payload
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "FV-S-Webhook/1.0",
            **webhook.headers
        }
        
        # Add HMAC signature if secret is configured
        if webhook.secret:
            signature = self._generate_signature(payload, webhook.secret)
            headers["X-Webhook-Signature"] = signature
        
        for attempt in range(webhook.retry_count + 1):
            delivery.attempts += 1
            delivery.last_attempt = time.time()
            
            try:
                async with self._session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=webhook.timeout)
                ) as response:
                    delivery.response_code = response.status
                    delivery.response_body = await response.text()
                    
                    if response.status < 400:
                        delivery.status = "delivered"
                        logger.info(f"Webhook delivered: {webhook.webhook_id}")
                        return
                    else:
                        delivery.error = f"HTTP {response.status}"
                        logger.warning(
                            f"Webhook delivery failed ({response.status}): "
                            f"{webhook.webhook_id}"
                        )
            
            except asyncio.TimeoutError:
                delivery.error = "Timeout"
                logger.warning(f"Webhook timeout: {webhook.webhook_id}")
            
            except Exception as e:
                delivery.error = str(e)
                logger.error(f"Webhook delivery error: {e}")
            
            # Retry with exponential backoff
            if attempt < webhook.retry_count:
                delay = webhook.retry_delay * (2 ** attempt)
                self._stats["retries"] += 1
                await asyncio.sleep(delay)
        
        delivery.status = "failed"
    
    def _generate_signature(self, payload: Dict[str, Any], secret: str) -> str:
        """Generate HMAC-SHA256 signature"""
        import json
        payload_str = json.dumps(payload, sort_keys=True)
        signature = hmac.new(
            secret.encode(),
            payload_str.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"
    
    def _add_to_history(self, delivery: WebhookDelivery):
        """Add delivery to history"""
        self._delivery_history.append(delivery)
        
        # Trim history if too large
        if len(self._delivery_history) > self._max_history:
            self._delivery_history = self._delivery_history[-self._max_history:]
    
    # ===========================================================================
    # STATISTICS
    # ===========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get webhook manager statistics"""
        return {
            **self._stats,
            "webhooks_registered": len(self._webhooks),
            "active_deliveries": len(self._active_deliveries),
            "queue_size": self._delivery_queue.qsize(),
            "recent_deliveries": len(self._delivery_history)
        }
    
    def get_recent_deliveries(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent delivery records"""
        recent = self._delivery_history[-limit:]
        return [
            {
                "delivery_id": d.delivery_id,
                "webhook_id": d.webhook_id,
                "event_type": d.event_type,
                "status": d.status,
                "attempts": d.attempts,
                "timestamp": d.timestamp,
                "response_code": d.response_code,
                "error": d.error
            }
            for d in recent
        ]


# =============================================================================
# GLOBAL WEBHOOK MANAGER INSTANCE
# =============================================================================

_webhook_manager: Optional[WebhookManager] = None


async def get_webhook_manager() -> WebhookManager:
    """Get or create the global webhook manager"""
    global _webhook_manager
    
    if _webhook_manager is None:
        _webhook_manager = WebhookManager(
            max_concurrent_deliveries=20,
            default_timeout=30,
            max_retries=3
        )
        await _webhook_manager.start()
    
    return _webhook_manager


async def shutdown_webhook_manager():
    """Shutdown the global webhook manager"""
    global _webhook_manager
    
    if _webhook_manager:
        await _webhook_manager.stop()
        _webhook_manager = None
        logger.info("Webhook Manager shutdown complete")


# =============================================================================
# WEBHOOK REGISTRATION HELPERS
# =============================================================================

async def register_user_webhook(
    user_id: str,
    url: str,
    events: List[str],
    secret: Optional[str] = None
) -> str:
    """
    Register a webhook for a user
    
    Args:
        user_id: User identifier
        url: Webhook endpoint URL
        events: List of event type strings
        secret: Optional HMAC secret
    
    Returns:
        str: Webhook ID
    """
    import uuid
    
    webhook_id = str(uuid.uuid4())
    
    # Convert string events to enum
    event_types = []
    for event_str in events:
        try:
            event_type = WebhookEventType(event_str)
            event_types.append(event_type)
        except ValueError:
            logger.warning(f"Unknown event type: {event_str}")
    
    manager = await get_webhook_manager()
    await manager.register_webhook(
        webhook_id=webhook_id,
        user_id=user_id,
        url=url,
        events=event_types,
        secret=secret
    )
    
    return webhook_id


async def dispatch_email_event(
    event_type: str,
    mailbox_id: str,
    user_id: str,
    data: Dict[str, Any]
):
    """
    Dispatch an email event to webhooks
    
    Args:
        event_type: Event type string
        mailbox_id: Mailbox identifier
        user_id: User identifier
        data: Event data
    """
    try:
        event = WebhookEventType(event_type)
        manager = await get_webhook_manager()
        await manager.dispatch_event(event, mailbox_id, user_id, data)
    except ValueError:
        logger.warning(f"Unknown event type: {event_type}")