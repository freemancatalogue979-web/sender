"""
Async IMAP Client Module - Step 3
Uses aioimaplib for asynchronous IMAP operations
Replaces blocking imaplib with async operations
"""

import asyncio
import ssl
import re
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from email import message_from_bytes
from email.utils import parsedate_to_datetime
import aioimaplib

# Flag to track if async IMAP is available
ASYNC_IMAP_AVAILABLE = False


def quote_imap_folder_name(folder_name: str) -> str:
    """
    Properly quote an IMAP folder name if it contains special characters.
    
    IMAP folder names containing spaces, quotes, backslashes, wildcards,
    or the hierarchy delimiter should be quoted. This function ensures
    proper quoting while avoiding double-quoting.
    
    Args:
        folder_name: The raw folder name from the IMAP server
        
    Returns:
        Properly quoted folder name safe for IMAP commands
    """
    if not folder_name:
        return folder_name
    
    # If already quoted, return as-is
    if folder_name.startswith('"') and folder_name.endswith('"'):
        return folder_name
    
    # Characters that require quoting in IMAP folder names
    # Based on RFC 3501: space, quote, backslash, asterisk, percent, list separator
    special_chars = [' ', '"', '\\', '*', '%']
    
    needs_quoting = any(char in folder_name for char in special_chars)
    
    if needs_quoting:
        # Escape any backslashes and wrap in quotes
        # First escape backslashes, then wrap
        escaped = folder_name.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    
    return folder_name

try:
    import aioimaplib
    ASYNC_IMAP_AVAILABLE = True
except ImportError:
    aioimaplib = None


class AsyncIMAPClient:
    """
    Async IMAP Client with connection management and IDLE support
    Features:
    - Async connect/login/logout
    - IDLE mode for real-time notifications
    - Connection pool support
    - Automatic reconnection
    """
    
    def __init__(
        self,
        host: str,
        port: int = 993,
        username: str = None,
        password: str = None,
        use_ssl: bool = True,
        timeout: int = 30
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.timeout = timeout
        self._client: Optional[aioimaplib.IMAP4_SSL] = None
        self._connected = False
    
    async def connect(self) -> bool:
        """
        Connect to IMAP server asynchronously
        
        Returns:
            bool: Connection success
        """
        if not ASYNC_IMAP_AVAILABLE:
            return False
        
        try:
            if self.use_ssl:
                self._client = aioimaplib.IMAP4_SSL(
                    host=self.host,
                    port=self.port,
                    timeout=self.timeout
                )
            else:
                self._client = aioimaplib.IMAP4(
                    host=self.host,
                    port=self.port,
                    timeout=self.timeout
                )
            
            # Wait for server greeting
            await self._client.wait_hello_from_server()
            
            # Login
            if self.username and self.password:
                await self._client.login(self.username, self.password)
            
            self._connected = True
            return True
            
        except Exception as e:
            print(f"Async IMAP connect error: {e}")
            self._connected = False
            return False
    
    async def logout(self):
        """Logout and close connection"""
        if self._client and self._connected:
            try:
                await self._client.logout()
            except Exception:
                pass
            self._connected = False
            self._client = None
    
    async def select_folder(self, folder: str = "INBOX") -> Tuple[bool, int, int]:
        """
        Select a folder and get status
        
        Returns:
            tuple: (success, total_emails, unread_count)
        """
        if not self._connected or not self._client:
            return False, 0, 0
        
        try:
            # Properly quote folder name if it contains special characters
            quoted_folder = quote_imap_folder_name(folder)
            
            # Try exact match first
            status, data = await self._client.select(quoted_folder)
            
            if status != 'OK':
                # Try variations without quoting
                for variant in [folder.lower(), folder.upper(), folder.title()]:
                    status, data = await self._client.select(quote_imap_folder_name(variant))
                    if status == 'OK':
                        folder = variant
                        break
            
            if status != 'OK':
                # Last attempt with raw folder name
                status, data = await self._client.select(quoted_folder)
                if status != 'OK':
                    return False, 0, 0
            
            # Get folder status - use quoted folder for consistency
            total_emails = 0
            unread_count = 0
            
            status, data = await self._client.status(quoted_folder, '(MESSAGES UNSEEN)')
            if status != 'OK' and folder:
                # Fallback without quoting for STATUS command
                status, data = await self._client.status(folder, '(MESSAGES UNSEEN)')
            
            if status == 'OK' and data:
                try:
                    # Parse: b'INBOX (MESSAGES 150 UNSEEN 5)'
                    status_data = data[0].decode('utf-8', errors='replace')
                    for part in status_data.split():
                        if part.isdigit():
                            if total_emails == 0:
                                total_emails = int(part)
                            else:
                                unread_count = int(part)
                except Exception:
                    pass
            
            return True, total_emails, unread_count
            
        except Exception as e:
            print(f"Select folder error: {e}")
            return False, 0, 0
    
    async def search_emails(self, criteria: str = 'ALL') -> List[int]:
        """
        Search emails with criteria
        
        Args:
            criteria: IMAP search criteria (ALL, UNSEEN, UID 100:*, etc.)
        
        Returns:
            List of email UIDs
        """
        if not self._connected or not self._client:
            return []
        
        try:
            status, data = await self._client.search(None, criteria)
            
            if status != 'OK' or not data or not data[0]:
                return []
            
            raw_ids = data[0]
            if isinstance(raw_ids, bytes):
                raw_ids = raw_ids.decode('utf-8', errors='replace')
            
            return [int(uid) for uid in raw_ids.split() if uid.strip()]
            
        except Exception as e:
            print(f"Search error: {e}")
            return []
    
    async def fetch_email(self, uid: int) -> Optional[bytes]:
        """
        Fetch full email by UID
        
        Args:
            uid: Email UID
        
        Returns:
            Raw email bytes or None
        """
        if not self._connected or not self._client:
            return None
        
        try:
            status, data = await self._client.fetch(str(uid), '(RFC822)')
            
            if status == 'OK' and data and data[0]:
                return data[0][1]
            
            return None
            
        except Exception as e:
            print(f"Fetch email error: {e}")
            return None
    
    async def fetch_headers(self, uid: int) -> Optional[Dict[str, str]]:
        """
        Fetch only headers (faster than full email)
        
        Args:
            uid: Email UID
        
        Returns:
            Dict of headers or None
        """
        if not self._connected or not self._client:
            return None
        
        try:
            status, data = await self._client.fetch(str(uid), '(BODY[HEADER])')
            
            if status == 'OK' and data and data[0]:
                raw_headers = data[0][1]
                msg = message_from_bytes(raw_headers)
                return {k.lower(): v for k, v in msg.items()}
            
            return None
            
        except Exception as e:
            print(f"Fetch headers error: {e}")
            return None
    
    async def idle_start(self, callback=None):
        """
        Start IDLE mode for real-time notifications
        
        Args:
            callback: Optional callback function called when new mail arrives
        
        Returns:
            IDLE poller task
        """
        if not self._connected or not self._client:
            return None
        
        try:
            # Start IDLE
            idle = self._client.idle()
            await idle.wait_start_idle()
            
            # Create poller task
            async def idle_poller():
                while self._connected:
                    try:
                        # Wait for data with timeout
                        resp = await self._client.idle_check(timeout=60)
                        
                        if resp:
                            # New data available
                            if callback:
                                callback(resp)
                                
                            # Restart IDLE
                            self._client.idle_done()
                            idle = self._client.idle()
                            await idle.wait_start_idle()
                    
                    except asyncio.TimeoutError:
                        # Normal timeout, continue
                        continue
                    except Exception as e:
                        print(f"IDLE error: {e}")
                        break
            
            return asyncio.create_task(idle_poller())
            
        except Exception as e:
            print(f"IDLE start error: {e}")
            return None
    
    async def idle_stop(self):
        """Stop IDLE mode"""
        if self._client and self._connected:
            try:
                self._client.idle_done()
            except Exception:
                pass


class AsyncIMAPPool:
    """
    Connection pool for multiple mailboxes
    Manages async connections efficiently
    """
    
    _pool: Dict[str, AsyncIMAPClient] = {}
    _lock = asyncio.Lock()
    
    @classmethod
    async def get_connection(
        cls,
        mailbox_id: str,
        host: str,
        port: int = 993,
        username: str = None,
        password: str = None,
        use_ssl: bool = True
    ) -> Optional[AsyncIMAPClient]:
        """
        Get or create a connection from the pool
        
        Args:
            mailbox_id: Unique mailbox identifier
            host: IMAP server host
            port: IMAP server port
            username: Login username
            password: Login password
            use_ssl: Use SSL/TLS
        
        Returns:
            AsyncIMAPClient or None
        """
        async with cls._lock:
            # Check if existing connection
            if mailbox_id in cls._pool:
                client = cls._pool[mailbox_id]
                if client._connected:
                    return client
                else:
                    # Try to reconnect
                    if await client.connect():
                        return client
                    # Connection failed, remove from pool
                    del cls._pool[mailbox_id]
            
            # Create new connection
            client = AsyncIMAPClient(
                host=host,
                port=port,
                username=username,
                password=password,
                use_ssl=use_ssl
            )
            
            if await client.connect():
                cls._pool[mailbox_id] = client
                return client
            
            return None
    
    @classmethod
    async def release_connection(cls, mailbox_id: str):
        """Release a connection back to the pool (keep alive)"""
        # Connection stays in pool for reuse
        pass
    
    @classmethod
    async def close_connection(cls, mailbox_id: str):
        """Close and remove a connection from the pool"""
        async with cls._lock:
            if mailbox_id in cls._pool:
                client = cls._pool[mailbox_id]
                await client.logout()
                del cls._pool[mailbox_id]
    
    @classmethod
    async def close_all(cls):
        """Close all connections in the pool"""
        async with cls._lock:
            for mailbox_id in list(cls._pool.keys()):
                try:
                    await cls._pool[mailbox_id].logout()
                except Exception:
                    pass
            cls._pool.clear()


# =============================================================================
# HELPER FUNCTIONS FOR ASYNC EMAIL OPERATIONS
# =============================================================================

async def async_fetch_folder_emails(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str = "INBOX",
    uids: List[int] = None,
    use_ssl: bool = True
) -> Tuple[bool, List[Dict], str]:
    """
    Fetch emails from a folder asynchronously
    
    Args:
        host: IMAP server host
        port: IMAP server port
        username: Login username
        password: Login password
        folder: Folder name
        uids: Specific UIDs to fetch (None = all)
        use_ssl: Use SSL/TLS
    
    Returns:
        tuple: (success, emails_list, message)
    """
    if not ASYNC_IMAP_AVAILABLE:
        return False, [], "aioimaplib not available"
    
    client = AsyncIMAPClient(host, port, username, password, use_ssl)
    
    try:
        # Connect
        if not await client.connect():
            return False, [], "Failed to connect"
        
        # Select folder
        success, total, unread = await client.select_folder(folder)
        if not success:
            return False, [], f"Failed to select folder: {folder}"
        
        # Search for emails
        if uids:
            criteria = f'UID {min(uids)}:{max(uids)}'
        else:
            criteria = 'ALL'
        
        email_uids = await client.search_emails(criteria)
        
        if not email_uids:
            await client.logout()
            return True, [], "No emails found"
        
        # Fetch all emails concurrently
        emails = []
        
        async def fetch_single(uid):
            raw_email = await client.fetch_email(uid)
            if raw_email:
                try:
                    msg = message_from_bytes(raw_email)
                    headers = {k.lower(): v for k, v in msg.items()}
                    
                    # Extract body
                    body_text = ""
                    body_html = ""
                    
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct == 'text/plain' and not body_text:
                                try:
                                    body_text = part.get_payload(decode=True).decode('utf-8', errors='replace')
                                except:
                                    pass
                            elif ct == 'text/html' and not body_html:
                                try:
                                    body_html = part.get_payload(decode=True).decode('utf-8', errors='replace')
                                except:
                                    pass
                    else:
                        ct = msg.get_content_type()
                        if ct == 'text/plain':
                            try:
                                body_text = msg.get_payload(decode=True).decode('utf-8', errors='replace')
                            except:
                                pass
                        elif ct == 'text/html':
                            try:
                                body_html = msg.get_payload(decode=True).decode('utf-8', errors='replace')
                            except:
                                pass
                    
                    # Parse date
                    email_date = None
                    date_str = headers.get('date', '')
                    if date_str:
                        try:
                            email_date = parsedate_to_datetime(date_str)
                        except:
                            pass
                    
                    return {
                        "uid": uid,
                        "subject": msg.get('subject', '(No Subject)'),
                        "from": headers.get('from', ''),
                        "to": headers.get('to', ''),
                        "date": email_date,
                        "message_id": headers.get('message-id'),
                        "body_text": body_text,
                        "body_html": body_html,
                        "is_read": '\\Seen' in str(msg.items()),
                        "has_attachments": msg.is_multipart()
                    }
                except Exception:
                    return None
            return None
        
        # Fetch all emails
        tasks = [fetch_single(uid) for uid in email_uids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        emails = [r for r in results if r is not None and not isinstance(r, Exception)]
        
        await client.logout()
        
        return True, emails, f"Fetched {len(emails)} emails"
        
    except Exception as e:
        await client.logout()
        return False, [], f"Error: {str(e)}"


# =============================================================================
# SYNC WRAPPER (For backward compatibility)
# =============================================================================

def is_async_available() -> bool:
    """Check if async IMAP is available"""
    return ASYNC_IMAP_AVAILABLE


def get_async_info() -> Dict[str, Any]:
    """Get async IMAP module info"""
    return {
        "available": ASYNC_IMAP_AVAILABLE,
        "version": aioimaplib.__version__ if ASYNC_IMAP_AVAILABLE else None,
        "features": [
            "async_connect",
            "async_search", 
            "async_fetch",
            "idle_mode",
            "connection_pool"
        ] if ASYNC_IMAP_AVAILABLE else []
    }