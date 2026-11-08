"""
Email Cache Manager - Performance Optimization
Local caching system for instant email access
Features:
- Email metadata cache
- Folder list cache
- Search index
- Recent emails cache
- Batch operations
"""

import asyncio
import json
import time
import os
import logging
from typing import Dict, Optional, List, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import OrderedDict
from pathlib import Path
import threading
import sqlite3

logger = logging.getLogger(__name__)


@dataclass
class CachedEmail:
    """Cached email metadata"""
    uid: int
    subject: str
    sender: str
    date: str
    size: int
    flags: str
    message_id: str
    folder: str
    is_read: bool
    cached_at: float
    body_cached: bool = False
    body_path: Optional[str] = None


@dataclass
class CachedFolder:
    """Cached folder information"""
    name: str
    total_emails: int
    unread_count: int
    last_modified: float
    cached_at: float


class EmailCacheManager:
    """
    Local email cache for instant access
    
    Features:
    - SQLite-backed metadata storage
    - In-memory LRU cache for hot data
    - Folder list caching
    - Email body caching
    - Search index
    """
    
    def __init__(
        self,
        cache_dir: str = None,
        max_memory_items: int = 1000,
        cache_ttl: int = 3600,  # 1 hour
        db_name: str = "email_cache.db"
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else Path("./cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        self.max_memory_items = max_memory_items
        self.cache_ttl = cache_ttl
        self.db_path = self.cache_dir / db_name
        
        # In-memory caches
        self._email_cache: OrderedDict[str, CachedEmail] = OrderedDict()
        self._folder_cache: Dict[str, List[CachedFolder]] = {}
        self._search_index: Dict[str, List[str]] = {}  # word -> [mailbox_id:folder:uid]
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # SQLite connection
        self._db: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()
        
        # Stats
        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "db_queries": 0,
            "items_cached": 0
        }
    
    async def start(self):
        """Initialize the cache database"""
        await self._init_database()
        logger.info("Email Cache Manager started")
    
    async def stop(self):
        """Close database connection"""
        if self._db:
            self._db.close()
            self._db = None
        logger.info("Email Cache Manager stopped")
    
    async def _init_database(self):
        """Initialize SQLite database"""
        try:
            self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS email_cache (
                    cache_key TEXT PRIMARY KEY,
                    mailbox_id TEXT,
                    folder TEXT,
                    uid INTEGER,
                    subject TEXT,
                    sender TEXT,
                    date TEXT,
                    size INTEGER,
                    flags TEXT,
                    message_id TEXT,
                    is_read INTEGER,
                    cached_at REAL,
                    body_cached INTEGER DEFAULT 0,
                    body_path TEXT,
                    data TEXT
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS folder_cache (
                    cache_key TEXT PRIMARY KEY,
                    mailbox_id TEXT,
                    name TEXT,
                    total_emails INTEGER,
                    unread_count INTEGER,
                    last_modified REAL,
                    cached_at REAL
                )
            """)
            # One-time migration for the per-folder uniqueness fix: the old
            # schema inserted every row in a folder list with the SAME
            # cache_key ("folders:<mailbox_id>"), so the second-and-onwards
            # rows were rejected with UNIQUE constraint failures. We rebuild
            # the table here to make the cache_key unique per folder.
            cursor = self._db.execute("PRAGMA table_info(folder_cache)")
            folder_columns = {row[1] for row in cursor.fetchall()}
            if folder_columns and folder_columns != {
                "cache_key", "mailbox_id", "name", "total_emails",
                "unread_count", "last_modified", "cached_at"
            }:
                try:
                    self._db.execute("ALTER TABLE folder_cache RENAME TO folder_cache__legacy")
                    self._db.execute("""
                        CREATE TABLE folder_cache (
                            cache_key TEXT PRIMARY KEY,
                            mailbox_id TEXT,
                            name TEXT,
                            total_emails INTEGER,
                            unread_count INTEGER,
                            last_modified REAL,
                            cached_at REAL
                        )
                    """)
                    self._db.execute("DROP TABLE folder_cache__legacy")
                except Exception as e:
                    logger.warning(f"Could not migrate folder_cache schema: {e}")
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_email_mailbox_folder 
                ON email_cache(mailbox_id, folder)
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_email_uid
                ON email_cache(mailbox_id, folder, uid)
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS envelope_cache (
                    cache_key TEXT PRIMARY KEY,
                    mailbox_id TEXT,
                    folder TEXT,
                    uid INTEGER,
                    position INTEGER,
                    subject TEXT,
                    sender TEXT,
                    date TEXT,
                    flags TEXT,
                    message_id TEXT,
                    is_read INTEGER,
                    cached_at REAL
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_envelope_lookup
                ON envelope_cache(mailbox_id, folder, cached_at)
            """)
            self._db.commit()
            logger.info(f"Cache database initialized: {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize cache database: {e}")
    
    def _make_email_key(self, mailbox_id: str, folder: str, uid: int) -> str:
        """Create cache key for email"""
        return f"{mailbox_id}:{folder}:{uid}"

    def _make_folder_key(self, mailbox_id: str) -> str:
        """Create cache key for folder list (mailbox-level, used to clear)."""
        return f"folders:{mailbox_id}"

    def _make_folder_row_key(self, mailbox_id: str, folder_name: str) -> str:
        """Create a cache key unique per (mailbox, folder) row.

        The old implementation used the same mailbox-level key for every
        row in a folder list, which produced UNIQUE constraint failures
        when caching more than one folder.
        """
        return f"folders:{mailbox_id}:{folder_name}"

    def _make_envelope_key(self, mailbox_id: str, folder: str, uid: int = 0) -> str:
        """
        Cache key for an envelope entry.

        With `uid=0` it returns the folder-level "list view" key.
        With a real `uid` it returns the per-envelope key (used to keep the
        PRIMARY KEY unique across the rows in the table).
        """
        if uid:
            return f"envelope:{mailbox_id}:{folder}:{uid}"
        return f"envelope:{mailbox_id}:{folder}"
    
    # ===========================================================================
    # EMAIL CACHE OPERATIONS
    # ===========================================================================
    
    async def cache_email(
        self,
        mailbox_id: str,
        folder: str,
        uid: int,
        subject: str,
        sender: str,
        date: str,
        size: int,
        flags: str,
        message_id: str,
        is_read: bool,
        extra_data: dict = None
    ) -> bool:
        """
        Cache email metadata
        
        Args:
            mailbox_id: Mailbox identifier
            folder: Folder name
            uid: Email UID
            subject: Email subject
            sender: Sender address
            date: Date string
            size: Email size
            flags: IMAP flags
            message_id: Message-ID header
            is_read: Read status
            extra_data: Additional data to cache
        
        Returns:
            bool: Success status
        """
        async with self._lock:
            cache_key = self._make_email_key(mailbox_id, folder, uid)
            now = time.time()
            
            # Create cached email
            cached = CachedEmail(
                uid=uid,
                subject=subject,
                sender=sender,
                date=date,
                size=size,
                flags=flags,
                message_id=message_id,
                folder=folder,
                is_read=is_read,
                cached_at=now
            )
            
            # Update in-memory cache
            if len(self._email_cache) >= self.max_memory_items:
                # Remove oldest item
                self._email_cache.popitem(last=False)
            
            self._email_cache[cache_key] = cached
            self._email_cache.move_to_end(cache_key)
            
            # Update search index
            self._update_search_index(mailbox_id, folder, uid, subject, sender)
            
            # Update database
            try:
                with self._db_lock:
                    self._db.execute("""
                        INSERT OR REPLACE INTO email_cache 
                        (cache_key, mailbox_id, folder, uid, subject, sender, date, 
                         size, flags, message_id, is_read, cached_at, data)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        cache_key, mailbox_id, folder, uid, subject, sender, date,
                        size, flags, message_id, int(is_read), now,
                        json.dumps(extra_data) if extra_data else None
                    ))
                    self._db.commit()
                
                self._stats["items_cached"] += 1
                return True
                
            except Exception as e:
                logger.error(f"Failed to cache email: {e}")
                return False
    
    async def get_cached_email(
        self,
        mailbox_id: str,
        folder: str,
        uid: int
    ) -> Optional[CachedEmail]:
        """
        Get cached email metadata
        
        Args:
            mailbox_id: Mailbox identifier
            folder: Folder name
            uid: Email UID
        
        Returns:
            CachedEmail or None
        """
        cache_key = self._make_email_key(mailbox_id, folder, uid)
        
        # Check in-memory cache first
        if cache_key in self._email_cache:
            cached = self._email_cache[cache_key]
            
            # Check TTL
            if time.time() - cached.cached_at < self.cache_ttl:
                self._email_cache.move_to_end(cache_key)
                self._stats["cache_hits"] += 1
                return cached
            else:
                # Expired - remove
                del self._email_cache[cache_key]
        
        # Check database
        self._stats["db_queries"] += 1
        try:
            with self._db_lock:
                cursor = self._db.execute("""
                    SELECT uid, subject, sender, date, size, flags, message_id,
                           folder, is_read, cached_at, body_cached, body_path
                    FROM email_cache 
                    WHERE cache_key = ?
                """, (cache_key,))
                
                row = cursor.fetchone()
                if row:
                    cached = CachedEmail(
                        uid=row[0],
                        subject=row[1],
                        sender=row[2],
                        date=row[3],
                        size=row[4],
                        flags=row[5],
                        message_id=row[6],
                        folder=row[7],
                        is_read=bool(row[8]),
                        cached_at=row[9],
                        body_cached=bool(row[10]),
                        body_path=row[11]
                    )
                    
                    # Update memory cache
                    if len(self._email_cache) >= self.max_memory_items:
                        self._email_cache.popitem(last=False)
                    self._email_cache[cache_key] = cached
                    
                    self._stats["cache_hits"] += 1
                    return cached
                    
        except Exception as e:
            logger.error(f"Failed to get cached email: {e}")
        
        self._stats["cache_misses"] += 1
        return None
    
    async def get_cached_emails_batch(
        self,
        mailbox_id: str,
        folder: str,
        uids: List[int]
    ) -> Dict[int, CachedEmail]:
        """
        Get multiple cached emails at once
        
        Args:
            mailbox_id: Mailbox identifier
            folder: Folder name
            uids: List of UIDs
        
        Returns:
            Dict of uid -> CachedEmail
        """
        results = {}
        
        # Get from memory cache
        for uid in uids:
            cache_key = self._make_email_key(mailbox_id, folder, uid)
            if cache_key in self._email_cache:
                results[uid] = self._email_cache[cache_key]
        
        # Get missing from database
        missing_uids = [uid for uid in uids if uid not in results]
        if missing_uids:
            try:
                with self._db_lock:
                    placeholders = ','.join(['?'] * len(missing_uids))
                    cursor = self._db.execute(f"""
                        SELECT uid, subject, sender, date, size, flags, message_id,
                               folder, is_read, cached_at, body_cached, body_path
                        FROM email_cache 
                        WHERE mailbox_id = ? AND folder = ? AND uid IN ({placeholders})
                    """, (mailbox_id, folder) + tuple(missing_uids))
                    
                    for row in cursor.fetchall():
                        cached = CachedEmail(
                            uid=row[0],
                            subject=row[1],
                            sender=row[2],
                            date=row[3],
                            size=row[4],
                            flags=row[5],
                            message_id=row[6],
                            folder=row[7],
                            is_read=bool(row[8]),
                            cached_at=row[9],
                            body_cached=bool(row[10]),
                            body_path=row[11]
                        )
                        results[cached.uid] = cached
                        
                        # Update memory cache
                        cache_key = self._make_email_key(mailbox_id, folder, cached.uid)
                        if len(self._email_cache) < self.max_memory_items:
                            self._email_cache[cache_key] = cached
                        
            except Exception as e:
                logger.error(f"Failed to get cached emails batch: {e}")
        
        return results
    
    async def update_email_flags(
        self,
        mailbox_id: str,
        folder: str,
        uid: int,
        flags: str,
        is_read: bool
    ) -> bool:
        """Update cached email flags"""
        cache_key = self._make_email_key(mailbox_id, folder, uid)
        
        # Update memory cache
        if cache_key in self._email_cache:
            self._email_cache[cache_key].flags = flags
            self._email_cache[cache_key].is_read = is_read
        
        # Update database
        try:
            with self._db_lock:
                self._db.execute("""
                    UPDATE email_cache 
                    SET flags = ?, is_read = ?
                    WHERE cache_key = ?
                """, (flags, int(is_read), cache_key))
                self._db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to update email flags: {e}")
            return False
    
    async def invalidate_email(
        self,
        mailbox_id: str,
        folder: str,
        uid: int
    ) -> bool:
        """Remove email from cache"""
        cache_key = self._make_email_key(mailbox_id, folder, uid)
        
        # Remove from memory cache
        if cache_key in self._email_cache:
            del self._email_cache[cache_key]
        
        # Remove from database
        try:
            with self._db_lock:
                self._db.execute("DELETE FROM email_cache WHERE cache_key = ?", (cache_key,))
                self._db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to invalidate email: {e}")
            return False
    
    # ===========================================================================
    # FOLDER CACHE OPERATIONS
    # ===========================================================================
    
    async def cache_folders(
        self,
        mailbox_id: str,
        folders: List[Dict[str, Any]]
    ) -> bool:
        """
        Cache folder list

        Args:
            mailbox_id: Mailbox identifier
            folders: List of folder info dicts

        Returns:
            bool: Success status
        """
        cache_key_prefix = self._make_folder_key(mailbox_id)
        now = time.time()

        cached_folders = [
            CachedFolder(
                name=f.get("name", ""),
                total_emails=f.get("total", 0),
                unread_count=f.get("unread", 0),
                last_modified=f.get("last_modified", now),
                cached_at=now
            )
            for f in folders
        ]

        # Update memory cache
        self._folder_cache[mailbox_id] = cached_folders

        # Update database
        try:
            with self._db_lock:
                # Clear old rows for this mailbox. We use a LIKE on the
                # prefix to wipe both legacy mailbox-only keys and the
                # per-folder keys we generate now.
                self._db.execute(
                    "DELETE FROM folder_cache WHERE cache_key = ? OR cache_key LIKE ?",
                    (cache_key_prefix, cache_key_prefix + ":%"),
                )

                # Insert new entries - each row gets its own unique key
                # so the PRIMARY KEY constraint cannot fire.
                inserted = set()
                for folder in cached_folders:
                    folder_name = (folder.name or '').strip()
                    if not folder_name:
                        continue
                    if folder_name in inserted:
                        continue
                    inserted.add(folder_name)
                    row_key = self._make_folder_row_key(mailbox_id, folder_name)
                    self._db.execute("""
                        INSERT INTO folder_cache
                        (cache_key, mailbox_id, name, total_emails, unread_count,
                         last_modified, cached_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        row_key, mailbox_id, folder_name, folder.total_emails,
                        folder.unread_count, folder.last_modified, now
                    ))
                self._db.commit()

            return True
        except Exception as e:
            logger.error(f"Failed to cache folders: {e}")
            return False
    
    async def get_cached_folders(self, mailbox_id: str) -> Optional[List[CachedFolder]]:
        """
        Get cached folder list

        Args:
            mailbox_id: Mailbox identifier

        Returns:
            List of CachedFolder or None
        """
        # Check memory cache
        if mailbox_id in self._folder_cache:
            return self._folder_cache[mailbox_id]

        # Check database - match both the legacy mailbox-only key and the
        # per-folder keys we generate now.
        cache_key_prefix = self._make_folder_key(mailbox_id)
        try:
            with self._db_lock:
                cursor = self._db.execute("""
                    SELECT name, total_emails, unread_count, last_modified, cached_at
                    FROM folder_cache
                    WHERE cache_key = ? OR cache_key LIKE ?
                """, (cache_key_prefix, cache_key_prefix + ":%"))

                rows = cursor.fetchall()
                if rows:
                    folders = [
                        CachedFolder(
                            name=row[0],
                            total_emails=row[1],
                            unread_count=row[2],
                            last_modified=row[3],
                            cached_at=row[4]
                        )
                        for row in rows
                    ]
                    self._folder_cache[mailbox_id] = folders
                    return folders

        except Exception as e:
            logger.error(f"Failed to get cached folders: {e}")

        return None
    
    # ===========================================================================
    # SEARCH INDEX
    # ===========================================================================
    
    def _update_search_index(
        self,
        mailbox_id: str,
        folder: str,
        uid: int,
        subject: str,
        sender: str
    ):
        """Update search index with email data"""
        words = set()
        
        # Extract words from subject
        for word in subject.lower().split():
            word = ''.join(c for c in word if c.isalnum())
            if len(word) >= 3:
                words.add(word)
        
        # Extract words from sender
        for word in sender.lower().split():
            word = ''.join(c for c in word if c.isalnum())
            if len(word) >= 3:
                words.add(word)
        
        # Add to index
        cache_key = f"{mailbox_id}:{folder}:{uid}"
        for word in words:
            if word not in self._search_index:
                self._search_index[word] = []
            if cache_key not in self._search_index[word]:
                self._search_index[word].append(cache_key)
    
    async def search_cached(
        self,
        query: str,
        mailbox_id: str = None
    ) -> List[str]:
        """
        Search cached emails
        
        Args:
            query: Search query
            mailbox_id: Optional mailbox filter
        
        Returns:
            List of cache keys
        """
        words = query.lower().split()
        results = set()
        
        for word in words:
            word = ''.join(c for c in word if c.isalnum())
            if word in self._search_index:
                if results:
                    results &= set(self._search_index[word])
                else:
                    results = set(self._search_index[word])
        
        if mailbox_id:
            results = {
                k for k in results
                if k.startswith(f"{mailbox_id}:")
            }
        
        return list(results)
    
    # ===========================================================================
    # CACHE MANAGEMENT
    # ===========================================================================
    
    async def clear_mailbox_cache(self, mailbox_id: str) -> bool:
        """Clear all cached data for a mailbox"""
        async with self._lock:
            # Clear memory cache
            keys_to_remove = [
                k for k in self._email_cache.keys()
                if k.startswith(f"{mailbox_id}:")
            ]
            for key in keys_to_remove:
                del self._email_cache[key]
            
            if mailbox_id in self._folder_cache:
                del self._folder_cache[mailbox_id]
            
            # Clear search index
            keys_to_remove = [
                k for k, items in self._search_index.items()
                if any(f"{mailbox_id}:" in item for item in items)
            ]
            for key in keys_to_remove:
                self._search_index[key] = [
                    item for item in self._search_index[key]
                    if not item.startswith(f"{mailbox_id}:")
                ]
                if not self._search_index[key]:
                    del self._search_index[key]
            
            # Clear database
            try:
                with self._db_lock:
                    self._db.execute("DELETE FROM email_cache WHERE mailbox_id = ?", (mailbox_id,))
                    self._db.execute("DELETE FROM folder_cache WHERE mailbox_id = ?", (mailbox_id,))
                    self._db.execute("DELETE FROM envelope_cache WHERE mailbox_id = ?", (mailbox_id,))
                    self._db.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to clear mailbox cache: {e}")
                return False

    # ===========================================================================
    # ENVELOPE-LEVEL CACHE OPERATIONS (list view: subject, sender, date, flags)
    # ===========================================================================

    async def get_envelopes(
        self,
        mailbox_id: str,
        folder: str,
        max_age_seconds: int = 300
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Get cached envelope list for a folder.

        Returns a list of dicts with the same shape as the list view
        (uid, subject, sender, date, flags, message_id, is_read),
        or None if not cached / expired.
        """
        now = time.time()
        try:
            with self._db_lock:
                cursor = self._db.execute("""
                    SELECT uid, position, subject, sender, date, flags, message_id, is_read, cached_at
                    FROM envelope_cache
                    WHERE mailbox_id = ? AND folder = ?
                    ORDER BY position ASC
                """, (mailbox_id, folder))
                rows = cursor.fetchall()
                if not rows:
                    return None
                # Reject if the entire envelope is older than max_age
                cached_at = rows[0][8]
                if now - cached_at > max_age_seconds:
                    return None
                return [
                    {
                        "uid": r[0],
                        "position": r[1],
                        "subject": r[2],
                        "sender": r[3],
                        "date": r[4],
                        "flags": r[5],
                        "message_id": r[6],
                        "is_read": bool(r[7]),
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Failed to get cached envelopes: {e}")
            return None

    async def put_envelopes(
        self,
        mailbox_id: str,
        folder: str,
        envelopes: List[Dict[str, Any]]
    ) -> bool:
        """
        Cache an envelope list for a folder. Replaces any prior entry.
        """
        now = time.time()
        try:
            with self._db_lock:
                # Clear all prior entries for this (mailbox, folder)
                self._db.execute(
                    "DELETE FROM envelope_cache WHERE mailbox_id = ? AND folder = ?",
                    (mailbox_id, folder),
                )
                rows = []
                for idx, env in enumerate(envelopes):
                    uid = env.get("uid", 0)
                    cache_key = self._make_envelope_key(mailbox_id, folder, uid)
                    rows.append((
                        cache_key, mailbox_id, folder, uid, idx,
                        (env.get("subject", "") or "")[:500],
                        (env.get("sender", "") or "")[:500],
                        (env.get("date", "") or "")[:100],
                        (env.get("flags", "") or "")[:100],
                        (env.get("message_id", "") or "")[:500],
                        int(bool(env.get("is_read", False))),
                        now
                    ))
                if rows:
                    self._db.executemany("""
                        INSERT INTO envelope_cache
                        (cache_key, mailbox_id, folder, uid, position,
                         subject, sender, date, flags, message_id, is_read, cached_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, rows)
                self._db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to cache envelopes: {e}")
            return False

    async def invalidate_envelopes(self, mailbox_id: str, folder: str = None) -> bool:
        """Invalidate cached envelope list for a mailbox/folder."""
        try:
            with self._db_lock:
                if folder:
                    self._db.execute(
                        "DELETE FROM envelope_cache WHERE mailbox_id = ? AND folder = ?",
                        (mailbox_id, folder),
                    )
                else:
                    self._db.execute("DELETE FROM envelope_cache WHERE mailbox_id = ?", (mailbox_id,))
                self._db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to invalidate envelopes: {e}")
            return False
    
    async def clear_expired(self, max_age: int = 86400) -> int:
        """
        Clear expired cache entries
        
        Args:
            max_age: Maximum age in seconds
        
        Returns:
            Number of entries cleared
        """
        now = time.time()
        cleared = 0
        
        # Clear expired memory entries
        keys_to_remove = []
        for key, cached in self._email_cache.items():
            if now - cached.cached_at > max_age:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self._email_cache[key]
            cleared += 1
        
        # Clear expired database entries
        try:
            with self._db_lock:
                cursor = self._db.execute("""
                    DELETE FROM email_cache WHERE cached_at < ?
                """, (now - max_age,))
                cleared += cursor.rowcount

                cursor = self._db.execute("""
                    DELETE FROM folder_cache WHERE cached_at < ?
                """, (now - max_age,))
                cleared += cursor.rowcount

                cursor = self._db.execute("""
                    DELETE FROM envelope_cache WHERE cached_at < ?
                """, (now - max_age,))
                cleared += cursor.rowcount

                self._db.commit()
        except Exception as e:
            logger.error(f"Failed to clear expired cache: {e}")
        
        return cleared
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return {
            **self._stats,
            "memory_items": len(self._email_cache),
            "max_memory_items": self.max_memory_items,
            "folders_cached": len(self._folder_cache),
            "search_index_words": len(self._search_index),
            "hit_rate": (
                self._stats["cache_hits"] / 
                max(1, self._stats["cache_hits"] + self._stats["cache_misses"])
            )
        }


# =============================================================================
# GLOBAL CACHE MANAGER INSTANCE
# =============================================================================

_cache_manager: Optional[EmailCacheManager] = None
_user_cache_managers: Dict[str, EmailCacheManager] = {}
_user_cache_lock = threading.Lock()


async def get_email_cache_manager() -> EmailCacheManager:
    """Get or create the global email cache manager (used for admin/aggregate views)."""
    global _cache_manager

    if _cache_manager is None:
        _cache_manager = EmailCacheManager(
            cache_dir="./cache",
            max_memory_items=1000,
            cache_ttl=3600
        )
        await _cache_manager.start()

    return _cache_manager


async def get_email_cache_manager_for_user(user_id: str) -> EmailCacheManager:
    """
    Get or create a per-user email cache manager.

    The cache DB lives at: {user_base_path}/{user_id}/cache/email_cache.db
    so each user is isolated and removable.

    Falls back to the global cache manager if USERS_BASE_PATH is not
    configured (so the function is safe to call from non-FastAPI contexts).
    """
    if not user_id:
        return await get_email_cache_manager()

    # Try to resolve a per-user cache directory. We import lazily so this
    # module is importable without the full app context.
    try:
        from config import USERS_BASE_PATH  # type: ignore
    except Exception:
        USERS_BASE_PATH = None  # type: ignore

    with _user_cache_lock:
        existing = _user_cache_managers.get(user_id)
        if existing is not None:
            return existing

        if USERS_BASE_PATH is None:
            # Fall back to shared cache when we can't resolve a user path.
            return await get_email_cache_manager()

        # The user folders on disk are named "fv-s-{user_id}". Some callers
        # already pass the full folder name; others pass just the user id.
        folder_name = user_id if user_id.startswith("fv-s-") else f"fv-s-{user_id}"
        user_cache_dir = Path(USERS_BASE_PATH) / folder_name / "cache"
        manager = EmailCacheManager(
            cache_dir=str(user_cache_dir),
            db_name="email_cache.db",
            max_memory_items=2000,
            cache_ttl=3600
        )
        await manager.start()
        _user_cache_managers[user_id] = manager
        logger.info(f"Created per-user email cache for {user_id} at {user_cache_dir}")
        return manager


async def shutdown_email_cache_manager():
    """Shutdown the global email cache manager"""
    global _cache_manager

    if _cache_manager:
        await _cache_manager.stop()
        _cache_manager = None
        logger.info("Email Cache Manager shutdown complete")


async def shutdown_user_cache_managers():
    """Shutdown all per-user cache managers (called on app shutdown)."""
    global _user_cache_managers
    with _user_cache_lock:
        for user_id, manager in list(_user_cache_managers.items()):
            try:
                await manager.stop()
            except Exception as e:
                logger.error(f"Error stopping cache manager for {user_id}: {e}")
        # Clear in-place so external references also see the change.
        _user_cache_managers.clear()
    logger.info("All per-user email cache managers shut down")