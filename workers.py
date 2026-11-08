"""
Background Worker System - Step 5
Mail sync workers with IDLE support and real-time notifications
"""

import asyncio
import time
from typing import Dict, Optional, Callable, List, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging

from async_imap import quote_imap_folder_name

logger = logging.getLogger(__name__)


class WorkerStatus(Enum):
    IDLE = "idle"
    SYNCING = "syncing"
    IDLE_MODE = "idle_mode"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class MailboxWorker:
    """Worker for a single mailbox"""
    mailbox_id: str
    user_id: str
    status: WorkerStatus = WorkerStatus.IDLE
    last_sync: Optional[datetime] = None
    last_idle_check: Optional[datetime] = None
    idle_task: Optional[asyncio.Task] = None
    sync_task: Optional[asyncio.Task] = None
    error_count: int = 0
    consecutive_errors: int = 0
    callbacks: List[Callable] = field(default_factory=list)
    
    def add_callback(self, callback: Callable):
        """Add a callback for new email notifications"""
        if callback not in self.callbacks:
            self.callbacks.append(callback)
    
    def remove_callback(self, callback: Callable):
        """Remove a callback"""
        if callback in self.callbacks:
            self.callbacks.remove(callback)
    
    def notify_new_email(self, email_data: dict):
        """Notify all callbacks of new email"""
        for callback in self.callbacks:
            try:
                callback(email_data)
            except Exception as e:
                logger.error(f"Callback error: {e}")


class MailSyncWorkerManager:
    """
    Manager for mailbox sync workers
    
    Features:
    - Per-mailbox workers
    - IMAP IDLE for real-time notifications
    - Periodic sync as fallback
    - Auto-reconnection
    - Event callbacks
    """
    
    def __init__(
        self,
        sync_interval: int = 300,        # 5 minutes between syncs
        idle_timeout: int = 1800,       # 30 minutes IDLE timeout
        max_concurrent_workers: int = 10,
        reconnect_delay: int = 60        # 1 minute reconnect delay
    ):
        self.sync_interval = sync_interval
        self.idle_timeout = idle_timeout
        self.max_concurrent_workers = max_concurrent_workers
        self.reconnect_delay = reconnect_delay
        
        # Workers storage
        self._workers: Dict[str, MailboxWorker] = {}
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Manager state
        self._running = False
        self._manager_task: Optional[asyncio.Task] = None
        
        # Event callbacks (global)
        self._global_callbacks: List[Callable] = []
        
        # Statistics
        self._stats = {
            "total_syncs": 0,
            "total_new_emails": 0,
            "total_errors": 0,
            "active_workers": 0
        }
    
    async def start(self):
        """Start the worker manager"""
        if self._running:
            return
        
        self._running = True
        self._manager_task = asyncio.create_task(self._manager_loop())
        logger.info("Mail Sync Worker Manager started")
    
    async def stop(self):
        """Stop all workers and the manager"""
        self._running = False
        
        if self._manager_task:
            self._manager_task.cancel()
            try:
                await self._manager_task
            except asyncio.CancelledError:
                pass
        
        # Stop all workers
        async with self._lock:
            for mailbox_id in list(self._workers.keys()):
                await self.stop_worker(mailbox_id)
        
        logger.info("Mail Sync Worker Manager stopped")
    
    async def _manager_loop(self):
        """Main manager loop for monitoring and cleanup"""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                async with self._lock:
                    # Update active worker count
                    self._stats["active_workers"] = len(self._workers)
                    
                    # Check for stuck workers
                    for mailbox_id, worker in list(self._workers.items()):
                        if worker.status == WorkerStatus.SYNCING:
                            # Check if sync is taking too long
                            if worker.last_sync:
                                elapsed = (datetime.now() - worker.last_sync).total_seconds()
                                if elapsed > 600:  # 10 minutes
                                    logger.warning(f"Worker {mailbox_id} stuck in syncing state")
                                    # Could force restart here
                    
                    # Remove workers with too many consecutive errors
                    for mailbox_id, worker in list(self._workers.items()):
                        if worker.consecutive_errors >= 5:
                            logger.warning(f"Worker {mailbox_id} disabled due to consecutive errors")
                            await self._cleanup_worker(worker)
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Manager loop error: {e}")
    
    # ===========================================================================
    # WORKER MANAGEMENT
    # ===========================================================================
    
    async def start_worker(
        self,
        mailbox_id: str,
        user_id: str,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        folders: List[str] = None
    ) -> bool:
        """
        Start a worker for a mailbox
        
        Args:
            mailbox_id: Mailbox identifier
            user_id: User identifier
            host: IMAP server host
            port: IMAP server port
            username: Login username
            password: Login password
            use_ssl: Use SSL/TLS
            folders: List of folders to monitor (default: ["INBOX"])
        
        Returns:
            bool: Success status
        """
        async with self._lock:
            # Check if worker already exists
            if mailbox_id in self._workers:
                logger.info(f"Worker {mailbox_id} already running")
                return True
            
            # Check worker limit
            if len(self._workers) >= self.max_concurrent_workers:
                logger.warning(f"Max workers reached, cannot start {mailbox_id}")
                return False
            
            # Create worker
            worker = MailboxWorker(
                mailbox_id=mailbox_id,
                user_id=user_id
            )
            
            # Store worker config for reconnection
            worker.config = {
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "use_ssl": use_ssl,
                "folders": folders or ["INBOX"]
            }
            
            self._workers[mailbox_id] = worker
            
            # Start sync task
            worker.sync_task = asyncio.create_task(
                self._sync_loop(worker)
            )
            
            logger.info(f"Worker started for mailbox {mailbox_id}")
            return True
    
    async def stop_worker(self, mailbox_id: str):
        """Stop a specific worker"""
        async with self._lock:
            if mailbox_id not in self._workers:
                return
            
            worker = self._workers[mailbox_id]
            await self._cleanup_worker(worker)
            del self._workers[mailbox_id]
            
            logger.info(f"Worker stopped for mailbox {mailbox_id}")
    
    async def _cleanup_worker(self, worker: MailboxWorker):
        """Cleanup a worker"""
        # Cancel tasks
        if worker.idle_task:
            worker.idle_task.cancel()
            try:
                await worker.idle_task
            except asyncio.CancelledError:
                pass
        
        if worker.sync_task:
            worker.sync_task.cancel()
            try:
                await worker.sync_task
            except asyncio.CancelledError:
                pass
        
        worker.status = WorkerStatus.STOPPED
    
    # ===========================================================================
    # SYNC LOOP
    # ===========================================================================
    
    async def _sync_loop(self, worker: MailboxWorker):
        """Main sync loop for a worker"""
        while self._running and worker.status != WorkerStatus.STOPPED:
            try:
                worker.status = WorkerStatus.SYNCING
                
                # Perform sync for each folder
                for folder in worker.config["folders"]:
                    try:
                        await self._sync_folder(
                            worker,
                            folder
                        )
                    except Exception as e:
                        logger.error(f"Sync error for {worker.mailbox_id}/{folder}: {e}")
                        worker.consecutive_errors += 1
                
                worker.last_sync = datetime.now()
                worker.consecutive_errors = 0
                self._stats["total_syncs"] += 1
                
                # Switch to IDLE mode until next sync
                worker.status = WorkerStatus.IDLE_MODE
                await self._enter_idle_mode(worker)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sync loop error for {worker.mailbox_id}: {e}")
                worker.consecutive_errors += 1
                self._stats["total_errors"] += 1
                
                # Wait before retry
                await asyncio.sleep(self.reconnect_delay)
    
    async def _sync_folder(
        self,
        worker: MailboxWorker,
        folder: str
    ):
        """Sync a single folder"""
        from imap_manager import get_connection_manager
        from functions import get_mailbox_last_uid, update_mailbox_last_uid, save_email_to_server, decode_email_header_value
        
        manager = await get_connection_manager()
        
        # Get connection from pool
        success, client, msg = await manager.get_connection(
            mailbox_id=worker.mailbox_id,
            host=worker.config["host"],
            port=worker.config["port"],
            username=worker.config["username"],
            password=worker.config["password"],
            use_ssl=worker.config["use_ssl"],
            folder=folder
        )
        
        if not success:
            raise Exception(msg)
        
        try:
            # Get last synced UID
            last_uid = get_mailbox_last_uid(worker.mailbox_id)
            
            # Select folder
            select_status, _ = await client.select(quote_imap_folder_name(folder))
            if select_status != 'OK':
                raise Exception(f"Failed to select folder: {folder}")
            
            # Search for new emails
            search_criteria = f'UID {last_uid + 1}:*'
            status, message = await client.search(None, search_criteria)
            
            if status != 'OK':
                # No new emails
                return
            
            # Get new email UIDs
            new_uids = message[0].split() if message[0] else []
            
            if not new_uids:
                return
            
            # Convert to integers
            new_uids = [int(uid) for uid in new_uids]
            new_uids.sort()
            
            # Fetch and save new emails
            highest_uid = new_uids[-1]
            new_email_count = 0
            
            for uid in new_uids:
                try:
                    status, msg_data = await client.fetch(str(uid), '(RFC822)')
                    
                    if status == 'OK' and msg_data and msg_data[0]:
                        raw_email = msg_data[0][1]
                        
                        # Parse email
                        from email import message_from_bytes
                        email_msg = message_from_bytes(raw_email)
                        
                        # Extract headers
                        headers = {k.lower(): v for k, v in email_msg.items()}
                        
                        email_data = {
                            "uid": uid,
                            "subject": decode_email_header_value(email_msg.get('subject'), '(No Subject)'),
                            "from": decode_email_header_value(headers.get('from', '')),
                            "date": decode_email_header_value(headers.get('date', '')),
                            "message_id": decode_email_header_value(headers.get('message-id')),
                            "folder": folder
                        }
                        
                        # Save to server
                        if save_email_to_server(
                            worker.user_id,
                            worker.config.get("base_path", None),
                            worker.mailbox_id,
                            folder,
                            email_data,
                            uid
                        ):
                            new_email_count += 1
                            
                            # Cache email metadata for instant access
                            try:
                                from email_cache import get_email_cache_manager
                                cache_mgr = await get_email_cache_manager()
                                await cache_mgr.cache_email(
                                    mailbox_id=worker.mailbox_id,
                                    folder=folder,
                                    uid=uid,
                                    subject=email_data.get("subject", ""),
                                    sender=email_data.get("from", ""),
                                    date=email_data.get("date", ""),
                                    size=len(raw_email) if raw_email else 0,
                                    flags="",
                                    message_id=email_data.get("message_id", ""),
                                    is_read=False,
                                    extra_data=email_data
                                )
                            except Exception:
                                pass  # Cache is optional
                            
                            # Notify callbacks
                            worker.notify_new_email(email_data)
                            
                            # Notify global callbacks
                            for callback in self._global_callbacks:
                                try:
                                    callback(worker.mailbox_id, email_data)
                                except Exception:
                                    pass
                            
                            # Publish to Redis for distributed systems
                            try:
                                from redis_manager import get_redis_manager
                                redis_mgr = await get_redis_manager()
                                await redis_mgr.publish_new_email(
                                    mailbox_id=worker.mailbox_id,
                                    user_id=worker.user_id,
                                    email_data=email_data
                                )
                            except Exception:
                                pass  # Redis is optional
                            
                            # Dispatch webhook for new email
                            try:
                                from webhook_manager import dispatch_email_event
                                await dispatch_email_event(
                                    event_type="new_email",
                                    mailbox_id=worker.mailbox_id,
                                    user_id=worker.user_id,
                                    data=email_data
                                )
                            except Exception:
                                pass  # Webhooks are optional
                
                except Exception as e:
                    logger.error(f"Error fetching email {uid}: {e}")
                    continue
            
            # Update last synced UID
            if new_email_count > 0:
                update_mailbox_last_uid(worker.mailbox_id, highest_uid)
                self._stats["total_new_emails"] += new_email_count
                
                # Publish sync complete to Redis
                try:
                    from redis_manager import get_redis_manager
                    redis_mgr = await get_redis_manager()
                    await redis_mgr.publish_sync_complete(
                        mailbox_id=worker.mailbox_id,
                        user_id=worker.user_id,
                        new_emails=new_email_count,
                        folder=folder
                    )
                except Exception:
                    pass  # Redis is optional
                
                # Dispatch webhook for sync complete
                try:
                    from webhook_manager import dispatch_email_event
                    await dispatch_email_event(
                        event_type="sync_complete",
                        mailbox_id=worker.mailbox_id,
                        user_id=worker.user_id,
                        data={"new_emails": new_email_count, "folder": folder}
                    )
                except Exception:
                    pass  # Webhooks are optional

            logger.info(f"Synced {new_email_count} new emails for {worker.mailbox_id}/{folder}")
            
        finally:
            await manager.release_connection(worker.mailbox_id, client)
    
    # ===========================================================================
    # IDLE MODE
    # ===========================================================================
    
    async def _enter_idle_mode(self, worker: MailboxWorker):
        """
        Enter IDLE mode for real-time notifications
        
        IDLE allows the server to push notifications when new mail arrives,
        instead of polling every sync_interval seconds.
        """
        from async_imap import AsyncIMAPClient
        
        idle_start_time = time.time()
        
        while self._running and worker.status == WorkerStatus.IDLE_MODE:
            try:
                # Check if IDLE timeout reached
                if time.time() - idle_start_time > self.idle_timeout:
                    logger.debug(f"IDLE timeout reached for {worker.mailbox_id}")
                    break
                
                # Create temporary connection for IDLE
                client = AsyncIMAPClient(
                    host=worker.config["host"],
                    port=worker.config["port"],
                    username=worker.config["username"],
                    password=worker.config["password"],
                    use_ssl=worker.config["use_ssl"],
                    timeout=60
                )
                
                if not await client.connect():
                    raise Exception("Failed to connect for IDLE")
                
                # Start IDLE for first folder
                folder = worker.config["folders"][0]
                success, _, _ = await client.select_folder(folder)
                
                if not success:
                    await client.logout()
                    raise Exception(f"Failed to select folder for IDLE: {folder}")
                
                # Start IDLE
                idle = client._client.idle()
                await idle.wait_start_idle()
                
                logger.info(f"IDLE started for {worker.mailbox_id}/{folder}")
                
                # IDLE loop
                while self._running and worker.status == WorkerStatus.IDLE_MODE:
                    try:
                        # Wait for response (max 60 seconds)
                        resp = await asyncio.wait_for(
                            client._client.idle_check(timeout=60),
                            timeout=70
                        )
                        
                        if resp:
                            # Check for new mail notification
                            resp_str = str(resp)
                            
                            if 'EXISTS' in resp_str or 'RECENT' in resp_str:
                                logger.info(f"New mail notification for {worker.mailbox_id}")
                                
                                # Trigger immediate sync
                                worker.status = WorkerStatus.SYNCING
                                
                                # Do a quick sync
                                await self._quick_sync(worker, folder)
                                
                                # Return to IDLE mode
                                worker.status = WorkerStatus.IDLE_MODE
                                
                                # Restart IDLE
                                idle = client._client.idle()
                                await idle.wait_start_idle()
                                idle_start_time = time.time()
                        
                        # Check IDLE timeout
                        if time.time() - idle_start_time > self.idle_timeout:
                            break
                    
                    except asyncio.TimeoutError:
                        # Normal timeout, continue
                        continue
                    except Exception as e:
                        logger.error(f"IDLE check error: {e}")
                        break
                
                # Stop IDLE
                try:
                    client._client.idle_done()
                except Exception:
                    pass
                
                await client.logout()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"IDLE mode error for {worker.mailbox_id}: {e}")
                worker.consecutive_errors += 1
                
                # Wait before retry
                await asyncio.sleep(self.reconnect_delay)
    
    async def _quick_sync(self, worker: MailboxWorker, folder: str):
        """Quick sync when IDLE detects new mail"""
        try:
            # Use connection from pool
            await self._sync_folder(worker, folder)
            
            worker.last_sync = datetime.now()
            self._stats["total_syncs"] += 1
            
        except Exception as e:
            logger.error(f"Quick sync error: {e}")
    
    # ===========================================================================
    # EVENT CALLBACKS
    # ===========================================================================
    
    def add_global_callback(self, callback: Callable):
        """Add a global callback for all mailboxes"""
        if callback not in self._global_callbacks:
            self._global_callbacks.append(callback)
    
    def remove_global_callback(self, callback: Callable):
        """Remove a global callback"""
        if callback in self._global_callbacks:
            self._global_callbacks.remove(callback)
    
    # ===========================================================================
    # STATISTICS
    # ===========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get worker manager statistics"""
        return {
            **self._stats,
            "workers": {
                mailbox_id: {
                    "status": worker.status.value,
                    "last_sync": worker.last_sync.isoformat() if worker.last_sync else None,
                    "error_count": worker.consecutive_errors
                }
                for mailbox_id, worker in self._workers.items()
            }
        }
    
    def get_worker_status(self, mailbox_id: str) -> Optional[Dict]:
        """Get status of a specific worker"""
        if mailbox_id not in self._workers:
            return None
        
        worker = self._workers[mailbox_id]
        return {
            "mailbox_id": mailbox_id,
            "status": worker.status.value,
            "last_sync": worker.last_sync.isoformat() if worker.last_sync else None,
            "consecutive_errors": worker.consecutive_errors
        }


# =============================================================================
# GLOBAL WORKER MANAGER INSTANCE
# =============================================================================

_worker_manager: Optional[MailSyncWorkerManager] = None


async def get_worker_manager() -> MailSyncWorkerManager:
    """Get or create the global worker manager"""
    global _worker_manager
    
    if _worker_manager is None:
        _worker_manager = MailSyncWorkerManager(
            sync_interval=300,        # 5 minutes
            idle_timeout=1800,       # 30 minutes
            max_concurrent_workers=10,
            reconnect_delay=60       # 1 minute
        )
        await _worker_manager.start()
    
    return _worker_manager


async def shutdown_worker_manager():
    """Shutdown the global worker manager"""
    global _worker_manager
    
    if _worker_manager:
        await _worker_manager.stop()
        _worker_manager = None