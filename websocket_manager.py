"""
WebSocket Manager - Step 5
Real-time email notifications via WebSocket
"""

import asyncio
import json
from typing import Dict, Set, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from fastapi import WebSocket, WebSocketDisconnect
import logging

logger = logging.getLogger(__name__)


@dataclass
class ConnectionInfo:
    """Information about a WebSocket connection"""
    websocket: WebSocket
    user_id: str
    connected_at: datetime = field(default_factory=datetime.now)
    subscribed_mailboxes: Set[str] = field(default_factory=set)
    last_ping: datetime = field(default_factory=datetime.now)


class WebSocketManager:
    """
    WebSocket Manager for real-time email notifications
    
    Features:
    - Multiple connections per user
    - Per-mailbox subscriptions
    - Heartbeat/ping-pong
    - Message broadcasting
    - Connection state management
    """
    
    def __init__(
        self,
        heartbeat_interval: int = 30,    # seconds
        heartbeat_timeout: int = 60,     # seconds
        max_connections_per_user: int = 5
    ):
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.max_connections_per_user = max_connections_per_user
        
        # Connections storage: user_id -> list of ConnectionInfo
        self._connections: Dict[str, list] = {}
        
        # All connections for broadcasting
        self._all_connections: list = []
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Event callbacks
        self._callbacks: list = []
    
    async def start(self):
        """Start the WebSocket manager"""
        if self._running:
            return
        
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("WebSocket Manager started")
    
    async def stop(self):
        """Stop the WebSocket manager and close all connections"""
        self._running = False
        
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        
        # Close all connections
        async with self._lock:
            for conn_info in self._all_connections:
                try:
                    await conn_info.websocket.close()
                except Exception:
                    pass
            self._connections.clear()
            self._all_connections.clear()
        
        logger.info("WebSocket Manager stopped")
    
    # ===========================================================================
    # CONNECTION MANAGEMENT
    # ===========================================================================
    
    async def connect(
        self,
        websocket: WebSocket,
        user_id: str,
        token: str = None
    ) -> bool:
        """
        Accept and register a new WebSocket connection
        
        Args:
            websocket: FastAPI WebSocket
            user_id: User identifier
            token: Optional auth token
        
        Returns:
            bool: Connection accepted
        """
        try:
            # Accept connection
            await websocket.accept()
            
            # Check connection limit
            async with self._lock:
                user_connections = self._connections.get(user_id, [])
                
                if len(user_connections) >= self.max_connections_per_user:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Max connections reached"
                    })
                    await websocket.close()
                    return False
                
                # Create connection info
                conn_info = ConnectionInfo(
                    websocket=websocket,
                    user_id=user_id
                )
                
                # Add to storage
                if user_id not in self._connections:
                    self._connections[user_id] = []
                
                self._connections[user_id].append(conn_info)
                self._all_connections.append(conn_info)
            
            # Send welcome message
            await websocket.send_json({
                "type": "connected",
                "user_id": user_id,
                "timestamp": datetime.now().isoformat()
            })
            
            logger.info(f"WebSocket connected: user={user_id}")
            return True
            
        except Exception as e:
            logger.error(f"WebSocket connect error: {e}")
            return False
    
    async def disconnect(self, websocket: WebSocket):
        """Handle WebSocket disconnection"""
        async with self._lock:
            # Find and remove connection
            for user_id, connections in list(self._connections.items()):
                for conn_info in connections:
                    if conn_info.websocket == websocket:
                        connections.remove(conn_info)
                        if websocket in self._all_connections:
                            self._all_connections.remove(websocket)
                        
                        if not connections:
                            del self._connections[user_id]
                        
                        logger.info(f"WebSocket disconnected: user={user_id}")
                        return
    
    async def handle_message(self, websocket: WebSocket, message: dict):
        """
        Handle incoming WebSocket message
        
        Message types:
        - subscribe: Subscribe to mailbox updates
        - unsubscribe: Unsubscribe from mailbox
        - ping: Heartbeat
        """
        msg_type = message.get("type")
        
        # Find connection info
        conn_info = None
        for user_id, connections in self._connections.items():
            for conn in connections:
                if conn.websocket == websocket:
                    conn_info = conn
                    break
        
        if not conn_info:
            return
        
        if msg_type == "subscribe":
            mailbox_id = message.get("mailbox_id")
            if mailbox_id:
                conn_info.subscribed_mailboxes.add(mailbox_id)
                await websocket.send_json({
                    "type": "subscribed",
                    "mailbox_id": mailbox_id
                })
                logger.info(f"User {conn_info.user_id} subscribed to {mailbox_id}")
        
        elif msg_type == "unsubscribe":
            mailbox_id = message.get("mailbox_id")
            if mailbox_id and mailbox_id in conn_info.subscribed_mailboxes:
                conn_info.subscribed_mailboxes.remove(mailbox_id)
                await websocket.send_json({
                    "type": "unsubscribed",
                    "mailbox_id": mailbox_id
                })
        
        elif msg_type == "ping":
            conn_info.last_ping = datetime.now()
            await websocket.send_json({
                "type": "pong",
                "timestamp": datetime.now().isoformat()
            })
        
        elif msg_type == "get_status":
            await websocket.send_json({
                "type": "status",
                "subscribed_mailboxes": list(conn_info.subscribed_mailboxes)
            })
    
    # ===========================================================================
    # NOTIFICATIONS
    # ===========================================================================
    
    async def notify_new_email(self, mailbox_id: str, email_data: dict):
        """
        Notify subscribed clients of new email
        
        Args:
            mailbox_id: Mailbox that received new email
            email_data: New email data
        """
        notification = {
            "type": "new_email",
            "mailbox_id": mailbox_id,
            "email": email_data,
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast(notification, mailbox_id)
    
    async def notify_email_deleted(self, mailbox_id: str, email_uid: int):
        """Notify of deleted email"""
        notification = {
            "type": "email_deleted",
            "mailbox_id": mailbox_id,
            "email_uid": email_uid,
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast(notification, mailbox_id)
    
    async def notify_email_moved(
        self,
        mailbox_id: str,
        email_uid: int,
        from_folder: str,
        to_folder: str
    ):
        """Notify of moved email"""
        notification = {
            "type": "email_moved",
            "mailbox_id": mailbox_id,
            "email_uid": email_uid,
            "from_folder": from_folder,
            "to_folder": to_folder,
            "timestamp": datetime.now().isoformat()
        }
        
        # Notify both folders
        await self._broadcast(notification, mailbox_id)
    
    async def notify_email_read(
        self,
        mailbox_id: str,
        email_uid: int,
        is_read: bool
    ):
        """Notify of read status change"""
        notification = {
            "type": "email_read_status",
            "mailbox_id": mailbox_id,
            "email_uid": email_uid,
            "is_read": is_read,
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast(notification, mailbox_id)
    
    async def notify_sync_complete(
        self,
        mailbox_id: str,
        new_emails: int,
        total_emails: int
    ):
        """Notify sync completion"""
        notification = {
            "type": "sync_complete",
            "mailbox_id": mailbox_id,
            "new_emails": new_emails,
            "total_emails": total_emails,
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast(notification, mailbox_id)
    
    async def _broadcast(self, message: dict, mailbox_id: str = None):
        """
        Broadcast message to subscribed connections
        
        Args:
            message: Message to send
            mailbox_id: Optional mailbox filter
        """
        async with self._lock:
            for conn_info in self._all_connections:
                try:
                    # Check if subscribed
                    if mailbox_id and mailbox_id not in conn_info.subscribed_mailboxes:
                        continue
                    
                    await conn_info.websocket.send_json(message)
                
                except Exception as e:
                    logger.error(f"Broadcast error: {e}")
    
    # ===========================================================================
    # HEARTBEAT
    # ===========================================================================
    
    async def _heartbeat_loop(self):
        """Send periodic heartbeats and check timeouts"""
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                await self._check_heartbeats()
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
    
    async def _check_heartbeats(self):
        """Check for timed out connections"""
        async with self._lock:
            current_time = datetime.now()
            timed_out = []
            
            for conn_info in self._all_connections:
                try:
                    elapsed = (current_time - conn_info.last_ping).total_seconds()
                    
                    if elapsed > self.heartbeat_timeout:
                        timed_out.append(conn_info)
                
                except Exception:
                    timed_out.append(conn_info)
            
            # Close timed out connections
            for conn_info in timed_out:
                try:
                    await conn_info.websocket.close()
                    logger.warning(f"Connection timeout: user={conn_info.user_id}")
                except Exception:
                    pass
                
                # Remove from storage
                if conn_info in self._all_connections:
                    self._all_connections.remove(conn_info)
                
                user_connections = self._connections.get(conn_info.user_id, [])
                if conn_info in user_connections:
                    user_connections.remove(conn_info)
                
                if not user_connections:
                    del self._connections[conn_info.user_id]
    
    # ===========================================================================
    # STATISTICS
    # ===========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get WebSocket manager statistics"""
        return {
            "total_connections": len(self._all_connections),
            "total_users": len(self._connections),
            "connections_by_user": {
                user_id: len(connections)
                for user_id, connections in self._connections.items()
            }
        }


# =============================================================================
# GLOBAL WEBSOCKET MANAGER INSTANCE
# =============================================================================

_websocket_manager: Optional[WebSocketManager] = None


async def get_websocket_manager() -> WebSocketManager:
    """Get or create the global WebSocket manager"""
    global _websocket_manager
    
    if _websocket_manager is None:
        _websocket_manager = WebSocketManager(
            heartbeat_interval=30,
            heartbeat_timeout=60,
            max_connections_per_user=5
        )
        await _websocket_manager.start()
    
    return _websocket_manager


async def shutdown_websocket_manager():
    """Shutdown the global WebSocket manager"""
    global _websocket_manager
    
    if _websocket_manager:
        await _websocket_manager.stop()
        _websocket_manager = None


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

async def notify_new_email(mailbox_id: str, email_data: dict):
    """Helper to notify new email (can be called from anywhere)"""
    manager = await get_websocket_manager()
    await manager.notify_new_email(mailbox_id, email_data)


async def notify_sync_complete(mailbox_id: str, new_emails: int, total_emails: int):
    """Helper to notify sync completion"""
    manager = await get_websocket_manager()
    await manager.notify_sync_complete(mailbox_id, new_emails, total_emails)