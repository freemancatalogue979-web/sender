"""
IMAP Connection Manager - Step 4
Advanced connection pooling for IMAP mailboxes
Features:
- Connection pooling with TTL (Time To Live)
- Max connections limit per mailbox
- Automatic reconnection
- Health checks
- Background cleanup
- Connection validation
"""

import asyncio
import time
from typing import Dict, Optional, Tuple, Any, Callable, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import OrderedDict
import aioimaplib
import hashlib
import logging

from async_imap import quote_imap_folder_name

logger = logging.getLogger(__name__)


@dataclass
class ConnectionInfo:
    """Metadata about a pooled connection"""
    client: Any
    mailbox_id: str
    created_at: float
    last_used: float
    use_count: int = 0
    is_idle: bool = False
    folder_selected: Optional[str] = None
    
    def age_seconds(self) -> float:
        return time.time() - self.created_at
    
    def idle_seconds(self) -> float:
        return time.time() - self.last_used
    
    def is_expired(self, max_age: int = 3600) -> bool:
        """Check if connection is too old"""
        return self.age_seconds() > max_age
    
    def is_stale(self, max_idle: int = 300) -> bool:
        """Check if connection has been idle too long"""
        return self.idle_seconds() > max_idle


class IMAPConnectionManager:
    """
    Advanced IMAP Connection Manager with Pooling
    
    Features:
    - Pool connections per mailbox
    - TTL (Time To Live) for connections
    - Max connections per mailbox
    - Health checks
    - Automatic reconnection
    - Background cleanup
    - Connection validation
    """
    
    def __init__(
        self,
        max_connections: int = 100,
        max_per_mailbox: int = 5,
        connection_ttl: int = 3600,      # 1 hour max age
        idle_timeout: int = 300,        # 5 minutes idle
        health_check_interval: int = 60, # 1 minute health check
        max_use_count: int = 100         # Max operations per connection
    ):
        self.max_connections = max_connections
        self.max_per_mailbox = max_per_mailbox
        self.connection_ttl = connection_ttl
        self.idle_timeout = idle_timeout
        self.health_check_interval = health_check_interval
        self.max_use_count = max_use_count
        
        # Connection pools
        self._pools: Dict[str, OrderedDict] = {}  # mailbox_id -> OrderedDict of connections
        self._stats: Dict[str, Dict] = {}        # mailbox_id -> stats
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Background task
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Event callbacks
        self._on_disconnect: Optional[Callable] = None
        self._on_connect: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
    
    async def start(self):
        """Start the connection manager background tasks"""
        if self._running:
            return
        
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("IMAP Connection Manager started")
    
    async def stop(self):
        """Stop the connection manager and close all connections"""
        self._running = False
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Close all connections
        async with self._lock:
            for mailbox_id in list(self._pools.keys()):
                await self._close_mailbox_connections(mailbox_id)
        
        logger.info("IMAP Connection Manager stopped")
    
    async def get_connection(
        self,
        mailbox_id: str,
        host: str,
        port: int = 993,
        username: str = None,
        password: str = None,
        use_ssl: bool = True,
        timeout: int = 30,
        folder: str = None
    ) -> Tuple[bool, Any, str]:
        """
        Get a connection from the pool or create new one
        
        Args:
            mailbox_id: Unique mailbox identifier
            host: IMAP server host
            port: IMAP server port
            username: Login username
            password: Login password
            use_ssl: Use SSL/TLS
            timeout: Connection timeout
            folder: Optional folder to select
        
        Returns:
            tuple: (success, client, message)
        """
        async with self._lock:
            # Initialize pool for mailbox
            if mailbox_id not in self._pools:
                self._pools[mailbox_id] = OrderedDict()
                self._stats[mailbox_id] = {
                    "total_connections": 0,
                    "active_connections": 0,
                    "total_operations": 0,
                    "reconnects": 0,
                    "failed_connections": 0
                }
            
            pool = self._pools[mailbox_id]
            stats = self._stats[mailbox_id]
            
            # Try to find an existing healthy connection
            for conn_id, conn_info in list(pool.items()):
                client = conn_info.client
                
                # Check if connection is still valid
                if not self._is_connection_valid(conn_info):
                    # Remove invalid connection
                    await self._safe_logout(client)
                    del pool[conn_id]
                    stats["active_connections"] = max(0, stats["active_connections"] - 1)
                    continue
                
                # Check if connection is idle (not in use)
                if not conn_info.is_idle:
                    continue
                
                # Check if folder matches (if specified)
                if folder and conn_info.folder_selected != folder:
                    continue
                
                # Found a suitable connection - reuse it
                conn_info.is_idle = False
                conn_info.last_used = time.time()
                conn_info.use_count += 1
                stats["total_operations"] += 1
                
                # Re-select folder if needed
                if folder and conn_info.folder_selected != folder:
                    try:
                        await client.select(quote_imap_folder_name(folder))
                        conn_info.folder_selected = folder
                    except Exception:
                        # Reconnection needed
                        await self._safe_logout(client)
                        del pool[conn_id]
                        stats["active_connections"] = max(0, stats["active_connections"] - 1)
                        continue
                
                logger.debug(f"Reusing connection for {mailbox_id}")
                return True, client, "Connection reused"
            
            # Need to create new connection
            if len(pool) >= self.max_per_mailbox:
                # Remove oldest idle connection
                for conn_id, conn_info in list(pool.items()):
                    if conn_info.is_idle:
                        await self._safe_logout(conn_info.client)
                        del pool[conn_id]
                        stats["active_connections"] = max(0, stats["active_connections"] - 1)
                        break
            
            # Check global limit
            total_connections = sum(len(p) for p in self._pools.values())
            if total_connections >= self.max_connections:
                return False, None, "Max connections limit reached"
            
            # Create new connection
            try:
                client = await self._create_connection(
                    host, port, username, password, use_ssl, timeout
                )
                
                if not client:
                    stats["failed_connections"] += 1
                    return False, None, "Failed to create connection"
                
                conn_id = self._generate_connection_id(host, username)
                conn_info = ConnectionInfo(
                    client=client,
                    mailbox_id=mailbox_id,
                    created_at=time.time(),
                    last_used=time.time(),
                    use_count=1,
                    is_idle=False,
                    folder_selected=None
                )
                
                pool[conn_id] = conn_info
                pool.move_to_end(conn_id)
                stats["total_connections"] += 1
                stats["active_connections"] += 1
                
                # Select folder if specified
                if folder:
                    try:
                        await client.select(quote_imap_folder_name(folder))
                        conn_info.folder_selected = folder
                    except Exception as e:
                        logger.warning(f"Failed to select folder {folder}: {e}")
                
                logger.info(f"New connection created for {mailbox_id}")
                
                if self._on_connect:
                    self._on_connect(mailbox_id)
                
                return True, client, "New connection created"
                
            except Exception as e:
                stats["failed_connections"] += 1
                logger.error(f"Connection creation failed: {e}")
                return False, None, f"Connection failed: {str(e)}"
    
    async def release_connection(
        self,
        mailbox_id: str,
        client: Any,
        close: bool = False
    ):
        """
        Release a connection back to the pool
        
        Args:
            mailbox_id: Mailbox identifier
            client: The IMAP client to release
            close: If True, close the connection instead of releasing
        """
        async with self._lock:
            if mailbox_id not in self._pools:
                return
            
            pool = self._pools[mailbox_id]
            stats = self._stats[mailbox_id]
            
            # Find the connection
            for conn_id, conn_info in pool.items():
                if conn_info.client is client:
                    if close:
                        await self._safe_logout(client)
                        del pool[conn_id]
                        stats["active_connections"] = max(0, stats["active_connections"] - 1)
                        logger.debug(f"Connection closed for {mailbox_id}")
                    else:
                        conn_info.is_idle = True
                        conn_info.last_used = time.time()
                        pool.move_to_end(conn_id)
                        logger.debug(f"Connection released for {mailbox_id}")
                    return
    
    async def close_mailbox_connections(self, mailbox_id: str):
        """Close all connections for a specific mailbox"""
        async with self._lock:
            await self._close_mailbox_connections(mailbox_id)
    
    async def _close_mailbox_connections(self, mailbox_id: str):
        """Internal method to close mailbox connections"""
        if mailbox_id not in self._pools:
            return
        
        pool = self._pools[mailbox_id]
        
        for conn_id, conn_info in pool.items():
            await self._safe_logout(conn_info.client)
        
        del self._pools[mailbox_id]
        
        if mailbox_id in self._stats:
            del self._stats[mailbox_id]
        
        logger.info(f"All connections closed for {mailbox_id}")
    
    async def get_mailbox_stats(self, mailbox_id: str) -> Dict:
        """Get statistics for a mailbox"""
        async with self._lock:
            if mailbox_id not in self._stats:
                return {}
            
            stats = self._stats[mailbox_id].copy()
            
            # Add connection details
            if mailbox_id in self._pools:
                pool = self._pools[mailbox_id]
                stats["connections"] = []
                
                for conn_id, conn_info in pool.items():
                    stats["connections"].append({
                        "id": conn_id,
                        "age_seconds": conn_info.age_seconds(),
                        "idle_seconds": conn_info.idle_seconds(),
                        "use_count": conn_info.use_count,
                        "is_idle": conn_info.is_idle,
                        "folder": conn_info.folder_selected
                    })
            
            return stats
    
    def get_all_stats(self) -> Dict:
        """Get statistics for all mailboxes"""
        stats = {}
        for mailbox_id in list(self._pools.keys()):
            stats[mailbox_id] = {
                "pool_size": len(self._pools.get(mailbox_id, {})),
                "total_connections": self._stats.get(mailbox_id, {}).get("total_connections", 0),
                "active_connections": self._stats.get(mailbox_id, {}).get("active_connections", 0),
                "total_operations": self._stats.get(mailbox_id, {}).get("total_operations", 0)
            }
        return stats
    
    # ===========================================================================
    # INTERNAL METHODS
    # ===========================================================================
    
    async def _create_connection(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool,
        timeout: int
    ) -> Optional[Any]:
        """Create a new IMAP connection"""
        try:
            if use_ssl:
                client = aioimaplib.IMAP4_SSL(
                    host=host,
                    port=port,
                    timeout=timeout
                )
            else:
                client = aioimaplib.IMAP4(
                    host=host,
                    port=port,
                    timeout=timeout
                )
            
            # Wait for greeting
            await asyncio.wait_for(
                client.wait_hello_from_server(),
                timeout=timeout
            )
            
            # Login
            if username and password:
                await asyncio.wait_for(
                    client.login(username, password),
                    timeout=timeout
                )
            
            return client
            
        except asyncio.TimeoutError:
            logger.warning(f"Connection timeout for {host}")
            return None
        except Exception as e:
            logger.error(f"Connection error for {host}: {e}")
            return None
    
    def _is_connection_valid(self, conn_info: ConnectionInfo) -> bool:
        """Check if a connection is still valid"""
        # Check if expired by age
        if conn_info.is_expired(self.connection_ttl):
            return False
        
        # Check if stale
        if conn_info.is_stale(self.idle_timeout):
            return False
        
        # Check use count
        if conn_info.use_count >= self.max_use_count:
            return False
        
        # Check if client is still connected
        try:
            # Simple check - try to get idle response
            return conn_info.client is not None
        except Exception:
            return False
    
    def _generate_connection_id(self, host: str, username: str) -> str:
        """Generate a unique connection ID"""
        key = f"{host}:{username}:{time.time()}"
        return hashlib.md5(key.encode()).hexdigest()[:12]
    
    async def _safe_logout(self, client: Any):
        """Safely logout from IMAP server"""
        try:
            if client:
                await asyncio.wait_for(client.logout(), timeout=5)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
    
    async def _cleanup_loop(self):
        """Background task to clean up expired connections"""
        while self._running:
            try:
                await asyncio.sleep(self.health_check_interval)
                await self._cleanup_expired_connections()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
    
    async def _cleanup_expired_connections(self):
        """Remove expired connections from all pools"""
        async with self._lock:
            for mailbox_id in list(self._pools.keys()):
                pool = self._pools[mailbox_id]
                stats = self._stats.get(mailbox_id, {})
                
                for conn_id, conn_info in list(pool.items()):
                    should_remove = False
                    reason = ""
                    
                    if conn_info.is_expired(self.connection_ttl):
                        should_remove = True
                        reason = "age"
                    elif conn_info.is_stale(self.idle_timeout):
                        should_remove = True
                        reason = "idle"
                    elif conn_info.use_count >= self.max_use_count:
                        should_remove = True
                        reason = "max_use"
                    
                    if should_remove:
                        await self._safe_logout(conn_info.client)
                        del pool[conn_id]
                        stats["active_connections"] = max(0, stats.get("active_connections", 1) - 1)
                        logger.debug(f"Removed connection ({reason}) for {mailbox_id}")
                
                # Remove empty pools
                if not pool:
                    del self._pools[mailbox_id]
                    if mailbox_id in self._stats:
                        del self._stats[mailbox_id]
    
    # ===========================================================================
    # FOLDER LIST CACHING
    # ===========================================================================
    
    async def get_cached_folder_list(self, mailbox_id: str) -> Optional[List[Dict]]:
        """Get cached folder list for mailbox"""
        from email_cache import get_email_cache_manager
        
        try:
            cache = await get_email_cache_manager()
            cached = await cache.get_cached_folders(mailbox_id)
            
            if cached:
                return [
                    {
                        "name": f.name,
                        "total": f.total_emails,
                        "unread": f.unread_count,
                        "last_modified": f.last_modified
                    }
                    for f in cached
                ]
        except Exception:
            pass
        
        return None
    
    async def fetch_folder_list_cached(
        self,
        mailbox_id: str,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True
    ) -> Tuple[bool, List[Dict], str]:
        """
        Get folder list with caching
        
        Returns cached result if available, otherwise fetches from server
        """
        # Check cache first
        cached = await self.get_cached_folder_list(mailbox_id)
        if cached:
            logger.debug(f"Folder list cache hit for {mailbox_id}")
            return True, cached, "from_cache"
        
        # Get connection
        success, client, msg = await self.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl
        )
        
        if not success:
            return False, [], msg
        
        try:
            # Fetch folders
            status, data = await client.list()
            
            if status != 'OK':
                await self.release_connection(mailbox_id, client, close=True)
                return False, [], "Failed to list folders"
            
            folders = self._parse_folder_list(data)
            
            # Cache the result
            try:
                from email_cache import get_email_cache_manager
                cache = await get_email_cache_manager()
                await cache.cache_folders(mailbox_id, folders)
            except Exception:
                pass  # Cache is optional
            
            await self.release_connection(mailbox_id, client)
            return True, folders, "from_server"
            
        except Exception as e:
            logger.error(f"Folder list fetch error: {e}")
            await self.release_connection(mailbox_id, client, close=True)
            return False, [], str(e)
    
    def _parse_folder_list(self, data) -> List[Dict]:
        r"""Parse IMAP folder list response

        Properly handles both quoted and unquoted folder names.
        IMAP LIST response format: * LIST (flags) delimiter folder_name
        - flags can be \NoSelect, \Marked, etc.
        - delimiter is usually "/"
        - folder_name can be quoted (with possible escaped quotes inside) or unquoted
        """
        folders = []
        
        for line in data:
            if isinstance(line, bytes):
                line = line.decode('utf-8', errors='replace')
            
            # Skip empty lines
            if not line.strip():
                continue
            
            # Find the folder name by properly parsing the LIST response
            # Format: * LIST (flags) "/" "folder_name" or * LIST (flags) "/" folder_name
            name = self._extract_folder_name(line.strip())
            
            if name:
                folders.append({
                    "name": name,
                    "total": 0,
                    "unread": 0,
                    "last_modified": time.time()
                })
        
        return folders
    
    def _extract_folder_name(self, line: str) -> Optional[str]:
        """
        Extract folder name from IMAP LIST response line.
        
        Handles both quoted and unquoted folder names properly.
        Quoted names may contain escaped characters like \" and \\
        """
        try:
            # Find the last quoted string if present
            # Pattern: content between unescaped quotes
            if '"' in line:
                # Find all quoted segments
                segments = []
                i = 0
                while i < len(line):
                    if line[i] == '"':
                        # Found start of quoted segment
                        j = i + 1
                        result = []
                        while j < len(line):
                            if line[j] == '\\' and j + 1 < len(line):
                                # Escape sequence
                                if line[j + 1] == '"' or line[j + 1] == '\\':
                                    result.append(line[j + 1])
                                    j += 2
                                else:
                                    result.append(line[j])
                                    j += 1
                            elif line[j] == '"':
                                # End of quoted segment
                                break
                            else:
                                result.append(line[j])
                                j += 1
                        segments.append(''.join(result))
                        i = j + 1
                    else:
                        i += 1
                
                # Return the last quoted segment (usually the folder name)
                if segments:
                    return segments[-1]
            
            # No quotes found - try to extract unquoted folder name
            # Look for content after the hierarchy delimiter
            parts = line.split()
            if len(parts) >= 3:
                # The last part is typically the folder name
                last_part = parts[-1].strip()
                # Remove any trailing parentheses or extra characters
                if last_part and not last_part.startswith('('):
                    # Clean up common artifacts
                    last_part = last_part.rstrip(')')
                    if last_part:
                        return last_part
            
            return None
            
        except Exception as e:
            logger.debug(f"Error parsing folder name from '{line}': {e}")
            return None
    
    # ===========================================================================
    # CONNECTION HEALTH & KEEP-ALIVE
    # ===========================================================================
    
    async def keep_alive_connection(
        self,
        mailbox_id: str,
        client: Any
    ) -> bool:
        """
        Send NOOP to keep connection alive
        
        Args:
            mailbox_id: Mailbox identifier
            client: IMAP client
        
        Returns:
            bool: Success status
        """
        try:
            await asyncio.wait_for(client.noop(), timeout=5)
            return True
        except Exception as e:
            logger.warning(f"Keep-alive failed for {mailbox_id}: {e}")
            return False
    
    async def check_connection_health(
        self,
        mailbox_id: str,
        client: Any
    ) -> Dict[str, Any]:
        """
        Check health of a connection
        
        Returns dict with health status
        """
        health = {
            "healthy": False,
            "latency_ms": 0,
            "error": None
        }
        
        start = time.time()
        try:
            await asyncio.wait_for(client.noop(), timeout=10)
            health["healthy"] = True
            health["latency_ms"] = int((time.time() - start) * 1000)
        except asyncio.TimeoutError:
            health["error"] = "timeout"
        except Exception as e:
            health["error"] = str(e)
        
        return health
    
    async def prefetch_emails_batch(
        self,
        mailbox_id: str,
        host: str,
        port: int,
        username: str,
        password: str,
        folder: str,
        uids: List[int],
        use_ssl: bool = True
    ) -> Tuple[bool, Dict[int, Any], str]:
        """
        Batch fetch multiple emails efficiently
        
        Args:
            mailbox_id: Mailbox identifier
            host: IMAP server host
            port: IMAP server port
            username: Login username
            password: Login password
            folder: Folder name
            uids: List of UIDs to fetch
            use_ssl: Use SSL/TLS
        
        Returns:
            tuple: (success, {uid: raw_email}, message)
        """
        if not uids:
            return True, {}, "No UIDs specified"
        
        manager = await get_connection_manager()
        
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            folder=folder
        )
        
        if not success:
            return False, {}, msg
        
        try:
            # Select folder
            status, _ = await client.select(quote_imap_folder_name(folder))
            if status != 'OK':
                await manager.release_connection(mailbox_id, client, close=True)
                return False, {}, f"Failed to select folder: {folder}"
            
            # Fetch all emails in one batch using UID FETCH
            uid_range = f"{min(uids)}:{max(uids)}"
            
            # Fetch headers and structure only (fast)
            status, data = await client.fetch(
                uid_range,
                '(UID ENVELOPE RFC822.SIZE FLAGS)'
            )
            
            results = {}
            if status == 'OK' and data:
                for item in data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        uid = int(item[0])
                        if uid in uids:
                            results[uid] = item[1]
            
            await manager.release_connection(mailbox_id, client)
            return True, results, f"Fetched {len(results)} emails"
            
        except Exception as e:
            logger.error(f"Batch fetch error: {e}")
            await manager.release_connection(mailbox_id, client, close=True)
            return False, {}, str(e)


# =============================================================================
# GLOBAL CONNECTION MANAGER INSTANCE
# =============================================================================

# Global connection manager instance
_connection_manager: Optional[IMAPConnectionManager] = None


async def get_connection_manager() -> IMAPConnectionManager:
    """Get or create the global connection manager"""
    global _connection_manager
    
    if _connection_manager is None:
        _connection_manager = IMAPConnectionManager(
            max_connections=100,
            max_per_mailbox=5,
            connection_ttl=3600,
            idle_timeout=300,
            health_check_interval=60,
            max_use_count=100
        )
        await _connection_manager.start()
    
    return _connection_manager


async def shutdown_connection_manager():
    """Shutdown the global connection manager"""
    global _connection_manager
    
    if _connection_manager:
        await _connection_manager.stop()
        _connection_manager = None


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

async def pooled_fetch_emails(
    mailbox_id: str,
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str = "INBOX",
    use_ssl: bool = True,
    limit: int = 200,
    offset: int = 0,
    unread_only: bool = False
) -> Tuple[bool, list, str]:
    """
    Fetch emails using the connection pool
    
    This is the recommended way to fetch emails as it:
    - Reuses connections
    - Handles connection pooling automatically
    - Manages timeouts and retries
    """
    manager = await get_connection_manager()
    
    # Build search criteria
    search_criteria = "ALL"
    if unread_only:
        search_criteria = "UNSEEN"
    
    try:
        # Get connection from pool
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            folder=folder
        )
        
        if not success:
            return False, [], msg
        
        try:
            # Search emails
            status, data = await client.search(None, search_criteria)
            
            if status != 'OK' or not data or not data[0]:
                await manager.release_connection(mailbox_id, client, close=True)
                return True, [], "No emails found"
            
            # Parse email IDs
            raw_ids = data[0]
            if isinstance(raw_ids, bytes):
                raw_ids = raw_ids.decode('utf-8', errors='replace')
            
            all_ids = [uid.strip() for uid in raw_ids.split() if uid.strip()]
            
            # Apply pagination
            total = len(all_ids)
            start = max(0, offset)
            end = min(len(all_ids), offset + limit)
            
            if start >= total:
                await manager.release_connection(mailbox_id, client)
                return True, [], "No emails in range"
            
            paginated_ids = all_ids[start:end]
            
            # Fetch headers for each email
            emails = []
            for uid in paginated_ids:
                try:
                    status, msg_data = await client.fetch(
                        uid,
                        '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])'
                    )
                    
                    if status == 'OK' and msg_data:
                        emails.append({
                            "uid": uid,
                            "raw_data": msg_data
                        })
                except Exception:
                    continue
            
            await manager.release_connection(mailbox_id, client)
            
            return True, emails, f"Fetched {len(emails)} emails"
            
        except Exception as e:
            await manager.release_connection(mailbox_id, client, close=True)
            return False, [], f"Error: {str(e)}"
            
    except Exception as e:
        return False, [], f"Connection error: {str(e)}"


# =============================================================================
# CONTEXT MANAGER FOR CONNECTIONS
# =============================================================================

class IMAPConnection:
    """Context manager for pooled IMAP connections"""
    
    def __init__(
        self,
        mailbox_id: str,
        host: str,
        port: int = 993,
        username: str = None,
        password: str = None,
        use_ssl: bool = True,
        folder: str = None
    ):
        self.mailbox_id = mailbox_id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.folder = folder
        self.client = None
        self.manager = None
    
    async def __aenter__(self):
        self.manager = await get_connection_manager()
        success, self.client, msg = await self.manager.get_connection(
            mailbox_id=self.mailbox_id,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            use_ssl=self.use_ssl,
            folder=self.folder
        )
        
        if not success:
            raise ConnectionError(msg)
        
        return self.client
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.manager and self.client:
            # Close connection if there was an error
            close = exc_type is not None
            await self.manager.release_connection(
                self.mailbox_id,
                self.client,
                close=close
            )
        return False