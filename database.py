"""
Database models and setup for Mail Sync System
Auto-initializes on first run - no manual setup required
"""

import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List
import uuid

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text, 
    DateTime, Boolean, ForeignKey, Index, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

Base = declarative_base()


class Mailbox(Base):
    """Mailbox configuration table"""
    __tablename__ = "mailboxes"

    id = Column(String(100), primary_key=True, default=lambda: f"mbox_{uuid.uuid4().hex[:12]}")
    user_id = Column(String(100), nullable=False, index=True)
    email = Column(String(255), nullable=False)
    provider = Column(String(50), nullable=False)
    imap_host = Column(String(255), nullable=False)
    imap_port = Column(Integer, nullable=False, default=993)
    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True, default=587)
    encrypted_password = Column(Text, nullable=True)
    username = Column(String(255), nullable=True)
    use_ssl = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    is_validated = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    folders = relationship("Folder", back_populates="mailbox", cascade="all, delete-orphan")
    emails = relationship("Email", back_populates="mailbox", cascade="all, delete-orphan")
    sync_state = relationship("SyncState", back_populates="mailbox", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_mailbox_user_email', 'user_id', 'email'),
    )


class Folder(Base):
    """Email folder table (INBOX, Sent, Drafts, etc.)"""
    __tablename__ = "folders"

    id = Column(String(100), primary_key=True, default=lambda: f"fld_{uuid.uuid4().hex[:12]}")
    mailbox_id = Column(String(100), ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    full_path = Column(String(500), nullable=False)
    total_emails = Column(Integer, default=0)
    unread_count = Column(Integer, default=0)
    is_selectable = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    mailbox = relationship("Mailbox", back_populates="folders")
    emails = relationship("Email", back_populates="folder")

    __table_args__ = (
        Index('idx_folder_mailbox_path', 'mailbox_id', 'full_path'),
        UniqueConstraint('mailbox_id', 'full_path', name='uq_folder_mailbox_path'),
    )


class Email(Base):
    """Email message table"""
    __tablename__ = "emails"

    id = Column(String(100), primary_key=True, default=lambda: f"eml_{uuid.uuid4().hex[:12]}")
    mailbox_id = Column(String(100), ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False)
    folder_id = Column(String(100), ForeignKey("folders.id", ondelete="CASCADE"), nullable=False)
    imap_uid = Column(BigInteger, nullable=False)
    message_id = Column(String(500), nullable=True, index=True)
    subject = Column(Text, nullable=True)
    sender = Column(Text, nullable=True)
    sender_email = Column(String(255), nullable=True)
    recipients = Column(Text, nullable=True)
    cc = Column(Text, nullable=True)
    bcc = Column(Text, nullable=True)
    date = Column(DateTime, nullable=True)
    flags = Column(String(255), nullable=True)
    has_attachments = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False)
    is_starred = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)
    snippet = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    received_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    mailbox = relationship("Mailbox", back_populates="emails")
    folder = relationship("Folder", back_populates="emails")
    attachments = relationship("Attachment", back_populates="email", cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_email_mailbox_uid', 'mailbox_id', 'imap_uid'),
        Index('idx_email_folder_date', 'folder_id', 'date'),
        Index('idx_email_message_id', 'message_id'),
        Index('idx_email_sender', 'sender_email'),
    )


class Attachment(Base):
    """Email attachment metadata table"""
    __tablename__ = "attachments"

    id = Column(String(100), primary_key=True, default=lambda: f"att_{uuid.uuid4().hex[:12]}")
    email_id = Column(String(100), ForeignKey("emails.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=False)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    storage_path = Column(String(1000), nullable=True)
    content_id = Column(String(255), nullable=True)
    is_inline = Column(Boolean, default=False)
    checksum = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    email = relationship("Email", back_populates="attachments")

    __table_args__ = (
        Index('idx_attachment_email', 'email_id'),
    )


class SyncState(Base):
    """Track last synced UID and sync status for each mailbox"""
    __tablename__ = "sync_state"

    id = Column(String(100), primary_key=True, default=lambda: f"syn_{uuid.uuid4().hex[:12]}")
    mailbox_id = Column(String(100), ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False, unique=True)
    last_uid = Column(BigInteger, default=0)
    last_uid_validity = Column(BigInteger, default=0)
    last_sync_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    is_syncing = Column(Boolean, default=False)
    sync_status = Column(String(50), default="idle")
    total_synced = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    mailbox = relationship("Mailbox", back_populates="sync_state")

    __table_args__ = (
        Index('idx_sync_state_mailbox', 'mailbox_id'),
    )


class EmailList(Base):
    """Email list for campaigns and validation"""
    __tablename__ = "email_lists"

    id = Column(String(100), primary_key=True, default=lambda: f"elist_{uuid.uuid4().hex[:12]}")
    user_id = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    source = Column(String(50), nullable=True)
    total_emails = Column(Integer, default=0)
    valid_count = Column(Integer, default=0)
    invalid_count = Column(Integer, default=0)
    unchecked_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    emails = relationship("EmailListItem", back_populates="email_list", cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_email_list_user', 'user_id'),
    )


class EmailListItem(Base):
    """Individual email in a list"""
    __tablename__ = "email_list_items"

    id = Column(String(100), primary_key=True, default=lambda: f"elistem_{uuid.uuid4().hex[:12]}")
    email_list_id = Column(String(100), ForeignKey("email_lists.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    status = Column(String(50), default="unchecked")
    validation_result = Column(Text, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    validated_at = Column(DateTime, nullable=True)

    email_list = relationship("EmailList", back_populates="emails")

    __table_args__ = (
        Index('idx_email_list_item_list', 'email_list_id'),
        Index('idx_email_list_item_email', 'email'),
    )


class ImapConnection(Base):
    """IMAP connection pool table"""
    __tablename__ = "imap_connections"

    id = Column(String(100), primary_key=True, default=lambda: f"iconn_{uuid.uuid4().hex[:12]}")
    mailbox_id = Column(String(100), ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False)
    connected_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    session_data = Column(Text, nullable=True)

    __table_args__ = (
        Index('idx_imap_conn_mailbox', 'mailbox_id'),
    )


# ==================== DATABASE MANAGER ====================

class DatabaseManager:
    """Manages database connections and sessions - auto-initializes on first use"""
    
    _engine = None
    _SessionLocal = None
    _initialized = False

    @classmethod
    def get_db_path(cls) -> Path:
        """Get database file path"""
        base_dir = Path(__file__).parent.resolve()
        data_dir = base_dir / "data"
        data_dir.mkdir(exist_ok=True)
        return data_dir / "mailbox.db"

    @classmethod
    def init(cls, db_path: Path = None, echo: bool = False):
        """Initialize database connection and create tables"""
        if cls._initialized and cls._engine is not None:
            return cls._engine
            
        if db_path is None:
            db_path = cls.get_db_path()
        
        # Ensure parent directory exists
        db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Create SQLite engine
        database_url = f"sqlite:///{db_path}"
        cls._engine = create_engine(
            database_url,
            echo=echo,
            connect_args={"check_same_thread": False}
        )
        cls._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls._engine)
        
        # Create all tables
        Base.metadata.create_all(bind=cls._engine)
        cls._initialized = True
        
        return cls._engine

    @classmethod
    def get_session(cls):
        """Get a new database session"""
        if cls._SessionLocal is None:
            cls.init()
        return cls._SessionLocal()

    @classmethod
    def create_tables(cls):
        """Create all tables"""
        if cls._engine is None:
            cls.init()
        Base.metadata.create_all(bind=cls._engine)

    @classmethod
    def is_initialized(cls) -> bool:
        """Check if database is initialized"""
        return cls._initialized

    @classmethod
    def reset(cls):
        """Reset database (WARNING: deletes all data)"""
        if cls._engine:
            Base.metadata.drop_all(bind=cls._engine)
            Base.metadata.create_all(bind=cls._engine)
            return True
        return False