"""
Database helper functions for CRUD operations
Provides a clean interface for working with the mail sync database
Auto-initializes database on first use
"""

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid

from database import (
    DatabaseManager, Mailbox, Folder, Email, Attachment, 
    SyncState, EmailList, EmailListItem, ImapConnection
)

# Auto-initialize database on import
DatabaseManager.init()


# ==================== MAILBOX OPERATIONS ====================

def create_mailbox(
    user_id: str,
    email: str,
    provider: str,
    imap_host: str,
    imap_port: int = 993,
    encrypted_password: str = "",
    smtp_host: str = None,
    smtp_port: int = 587,
    use_ssl: bool = True,
    username: str = None,
    mailbox_id: str = None
) -> Mailbox:
    """Create a new mailbox"""
    db = DatabaseManager.get_session()
    try:
        mailbox = Mailbox(
            id=mailbox_id or f"mbox_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            email=email,
            provider=provider,
            imap_host=imap_host,
            imap_port=imap_port,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            encrypted_password=encrypted_password,
            username=username,
            use_ssl=use_ssl,
            is_active=True
        )
        db.add(mailbox)
        db.commit()
        db.refresh(mailbox)
        return mailbox
    finally:
        db.close()


def get_mailbox_by_id(mailbox_id: str) -> Optional[Mailbox]:
    """Get mailbox by ID"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Mailbox).filter(Mailbox.id == mailbox_id).first()
    finally:
        db.close()


def get_mailboxes_by_user(user_id: str) -> List[Mailbox]:
    """Get all mailboxes for a user"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Mailbox).filter(Mailbox.user_id == user_id).all()
    finally:
        db.close()


def update_mailbox(mailbox_id: str, **kwargs) -> Optional[Mailbox]:
    """Update mailbox fields"""
    db = DatabaseManager.get_session()
    try:
        mailbox = db.query(Mailbox).filter(Mailbox.id == mailbox_id).first()
        if mailbox:
            for key, value in kwargs.items():
                if hasattr(mailbox, key):
                    setattr(mailbox, key, value)
            mailbox.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(mailbox)
        return mailbox
    finally:
        db.close()


def delete_mailbox(mailbox_id: str) -> bool:
    """Delete a mailbox and all its data (cascade)"""
    db = DatabaseManager.get_session()
    try:
        mailbox = db.query(Mailbox).filter(Mailbox.id == mailbox_id).first()
        if mailbox:
            db.delete(mailbox)
            db.commit()
            return True
        return False
    finally:
        db.close()


def validate_mailbox(mailbox_id: str) -> bool:
    """Mark mailbox as validated"""
    return update_mailbox(mailbox_id, is_validated=True) is not None


# ==================== FOLDER OPERATIONS ====================

def create_folder(
    mailbox_id: str,
    name: str,
    full_path: str,
    total_emails: int = 0,
    unread_count: int = 0
) -> Folder:
    """Create a new folder"""
    db = DatabaseManager.get_session()
    try:
        folder = Folder(
            mailbox_id=mailbox_id,
            name=name,
            full_path=full_path,
            total_emails=total_emails,
            unread_count=unread_count,
            is_selectable=True
        )
        db.add(folder)
        db.commit()
        db.refresh(folder)
        return folder
    finally:
        db.close()


def get_folders_by_mailbox(mailbox_id: str) -> List[Folder]:
    """Get all folders for a mailbox"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Folder).filter(Folder.mailbox_id == mailbox_id).all()
    finally:
        db.close()


def get_folder_by_path(mailbox_id: str, full_path: str) -> Optional[Folder]:
    """Get folder by mailbox and path"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Folder).filter(
            and_(
                Folder.mailbox_id == mailbox_id,
                Folder.full_path == full_path
            )
        ).first()
    finally:
        db.close()


def update_folder_counts(folder_id: str, total: int = None, unread: int = None):
    """Update folder email counts"""
    db = DatabaseManager.get_session()
    try:
        folder = db.query(Folder).filter(Folder.id == folder_id).first()
        if folder:
            if total is not None:
                folder.total_emails = total
            if unread is not None:
                folder.unread_count = unread
            folder.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def sync_folders_from_imap(mailbox_id: str, folders_data: List[Dict]) -> int:
    """Sync folders from IMAP server"""
    db = DatabaseManager.get_session()
    count = 0
    try:
        for fd in folders_data:
            existing = db.query(Folder).filter(
                and_(
                    Folder.mailbox_id == mailbox_id,
                    Folder.full_path == fd.get("full_path", fd.get("name"))
                )
            ).first()
            
            if existing:
                # Update counts
                existing.total_emails = fd.get("total_emails", existing.total_emails)
                existing.unread_count = fd.get("unread_count", existing.unread_count)
            else:
                # Create new folder
                folder = Folder(
                    mailbox_id=mailbox_id,
                    name=fd.get("name", fd.get("full_path")),
                    full_path=fd.get("full_path", fd.get("name")),
                    total_emails=fd.get("total_emails", 0),
                    unread_count=fd.get("unread_count", 0),
                    is_selectable=fd.get("is_selectable", True)
                )
                db.add(folder)
                count += 1
        
        db.commit()
        return count
    finally:
        db.close()


# ==================== EMAIL OPERATIONS ====================

def create_email(
    mailbox_id: str,
    folder_id: str,
    imap_uid: int,
    subject: str = None,
    sender: str = None,
    sender_email: str = None,
    recipients: str = None,
    date: datetime = None,
    message_id: str = None,
    has_attachments: bool = False,
    is_read: bool = False,
    snippet: str = None,
    body_text: str = None,
    body_html: str = None
) -> Email:
    """Create a new email"""
    db = DatabaseManager.get_session()
    try:
        email = Email(
            mailbox_id=mailbox_id,
            folder_id=folder_id,
            imap_uid=imap_uid,
            subject=subject,
            sender=sender,
            sender_email=sender_email,
            recipients=recipients,
            date=date,
            message_id=message_id,
            has_attachments=has_attachments,
            is_read=is_read,
            snippet=snippet,
            body_text=body_text,
            body_html=body_html
        )
        db.add(email)
        db.commit()
        db.refresh(email)
        return email
    finally:
        db.close()


def create_attachment(
    email_id: str,
    filename: str,
    content_type: str = 'application/octet-stream',
    size_bytes: int = 0,
    storage_path: str = None,
    content_id: str = None,
    is_inline: bool = False,
    checksum: str = None
) -> Attachment:
    """Create a new attachment metadata record."""
    db = DatabaseManager.get_session()
    try:
        attachment = Attachment(
            email_id=email_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_path=storage_path,
            content_id=content_id,
            is_inline=is_inline,
            checksum=checksum
        )
        db.add(attachment)
        db.commit()
        db.refresh(attachment)
        return attachment
    finally:
        db.close()


def delete_email_attachments(email_id: str):
    """Delete all attachments for a specific email."""
    db = DatabaseManager.get_session()
    try:
        db.query(Attachment).filter(Attachment.email_id == email_id).delete()
        db.commit()
    finally:
        db.close()


def update_email_attachments(email_id: str, attachments: List[Dict[str, Any]]):
    """Replace attachment metadata for an email."""
    if not attachments:
        return False

    db = DatabaseManager.get_session()
    try:
        db.query(Attachment).filter(Attachment.email_id == email_id).delete()

        for att in attachments:
            filename = att.get('filename') or att.get('name') or 'attachment'
            content_type = att.get('content_type') or att.get('mime_type') or 'application/octet-stream'
            size_bytes = int(att.get('size') or att.get('size_bytes') or 0)
            is_inline = bool(att.get('is_inline'))
            storage_path = att.get('storage_path')
            content_id = att.get('content_id')
            checksum = att.get('checksum')

            attachment = Attachment(
                email_id=email_id,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                storage_path=storage_path,
                content_id=content_id,
                is_inline=is_inline,
                checksum=checksum
            )
            db.add(attachment)

        db.commit()
        return True
    finally:
        db.close()


def get_email_by_uid(mailbox_id: str, imap_uid: int) -> Optional[Email]:
    """Get email by mailbox and IMAP UID"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Email).options(
            joinedload(Email.attachments),
            joinedload(Email.folder)
        ).filter(
            and_(
                Email.mailbox_id == mailbox_id,
                Email.imap_uid == imap_uid
            )
        ).first()
    finally:
        db.close()


def get_emails_by_folder(folder_id: str, limit: int = 200, offset: int = 0) -> List[Email]:
    """Get emails from a folder with pagination"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Email).filter(
            Email.folder_id == folder_id
        ).order_by(
            Email.date.desc()
        ).offset(offset).limit(limit).all()
    finally:
        db.close()


def count_emails_by_folder(folder_id: str) -> int:
    """Count emails in a folder"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Email).filter(Email.folder_id == folder_id).count()
    finally:
        db.close()


def get_folder_last_uid(mailbox_id: str, folder_id: str) -> int:
    """Highest synced IMAP UID for a specific folder."""
    db = DatabaseManager.get_session()
    try:
        from sqlalchemy import func
        result = db.query(func.max(Email.imap_uid)).filter(
            and_(
                Email.mailbox_id == mailbox_id,
                Email.folder_id == folder_id,
            )
        ).scalar()
        return int(result or 0)
    finally:
        db.close()


def mark_email_read_by_uid(mailbox_id: str, imap_uid: int, is_read: bool = True) -> bool:
    """Mark email read/unread by mailbox and IMAP UID"""
    db = DatabaseManager.get_session()
    try:
        email = db.query(Email).filter(
            and_(
                Email.mailbox_id == mailbox_id,
                Email.imap_uid == imap_uid
            )
        ).first()
        if email:
            email.is_read = is_read
            email.updated_at = datetime.utcnow()
            db.commit()
            return True
        return False
    finally:
        db.close()


def get_recent_emails(mailbox_id: str, limit: int = 200) -> List[Email]:
    """Get most recent emails from mailbox"""
    db = DatabaseManager.get_session()
    try:
        return db.query(Email).filter(
            Email.mailbox_id == mailbox_id
        ).order_by(
            Email.date.desc()
        ).limit(limit).all()
    finally:
        db.close()


def update_email_body(email_id: str, body_html: str = None, body_text: str = None):
    """Update email body (fetched on demand)"""
    db = DatabaseManager.get_session()
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if email:
            if body_html is not None:
                email.body_html = body_html
            if body_text is not None:
                email.body_text = body_text
            email.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def mark_email_read(email_id: str, is_read: bool = True):
    """Mark email as read/unread"""
    db = DatabaseManager.get_session()
    try:
        email = db.query(Email).filter(Email.id == email_id).first()
        if email:
            email.is_read = is_read
            email.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def search_emails(mailbox_id: str, query: str, limit: int = 200) -> List[Email]:
    """Search emails by subject, sender, or recipients"""
    db = DatabaseManager.get_session()
    try:
        search_filter = or_(
            Email.subject.ilike(f"%{query}%"),
            Email.sender.ilike(f"%{query}%"),
            Email.sender_email.ilike(f"%{query}%"),
            Email.recipients.ilike(f"%{query}%")
        )
        return db.query(Email).filter(
            and_(
                Email.mailbox_id == mailbox_id,
                search_filter
            )
        ).order_by(
            Email.date.desc()
        ).limit(limit).all()
    finally:
        db.close()


def bulk_insert_emails(emails_data: List[Dict]) -> int:
    """Bulk insert emails for faster sync"""
    db = DatabaseManager.get_session()
    count = 0
    try:
        for ed in emails_data:
            # Check if email already exists
            existing = db.query(Email).filter(
                and_(
                    Email.mailbox_id == ed.get("mailbox_id"),
                    Email.imap_uid == ed.get("imap_uid")
                )
            ).first()
            
            if not existing:
                email = Email(**ed)
                db.add(email)
                count += 1
        
        db.commit()
        return count
    finally:
        db.close()


# ==================== SYNC STATE OPERATIONS ====================

def get_sync_state(mailbox_id: str) -> Optional[SyncState]:
    """Get sync state for a mailbox"""
    db = DatabaseManager.get_session()
    try:
        return db.query(SyncState).filter(SyncState.mailbox_id == mailbox_id).first()
    finally:
        db.close()


def update_sync_state(
    mailbox_id: str,
    last_uid: int = None,
    is_syncing: bool = None,
    status: str = None,
    error: str = None,
    increment_synced: int = 0
):
    """Update sync state"""
    db = DatabaseManager.get_session()
    try:
        sync_state = db.query(SyncState).filter(SyncState.mailbox_id == mailbox_id).first()
        
        if not sync_state:
            # Create if doesn't exist
            sync_state = SyncState(
                mailbox_id=mailbox_id
            )
            db.add(sync_state)
        
        if last_uid is not None:
            sync_state.last_uid = last_uid
        if is_syncing is not None:
            sync_state.is_syncing = is_syncing
        if status is not None:
            sync_state.sync_status = status
        if error is not None:
            sync_state.last_error = error
        if increment_synced > 0:
            sync_state.total_synced += increment_synced
            
        sync_state.last_sync_at = datetime.utcnow()
        db.commit()
        
    finally:
        db.close()


def start_sync(mailbox_id: str):
    """Mark mailbox as currently syncing"""
    update_sync_state(mailbox_id, is_syncing=True, status="syncing")


def complete_sync(mailbox_id: str, last_uid: int):
    """Mark mailbox sync as completed"""
    update_sync_state(mailbox_id, is_syncing=False, status="completed", last_uid=last_uid)


def fail_sync(mailbox_id: str, error: str):
    """Mark mailbox sync as failed"""
    update_sync_state(mailbox_id, is_syncing=False, status="error", error=error)


# ==================== EMAIL LIST OPERATIONS ====================

def create_email_list(
    user_id: str,
    name: str,
    description: str = None,
    source: str = "manual"
) -> EmailList:
    """Create a new email list"""
    db = DatabaseManager.get_session()
    try:
        email_list = EmailList(
            user_id=user_id,
            name=name,
            description=description,
            source=source,
            total_emails=0
        )
        db.add(email_list)
        db.commit()
        db.refresh(email_list)
        return email_list
    finally:
        db.close()


def get_email_list_by_id(list_id: str) -> Optional[EmailList]:
    """Get email list by ID"""
    db = DatabaseManager.get_session()
    try:
        return db.query(EmailList).filter(EmailList.id == list_id).first()
    finally:
        db.close()


def get_email_lists_by_user(user_id: str) -> List[EmailList]:
    """Get all email lists for a user"""
    db = DatabaseManager.get_session()
    try:
        return db.query(EmailList).filter(EmailList.user_id == user_id).all()
    finally:
        db.close()


def add_email_to_list(list_id: str, email_address: str) -> EmailListItem:
    """Add email to a list"""
    db = DatabaseManager.get_session()
    try:
        item = EmailListItem(
            email_list_id=list_id,
            email=email_address,
            status="unchecked"
        )
        db.add(item)
        
        # Update list count
        email_list = db.query(EmailList).filter(EmailList.id == list_id).first()
        if email_list:
            email_list.total_emails += 1
            email_list.unchecked_count += 1
        
        db.commit()
        db.refresh(item)
        return item
    finally:
        db.close()


def bulk_add_emails_to_list(list_id: str, email_addresses: List[str]) -> int:
    """Add multiple emails to a list"""
    db = DatabaseManager.get_session()
    count = 0
    try:
        for email_address in email_addresses:
            item = EmailListItem(
                email_list_id=list_id,
                email=email_address.strip(),
                status="unchecked"
            )
            db.add(item)
            count += 1
        
        # Update list counts
        email_list = db.query(EmailList).filter(EmailList.id == list_id).first()
        if email_list:
            email_list.total_emails += count
            email_list.unchecked_count += count
        
        db.commit()
        return count
    finally:
        db.close()


def update_email_validation_result(list_item_id: str, status: str, validation_data: dict = None):
    """Update email validation result"""
    db = DatabaseManager.get_session()
    try:
        item = db.query(EmailListItem).filter(EmailListItem.id == list_item_id).first()
        if item:
            old_status = item.status
            item.status = status
            item.validation_result = str(validation_data) if validation_data else None
            item.validated_at = datetime.utcnow()
            
            # Update list counts
            email_list = db.query(EmailList).filter(EmailList.id == item.email_list_id).first()
            if email_list:
                if old_status == "unchecked":
                    email_list.unchecked_count -= 1
                elif old_status == "valid":
                    email_list.valid_count -= 1
                elif old_status == "invalid":
                    email_list.invalid_count -= 1
                    
                if status == "valid":
                    email_list.valid_count += 1
                elif status == "invalid":
                    email_list.invalid_count += 1
                elif status == "unchecked":
                    email_list.unchecked_count += 1
            
            db.commit()
    finally:
        db.close()


def get_email_list_items(list_id: str, status: str = None, limit: int = 100) -> List[EmailListItem]:
    """Get items from an email list"""
    db = DatabaseManager.get_session()
    try:
        query = db.query(EmailListItem).filter(EmailListItem.email_list_id == list_id)
        if status:
            query = query.filter(EmailListItem.status == status)
        return query.limit(limit).all()
    finally:
        db.close()


def delete_email_list(list_id: str) -> bool:
    """Delete an email list and all its items"""
    db = DatabaseManager.get_session()
    try:
        email_list = db.query(EmailList).filter(EmailList.id == list_id).first()
        if email_list:
            db.delete(email_list)
            db.commit()
            return True
        return False
    finally:
        db.close()


# ==================== IMAP CONNECTION POOL ====================

def create_imap_connection(mailbox_id: str, session_data: str = None) -> ImapConnection:
    """Create a new IMAP connection record"""
    db = DatabaseManager.get_session()
    try:
        conn = ImapConnection(
            mailbox_id=mailbox_id,
            session_data=session_data
        )
        db.add(conn)
        db.commit()
        db.refresh(conn)
        return conn
    finally:
        db.close()


def get_active_imap_connection(mailbox_id: str) -> Optional[ImapConnection]:
    """Get active IMAP connection for mailbox"""
    db = DatabaseManager.get_session()
    try:
        return db.query(ImapConnection).filter(
            and_(
                ImapConnection.mailbox_id == mailbox_id,
                ImapConnection.is_active == True
            )
        ).first()
    finally:
        db.close()


def update_imap_connection(connection_id: str, **kwargs):
    """Update IMAP connection"""
    db = DatabaseManager.get_session()
    try:
        conn = db.query(ImapConnection).filter(ImapConnection.id == connection_id).first()
        if conn:
            for key, value in kwargs.items():
                if hasattr(conn, key):
                    setattr(conn, key, value)
            conn.last_used = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def deactivate_imap_connections(mailbox_id: str):
    """Deactivate all IMAP connections for a mailbox"""
    db = DatabaseManager.get_session()
    try:
        db.query(ImapConnection).filter(
            ImapConnection.mailbox_id == mailbox_id
        ).update({"is_active": False})
        db.commit()
    finally:
        db.close()


# ==================== UTILITY FUNCTIONS ====================

def get_mailbox_stats(mailbox_id: str) -> Dict[str, Any]:
    """Get mailbox statistics"""
    db = DatabaseManager.get_session()
    try:
        total_emails = db.query(Email).filter(Email.mailbox_id == mailbox_id).count()
        unread_emails = db.query(Email).filter(
            and_(
                Email.mailbox_id == mailbox_id,
                Email.is_read == False
            )
        ).count()
        folders = db.query(Folder).filter(Folder.mailbox_id == mailbox_id).count()
        sync_state = get_sync_state(mailbox_id)
        
        return {
            "total_emails": total_emails,
            "unread_emails": unread_emails,
            "folders": folders,
            "last_sync": sync_state.last_sync_at if sync_state else None,
            "sync_status": sync_state.sync_status if sync_state else "unknown",
            "last_uid": sync_state.last_uid if sync_state else 0
        }
    finally:
        db.close()


def get_or_create_inbox_folder(mailbox_id: str) -> Folder:
    """Get or create INBOX folder for a mailbox"""
    return get_or_create_folder_by_path(mailbox_id, "INBOX")


def get_or_create_folder_by_path(mailbox_id: str, full_path: str) -> Folder:
    """Get or create a folder record by IMAP path."""
    db = DatabaseManager.get_session()
    try:
        folder = db.query(Folder).filter(
            and_(
                Folder.mailbox_id == mailbox_id,
                Folder.full_path == full_path
            )
        ).first()

        if not folder:
            folder = Folder(
                mailbox_id=mailbox_id,
                name=full_path.split("/")[-1] or full_path,
                full_path=full_path,
                total_emails=0,
                is_selectable=True
            )
            db.add(folder)
            db.commit()
            db.refresh(folder)

        return folder
    finally:
        db.close()


def get_total_stats() -> Dict[str, Any]:
    """Get total statistics across all mailboxes"""
    db = DatabaseManager.get_session()
    try:
        return {
            "total_mailboxes": db.query(Mailbox).count(),
            "total_folders": db.query(Folder).count(),
            "total_emails": db.query(Email).count(),
            "total_attachments": db.query(Attachment).count(),
            "total_email_lists": db.query(EmailList).count(),
            "total_email_list_items": db.query(EmailListItem).count()
        }
    finally:
        db.close()


def cleanup_old_connections(max_age_hours: int = 1):
    """Clean up old inactive IMAP connections"""
    db = DatabaseManager.get_session()
    try:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        
        count = db.query(ImapConnection).filter(
            and_(
                ImapConnection.last_used < cutoff,
                ImapConnection.is_active == False
            )
        ).delete()
        db.commit()
        return count
    finally:
        db.close()