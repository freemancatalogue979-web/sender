"""
API Routes for FV-S Admin Panel
"""

from datetime import datetime, timedelta
from typing import Optional, List
from pathlib import Path
import asyncio
import json
import logging
import re as _re
import threading

logger = logging.getLogger(__name__)

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Header, UploadFile, File, Form, Body, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from config import config, FileNames, Defaults, Providers, USERS_BASE_PATH, encrypt_secret, decrypt_secret
from email_providers import send_test_email, send_test_email_result, send_email_via_provider, check_smtp_valid
from functions import (
    # Models
    UserLogin,
    UserRegister,
    UserResponse,
    CentralUserCreate,
    CentralUserUpdate,
    CentralUserLogin,
    SMTCPCredential,
    SMTCPCredentialCreate,
    SMTCPCredentialUpdate,
    APICredential,
    APICredentialCreate,
    APICredentialUpdate,
    TestResult,
    AboutInfo,
    ServerStats,
    # Auth functions
    create_access_token,
    verify_password,
    get_password_hash,
    decode_token,
    # Central User functions
    read_users_database,
    get_user_by_username,
    get_user_by_id,
    create_central_user,
    update_central_user,
    delete_central_user,
    restore_central_user,
    get_deleted_users,
    is_user_deleted,
    initialize_user_folder,
    # SMTP functions
    test_smtp_connection,
    # Email list functions
    EmailListCreate,
    EmailListUpdate,
    EmailListResponse,
    EmailListDetailResponse,
    EmailListImport,
    list_email_lists,
    get_email_list,
    create_email_list,
    update_email_list,
    delete_email_list,
    # Mailbox functions
    GoogleMailboxData,
    OutlookMailboxData,
    read_google_mailbox,
    write_google_mailbox,
    delete_google_mailbox,
    read_outlook_mailbox,
    write_outlook_mailbox,
    delete_outlook_mailbox,
    # Letters functions
    LetterCreate,
    LetterUpdate,
    list_letters,
    get_letter,
    create_letter,
    update_letter,
    delete_letter,
    # Utility functions
    read_user_data,
    write_user_data,
    delete_user_data,
    list_user_files,
    generate_user_id,
    get_user_emails_folder,
    get_user_mailbox_folder,
    get_user_letters_folder,
    generate_credential_id,
    get_system_info,
    detect_smtp_provider,
    get_smtp_defaults,
    parse_credential_line,
    success_response,
    error_response,
    require_fields,
    sanitize_filename,
    mask_credential,
    write_text_file,
    write_json_atomic,
    audit_event,
    read_audit_log,
    ensure_default_admin,
    recover_interrupted_sessions,
    build_unsubscribe_url,
    verify_unsubscribe_token,
    suppress_email,
    unsuppress_email,
    get_suppression_list,
    is_suppressed,
    check_domain_deliverability,
    # New logging functions
    log_email_send,
    get_sent_logs,
    get_sending_stats,
    log_access_event,
    get_access_logs,
    get_security_info,
    initialize_sent_log,
    get_sent_log_file,
    get_access_log_file,
    # User session management
    read_user_sessions,
    write_user_sessions,
    create_user_session,
    update_user_session_activity,
    logout_user_session,
    logout_all_other_sessions,
    get_active_user_sessions,
    # Inbox/Mailbox functions
    InboxMailboxCreate,
    InboxMailboxUpdate,
    InboxMailboxResponse,
    EmailMessageResponse,
    EmailFolderResponse,
    MailboxImportRequest,
    MailboxImportResult,
    EmailSearchQuery,
    EmailActionRequest,
    ComposeEmailRequest,
    ConnectionTestResult,
    IMAPConfig,
    SMTPConfigSimple,
    save_mailbox_config,
    get_user_mailboxes,
    get_mailbox_by_id,
    update_mailbox_config,
    delete_mailbox_config,
    set_primary_mailbox,
    get_primary_mailbox,
    fetch_emails_from_folder,
    get_email_body,
    get_email_attachment,
    get_mailbox_storage_path,
    search_emails_in_mailbox,
    sync_mailbox_folder,
    get_folder_list,
    mark_email_as_read,
    mark_email_as_unread,
    move_email_to_folder,
    delete_email_permanently,
    send_email_from_mailbox,
    import_mailboxes_from_text,
    export_mailboxes_to_json,
    test_mailbox_connections,
    detect_imap_provider,
    get_provider_defaults,
    get_imap_config_from_email,
    detect_imap_configs_batch,
    create_phone_list,
    import_phone_numbers,
    get_phone_list_numbers,
    test_imap_connection,
    test_smtp_connection_simple,
    Inbox_state,
    # NEW async IMAP functions (Step 6 - Integration)
    test_imap_connection_async,
    connect_imap_async,
    get_imap_folders_async,
    get_folder_status_async,
    fetch_emails_async,
    search_emails_async,
    search_mailbox_async,
    get_sync_state_async,
    update_sync_state_async,
    mark_email_read_async,
    # Phone list functions
    get_phone_lists,
    get_phone_list_by_id,
    delete_phone_list,
    rename_phone_list,
    # Phone validation functions
    create_phone_validation_session,
    format_phone_number,
    process_phone_validation,
    # Email validation functions (Decodo-only transport)
    create_email_validation_session,
    process_email_validation_batch,
    DEFAULT_VALIDATION_BATCH_SIZE,
    # Validator session functions
    get_validator_session,
    get_active_validator_session,
    update_validator_progress,
    save_active_session_state,
    clear_active_session
)


# =============================================================================
# ROUTERS
# =============================================================================

router = APIRouter()
smtp_router = APIRouter(prefix="/smtp", tags=["SMTP"])
api_router = APIRouter(prefix="/api-credentials", tags=["API"])
auth_router = APIRouter(prefix="/auth", tags=["Authentication"])
user_router = APIRouter(prefix="/user", tags=["User"])
inbox_router = APIRouter(prefix="/inbox", tags=["Inbox"])
ws_router = APIRouter(prefix="/ws", tags=["WebSocket"])


# =============================================================================
# WEBSOCKET ENDPOINTS
# =============================================================================

@ws_router.websocket("/inbox")
async def websocket_inbox(websocket: WebSocket, token: str = None):
    """
    WebSocket endpoint for real-time inbox updates
    
    Client sends:
    - {"type": "subscribe", "mailbox_id": "xxx"}
    - {"type": "unsubscribe", "mailbox_id": "xxx"}
    - {"type": "ping"}
    
    Server sends:
    - {"type": "connected", "user_id": "xxx"}
    - {"type": "new_email", "mailbox_id": "xxx", "email": {...}}
    - {"type": "email_deleted", "mailbox_id": "xxx", "email_uid": 123}
    - {"type": "email_moved", "mailbox_id": "xxx", ...}
    - {"type": "email_read_status", "mailbox_id": "xxx", "is_read": true}
    - {"type": "sync_complete", "mailbox_id": "xxx", "new_emails": 5}
    - {"type": "pong"}
    """
    from websocket_manager import get_websocket_manager, shutdown_websocket_manager
    
    # Extract user_id from token (simplified - in production, validate properly)
    user_id = "anonymous"
    
    if token:
        try:
            from functions import decode_token
            payload = decode_token(token)
            if payload:
                user_id = payload.get("sub", "anonymous")
        except Exception:
            pass
    
    manager = await get_websocket_manager()
    
    # Accept connection
    connected = await manager.connect(websocket, user_id, token)
    
    if not connected:
        return
    
    try:
        while True:
            # Receive message
            data = await websocket.receive_text()
            
            try:
                message = json.loads(data)
                await manager.handle_message(websocket, message)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid JSON"
                })
    
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        await manager.disconnect(websocket)


# =============================================================================
# AUTHENTICATION DEPENDENCY
# =============================================================================

async def get_current_user(authorization: str = Header(None)) -> dict:
    """Get current user from JWT token"""
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication scheme",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Verify user exists
    logs = read_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS)
    if not logs:
        raise HTTPException(
            status_code=401,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return {
        "user_id": user_id,
        "username": logs.get("username"),
        "created_at": logs.get("created_at")
    }


# =============================================================================
# SYSTEM ENDPOINTS
# =============================================================================

@router.get("/")
async def root():
    """Root endpoint - API info"""
    return {
        "name": "FV-S Admin Panel API",
        "version": "1.0.0",
        "status": "online",
        "docs_url": "/docs"
    }


@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": "now"}


@router.get("/inbox-state")
async def get_inbox_state():
    """Get inbox feature state"""
    return {"inbox_enabled": Inbox_state}


@router.get("/stats", response_model=ServerStats)
async def get_server_stats(current_user: dict = Depends(get_current_user)):
    """Get server statistics for current user"""
    user_id = current_user["user_id"]
    
    # Count credentials
    smtp_creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    api_creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
    
    return ServerStats(
        total_smtp=len(smtp_creds),
        total_api=len(api_creds),
        active_smtp=len([c for c in smtp_creds if c.get("status") == "active"]),
        active_api=len([c for c in api_creds if c.get("status") == "active"]),
        system_info=get_system_info()
    )


@router.get("/providers")
async def get_providers():
    """Get available SMTP and API providers"""
    return {
        "smtp": Providers.SMTP,
        "api": Providers.API
    }

# CORS preflight handlers for common endpoints
@router.options("/stats")
async def options_stats():
    """Handle CORS preflight for stats"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.options("/api-credentials")
async def options_api_credentials():
    """Handle CORS preflight for API credentials"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


# =============================================================================
# AUTHENTICATION ENDPOINTS
# =============================================================================

# CORS preflight handlers
@router.options("/auth/register")
async def options_register():
    """Handle CORS preflight for registration"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.options("/auth/login")
async def options_login():
    """Handle CORS preflight for login"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.options("/auth/verify")
async def options_verify():
    """Handle CORS preflight for verify"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.post("/auth/register", response_model=UserResponse)
async def register(user_data: UserRegister):
    """
    Register a new user or return existing user
    
    If user_id is provided, checks if user exists
    If not, generates new user_id and creates user folder with default credentials
    """
    user_id = user_data.user_id
    
    # Check if user already exists
    if user_id:
        logs = read_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS)
        if logs:
            about = read_user_data(user_id, USERS_BASE_PATH, FileNames.ABOUT_JSON, {})
            return UserResponse(
                user_id=user_id,
                username=logs.get("username"),
                created_at=logs.get("created_at"),
                about=about
            )
    
    # Generate new user ID if not provided
    if not user_id:
        user_id = generate_user_id()
    
    # Get username and password
    username = user_data.username or Defaults.USERNAME
    password = user_data.password or Defaults.PASSWORD
    
    # Create user data
    from datetime import datetime
    logs_data = {
        "username": username,
        "password_hash": get_password_hash(password),
        "created_at": datetime.now().isoformat(),
        "last_login": None,
        "login_count": 0
    }
    
    # Save logs.json
    write_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS, logs_data)
    
    # Create empty credential files
    write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    write_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
    
    # Create empty arrays for email lists, from names, and subjects
    write_user_data(user_id, USERS_BASE_PATH, FileNames.FROM_NAMES, [])
    write_user_data(user_id, USERS_BASE_PATH, FileNames.SUBJECTS, [])
    
    # Create emails directory for email list files
    emails_folder = get_user_emails_folder(user_id, USERS_BASE_PATH)
    emails_folder.mkdir(parents=True, exist_ok=True)
    
    # Create mailbox directory for Google and Outlook configurations
    mailbox_folder = get_user_mailbox_folder(user_id, USERS_BASE_PATH)
    mailbox_folder.mkdir(parents=True, exist_ok=True)
    
    # Create letters directory for HTML and plain text letters
    letters_folder = get_user_letters_folder(user_id, USERS_BASE_PATH)
    letters_folder.mkdir(parents=True, exist_ok=True)
    
    # Create about.txt and about.json
    system_info = get_system_info()
    system_info.update({
        "user_id": user_id,
        "user_agent": user_data.user_agent or "unknown",
        "registered_at": datetime.now().isoformat()
    })
    
    about_content = f"""FV-S User Information
====================
User ID: {user_id}
Registered: {datetime.now().isoformat()}
User Agent: {user_data.user_agent or 'unknown'}

System Information:
-------------------
Hostname: {system_info['hostname']}
IP Address: {system_info['ip_address']}
Platform: {system_info['platform']} {system_info['platform_release']}
Architecture: {system_info['architecture']}
Python: {system_info['python_version']}
"""
    
    from functions import write_text_file
    write_text_file(
        USERS_BASE_PATH / f"fv-s-{user_id}" / FileNames.ABOUT_TXT,
        about_content
    )
    write_user_data(user_id, USERS_BASE_PATH, FileNames.ABOUT_JSON, system_info)
    
    return UserResponse(
        user_id=user_id,
        username=username,
        created_at=logs_data["created_at"],
        about=system_info
    )


@router.post("/auth/login")
async def login(credentials: UserLogin):
    """Authenticate user and return JWT token"""
    user_id = credentials.user_id
    
    # Get user logs
    logs = read_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS)
    
    if not logs:
        raise HTTPException(status_code=404, detail="User not found. Please register first.")
    
    # Verify password
    if not verify_password(credentials.password, logs.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid password")
    
    # Update last login
    from datetime import datetime
    logs["last_login"] = datetime.now().isoformat()
    logs["login_count"] = logs.get("login_count", 0) + 1
    write_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS, logs)
    
    # Create access token
    access_token = create_access_token(
        data={"sub": user_id},
        expires_delta=timedelta(minutes=config.access_token_expire_minutes)
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user_id,
        "username": logs.get("username")
    }


@router.post("/auth/verify")
async def verify_token(current_user: dict = Depends(get_current_user)):
    """Verify if the current token is valid"""
    return {
        "valid": True,
        "user_id": current_user["user_id"],
        "username": current_user["username"]
    }


# =============================================================================
# CENTRAL USER MANAGEMENT ENDPOINTS
# =============================================================================

# CORS preflight handlers for central users
@router.options("/users")
async def options_users():
    """Handle CORS preflight for users"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.options("/users/{user_id}")
async def options_user_single(user_id: str):
    """Handle CORS preflight for single user operations"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, PUT, DELETE, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.options("/users/deleted")
async def options_users_deleted():
    """Handle CORS preflight for deleted users"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.options("/users/login")
async def options_user_login():
    """Handle CORS preflight for user login"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/users")
async def list_all_users(current_user: dict = Depends(get_current_user)):
    """List all active (non-deleted) users in the central database (without passwords)"""
    users = read_users_database(USERS_BASE_PATH)
    
    # Return only active users (not deleted) without password hashes
    safe_users = []
    for user in users:
        if not user.get('deleted', False):
            safe_user = {
                'user_id': user.get('user_id'),
                'username': user.get('username'),
                'created_at': user.get('created_at'),
                'last_login': user.get('last_login'),
                'login_count': user.get('login_count', 0)
            }
            safe_users.append(safe_user)
    
    return safe_users


@router.get("/users/deleted")
async def list_deleted_users(current_user: dict = Depends(get_current_user)):
    """Get all soft-deleted users"""
    print(f"[DEBUG] /users/deleted called - USERS_BASE_PATH: {USERS_BASE_PATH}")
    deleted = get_deleted_users(USERS_BASE_PATH)
    print(f"[DEBUG] Found {len(deleted)} deleted users")
    return deleted


@router.post("/users")
async def create_new_user(user_data: CentralUserCreate, current_user: dict = Depends(get_current_user)):
    """Create a new user in the central database"""
    # Check if username already exists
    existing = get_user_by_username(USERS_BASE_PATH, user_data.username)
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    
    result = create_central_user(USERS_BASE_PATH, user_data.username, user_data.password)
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create user")

    audit_event(current_user["user_id"], USERS_BASE_PATH, "user_created", {"user_id": result.get("user_id"), "username": user_data.username})
    return result


@router.post("/users/initialize-folders")
async def initialize_all_user_folders(current_user: dict = Depends(get_current_user)):
    """Initialize user folders with all required files (for existing users)"""
    # Get all users from the database
    users = read_users_database(USERS_BASE_PATH)
    
    initialized_count = 0
    errors = []
    
    for user in users:
        user_id = user.get('user_id')
        if user_id:
            try:
                initialize_user_folder(USERS_BASE_PATH, user_id)
                initialized_count += 1
            except Exception as e:
                errors.append(f"User {user_id}: {str(e)}")
    
    return {
        "message": "User folders initialized successfully",
        "initialized": initialized_count,
        "total_users": len(users),
        "errors": errors if errors else None
    }


@router.get("/users/{user_id}")
async def get_single_user(user_id: str, current_user: dict = Depends(get_current_user)):
    """Get a single user by ID"""
    # Block "deleted" - it should be handled by /users/deleted
    if user_id == "deleted":
        raise HTTPException(status_code=404, detail="User not found")
    
    user = get_user_by_id(USERS_BASE_PATH, user_id)
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Return user without password hash
    return {
        'user_id': user.get('user_id'),
        'username': user.get('username'),
        'created_at': user.get('created_at'),
        'last_login': user.get('last_login'),
        'login_count': user.get('login_count', 0)
    }


@router.put("/users/{user_id}")
async def update_existing_user(user_id: str, updates: CentralUserUpdate, current_user: dict = Depends(get_current_user)):
    """Update a user"""
    # Block "deleted" - it should be handled by /users/deleted
    if user_id == "deleted":
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if user exists
    existing = get_user_by_id(USERS_BASE_PATH, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Prepare update data
    update_data = {}
    if updates.username:
        update_data['username'] = updates.username
    if updates.password:
        update_data['password'] = updates.password
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No updates provided")
    
    result = update_central_user(USERS_BASE_PATH, user_id, update_data)
    
    if not result:
        raise HTTPException(status_code=400, detail="Username already taken or update failed")
    
    return result


@router.delete("/users/{user_id}")
async def delete_existing_user(user_id: str, current_user: dict = Depends(get_current_user)):
    """Soft delete a user - marks as deleted but keeps folder"""
    # Block "deleted" - it should be handled by /users/deleted
    if user_id == "deleted":
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if user exists
    existing = get_user_by_id(USERS_BASE_PATH, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    
    success = delete_central_user(USERS_BASE_PATH, user_id)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete user")

    audit_event(current_user["user_id"], USERS_BASE_PATH, "user_deleted", {"user_id": user_id})
    return {"message": "User deleted successfully. Use the Restore tab to recover.", "user_id": user_id}


@router.post("/users/{user_id}/restore")
async def restore_existing_user(user_id: str, current_user: dict = Depends(get_current_user)):
    """Restore a soft-deleted user"""
    # Block "deleted" - it should be handled by /users/deleted
    if user_id == "deleted":
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if user exists
    existing = get_user_by_id(USERS_BASE_PATH, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    
    if not existing.get('deleted', False):
        raise HTTPException(status_code=400, detail="User is not deleted")
    
    result = restore_central_user(USERS_BASE_PATH, user_id)
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to restore user")
    
    return {"message": "User restored successfully", "user": result}


@router.post("/users/login")
async def login_central_user(credentials: CentralUserLogin, request: Request):
    """Authenticate a user from the central database and return JWT token"""
    # Find user by username
    user = get_user_by_username(USERS_BASE_PATH, credentials.username)
    
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    # Check if user is deleted
    if user.get('deleted', False):
        raise HTTPException(status_code=403, detail="This account has been deactivated. Contact admin to restore.")
    
    # Verify password
    if not verify_password(credentials.password, user.get('password_hash', '')):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    user_id = user.get('user_id')
    
    # Update last login
    from datetime import datetime
    user['last_login'] = datetime.now().isoformat()
    user['login_count'] = user.get('login_count', 0) + 1
    
    # Save updated login info to central database
    users = read_users_database(USERS_BASE_PATH)
    for i, u in enumerate(users):
        if u.get('user_id') == user_id:
            users[i] = user
            break
    
    from functions import write_json_file
    users_file = USERS_BASE_PATH / "users.json"
    write_json_file(users_file, {'users': users})
    
    # Also update the user's logs.json file
    user_folder = USERS_BASE_PATH / f"fv-s-{user_id}"
    logs_file = user_folder / "logs.json"
    
    if logs_file.exists():
        try:
            with open(logs_file, 'r', encoding='utf-8') as f:
                logs_data = json.load(f)
        except Exception:
            logs_data = {}
    else:
        logs_data = {}
    
    logs_data['last_login'] = user['last_login']
    logs_data['login_count'] = user['login_count']
    
    # Get real IP address from request and update user's IP if not already set
    client_ip = request.client.host if request.client else "Unknown"
    
    # If user doesn't have an IP address recorded, save this one
    if not user.get('ip_address'):
        user['ip_address'] = client_ip
        # Also update in the users list
        for i, u in enumerate(users):
            if u.get('user_id') == user_id:
                users[i] = user
                break
        # Save the updated IP to users.json
        write_json_file(users_file, {'users': users})
    
    write_json_file(logs_file, logs_data)
    
    # Create login session for tracking
    try:
        from functions import create_user_session
        import secrets
        session_token = secrets.token_hex(16)
        user_agent_str = "FV-S Admin Panel"
        device_info_str = "Admin Panel Web"
        session = create_user_session(
            user_id,
            USERS_BASE_PATH,
            session_token,
            client_ip,
            user_agent_str,
            device_info_str
        )
        current_session_id = session.get('session_id') if session else None
    except Exception as e:
        print(f"Error creating login session: {e}")
        current_session_id = None
    
    # Create access token
    access_token = create_access_token(
        data={"sub": user_id},
        expires_delta=timedelta(minutes=config.access_token_expire_minutes)
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user_id,
        "username": user.get('username'),
        "session_id": current_session_id
    }


# =============================================================================
# USER INFO ENDPOINTS
# =============================================================================

@router.get("/about", response_model=AboutInfo)
async def get_about(current_user: dict = Depends(get_current_user)):
    """Get user about information"""
    user_id = current_user["user_id"]
    about = read_user_data(user_id, USERS_BASE_PATH, FileNames.ABOUT_JSON, {})
    
    return AboutInfo(
        user_id=user_id,
        hostname=about.get("hostname"),
        ip_address=about.get("ip_address"),
        platform=about.get("platform"),
        platform_release=about.get("platform_release"),
        user_agent=about.get("user_agent"),
        registered_at=about.get("registered_at")
    )


@router.put("/about", response_model=AboutInfo)
async def update_about(
    about_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Update user about information"""
    user_id = current_user["user_id"]
    
    # Read existing about
    about = read_user_data(user_id, USERS_BASE_PATH, FileNames.ABOUT_JSON, {})
    
    # Update with new data
    from datetime import datetime
    about.update(about_data)
    about["updated_at"] = datetime.now().isoformat()
    
    # Save to both JSON and TXT
    write_user_data(user_id, USERS_BASE_PATH, FileNames.ABOUT_JSON, about)
    
    # Update about.txt
    system_info = get_system_info()
    about_content = f"""FV-S User Information
====================
User ID: {user_id}
Updated: {datetime.now().isoformat()}
User Agent: {about.get('user_agent', 'unknown')}

System Information:
-------------------
Hostname: {about.get('hostname', 'N/A')}
IP Address: {about.get('ip_address', 'N/A')}
Platform: {about.get('platform', 'N/A')} {about.get('platform_release', '')}
Architecture: {system_info['architecture']}
Python: {system_info['python_version']}
"""
    
    from functions import write_text_file
    write_text_file(
        USERS_BASE_PATH / f"fv-s-{user_id}" / FileNames.ABOUT_TXT,
        about_content
    )
    
    return AboutInfo(
        user_id=user_id,
        hostname=about.get("hostname"),
        ip_address=about.get("ip_address"),
        platform=about.get("platform"),
        platform_release=about.get("platform_release"),
        user_agent=about.get("user_agent"),
        registered_at=about.get("registered_at")
    )


@router.delete("/user")
async def delete_user(current_user: dict = Depends(get_current_user)):
    """Delete the current user and all their data"""
    import shutil
    user_id = current_user["user_id"]
    folder_path = USERS_BASE_PATH / f"fv-s-{user_id}"
    
    if folder_path.exists():
        shutil.rmtree(folder_path)
    
    return {"message": "User deleted successfully"}


@router.get("/user/files")
async def list_files(current_user: dict = Depends(get_current_user)):
    """List all files for the current user"""
    user_id = current_user["user_id"]
    return {"files": list_user_files(user_id, USERS_BASE_PATH)}


# =============================================================================
# SMTP CREDENTIALS ENDPOINTS
# =============================================================================

# CORS preflight handlers for SMTP
@router.options("/smtp")
async def options_smtp():
    """Handle CORS preflight for SMTP credentials"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.options("/api/test-credential")
async def options_test_credential():
    """Handle CORS preflight for test-credential"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.options("/smtp/{credential_id}/test")
async def options_smtp_test(credential_id: str):
    """Handle CORS preflight for SMTP credential testing"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.options("/smtp/{credential_id}")
async def options_smtp_single(credential_id: str):
    """Handle CORS preflight for SMTP single credential operations"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.get("/smtp", response_model=List[SMTCPCredential])
async def get_smtp_credentials(current_user: dict = Depends(get_current_user)):
    """Get all SMTP credentials for the current user (passwords masked)."""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    
    # Never return plaintext passwords to the client.
    return _mask_smtp_list(credentials)


@router.post("/smtp", response_model=SMTCPCredential)
async def create_smtp_credential(
    credential: SMTCPCredentialCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new SMTP credential"""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    
    # Check for duplicate
    for existing in credentials:
        if existing.get("host") == credential.host and existing.get("port") == credential.port:
            if existing.get("user") == credential.user:
                raise HTTPException(status_code=400, detail="SMTP credential already exists")
    
    # Create new credential (password encrypted at rest)
    from datetime import datetime
    new_credential = {
        "id": generate_credential_id(user_id, "smtp"),
        "type": "smtp",
        "provider": credential.provider or detect_smtp_provider(credential.host),
        "host": credential.host,
        "port": credential.port,
        "user": credential.user,
        "password": encrypt_secret(credential.password or ""),
        "from_address": credential.from_address or credential.user,
        "success_rate": 0,
        "status": "active",
        "created_at": datetime.now().isoformat()
    }
    
    credentials.append(new_credential)
    write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, credentials)
    audit_event(user_id, USERS_BASE_PATH, "smtp_credential_created", {"id": new_credential["id"], "host": new_credential["host"], "user": new_credential["user"]})
    
    # Return without the plaintext password
    result = new_credential.copy()
    result["password"] = mask_credential(credential.password) if credential.password else None
    return result


@router.put("/smtp/{credential_id}", response_model=SMTCPCredential)
async def update_smtp_credential(
    credential_id: str,
    credential: SMTCPCredentialUpdate,
    current_user: dict = Depends(get_current_user)
):
    """Update an SMTP credential"""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    
    # Find and update credential
    found = False
    from datetime import datetime
    
    for i, existing in enumerate(credentials):
        if existing.get("id") == credential_id:
            # Update fields
            if credential.provider is not None:
                existing["provider"] = credential.provider
            if credential.host is not None:
                existing["host"] = credential.host
            if credential.port is not None:
                existing["port"] = credential.port
            if credential.user is not None:
                existing["user"] = credential.user
            if credential.password is not None and credential.password:
                existing["password"] = encrypt_secret(credential.password)
            if credential.from_address is not None:
                existing["from_address"] = credential.from_address
            if credential.status is not None:
                existing["status"] = credential.status
            
            existing["updated_at"] = datetime.now().isoformat()
            credentials[i] = existing
            found = True
            
            write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, credentials)
            
            # Return without password
            result = existing.copy()
            result["password"] = mask_credential(result["password"]) if result.get("password") else None
            
            return result
    
    if not found:
        raise HTTPException(status_code=404, detail="Credential not found")


@router.delete("/smtp/{credential_id}")
async def delete_smtp_credential(
    credential_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete an SMTP credential"""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    
    # Find and remove credential
    initial_length = len(credentials)
    credentials = [c for c in credentials if c.get("id") != credential_id]
    
    if len(credentials) == initial_length:
        raise HTTPException(status_code=404, detail="Credential not found")
    
    write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, credentials)
    audit_event(user_id, USERS_BASE_PATH, "smtp_credential_deleted", {"id": credential_id})
    
    return {"message": "Credential deleted successfully", "id": credential_id}


@router.post("/smtp/cleanup-inactive")
async def cleanup_inactive_smtp(current_user: dict = Depends(get_current_user)):
    """Remove SMTP credentials marked as inactive for the current user"""
    user_id = current_user["user_id"]

    with _smtp_storage_lock:
        credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])

        inactive = [c for c in credentials if str(c.get('status') or '').lower() in ('inactive', 'disabled', 'deleted')]
        kept = [c for c in credentials if c not in inactive]

        if len(inactive) == 0:
            return {"removed": 0, "removed_ids": []}

        write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, kept)

    return {"removed": len(inactive), "removed_ids": [c.get('id') for c in inactive]}


# Serializes read-modify-write access to a user's smtp.json so concurrent
# SMTP test requests cannot clobber each other's deletes/status updates.
_smtp_storage_lock = threading.Lock()

# Same for api.json (API credential deletes/status updates during sending/tests).
_api_storage_lock = threading.Lock()


def _decrypt_smtp_creds(creds):
    """Return copies of SMTP credentials with plaintext passwords (for use)."""
    out = []
    for c in creds or []:
        c2 = dict(c)
        if c2.get("password"):
            c2["password"] = decrypt_secret(c2["password"])
        out.append(c2)
    return out


def _decrypt_api_creds(creds):
    """Return copies of API credentials with plaintext keys (for use)."""
    out = []
    for c in creds or []:
        c2 = dict(c)
        if c2.get("api_key"):
            c2["api_key"] = decrypt_secret(c2["api_key"])
        if c2.get("api_secret"):
            c2["api_secret"] = decrypt_secret(c2["api_secret"])
        out.append(c2)
    return out


def _mask_smtp_list(creds):
    """Mask passwords in SMTP credentials for API responses."""
    out = []
    for c in creds or []:
        c2 = dict(c)
        if c2.get("password"):
            c2["password"] = mask_credential(c2["password"])
        out.append(c2)
    return out


def _mask_api_list(creds):
    """Mask keys/secrets in API credentials for API responses."""
    out = []
    for c in creds or []:
        c2 = dict(c)
        if c2.get("api_key"):
            c2["api_key"] = mask_credential(c2["api_key"])
        if c2.get("api_secret"):
            c2["api_secret"] = mask_credential(c2["api_secret"])
        out.append(c2)
    return out


def _run_smtp_test_sync(host, port, username, password, from_address, test_email, timeout):
    """Run the SMTP connection test synchronously in a worker thread.

    ``test_smtp_connection`` is declared ``async`` but performs only blocking
    socket I/O (no ``await``), so awaiting it directly would stall the event
    loop and serialize concurrent requests. Running it through
    ``asyncio.to_thread`` lets multiple tests actually run in parallel.
    """
    return asyncio.run(test_smtp_connection(
        host=host,
        port=port,
        username=username,
        password=password,
        from_address=from_address,
        test_email=test_email,
        timeout=timeout,
    ))


@router.post("/smtp/{credential_id}/test", response_model=TestResult)
async def test_smtp_credential(
    credential_id: str,
    test_data: dict = Body(default={"test_email": "test@example.com"}),
    current_user: dict = Depends(get_current_user)
):
    """Test a single SMTP credential"""
    user_id = current_user["user_id"]
    test_email = test_data.get("test_email", "test@example.com")
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    
    # Find credential
    credential = None
    for c in credentials:
        if c.get("id") == credential_id:
            credential = c
            break
    
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    
    # Test the connection (in a worker thread so parallel requests don't block)
    result = await asyncio.to_thread(
        _run_smtp_test_sync,
        host=credential.get("host"),
        port=int(credential.get("port", 587)),
        username=credential.get("user"),
        password=decrypt_secret(credential.get("password", "")),
        from_address=credential.get("from_address") or credential.get("user"),
        test_email=test_email,
        timeout=config.smtp_timeout,
    )
    
    # Update credential status — remove immediately if test fails. Re-read the
    # latest list under the lock so concurrent test requests don't overwrite
    # each other's deletions.
    with _smtp_storage_lock:
        credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
        if result.success:
            for c in credentials:
                if c.get("id") == credential_id:
                    c["status"] = "active"
                    c["success_rate"] = 100
                    c["last_tested"] = datetime.now().isoformat()
                    break
        else:
            credentials = [c for c in credentials if c.get("id") != credential_id]
        write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, credentials)
    
    # Return result with CORS headers
    return JSONResponse(
        content={
            "success": result.success,
            "credential_id": credential_id,
            "deleted": not result.success,
            "message": result.message,
            "error": result.error,
            "response_time": result.response_time
        },
        headers={
            "Access-Control-Allow-Origin": "*",
        }
    )


@router.post("/smtp/test-all")
async def test_all_smtp_credentials(
    test_email: str = "test@example.com",
    batch_size: int = 5,
    delay: int = 2,
    timeout: int = 20,
    current_user: dict = Depends(get_current_user)
):
    """
    Test all SMTP credentials concurrently in batches.

    Credentials are tested `batch_size` at a time (concurrently). Any
    credential that fails the test is removed from server storage, so only
    working credentials remain after the run.
    """
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])

    if not credentials:
        return {
            "total": 0,
            "active": 0,
            "inactive": 0,
            "removed": 0,
            "results": [],
        }

    # Clamp batch size to a sane range
    batch_size = max(1, min(int(batch_size), 50))

    from datetime import datetime

    results = []
    kept_credentials = []

    async def test_one(cred):
        result = await asyncio.to_thread(
            _run_smtp_test_sync,
            host=cred.get("host"),
            port=int(cred.get("port", 587)),
            username=cred.get("user"),
            password=decrypt_secret(cred.get("password", "")),
            from_address=cred.get("from_address") or cred.get("user"),
            test_email=test_email,
            timeout=timeout,
        )
        return cred, result

    # Process in batches of `batch_size` concurrent tests, pausing `delay`
    # seconds between batches to throttle the sending rate.
    for start in range(0, len(credentials), batch_size):
        batch = credentials[start:start + batch_size]
        batch_outcomes = await asyncio.gather(*(test_one(cred) for cred in batch))

        for cred, result in batch_outcomes:
            if result.success:
                cred["status"] = "active"
                cred["success_rate"] = 100
                kept_credentials.append(cred)
            else:
                cred["status"] = "inactive"
                cred["success_rate"] = 0
            cred["last_tested"] = datetime.now().isoformat()

            results.append({
                "id": cred.get("id"),
                "host": cred.get("host"),
                "port": cred.get("port"),
                "success": result.success,
                "message": result.message,
                "response_time": result.response_time,
            })

        # Persist after each batch so failed credentials disappear from the
        # server side even if the run is interrupted mid-way.
        with _smtp_storage_lock:
            write_user_data(
                user_id,
                USERS_BASE_PATH,
                FileNames.SMTP,
                kept_credentials + credentials[start + batch_size:],
            )

        if start + batch_size < len(credentials) and delay > 0:
            await asyncio.sleep(delay)

    active_count = sum(1 for r in results if r["success"])
    inactive_count = len(results) - active_count

    return {
        "total": len(credentials),
        "active": active_count,
        "inactive": inactive_count,
        "removed": inactive_count,
        "results": results,
    }


@router.post("/api/test-credential", response_model=TestResult)
async def test_credential_direct(
    test_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Test SMTP credentials directly with provided data
    Used by the frontend batch test modal
    """
    credential_type = test_data.get("type", "smtp")
    test_email = test_data.get("test_email", "test@example.com")
    timeout = test_data.get("timeout", 20)
    
    if credential_type == "smtp":
        host = test_data.get("host")
        port = int(test_data.get("port", 587))
        username = test_data.get("user")
        password = test_data.get("password", "")
        from_address = test_data.get("from_address") or username
        
        if not host or not username:
            raise HTTPException(status_code=400, detail="Host and username are required")
        
        result = await test_smtp_connection(
            host=host,
            port=port,
            username=username,
            password=password,
            from_address=from_address,
            test_email=test_email,
            timeout=timeout
        )
        
        return result
    else:
        raise HTTPException(status_code=400, detail="API credential testing not yet implemented")


@router.post("/smtp/import")
async def import_smtp_credentials(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """
    Import SMTP credentials from a file
    
    Supports pipe-delimited (|), colon-delimited (:), and comma-delimited (,) formats
    """
    user_id = current_user["user_id"]
    content = await file.read()
    
    try:
        content = content.decode('utf-8')
    except UnicodeDecodeError:
        content = content.decode('latin-1')
    
    lines = content.strip().split('\n')
    
    imported = 0
    skipped = 0
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    from datetime import datetime
    
    for line in lines:
        parsed = parse_credential_line(line)
        
        if parsed:
            new_credential = {
                "id": generate_credential_id(user_id, "smtp"),
                "type": "smtp",
                "provider": parsed.get("provider", "custom"),
                "host": parsed["host"],
                "port": parsed["port"],
                "user": parsed["user"],
                "password": encrypt_secret(parsed.get("password", "") or ""),
                "from_address": parsed.get("from_address", parsed["user"]),
                "success_rate": 0,
                "status": "active",
                "created_at": datetime.now().isoformat()
            }
            
            credentials.append(new_credential)
            imported += 1
        else:
            skipped += 1
    
    write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, credentials)
    
    return {
        "imported": imported,
        "skipped": skipped,
        "total_credentials": len(credentials)
    }


# =============================================================================
# API CREDENTIALS ENDPOINTS
# =============================================================================

@router.get("/api-credentials", response_model=List[APICredential])
async def get_api_credentials(current_user: dict = Depends(get_current_user)):
    """Get all API credentials for the current user (keys masked)."""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
    
    # Mask API keys in response (never return plaintext credentials)
    return _mask_api_list(credentials)


@router.post("/api-credentials", response_model=APICredential)
async def create_api_credential(
    credential: APICredentialCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new API credential"""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
    
    # Check for duplicate
    for existing in credentials:
        if existing.get("provider") == credential.provider:
            raise HTTPException(status_code=400, detail="API credential already exists")
    
    from datetime import datetime
    new_credential = {
        "id": generate_credential_id(user_id, "api"),
        "type": "api",
        "provider": credential.provider,
        "api_key": encrypt_secret(credential.api_key or ""),
        "api_secret": encrypt_secret(credential.api_secret or ""),
        "region": credential.region or "us-east-1",
        "domain": credential.domain,
        "from_name": credential.from_name,
        "sender_email": credential.sender_email,
        "success_rate": 0,
        "status": "active",
        "created_at": datetime.now().isoformat()
    }
    
    credentials.append(new_credential)
    write_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, credentials)
    audit_event(user_id, USERS_BASE_PATH, "api_credential_created", {"id": new_credential["id"], "provider": new_credential["provider"]})
    
    # Return without API key/secret (masked)
    result = new_credential.copy()
    result["api_key"] = mask_credential(credential.api_key) if credential.api_key else None
    if credential.api_secret:
        result["api_secret"] = mask_credential(credential.api_secret)
    
    return result


@router.put("/api-credentials/{credential_id}", response_model=APICredential)
async def update_api_credential(
    credential_id: str,
    credential: APICredentialUpdate,
    current_user: dict = Depends(get_current_user)
):
    """Update an API credential"""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
    
    found = False
    from datetime import datetime
    
    for i, existing in enumerate(credentials):
        if existing.get("id") == credential_id:
            if credential.api_key is not None and credential.api_key:
                existing["api_key"] = encrypt_secret(credential.api_key)
            if credential.api_secret is not None and credential.api_secret:
                existing["api_secret"] = encrypt_secret(credential.api_secret)
            if credential.region is not None:
                existing["region"] = credential.region
            if credential.domain is not None:
                existing["domain"] = credential.domain
            if credential.from_name is not None:
                existing["from_name"] = credential.from_name
            if credential.sender_email is not None:
                existing["sender_email"] = credential.sender_email
            if credential.status is not None:
                existing["status"] = credential.status
            
            existing["updated_at"] = datetime.now().isoformat()
            credentials[i] = existing
            found = True
            
            write_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, credentials)
            
            result = existing.copy()
            result["api_key"] = mask_credential(result["api_key"]) if result.get("api_key") else None
            if result.get("api_secret"):
                result["api_secret"] = mask_credential(result["api_secret"])
            
            return result
    
    if not found:
        raise HTTPException(status_code=404, detail="Credential not found")


@router.delete("/api-credentials/{credential_id}")
async def delete_api_credential(
    credential_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete an API credential"""
    user_id = current_user["user_id"]
    credentials = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
    
    initial_length = len(credentials)
    credentials = [c for c in credentials if c.get("id") != credential_id]
    
    if len(credentials) == initial_length:
        raise HTTPException(status_code=404, detail="Credential not found")
    
    write_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, credentials)
    audit_event(user_id, USERS_BASE_PATH, "api_credential_deleted", {"id": credential_id})
    
    return {"message": "Credential deleted successfully", "id": credential_id}


# =============================================================================
# MAILBOX CONFIGURATION ENDPOINTS
# =============================================================================

@router.get("/mailbox")
async def get_mailbox_config(current_user: dict = Depends(get_current_user)):
    """Get mailbox configuration"""
    user_id = current_user["user_id"]
    config = read_user_data(user_id, USERS_BASE_PATH, FileNames.MAILBOX, {})
    
    # Mask client secret
    if config.get("client_secret"):
        config["client_secret"] = mask_credential(config["client_secret"])
    
    return config


@router.post("/mailbox")
async def save_oauth_mailbox(
    config_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save OAuth mailbox configuration"""
    user_id = current_user["user_id"]
    
    # Validate required fields
    error = require_fields(config_data, ['tenant_id', 'client_id', 'client_secret'])
    if error:
        raise HTTPException(status_code=400, detail=error)
    
    # Save sender user IDs separately
    sender_user_ids = config_data.pop('sender_user_ids', [])
    
    from datetime import datetime
    config_data["created_at"] = config_data.get("created_at") or datetime.now().isoformat()
    config_data["updated_at"] = datetime.now().isoformat()
    
    write_user_data(user_id, USERS_BASE_PATH, FileNames.MAILBOX, config_data)
    write_user_data(user_id, USERS_BASE_PATH, FileNames.MAILBOX_SENDERS, sender_user_ids)
    
    return {
        "message": "Mailbox configuration saved",
        "sender_count": len(sender_user_ids)
    }


@router.post("/mailbox/detect-imap")
async def detect_imap_server(email_data: dict, current_user: dict = Depends(get_current_user)):
    """
    Auto-detect IMAP server and port from email address.
    Example: POST /mailbox/detect-imap with {"email": "user@gmail.com"}
    Returns: {"host": "imap.gmail.com", "port": 993, "found": true}
    """
    try:
        email = email_data.get("email", "").strip()
        if not email:
            raise HTTPException(status_code=400, detail="Email address required")
        
        from functions import get_imap_server_for_email
        
        host, port = get_imap_server_for_email(email)
        
        if host and port:
            return {
                "host": host,
                "port": port,
                "found": True,
                "email": email
            }
        else:
            domain = email.split('@')[1] if '@' in email else 'unknown'
            return {
                "host": None,
                "port": None,
                "found": False,
                "email": email,
                "message": f"IMAP server for {domain} not found. Enter manually."
            }
    except Exception as e:
        logger.error(f"IMAP detection error: {e}")
        raise HTTPException(status_code=500, detail="Unable to detect IMAP server")


@router.post("/mailbox/detect-and-test")
async def detect_and_test_imap_server(email_data: dict, current_user: dict = Depends(get_current_user)):
    """
    Detect IMAP host/port from an email address and test the IMAP connection.
    Example: POST /mailbox/detect-and-test with {
        "email": "user@example.com",
        "password": "secret"
    }
    """
    email = (email_data.get("email") or "").strip()
    password = email_data.get("password") or email_data.get("imap_password") or ""
    imap_host = email_data.get("imap_host")
    imap_port = int(email_data.get("imap_port") or 0)
    imap_user = email_data.get("imap_user") or email
    use_ssl = email_data.get("use_ssl", True)
    validate_cert = email_data.get("validate_cert", True)

    if not email:
        raise HTTPException(status_code=400, detail="Email address required")
    if not password:
        raise HTTPException(status_code=400, detail="IMAP password required")

    from functions import get_imap_server_for_email, test_imap_connection

    detected_host, detected_port = get_imap_server_for_email(email)
    if not imap_host:
        imap_host = detected_host
    if not imap_port:
        imap_port = detected_port or 993

    domain = email.split("@")[1] if "@" in email else ""
    if not imap_host and domain:
        imap_host = f"imap.{domain}"

    if not imap_host:
        raise HTTPException(status_code=400, detail="IMAP host could not be determined")

    imap_result = await test_imap_connection(
        host=imap_host,
        port=imap_port,
        username=imap_user,
        password=password,
        use_ssl=use_ssl,
        validate_cert=validate_cert
    )

    response = {
        "email": email,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "imap_user": imap_user,
        "host_detected": bool(detected_host),
        "detected_host": detected_host,
        "detected_port": detected_port,
        "imap_success": imap_result.get("success", False),
        "imap_message": imap_result.get("message", ""),
        "imap_response_time": imap_result.get("response_time"),
        "found": bool(detected_host)
    }

    return response


# =============================================================================
# EMAIL LISTS ENDPOINTS
# =============================================================================

# CORS preflight handlers for Email Lists
@router.options("/email-lists")
async def options_email_lists():
    """Handle CORS preflight for email lists"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.options("/email-lists/{list_id}")
async def options_email_list_single(list_id: str):
    """Handle CORS preflight for single email list operations"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

# CORS preflight handlers for From Names
@router.options("/from-names")
async def options_from_names():
    """Handle CORS preflight for from names"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

# CORS preflight handlers for Subjects
@router.options("/subjects")
async def options_subjects():
    """Handle CORS preflight for subjects"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.get("/email-lists", response_model=List[EmailListResponse])
async def get_email_lists(current_user: dict = Depends(get_current_user)):
    """Get all email lists for the current user (without full email content)"""
    user_id = current_user["user_id"]
    return list_email_lists(user_id, USERS_BASE_PATH)


@router.post("/email-lists", response_model=EmailListResponse)
async def create_new_email_list(
    email_list: EmailListCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new email list"""
    user_id = current_user["user_id"]
    
    # Create the email list
    result = create_email_list(
        user_id=user_id,
        base_path=USERS_BASE_PATH,
        name=email_list.name,
        emails=email_list.emails
    )
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create email list")
    
    # Return without emails array
    return EmailListResponse(
        id=result["id"],
        name=result["name"],
        created_at=result["created_at"],
        updated_at=result.get("updated_at"),
        total_emails=result["total_emails"],
        valid_count=result["valid_count"],
        invalid_count=result["invalid_count"]
    )


@router.post("/email-lists/import", response_model=EmailListResponse)
async def import_email_list(
    import_data: EmailListImport,
    current_user: dict = Depends(get_current_user)
):
    """Import an email list from raw text content"""
    user_id = current_user["user_id"]
    
    # Create from raw content
    result = create_email_list(
        user_id=user_id,
        base_path=USERS_BASE_PATH,
        name=import_data.name,
        emails=import_data.content.strip().split('\n')
    )
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to import email list")
    
    return EmailListResponse(
        id=result["id"],
        name=result["name"],
        created_at=result["created_at"],
        updated_at=result.get("updated_at"),
        total_emails=result["total_emails"],
        valid_count=result["valid_count"],
        invalid_count=result["invalid_count"]
    )


@router.get("/email-lists/{list_id}", response_model=EmailListDetailResponse)
async def get_single_email_list(
    list_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get a single email list with all emails"""
    user_id = current_user["user_id"]
    
    email_list = get_email_list(user_id, USERS_BASE_PATH, list_id)
    
    if not email_list:
        raise HTTPException(status_code=404, detail="Email list not found")
    
    return EmailListDetailResponse(**email_list)


@router.put("/email-lists/{list_id}", response_model=EmailListResponse)
async def update_existing_email_list(
    list_id: str,
    updates: EmailListUpdate,
    current_user: dict = Depends(get_current_user)
):
    """Update an email list"""
    user_id = current_user["user_id"]
    
    # Check if list exists
    existing = get_email_list(user_id, USERS_BASE_PATH, list_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Email list not found")
    
    # Prepare update data
    update_data = {}
    if updates.name:
        update_data['name'] = updates.name
    if updates.emails is not None:
        update_data['emails'] = updates.emails
    if updates.action:
        update_data['action'] = updates.action
    
    result = update_email_list(user_id, USERS_BASE_PATH, list_id, update_data)
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update email list")
    
    return EmailListResponse(
        id=result["id"],
        name=result["name"],
        created_at=result["created_at"],
        updated_at=result.get("updated_at"),
        total_emails=result["total_emails"],
        valid_count=result["valid_count"],
        invalid_count=result["invalid_count"]
    )


@router.delete("/email-lists/{list_id}")
async def delete_existing_email_list(
    list_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete an email list"""
    user_id = current_user["user_id"]
    
    # Check if list exists
    existing = get_email_list(user_id, USERS_BASE_PATH, list_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Email list not found")
    
    success = delete_email_list(user_id, USERS_BASE_PATH, list_id)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete email list")
    
    return {"message": "Email list deleted successfully", "id": list_id}


# =============================================================================
# FROM NAMES ENDPOINTS
# =============================================================================

@router.get("/from-names")
async def get_from_names(current_user: dict = Depends(get_current_user)):
    """Get all from names for the current user"""
    user_id = current_user["user_id"]
    data = read_user_data(user_id, USERS_BASE_PATH, FileNames.FROM_NAMES, [])
    return data


@router.put("/from-names")
async def save_from_names(
    data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save from names"""
    user_id = current_user["user_id"]
    names = data.get("names", [])
    write_user_data(user_id, USERS_BASE_PATH, FileNames.FROM_NAMES, names)
    return {"success": True, "count": len(names)}


@router.post("/from-names/import")
async def import_from_names(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """Import from names from file"""
    user_id = current_user["user_id"]
    content = await file.read()
    
    try:
        content = content.decode('utf-8')
    except UnicodeDecodeError:
        content = content.decode('latin-1')
    
    lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
    
    # Get existing and add new ones
    existing = read_user_data(user_id, USERS_BASE_PATH, FileNames.FROM_NAMES, [])
    new_names = [name for name in lines if name not in existing]
    existing.extend(new_names)
    
    write_user_data(user_id, USERS_BASE_PATH, FileNames.FROM_NAMES, existing)
    
    return {"success": True, "imported": len(new_names), "total": len(existing)}


# =============================================================================
# SUBJECTS ENDPOINTS
# =============================================================================

@router.get("/subjects")
async def get_subjects(current_user: dict = Depends(get_current_user)):
    """Get all subject templates for the current user"""
    user_id = current_user["user_id"]
    data = read_user_data(user_id, USERS_BASE_PATH, FileNames.SUBJECTS, [])
    return data


@router.put("/subjects")
async def save_subjects(
    data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save subject templates"""
    user_id = current_user["user_id"]
    subjects = data.get("subjects", [])
    write_user_data(user_id, USERS_BASE_PATH, FileNames.SUBJECTS, subjects)
    return {"success": True, "count": len(subjects)}


@router.post("/subjects/import")
async def import_subjects(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """Import subjects from file"""
    user_id = current_user["user_id"]
    content = await file.read()
    
    try:
        content = content.decode('utf-8')
    except UnicodeDecodeError:
        content = content.decode('latin-1')
    
    lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
    
    # Get existing and add new ones
    existing = read_user_data(user_id, USERS_BASE_PATH, FileNames.SUBJECTS, [])
    new_subjects = [subj for subj in lines if subj not in existing]
    existing.extend(new_subjects)
    
    write_user_data(user_id, USERS_BASE_PATH, FileNames.SUBJECTS, existing)
    
    return {"success": True, "imported": len(new_subjects), "total": len(existing)}


# =============================================================================
# MAILBOX ENDPOINTS
# =============================================================================

@router.get("/mailbox/google")
async def get_google_mailbox(current_user: dict = Depends(get_current_user)):
    """Get Google mailbox configuration"""
    user_id = current_user["user_id"]
    data = read_google_mailbox(user_id, USERS_BASE_PATH)
    
    # Return empty object if not configured, instead of 404
    if not data:
        return {}
    
    return data


@router.post("/mailbox/google")
async def save_google_mailbox(
    mailbox_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save Google mailbox configuration"""
    user_id = current_user["user_id"]
    
    # Create Pydantic model for validation
    data = GoogleMailboxData(**mailbox_data)
    
    result = write_google_mailbox(user_id, USERS_BASE_PATH, data)
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to save Google mailbox configuration")
    
    return result


@router.delete("/mailbox/google")
async def delete_google_mailbox(current_user: dict = Depends(get_current_user)):
    """Delete Google mailbox configuration"""
    user_id = current_user["user_id"]
    
    success = delete_google_mailbox(user_id, USERS_BASE_PATH)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete Google mailbox configuration")
    
    return {"message": "Google mailbox configuration deleted successfully"}


@router.options("/mailbox/google")
async def options_google_mailbox():
    """Handle CORS preflight for Google mailbox"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/mailbox/outlook")
async def get_outlook_mailbox(current_user: dict = Depends(get_current_user)):
    """Get Outlook mailbox configuration"""
    user_id = current_user["user_id"]
    data = read_outlook_mailbox(user_id, USERS_BASE_PATH)
    
    # Return empty object if not configured, instead of 404
    if not data:
        return {}
    
    return data


@router.post("/mailbox/outlook")
async def save_outlook_mailbox(
    mailbox_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save Outlook mailbox configuration"""
    user_id = current_user["user_id"]
    
    # Create Pydantic model for validation
    data = OutlookMailboxData(**mailbox_data)
    
    result = write_outlook_mailbox(user_id, USERS_BASE_PATH, data)
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to save Outlook mailbox configuration")
    
    return result


@router.delete("/mailbox/outlook")
async def delete_outlook_mailbox(current_user: dict = Depends(get_current_user)):
    """Delete Outlook mailbox configuration"""
    user_id = current_user["user_id"]
    
    success = delete_outlook_mailbox(user_id, USERS_BASE_PATH)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete Outlook mailbox configuration")
    
    return {"message": "Outlook mailbox configuration deleted successfully"}


@router.options("/mailbox/outlook")
async def options_outlook_mailbox():
    """Handle CORS preflight for Outlook mailbox"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


# =============================================================================
# MAILBOX / API SENDING TEST ENDPOINTS
# =============================================================================

def _resolve_sender_config(user_id: str, sender: dict):
    """
    Resolve a sender descriptor (from the unified `senders` array) into a
    (provider_type, config) tuple.

    Supported descriptors:
      {"type": "smtp", "id": "<smtp credential id>"}
      {"type": "api", "id": "<api credential id>"}
      {"type": "google"}
      {"type": "outlook"}
    """
    sender_type = (sender.get("type") or "").strip().lower()
    sender_id = sender.get("id")

    if sender_type == "smtp":
        creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
        cred = next((c for c in creds if c.get("id") == sender_id), None)
        if not cred:
            raise ValueError(f"SMTP credential not found: {sender_id}")
        cred = dict(cred)
        cred["password"] = decrypt_secret(cred.get("password", ""))
        return "smtp", cred

    if sender_type == "api":
        creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
        cred = next((c for c in creds if c.get("id") == sender_id), None)
        if not cred:
            raise ValueError(f"API credential not found: {sender_id}")
        cred = dict(cred)
        cred["api_key"] = decrypt_secret(cred.get("api_key", ""))
        cred["api_secret"] = decrypt_secret(cred.get("api_secret", ""))
        return cred.get("provider") or "smtp", cred

    if sender_type in ("google", "gmail"):
        config = read_google_mailbox(user_id, USERS_BASE_PATH)
        if not config:
            raise ValueError("Google mailbox is not configured")
        return "google", config

    if sender_type in ("outlook", "microsoft", "graph"):
        config = read_outlook_mailbox(user_id, USERS_BASE_PATH)
        if not config:
            raise ValueError("Outlook mailbox is not configured")
        return "outlook", config

    raise ValueError(f"Unknown sender type: {sender_type}")


def _sender_label(provider_type: str, config: dict) -> str:
    """Human-readable label for a resolved sender (used in logs and monitor)."""
    if provider_type == "smtp":
        return f"{config.get('host', 'smtp')}:{config.get('port', 587)} ({config.get('user') or config.get('from_address') or 'smtp'})"
    if provider_type in ("google", "gmail"):
        return f"Gmail API ({config.get('sender_email', 'google')})"
    if provider_type in ("outlook", "graph"):
        return f"Microsoft Graph ({config.get('sender_email', 'outlook')})"
    return f"{provider_type} ({config.get('sender_email') or config.get('domain') or config.get('region') or 'api'})"


class _SenderPool:
    """
    Thread-safe round-robin sender pool with rotation, preflight validation,
    and automatic removal of invalid (auth/connection-failed) senders.
    """

    def __init__(self, senders, rotate_after, preflight_fn, on_invalid=None):
        self._lock = threading.Lock()
        self._senders = list(senders)
        self._rotate_after = max(1, int(rotate_after or 50))
        self._cursor = 0
        self._since_rotate = 0
        self._validated = set()
        self._preflight_fn = preflight_fn
        self._on_invalid = on_invalid

    def __len__(self):
        with self._lock:
            return len(self._senders)

    def acquire(self):
        """
        Return the next sender to use (rotates after `rotate_after` uses).
        Returns None when the pool is empty.
        """
        with self._lock:
            if not self._senders:
                return None
            sender = self._senders[self._cursor % len(self._senders)]
            self._since_rotate += 1
            if self._since_rotate >= self._rotate_after:
                self._since_rotate = 0
                self._cursor = (self._cursor + 1) % len(self._senders)
            return sender

    def is_validated(self, sender):
        with self._lock:
            return sender.get("key") in self._validated

    def mark_validated(self, sender):
        with self._lock:
            self._validated.add(sender.get("key"))

    def preflight(self, sender):
        """Run the preflight check (cached per sender key). Returns bool."""
        if self.is_validated(sender):
            return True
        if self._preflight_fn is None:
            return True
        ok = bool(self._preflight_fn(sender))
        if ok:
            self.mark_validated(sender)
        return ok

    def invalidate(self, sender):
        """Remove a sender from rotation and invoke the invalid callback."""
        with self._lock:
            if sender in self._senders:
                self._senders.remove(sender)
            if self._cursor >= len(self._senders):
                self._cursor = 0
        if self._on_invalid is not None:
            try:
                self._on_invalid(sender)
            except Exception:
                pass


@router.post("/mailbox/google/test")
async def test_google_mailbox(
    test_data: dict = Body(default={"test_email": ""}),
    current_user: dict = Depends(get_current_user)
):
    """Send a test email through the configured Gmail API mailbox."""
    user_id = current_user["user_id"]
    test_email = (test_data.get("test_email") or "").strip()
    if not test_email:
        raise HTTPException(status_code=400, detail="test_email is required")

    config = read_google_mailbox(user_id, USERS_BASE_PATH)
    if not config:
        raise HTTPException(status_code=404, detail="Google mailbox is not configured")

    success, message = await asyncio.to_thread(
        send_test_email, "google", config, test_email, config.get("sender_name")
    )
    return JSONResponse(
        content={"success": success, "message": message},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.post("/mailbox/outlook/test")
async def test_outlook_mailbox(
    test_data: dict = Body(default={"test_email": ""}),
    current_user: dict = Depends(get_current_user)
):
    """Send a test email through the configured Microsoft Graph mailbox."""
    user_id = current_user["user_id"]
    test_email = (test_data.get("test_email") or "").strip()
    if not test_email:
        raise HTTPException(status_code=400, detail="test_email is required")

    config = read_outlook_mailbox(user_id, USERS_BASE_PATH)
    if not config:
        raise HTTPException(status_code=404, detail="Outlook mailbox is not configured")

    success, message = await asyncio.to_thread(
        send_test_email, "outlook", config, test_email, config.get("sender_name")
    )
    return JSONResponse(
        content={"success": success, "message": message},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.post("/api-credentials/{credential_id}/test")
async def test_api_credential(
    credential_id: str,
    test_data: dict = Body(default={"test_email": ""}),
    current_user: dict = Depends(get_current_user)
):
    """Send a test email through an API credential (SendGrid, Mailgun, SES, etc.)."""
    user_id = current_user["user_id"]
    test_email = (test_data.get("test_email") or "").strip()
    if not test_email:
        raise HTTPException(status_code=400, detail="test_email is required")

    creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
    cred = next((c for c in creds if c.get("id") == credential_id), None)
    if not cred:
        raise HTTPException(status_code=404, detail="API credential not found")

    # Decrypt for use (never persisted back in plaintext)
    use_cred = dict(cred)
    use_cred["api_key"] = decrypt_secret(cred.get("api_key", ""))
    use_cred["api_secret"] = decrypt_secret(cred.get("api_secret", ""))

    provider = cred.get("provider") or ""
    success, message, kind = await asyncio.to_thread(
        send_test_email_result, provider, use_cred, test_email, cred.get("from_name")
    )

    # Record the test outcome. Auth / connection failures remove the
    # credential (same policy as campaign sending and SMTP testing).
    with _api_storage_lock:
        creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
        if success:
            for c in creds:
                if c.get("id") == credential_id:
                    c["status"] = "active"
                    c["success_rate"] = 100
                    c["last_tested"] = datetime.now().isoformat()
                    break
        elif kind in ("auth", "connection"):
            creds = [c for c in creds if c.get("id") != credential_id]
        else:
            for c in creds:
                if c.get("id") == credential_id:
                    c["success_rate"] = 0
                    c["last_tested"] = datetime.now().isoformat()
                    break
        write_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, creds)

    return JSONResponse(
        content={"success": success, "message": message, "deleted": (not success and kind in ("auth", "connection"))},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# =============================================================================
# LETTERS ENDPOINTS
# =============================================================================

@router.get("/letters/{letter_type}")
async def get_letters(
    letter_type: str,
    current_user: dict = Depends(get_current_user)
):
    """Get all letters of a specific type (html or plain)"""
    if letter_type not in ['html', 'plain']:
        raise HTTPException(status_code=400, detail="Invalid letter type. Use 'html' or 'plain'.")
    
    user_id = current_user["user_id"]
    letters = list_letters(user_id, USERS_BASE_PATH, letter_type)
    return letters


@router.post("/letters/{letter_type}")
async def create_new_letter(
    letter_type: str,
    letter_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Create a new letter"""
    if letter_type not in ['html', 'plain']:
        raise HTTPException(status_code=400, detail="Invalid letter type. Use 'html' or 'plain'.")
    
    user_id = current_user["user_id"]
    
    result = create_letter(
        user_id=user_id,
        base_path=USERS_BASE_PATH,
        letter_type=letter_type,
        name=letter_data.get('name'),
        content=letter_data.get('content')
    )
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create letter")
    
    return result


@router.get("/letters/{letter_type}/{letter_id}")
async def get_single_letter(
    letter_type: str,
    letter_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get a single letter by ID"""
    if letter_type not in ['html', 'plain']:
        raise HTTPException(status_code=400, detail="Invalid letter type. Use 'html' or 'plain'.")
    
    user_id = current_user["user_id"]
    
    letter = get_letter(user_id, USERS_BASE_PATH, letter_type, letter_id)
    
    if not letter:
        raise HTTPException(status_code=404, detail="Letter not found")
    
    return letter


@router.put("/letters/{letter_type}/{letter_id}")
async def update_existing_letter(
    letter_type: str,
    letter_id: str,
    letter_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Update a letter"""
    if letter_type not in ['html', 'plain']:
        raise HTTPException(status_code=400, detail="Invalid letter type. Use 'html' or 'plain'.")
    
    user_id = current_user["user_id"]
    
    # Check if letter exists
    existing = get_letter(user_id, USERS_BASE_PATH, letter_type, letter_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Letter not found")
    
    result = update_letter(
        user_id=user_id,
        base_path=USERS_BASE_PATH,
        letter_type=letter_type,
        letter_id=letter_id,
        name=letter_data.get('name'),
        content=letter_data.get('content')
    )
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update letter")
    
    return result


@router.delete("/letters/{letter_type}/{letter_id}")
async def delete_existing_letter(
    letter_type: str,
    letter_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a letter"""
    if letter_type not in ['html', 'plain']:
        raise HTTPException(status_code=400, detail="Invalid letter type. Use 'html' or 'plain'.")
    
    user_id = current_user["user_id"]
    
    # Check if letter exists
    existing = get_letter(user_id, USERS_BASE_PATH, letter_type, letter_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Letter not found")
    
    success = delete_letter(user_id, USERS_BASE_PATH, letter_type, letter_id)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete letter")
    
    return {"message": "Letter deleted successfully", "id": letter_id}


@router.options("/letters/{letter_type}")
async def options_letters(letter_type: str):
    """Handle CORS preflight for letters"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


# =============================================================================
# SETTINGS ENDPOINTS
# =============================================================================

@router.get("/settings")
async def get_user_settings(current_user: dict = Depends(get_current_user)):
    """Get user settings from settings.json"""
    user_id = current_user["user_id"]
    settings = read_user_data(user_id, USERS_BASE_PATH, FileNames.SETTINGS, {})
    
    # Return default settings if none exist
    if not settings:
        settings = {
            "random_delay": 5,
            "connection_timeout": 20,
            "smtp_rotate_after": 50,
            "proxy_rotate_after": 25,
            "retry_limit": 3,
            "batch_size": 5,
            "rate_limit": 25,
            "ip_lock": True,
            "jwt_auth": True,
            "rate_limiting": True,
            "encrypt_config": False,
            "updated_at": datetime.now().isoformat()
        }
    
    return settings


@router.put("/settings")
async def save_user_settings(
    settings_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save user settings to settings.json"""
    user_id = current_user["user_id"]
    
    # Validate and sanitize settings
    settings = {
        "random_delay": max(1, min(30, settings_data.get("random_delay", 5))),
        "connection_timeout": max(5, min(120, settings_data.get("connection_timeout", 20))),
        "smtp_rotate_after": max(1, min(500, settings_data.get("smtp_rotate_after", 50))),
        "proxy_rotate_after": max(1, min(200, settings_data.get("proxy_rotate_after", 25))),
        "retry_limit": max(1, min(10, settings_data.get("retry_limit", 3))),
        "batch_size": max(1, min(50, settings_data.get("batch_size", 5))),
        "rate_limit": max(1, min(100, settings_data.get("rate_limit", 25))),
        "ip_lock": bool(settings_data.get("ip_lock", True)),
        "jwt_auth": bool(settings_data.get("jwt_auth", True)),
        "rate_limiting": bool(settings_data.get("rate_limiting", True)),
        "encrypt_config": bool(settings_data.get("encrypt_config", False)),
        "updated_at": datetime.now().isoformat()
    }
    
    success = write_user_data(user_id, USERS_BASE_PATH, FileNames.SETTINGS, settings)
    
    if success:
        return {"success": True, "message": "Settings saved successfully", "settings": settings}
    else:
        raise HTTPException(status_code=500, detail="Failed to save settings")


@router.options("/settings/change-password")
async def options_change_password():
    """Handle CORS preflight for change-password"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/settings/change-password")
async def change_user_password(
    password_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Change user password - updates users.json and logs.json"""
    user_id = current_user["user_id"]
    current_password = password_data.get("current_password")
    new_password = password_data.get("new_password")
    confirm_password = password_data.get("confirm_password")
    
    # Validate inputs
    if not current_password:
        raise HTTPException(status_code=400, detail="Current password is required")
    
    if not new_password:
        raise HTTPException(status_code=400, detail="New password is required")
    
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    
    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match")
    
    # Get current user data from CENTRAL database (users.json) for password verification
    from functions import get_user_by_id, read_users_database, write_users_database
    user = get_user_by_id(USERS_BASE_PATH, user_id)
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found in central database")
    
    # Verify current password against the central database password hash
    from functions import verify_password, get_password_hash
    if not verify_password(current_password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    
    # Generate new password hash
    new_password_hash = get_password_hash(new_password)
    
    # Update logs.json in user profile folder
    logs = read_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS, {})
    logs["password_hash"] = new_password_hash
    logs["password_changed_at"] = datetime.now().isoformat()
    logs["last_password_change"] = datetime.now().isoformat()
    write_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS, logs)
    
    # Update users.json in central database
    users = read_users_database(USERS_BASE_PATH)
    
    for i, u in enumerate(users):
        if u.get("user_id") == user_id:
            users[i]["password_hash"] = new_password_hash
            users[i]["password_changed_at"] = datetime.now().isoformat()
            break
    
    write_users_database(USERS_BASE_PATH, users)
    audit_event(user_id, USERS_BASE_PATH, "password_changed", {"user_id": user_id})
    
    return {
        "success": True,
        "message": "Password changed successfully. You will be logged out in 10 seconds.",
        "logout_required": True,
        "countdown_seconds": 10
    }


@router.options("/settings/password-history")
async def options_password_history():
    """Handle CORS preflight for password history"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/settings/password-history")
async def get_password_history(current_user: dict = Depends(get_current_user)):
    """Get password change history from access logs"""
    user_id = current_user["user_id"]
    
    # Get access logs
    logs = get_access_logs(user_id, USERS_BASE_PATH, limit=100, offset=0)
    events = logs.get("events", [])
    
    # Filter for password-related events
    password_events = [e for e in events if e.get("type") in ["password_change", "password_changed", "password_change_success"]]
    
    # Also include from logs.json if available
    logs_file_data = read_user_data(user_id, USERS_BASE_PATH, FileNames.LOGS, {})
    if logs_file_data.get("password_changed_at"):
        last_change = {
            "timestamp": logs_file_data.get("password_changed_at"),
            "type": "password_change",
            "success": True,
            "ip_address": logs_file_data.get("last_change_ip", "-")
        }
        # Add if not already in events
        if not any(e.get("timestamp") == last_change["timestamp"] for e in password_events):
            password_events.insert(0, last_change)
    
    return password_events[:20]  # Return last 20 password changes


# =============================================================================
# CAMPAIGN ENDPOINTS
# =============================================================================

@router.post("/campaign/start")
async def start_campaign(
    campaign_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Start an email campaign"""
    user_id = current_user["user_id"]
    
    # Get active credentials
    smtp_creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    active_creds = [c for c in smtp_creds if c.get("status") == "active"]
    
    if not active_creds:
        raise HTTPException(status_code=400, detail="No active SMTP credentials available")
    
    from datetime import datetime
    return {
        "campaign_id": generate_credential_id(),
        "status": "started",
        "active_credentials": len(active_creds),
        "message": "Campaign started. Emails will be sent in background."
    }


# =============================================================================
# DETAILED STATS ENDPOINTS
# =============================================================================

@router.get("/stats/detailed")
async def get_detailed_stats(current_user: dict = Depends(get_current_user)):
    """Get detailed sending statistics from sent.json"""
    user_id = current_user["user_id"]
    
    stats = get_sending_stats(user_id, USERS_BASE_PATH)
    
    return stats


@router.get("/stats/emails-per-minute")
async def get_emails_per_minute(
    timeframe: int = 60,
    current_user: dict = Depends(get_current_user)
):
    """Get emails per minute for a timeframe (in minutes)"""
    user_id = current_user["user_id"]
    
    stats = get_sending_stats(user_id, USERS_BASE_PATH)
    
    return {
        "emails_per_minute": stats.get("emails_per_minute", 0),
        "timeframe_minutes": timeframe,
        "sent_in_period": stats.get("sent_today", 0)
    }


@router.get("/stats/hourly")
async def get_hourly_stats(current_user: dict = Depends(get_current_user)):
    """Get hourly sending statistics for the last 24 hours"""
    user_id = current_user["user_id"]
    
    sent_file = get_sent_log_file(user_id, USERS_BASE_PATH)
    
    hourly_data = []
    now = datetime.now()
    
    # Initialize last 24 hours with zero counts
    for i in range(24):
        hour_start = (now - timedelta(hours=23-i)).replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)
        hourly_data.append({
            "hour": hour_start.strftime("%Y-%m-%d %H:00"),
            "sent": 0,
            "failed": 0
        })
    
    if sent_file.exists():
        try:
            with open(sent_file, 'r', encoding='utf-8') as f:
                log_data = json.load(f)
            
            sends = log_data.get("sends", [])
            
            for log in sends:
                try:
                    log_time = datetime.fromisoformat(log.get("timestamp", "").replace('Z', '+00:00'))
                    
                    # Check if in last 24 hours
                    if log_time >= now - timedelta(hours=24):
                        # Find the correct hour slot
                        hour_key = log_time.strftime("%Y-%m-%d %H:00")
                        for slot in hourly_data:
                            if slot["hour"] == hour_key:
                                if log.get("success", False):
                                    slot["sent"] += 1
                                else:
                                    slot["failed"] += 1
                                break
                except Exception:
                    continue
        except Exception:
            pass
    
    return {
        "hourly_data": hourly_data,
        "period": "last_24_hours"
    }


# =============================================================================
# SENT LOGS ENDPOINTS
# =============================================================================

@router.options("/logs/sent")
async def options_sent_logs():
    """Handle CORS preflight for sent logs"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/logs/sent")
async def get_sent_email_logs(
    limit: int = 100,
    offset: int = 0,
    success_only: bool = False,
    failed_only: bool = False,
    start_date: str = None,
    end_date: str = None,
    current_user: dict = Depends(get_current_user)
):
    """Get paginated sent email logs"""
    user_id = current_user["user_id"]
    
    # Initialize log file if not exists
    initialize_sent_log(user_id, USERS_BASE_PATH)
    
    logs = get_sent_logs(
        user_id,
        USERS_BASE_PATH,
        limit=limit,
        offset=offset,
        success_only=success_only,
        failed_only=failed_only,
        start_date=start_date,
        end_date=end_date
    )
    
    return logs


@router.get("/logs/sent/export")
async def export_sent_logs(
    format: str = "json",
    current_user: dict = Depends(get_current_user)
):
    """Export all sent logs"""
    user_id = current_user["user_id"]
    
    sent_file = get_sent_log_file(user_id, USERS_BASE_PATH)
    
    if not sent_file.exists():
        return JSONResponse(
            content={"success": False, "error": "No logs found"},
            status_code=404
        )
    
    try:
        with open(sent_file, 'r', encoding='utf-8') as f:
            log_data = json.load(f)
        
        return JSONResponse(
            content=log_data,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Disposition": f"attachment; filename=sent_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            }
        )
    except Exception as e:
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@router.delete("/logs/sent")
async def clear_sent_logs(current_user: dict = Depends(get_current_user)):
    """Clear all sent logs"""
    user_id = current_user["user_id"]
    
    sent_file = get_sent_log_file(user_id, USERS_BASE_PATH)
    
    # Re-initialize with empty data
    initialize_sent_log(user_id, USERS_BASE_PATH)
    
    return {
        "success": True,
        "message": "Sent logs cleared successfully"
    }


# =============================================================================
# ACCESS LOGS ENDPOINTS
# =============================================================================

@router.options("/logs/access")
async def options_access_logs():
    """Handle CORS preflight for access logs"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/logs/access")
async def get_access_email_logs(
    limit: int = 50,
    offset: int = 0,
    event_type: str = None,
    current_user: dict = Depends(get_current_user)
):
    """Get access/security logs"""
    user_id = current_user["user_id"]
    
    logs = get_access_logs(
        user_id,
        USERS_BASE_PATH,
        limit=limit,
        offset=offset,
        event_type=event_type
    )
    
    return logs


@router.get("/logs/access/export")
async def export_access_logs(
    current_user: dict = Depends(get_current_user)
):
    """Export all access logs"""
    user_id = current_user["user_id"]
    
    access_file = get_access_log_file(user_id, USERS_BASE_PATH)
    
    if not access_file.exists():
        return JSONResponse(
            content={"success": False, "error": "No logs found"},
            status_code=404
        )
    
    try:
        with open(access_file, 'r', encoding='utf-8') as f:
            log_data = json.load(f)
        
        return JSONResponse(
            content=log_data,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Disposition": f"attachment; filename=access_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            }
        )
    except Exception as e:
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@router.get("/logs/audit")
async def get_audit_log(
    limit: int = 200,
    current_user: dict = Depends(get_current_user)
):
    """Get the current user's audit trail (credential/user/campaign changes)."""
    user_id = current_user["user_id"]
    entries = read_audit_log(user_id, USERS_BASE_PATH, limit=limit)
    return {"events": entries, "total": len(entries)}


# =============================================================================
# UNSUBSCRIBE / SUPPRESSION ENDPOINTS
# =============================================================================

@router.get("/unsubscribe/{token}")
async def unsubscribe(token: str):
    """
    One-click unsubscribe. Adds the signed recipient to their user's
    suppression list so future campaigns skip them.
    """
    result = verify_unsubscribe_token(token)
    if not result:
        return Response(
            content="<html><body><h3>Invalid unsubscribe link</h3><p>This link is invalid or has expired.</p></body></html>",
            media_type="text/html",
            status_code=400,
        )

    user_id, email = result
    suppress_email(user_id, USERS_BASE_PATH, email)
    audit_event(user_id, USERS_BASE_PATH, "unsubscribed", {"email": email})
    return Response(
        content=f"<html><body style='font-family:sans-serif;'><h3>Unsubscribed</h3><p>{email} has been unsubscribed and will not receive further emails.</p></body></html>",
        media_type="text/html",
    )


@router.get("/suppression")
async def list_suppression(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    return {"emails": get_suppression_list(user_id, USERS_BASE_PATH)}


@router.post("/suppression")
async def add_suppression(data: dict, current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    email = (data.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    suppress_email(user_id, USERS_BASE_PATH, email)
    return {"success": True, "emails": get_suppression_list(user_id, USERS_BASE_PATH)}


@router.delete("/suppression/{email:path}")
async def remove_suppression(email: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    unsuppress_email(user_id, USERS_BASE_PATH, email)
    return {"success": True, "emails": get_suppression_list(user_id, USERS_BASE_PATH)}


# =============================================================================
# SENDER DOMAIN DELIVERABILITY (SPF / DKIM / DMARC)
# =============================================================================

@router.get("/providers/dns-check")
async def dns_check(domain: str, current_user: dict = Depends(get_current_user)):
    """Check SPF / DKIM / DMARC records for a sending domain (advisory)."""
    if not domain:
        raise HTTPException(status_code=400, detail="domain query parameter is required")
    return check_domain_deliverability(domain)


@router.delete("/logs/access")
async def clear_access_logs(current_user: dict = Depends(get_current_user)):
    """Clear all access logs"""
    user_id = current_user["user_id"]
    
    access_file = get_access_log_file(user_id, USERS_BASE_PATH)
    
    try:
        # Re-initialize with empty data
        access_data = {"user_id": user_id, "events": [], "updated_at": datetime.now().isoformat()}
        
        with open(access_file, 'w', encoding='utf-8') as f:
            json.dump(access_data, f, indent=4, ensure_ascii=False)
        
        return {
            "success": True,
            "message": "Access logs cleared successfully"
        }
    except Exception as e:
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


# =============================================================================
# SECURITY INFO ENDPOINTS
# =============================================================================

@router.options("/security")
async def options_security():
    """Handle CORS preflight for security info"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/security")
async def get_user_security_info(current_user: dict = Depends(get_current_user)):
    """Get comprehensive security information for the user"""
    user_id = current_user["user_id"]
    
    security_info = get_security_info(user_id, USERS_BASE_PATH)
    
    return security_info


@router.post("/security/log-event")
async def log_security_event(
    event_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Log a security/access event"""
    user_id = current_user["user_id"]
    
    event_type = event_data.get("type")
    details = event_data.get("details", {})
    ip_address = event_data.get("ip_address")
    user_agent = event_data.get("user_agent")
    
    if not event_type:
        raise HTTPException(status_code=400, detail="Event type is required")
    
    success = log_access_event(
        user_id,
        USERS_BASE_PATH,
        event_type,
        details,
        ip_address,
        user_agent
    )
    
    return {
        "success": success,
        "message": "Event logged successfully" if success else "Failed to log event"
    }


# =============================================================================
# USER SESSIONS (Login Sessions across devices)
# =============================================================================

@router.options("/user/sessions")
async def options_user_sessions():
    """Handle CORS preflight for user sessions"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/user/sessions")
async def get_user_sessions(current_user: dict = Depends(get_current_user)):
    """Get all login sessions for the current user"""
    user_id = current_user["user_id"]
    
    sessions_data = read_user_sessions(user_id, USERS_BASE_PATH)
    active_sessions = [s for s in sessions_data.get("sessions", []) if s.get("is_active", True)]
    
    return {
        "sessions": active_sessions,
        "total_count": len(active_sessions),
        "current_session": current_user.get("session_id", "")
    }


@router.options("/user/sessions/logout/{session_id}")
async def options_logout_session(session_id: str):
    """Handle CORS preflight for logout session"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/user/sessions/logout/{session_id}")
async def logout_session(session_id: str, current_user: dict = Depends(get_current_user)):
    """Logout a specific session"""
    user_id = current_user["user_id"]
    
    success = logout_user_session(user_id, USERS_BASE_PATH, session_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return {"success": True, "message": "Session logged out successfully"}


@router.options("/user/sessions/logout-all")
async def options_logout_all_sessions():
    """Handle CORS preflight for logout all sessions"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/user/sessions/logout-all")
async def logout_all_sessions(current_user: dict = Depends(get_current_user)):
    """Logout all other sessions except the current one"""
    user_id = current_user["user_id"]
    current_session_id = current_user.get("session_id", "")
    
    count = logout_all_other_sessions(user_id, USERS_BASE_PATH, current_session_id)
    
    return {
        "success": True, 
        "message": f"Logged out {count} other session(s)",
        "logged_out_count": count
    }


@router.post("/user/sessions/create")
async def create_new_session(
    session_token: str = Body(...),
    ip_address: str = Body(None),
    user_agent: str = Body(None),
    device_info: str = Body(None),
    current_user: dict = Depends(get_current_user)
):
    """Create a new user login session"""
    user_id = current_user["user_id"]
    
    session = create_user_session(
        user_id,
        USERS_BASE_PATH,
        session_token,
        ip_address or "unknown",
        user_agent or "unknown",
        device_info or "Unknown Device"
    )
    
    return {"success": True, "session": session}


# =============================================================================
# CAMPAIGN MANAGEMENT ENDPOINTS
# =============================================================================

@router.options("/campaigns")
async def options_campaigns():
    """Handle CORS preflight for campaigns"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/campaigns")
async def list_campaigns(current_user: dict = Depends(get_current_user)):
    """List all saved campaigns for the user"""
    user_id = current_user["user_id"]
    
    campaigns = read_user_data(user_id, USERS_BASE_PATH, "sender-config/campaigns.json", [])
    return campaigns


@router.post("/campaigns")
async def save_campaign(
    campaign_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save a campaign configuration"""
    user_id = current_user["user_id"]
    
    campaigns = read_user_data(user_id, USERS_BASE_PATH, "sender-config/campaigns.json", [])
    
    # Generate ID if not provided
    if not campaign_data.get("id"):
        campaign_data["id"] = generate_credential_id()
    
    campaign_data["created_at"] = datetime.now().isoformat()
    campaign_data["updated_at"] = datetime.now().isoformat()
    campaign_data["status"] = "draft"
    
    campaigns.append(campaign_data)
    
    # Ensure sender-config directory exists
    sender_config_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config"
    sender_config_dir.mkdir(parents=True, exist_ok=True)
    
    write_user_data(user_id, USERS_BASE_PATH, "sender-config/campaigns.json", campaigns)
    
    return {
        "success": True,
        "campaign_id": campaign_data["id"],
        "message": "Campaign saved successfully"
    }


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Get a specific campaign by ID"""
    user_id = current_user["user_id"]
    
    campaigns = read_user_data(user_id, USERS_BASE_PATH, "sender-config/campaigns.json", [])
    
    for campaign in campaigns:
        if campaign.get("id") == campaign_id:
            return campaign
    
    raise HTTPException(status_code=404, detail="Campaign not found")


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a campaign"""
    user_id = current_user["user_id"]
    
    campaigns = read_user_data(user_id, USERS_BASE_PATH, "sender-config/campaigns.json", [])
    campaigns = [c for c in campaigns if c.get("id") != campaign_id]
    
    write_user_data(user_id, USERS_BASE_PATH, "sender-config/campaigns.json", campaigns)
    
    return {"success": True, "message": "Campaign deleted"}


# =============================================================================
# SENDING ENGINE ENDPOINTS
# =============================================================================

@router.options("/send/start")
async def options_send_start():
    """Handle CORS preflight for send start"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/start")
async def start_sending(
    send_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Start sending emails for a campaign"""
    user_id = current_user["user_id"]
    
    # Resolve senders. The frontend sends a unified `senders` array, e.g.:
    #   [{"type":"smtp","id":"..."}, {"type":"api","id":"..."},
    #    {"type":"google"}, {"type":"outlook"}]
    # A special `{"type":"any_smtp"}` (or `use_any_smtp: true`) resolves to
    # ALL currently-active SMTP credentials so the campaign can auto-rotate
    # through every valid SMTP in the list.
    # For backward compatibility we fall back to `smtp_config_ids`.
    senders_raw = list(send_data.get("senders") or [])
    if not senders_raw:
        for smtp_id in (send_data.get("smtp_config_ids") or []):
            senders_raw.append({"type": "smtp", "id": smtp_id})

    use_any_smtp = bool(send_data.get("use_any_smtp")) or any(
        (s.get("type") == "any_smtp") for s in senders_raw
    )

    senders = []
    sender_errors = []

    if use_any_smtp:
        # Auto-select every active SMTP credential (password decrypted for use).
        smtp_creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
        for cred in smtp_creds:
            if str(cred.get("status", "active")).lower() != "active":
                continue
            if not cred.get("host"):
                continue
            cred = dict(cred)
            cred["password"] = decrypt_secret(cred.get("password", ""))
            senders.append({
                "type": "smtp",
                "id": cred.get("id"),
                "config": cred,
                "label": _sender_label("smtp", cred),
                "host": cred.get("host"),
                "key": f"smtp:{cred.get('id')}",
            })
        if not senders:
            raise HTTPException(status_code=400, detail="No active SMTP credentials found for 'use any valid SMTP'.")
    else:
        for s in senders_raw:
            try:
                provider_type, config = _resolve_sender_config(user_id, s)
                sender_id = s.get("id")
                key = f"{provider_type}:{sender_id}" if sender_id else provider_type
                senders.append({
                    "type": provider_type,
                    "id": sender_id,
                    "config": config,
                    "label": _sender_label(provider_type, config),
                    "host": config.get("host") or config.get("sender_email") or config.get("domain") or provider_type,
                    "key": key,
                })
            except Exception as e:
                sender_errors.append(str(e))

        if not senders:
            raise HTTPException(status_code=400, detail="; ".join(sender_errors) or "No valid senders resolved")

    # Get user settings for sending parameters
    settings = read_user_data(user_id, USERS_BASE_PATH, FileNames.SETTINGS, {})

    # Skip recipients who have unsubscribed (suppression list).
    try:
        suppressed = {str(e).strip().lower() for e in get_suppression_list(user_id, USERS_BASE_PATH)}
        if suppressed:
            send_data["emails"] = [e for e in (send_data.get("emails") or []) if str(e).strip().lower() not in suppressed]
    except Exception:
        pass

    # Get rotation setting (how many emails per sender before rotating)
    smtp_rotate_after = settings.get("smtp_rotate_after", 50)
    
    # Create sending session
    campaign_id = generate_credential_id()
    
    # Get subjects and campaign name from send_data
    subjects = send_data.get("subjects", [])
    subject = subjects[0] if subjects else "No Subject"
    campaign_name = send_data.get("name") or send_data.get("campaign_name") or subject
    
    # Get email list ID for restoration
    email_list_id = send_data.get("email_list_id")

    audit_event(user_id, USERS_BASE_PATH, "campaign_started", {"campaign_id": campaign_id, "name": campaign_name, "emails": len(send_data.get("emails", []))})
    
    sending_session = {
        "id": campaign_id,
        "user_id": user_id,
        "created_at": datetime.now().isoformat(),
        "started_at": datetime.now().isoformat(),
        "smtp_config_ids": send_data.get("smtp_config_ids", []),
        "sender_keys": send_data.get("sender_keys", []),
        "senders": [{"type": s["type"], "label": s["label"]} for s in senders],
        "smtp_rotate_after": smtp_rotate_after,
        "batch_size": max(1, min(50, int(settings.get("batch_size", 1) or 1))),
        "retry_limit": max(1, min(10, int(settings.get("retry_limit", 3) or 3))),
        "random_delay": max(0, int(settings.get("random_delay", 5) or 5)),
        "rate_limit": max(1, int(settings.get("rate_limit", 25) or 25)),
        "rate_limiting": bool(settings.get("rate_limiting", True)),
        "total_emails": len(send_data.get("emails", [])),
        "sent": 0,
        "failed": 0,
        "retries": 0,
        "status": "running",
        "subject": subject,
        "campaign_name": campaign_name,
        "from_names": send_data.get("from_names", []),
        "recent_logs": [],
        "email_list_id": email_list_id,
        "emails": send_data.get("emails", [])  # Store emails for queue restoration
    }
    
    # Save sending session
    sessions_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    
    session_file = sessions_dir / f"{campaign_id}.json"
    write_json_atomic(session_file, sending_session)
    
    # Create log file for this session
    logs_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = logs_dir / f"{campaign_id}_log.json"
    write_json_atomic(log_file, {"campaign_id": campaign_id, "logs": []})
    
    # Start sending in background using threading
    import threading
    import time
    
    async def send_emails():
        import random
        import time
        from concurrent.futures import ThreadPoolExecutor
        from functions import update_sending_session, build_proxy_url

        emails = send_data.get("emails", [])
        from_names = send_data.get("from_names", [])
        subjects = send_data.get("subjects", [])
        letter_type = send_data.get("letter_type", "html")
        letter_content = send_data.get("letter_content", "")
        reply_to = send_data.get("reply_to", "")
        personalization = send_data.get("personalization", {})

        if not from_names:
            from_names = ["User"]
        if not subjects:
            subjects = ["No Subject"]

        # ---- Settings actually drive the sending behaviour ----
        random_delay = max(0, int(settings.get("random_delay", 5) or 5))
        retry_limit = max(1, min(10, int(settings.get("retry_limit", 3) or 3)))
        batch_size = max(1, min(50, int(settings.get("batch_size", 1) or 1)))
        connection_timeout = max(5, min(120, int(settings.get("connection_timeout", 20) or 20)))
        rate_limit = max(1, int(settings.get("rate_limit", 25) or 25))
        rate_limiting = bool(settings.get("rate_limiting", True))
        send_times = []  # timestamps of sends in the current 60s window

        # ---- Sender pool with preflight + auto-removal of bad senders ----
        def _preflight(sender):
            if sender.get("type") != "smtp":
                return True
            ok, _msg = check_smtp_valid(sender["config"], timeout=connection_timeout)
            return ok

        def _on_invalid(sender):
            # Remove a failed SMTP or API credential from server storage so it
            # never gets selected again (auth / connection failures only).
            # Mailbox configs (google / outlook) are single-value configs, so
            # they are only excluded from this campaign's rotation — not
            # deleted — since recovering them would require full re-setup.
            sender_type = sender.get("type")
            sender_id = sender.get("id")
            if not sender_id:
                return

            if sender_type == "smtp":
                with _smtp_storage_lock:
                    creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
                    creds = [c for c in creds if c.get("id") != sender_id]
                    write_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, creds)
            elif sender_type not in ("google", "outlook"):
                # API credential (sendgrid, mailgun, mailjet, postmark,
                # smtp2go, aws_ses / ses / aws).
                with _api_storage_lock:
                    creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, [])
                    creds = [c for c in creds if c.get("id") != sender_id]
                    write_user_data(user_id, USERS_BASE_PATH, FileNames.API_CREDENTIALS, creds)

        pool = _SenderPool(senders, smtp_rotate_after, _preflight, on_invalid=_on_invalid)

        # Proxy info (recorded in logs for the monitor; static per campaign)
        proxy_info = None
        try:
            proxy_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config" / "proxies.json"
            if proxy_file.exists():
                with open(proxy_file, 'r', encoding='utf-8') as f:
                    proxy_data = json.load(f)
                if proxy_data.get("useProxy"):
                    configs = proxy_data.get("configs", {}) or {}
                    prox = proxy_data.get("proxies") or []
                    if prox:
                        proxy_info = {"type": prox[0].get("type", "custom"), "host": prox[0].get("host", "unknown")}
                    elif configs:
                        for key in ("decodo", "brightdata", "custom"):
                            if configs.get(key, {}).get("host"):
                                proxy_info = {"type": key, "host": configs[key].get("host", "unknown")}
                                break
        except Exception:
            proxy_info = None

        def send_one(email, final_from_name, final_subject, html_content, text_content):
            """
            Send a single email with preflight validation, retry, and failover.
            Auth/connection failures invalidate the sender so it is removed
            from rotation and (for SMTP) deleted from storage. Other (retryable)
            failures retry the SAME sender up to `retry_limit` times.
            """
            max_attempts = retry_limit * max(1, len(senders)) + retry_limit
            attempts = 0
            sender = None
            last_label = ""
            last_message = "No valid sender available"
            response_time = 0.0

            def acquire_valid():
                nonlocal sender
                while True:
                    sender = pool.acquire()
                    if sender is None:
                        return False
                    if pool.preflight(sender):
                        return True
                    pool.invalidate(sender)

            while attempts < max_attempts:
                if sender is None:
                    if not acquire_valid():
                        return {
                            "email": email, "success": False,
                            "message": last_message or "No valid sender available",
                            "smtp": last_label, "response_time": response_time,
                        }

                attempts += 1
                start = time.time()
                ok, msg, kind = send_email_via_provider(
                    sender["type"], sender["config"], email, final_from_name,
                    final_subject, html_content, text_content, reply_to=reply_to,
                )
                response_time = round(time.time() - start, 3)
                last_label = sender["label"]
                last_message = msg

                if ok:
                    return {
                        "email": email, "success": True, "message": msg,
                        "smtp": sender["label"], "sender_type": sender["type"],
                        "response_time": response_time,
                    }

                if kind in ("auth", "connection"):
                    # Bad credential -> remove and fail over to the next sender.
                    pool.invalidate(sender)
                    sender = None
                    continue

                # Transient / other failure -> retry the same sender.
                if attempts < retry_limit:
                    time.sleep(min(2 ** attempts, 10))
                else:
                    return {
                        "email": email, "success": False, "message": last_message,
                        "smtp": last_label, "response_time": response_time,
                    }

            return {
                "email": email, "success": False, "message": last_message,
                "smtp": last_label, "response_time": response_time,
            }

        sent = 0
        failed = 0
        logs = []
        idx = 0
        total = len(emails)

        while idx < total:
            # Respect pause / stop / completed
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session = json.load(f)
                status = session.get("status")
                if status in ("stopped", "completed"):
                    break
                if status == "paused":
                    time.sleep(2)
                    continue
            except Exception:
                break

            batch = emails[idx: idx + batch_size]

            # Enforce the rate limit (max N emails per 60s window) from settings.
            if rate_limiting:
                now = time.time()
                send_times = [t for t in send_times if now - t < 60.0]
                while send_times and (len(send_times) + len(batch)) > rate_limit:
                    wait = 60.0 - (now - send_times[0]) + 0.05
                    if wait > 0:
                        time.sleep(wait)
                    now = time.time()
                    send_times = [t for t in send_times if now - t < 60.0]

            # Build tasks (personalization resolved per email)
            tasks = []
            for j, email in enumerate(batch):
                gi = idx + j
                from_name = from_names[gi % len(from_names)]
                subject = subjects[gi % len(subjects)]

                email_parts = email.split("@")
                email_local = email_parts[0] if email_parts else ""

                final_from_name = personalization.get("from_name") or from_name
                final_from_name = final_from_name.replace("{Email}", email).replace("{E-Name}", email_local)
                final_subject = personalization.get("subject") or subject
                final_subject = final_subject.replace("{Email}", email).replace("{E-Name}", email_local)
                final_content = letter_content.replace("*|EMAIL|*", email)
                # Replace the unsubscribe placeholder with a real, signed
                # per-recipient unsubscribe link when the template asks for it.
                if "*|UNSUBSCRIBE|*" in final_content:
                    final_content = final_content.replace("*|UNSUBSCRIBE|*", build_unsubscribe_url(email, user_id))

                if letter_type == "plain":
                    tasks.append((email, final_from_name, final_subject, "", final_content))
                else:
                    tasks.append((email, final_from_name, final_subject, final_content, ""))

            # Send the batch concurrently (batch_size emails at once)
            if len(tasks) == 1:
                results = [send_one(*tasks[0])]
            else:
                with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                    results = list(ex.map(lambda t: send_one(*t), tasks))

            # Process results in order: log + live session updates
            for (email, final_from_name, final_subject, _hc, _tc), res in zip(tasks, results):
                success = res["success"]
                message = res["message"]
                smtp_label = res.get("smtp") or "unknown"
                resp_time = res.get("response_time", 0)

                log_email_send(
                    user_id=user_id,
                    base_path=USERS_BASE_PATH,
                    email=email,
                    smtp_host=smtp_label,
                    smtp_port=0,
                    smtp_user=smtp_label,
                    success=success,
                    message=message,
                    from_name=final_from_name,
                    subject=final_subject,
                    campaign_id=campaign_id,
                    response_time=resp_time,
                    proxy_info=proxy_info,
                )
                log_access_event(
                    user_id,
                    USERS_BASE_PATH,
                    "campaign_email_sent" if success else "campaign_email_failed",
                    {
                        "email": email,
                        "campaign_id": campaign_id,
                        "smtp": smtp_label,
                        "message": message,
                        "proxy": proxy_info,
                    },
                )

                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "email": email,
                    "from_name": final_from_name,
                    "subject": final_subject,
                    "smtp": smtp_label,
                    "success": success,
                    "message": message,
                    "proxy": proxy_info,
                    "type": "success" if success else "error",
                }
                if success:
                    sent += 1
                else:
                    failed += 1
                logs.append(log_entry)

                update_sending_session(user_id, USERS_BASE_PATH, campaign_id, sent, failed, "running")

                try:
                    if log_file.exists():
                        with open(log_file, 'r', encoding='utf-8') as f:
                            log_data = json.load(f)
                    else:
                        log_data = {"logs": [], "campaign_id": campaign_id}
                    log_data["logs"].append(log_entry)
                    log_data["logs"] = log_data["logs"][-200:]
                    write_json_atomic(log_file, log_data)
                except Exception:
                    pass

            idx += len(batch)

            # Record send timestamps for the rate limiter
            if rate_limiting:
                now = time.time()
                send_times.extend([now] * len(batch))
                send_times = [t for t in send_times if now - t < 60.0]

            # Random delay between batches
            if idx < total and random_delay > 0:
                time.sleep(random.uniform(0.5, max(0.5, random_delay)))

        # Final update
        update_sending_session(user_id, USERS_BASE_PATH, campaign_id, sent, failed, "completed")

        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_data = json.load(f)
            log_data["logs"] = logs
            log_data["completed_at"] = datetime.now().isoformat()
            log_data["sent"] = sent
            log_data["failed"] = failed
            write_json_atomic(log_file, log_data)
        except Exception:
            pass
    
    # Start sending in background thread
    def run_async_send():
        import asyncio
        asyncio.run(send_emails())
    
    thread = threading.Thread(target=run_async_send, daemon=True)
    thread.start()
    
    return JSONResponse(
        content={
            "success": True,
            "campaign_id": campaign_id,
            "message": "Sending started",
            "total_emails": len(send_data.get("emails", [])),
            "senders": [{"type": s["type"], "label": s["label"]} for s in senders],
            "senders_count": len(senders),
            "smtp_rotate_after": smtp_rotate_after
        },
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.options("/send/status/{campaign_id}")
async def options_send_status(campaign_id: str):
    """Handle CORS preflight for send status"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/send/status/{campaign_id}")
async def get_send_status(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Get sending progress for a campaign"""
    user_id = current_user["user_id"]
    
    session_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions" / f"{campaign_id}.json"
    
    if not session_file.exists():
        return {
            "status": "not_found",
            "sent": 0,
            "failed": 0,
            "retries": 0,
            "total": 0,
            "recent_logs": [],
            "started_at": None,
            "email_list_id": None,
            "campaign_name": None,
            "emails": []
        }
    
    with open(session_file, 'r', encoding='utf-8') as f:
        session = json.load(f)
    
    # Debug: log what's in the session file
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"Session file {campaign_id} keys: {list(session.keys())}")
    logger.debug(f"Session emails count: {len(session.get('emails', []))}")
    logger.debug(f"Session campaign_name: {session.get('campaign_name')}")
    
    # Load logs
    log_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "logs" / f"{campaign_id}_log.json"
    recent_logs = []
    if log_file.exists():
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_data = json.load(f)
                recent_logs = log_data.get("logs", [])[-50:]  # Return more logs for restoration
        except Exception:
            pass
    
    return {
        "status": session.get("status", "unknown"),
        "sent": session.get("sent", 0),
        "failed": session.get("failed", 0),
        "retries": session.get("retries", 0),
        "total": session.get("total_emails", 0),
        "recent_logs": recent_logs,
        "started_at": session.get("started_at") or datetime.now().isoformat(),
        "campaign_name": session.get("campaign_name") or session.get("name") or session.get("subject") or "Campaign",
        "email_list_id": session.get("email_list_id"),
        "emails": session.get("emails", []),
        "name": session.get("name", ""),
        "subject": session.get("subject", ""),
        "_debug": {
            "session_has_campaign_name": "campaign_name" in session,
            "session_campaign_name_value": session.get("campaign_name"),
            "session_has_emails": "emails" in session,
            "session_emails_count": len(session.get("emails", []))
        }
    }


@router.options("/send/stop")
async def options_send_stop():
    """Handle CORS preflight for send stop"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/stop")
async def stop_sending(current_user: dict = Depends(get_current_user)):
    """Stop the current sending session (legacy endpoint - use /send/stop/{campaign_id} for specific campaign)"""
    user_id = current_user["user_id"]
    
    # Find active session
    sessions_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions"
    
    if sessions_dir.exists():
        for session_file in sessions_dir.glob("*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session = json.load(f)
                
                if session.get("status") == "running":
                    session["status"] = "stopped"
                    session["stopped_at"] = datetime.now().isoformat()
                    
                    with open(session_file, 'w', encoding='utf-8') as f:
                        json.dump(session, f, indent=2)
                    
                    return {"success": True, "message": "Sending stopped"}
            except Exception:
                continue
    
    return {"success": True, "message": "No active session found"}


@router.options("/send/pause")
async def options_send_pause():
    """Handle CORS preflight for send pause"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/pause")
async def pause_sending(current_user: dict = Depends(get_current_user)):
    """Pause the current sending session"""
    user_id = current_user["user_id"]
    
    # Find running session
    sessions_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions"
    
    if sessions_dir.exists():
        for session_file in sessions_dir.glob("*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session = json.load(f)
                
                if session.get("status") == "running":
                    session["status"] = "paused"
                    session["paused_at"] = datetime.now().isoformat()
                    
                    with open(session_file, 'w', encoding='utf-8') as f:
                        json.dump(session, f, indent=2)
                    
                    return {"success": True, "message": "Sending paused"}
            except Exception:
                continue
    
    return {"success": True, "message": "No active session found"}


@router.options("/send/resume")
async def options_send_resume():
    """Handle CORS preflight for send resume"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/resume")
async def resume_sending(current_user: dict = Depends(get_current_user)):
    """Resume a paused sending session"""
    user_id = current_user["user_id"]
    
    # Find paused session
    sessions_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions"
    
    if sessions_dir.exists():
        for session_file in sessions_dir.glob("*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session = json.load(f)
                
                if session.get("status") == "paused":
                    session["status"] = "running"
                    session["resumed_at"] = datetime.now().isoformat()
                    
                    with open(session_file, 'w', encoding='utf-8') as f:
                        json.dump(session, f, indent=2)
                    
                    return {"success": True, "message": "Sending resumed"}
            except Exception:
                continue
    
    return {"success": True, "message": "No paused session found"}


@router.options("/send/sessions")
async def options_send_sessions():
    """Handle CORS preflight for send sessions"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/send/sessions")
async def get_all_sessions(current_user: dict = Depends(get_current_user)):
    """Get all active sending sessions"""
    user_id = current_user["user_id"]
    sessions_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions"
    
    sessions = []
    
    if sessions_dir.exists():
        for session_file in sessions_dir.glob("*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session = json.load(f)
                
                # Include all sessions (running, paused, stopped)
                # Only include if not completed
                status = session.get("status", "unknown")
                if status not in ["completed", "deleted"]:
                    campaign_id = session_file.stem
                    sessions.append({
                        "campaign_id": campaign_id,
                        "status": status,
                        "sent": session.get("sent", 0),
                        "failed": session.get("failed", 0),
                        "total": session.get("total_emails", 0),
                        "subject": session.get("subject", "Unknown Subject"),
                        "name": session.get("name", ""),
                        "campaign_name": session.get("campaign_name") or session.get("name") or session.get("subject") or "Campaign",
                        "started_at": session.get("started_at") or "",
                        "from_names": session.get("from_names", []),
                        "emails": session.get("emails", []),  # Include emails for queue restoration
                        "progress": round((session.get("sent", 0) / max(session.get("total_emails", 1), 1)) * 100, 1)
                    })
            except Exception as e:
                continue
    
    return {"sessions": sessions, "count": len(sessions)}


@router.options("/send/stop/{campaign_id}")
async def options_send_stop_campaign(campaign_id: str):
    """Handle CORS preflight for send stop campaign"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/stop/{campaign_id}")
async def stop_specific_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Stop a specific campaign by ID"""
    user_id = current_user["user_id"]
    
    session_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions" / f"{campaign_id}.json"
    
    if not session_file.exists():
        return {"success": False, "message": "Campaign not found"}
    
    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            session = json.load(f)
        
        session["status"] = "stopped"
        session["stopped_at"] = datetime.now().isoformat()
        
        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(session, f, indent=2)
        
        return {"success": True, "message": f"Campaign {campaign_id} stopped", "campaign_id": campaign_id}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.options("/send/pause/{campaign_id}")
async def options_send_pause_campaign(campaign_id: str):
    """Handle CORS preflight for send pause campaign"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/pause/{campaign_id}")
async def pause_specific_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Pause a specific campaign by ID"""
    user_id = current_user["user_id"]
    
    session_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions" / f"{campaign_id}.json"
    
    if not session_file.exists():
        return {"success": False, "message": "Campaign not found"}
    
    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            session = json.load(f)
        
        session["status"] = "paused"
        session["paused_at"] = datetime.now().isoformat()
        
        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(session, f, indent=2)
        
        return {"success": True, "message": f"Campaign {campaign_id} paused", "campaign_id": campaign_id}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.options("/send/resume/{campaign_id}")
async def options_send_resume_campaign(campaign_id: str):
    """Handle CORS preflight for send resume campaign"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/resume/{campaign_id}")
async def resume_specific_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Resume a specific campaign by ID"""
    user_id = current_user["user_id"]
    
    session_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions" / f"{campaign_id}.json"
    
    if not session_file.exists():
        return {"success": False, "message": "Campaign not found"}
    
    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            session = json.load(f)
        
        session["status"] = "running"
        session["resumed_at"] = datetime.now().isoformat()
        
        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(session, f, indent=2)
        
        return {"success": True, "message": f"Campaign {campaign_id} resumed", "campaign_id": campaign_id}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.options("/send/delete/{campaign_id}")
async def options_send_delete_campaign(campaign_id: str):
    """Handle CORS preflight for send delete campaign"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.post("/send/delete/{campaign_id}")
async def delete_specific_campaign(campaign_id: str, current_user: dict = Depends(get_current_user)):
    """Delete/Mark a specific campaign as deleted"""
    user_id = current_user["user_id"]
    
    session_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "sessions" / f"{campaign_id}.json"
    log_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "logs" / f"{campaign_id}_log.json"
    
    try:
        if session_file.exists():
            with open(session_file, 'r', encoding='utf-8') as f:
                session = json.load(f)
            
            session["status"] = "deleted"
            session["deleted_at"] = datetime.now().isoformat()
            
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(session, f, indent=2)
        
        return {"success": True, "message": f"Campaign {campaign_id} deleted", "campaign_id": campaign_id}
    except Exception as e:
        return {"success": False, "message": str(e)}


# =============================================================================
# STATS ENDPOINTS
# =============================================================================

@router.get("/stats/dashboard")
async def get_dashboard_stats(current_user: dict = Depends(get_current_user)):
    """Get dashboard statistics"""
    user_id = current_user["user_id"]
    
    # Load sending history
    logs_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "sender-config" / "logs"
    
    total_sent = 0
    total_failed = 0
    campaigns_completed = 0
    
    if logs_dir.exists():
        for log_file in logs_dir.glob("*_log.json"):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    log_data = json.load(f)
                    logs = log_data.get("logs", [])
                    
                    for log in logs:
                        if log.get("type") == "success":
                            total_sent += 1
                        elif log.get("type") == "error":
                            total_failed += 1
                    
                    campaigns_completed += 1
            except Exception:
                continue
    
    # Get email list count
    email_lists = read_user_data(user_id, USERS_BASE_PATH, FileNames.EMAIL_LISTS, [])
    total_email_lists = len(email_lists)
    total_emails_stored = sum(l.get("total_emails", 0) for l in email_lists)
    
    # Get credentials count
    smtp_creds = read_user_data(user_id, USERS_BASE_PATH, FileNames.SMTP, [])
    active_smtp = len([c for c in smtp_creds if c.get("status") == "active"])
    
    return {
        "total_sent": total_sent,
        "total_failed": total_failed,
        "campaigns_completed": campaigns_completed,
        "total_email_lists": total_email_lists,
        "total_emails_stored": total_emails_stored,
        "active_smtp_credentials": active_smtp
    }


# =============================================================================
# PROXY MANAGEMENT ENDPOINTS
# =============================================================================

@router.options("/proxy/data")
async def options_proxy_data():
    """Handle CORS preflight for proxy data"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@router.get("/proxy/data")
async def get_proxy_data(current_user: dict = Depends(get_current_user)):
    """Get all proxy data for the user"""
    user_id = current_user["user_id"]
    
    proxy_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config" / "proxies.json"
    
    if not proxy_file.exists():
        return {
            "useProxy": False,
            "proxies": [],
            "settings": {
                "rotateAfterEmails": 5,
                "timeout": 30,
                "autoRemoveBad": True,
                "maxRetryAttempts": 3,
                "testInterval": 60
            },
            "configs": {
                "decodo": {},
                "brightdata": {},
                "oxylabs": {},
                "custom": {}
            }
        }
    
    with open(proxy_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return data


@router.post("/proxy/settings")
async def save_proxy_settings(
    settings_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save proxy settings (global toggle and rotation settings)"""
    user_id = current_user["user_id"]
    
    # Ensure proxy-config directory exists
    proxy_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    
    proxy_file = proxy_dir / "proxies.json"
    
    # Load existing data or create new
    if proxy_file.exists():
        with open(proxy_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {
            "useProxy": False,
            "proxies": [],
            "settings": {},
            "configs": {
                "decodo": {},
                "brightdata": {},
                "oxylabs": {},
                "custom": {}
            }
        }
    
    # Update settings
    if "use_proxy" in settings_data:
        data["useProxy"] = settings_data["use_proxy"]
    
    # Update other settings
    if "rotateAfterEmails" in settings_data:
        data["settings"]["rotateAfterEmails"] = max(1, min(100, int(settings_data["rotateAfterEmails"])))
    if "timeout" in settings_data:
        data["settings"]["timeout"] = max(5, min(120, int(settings_data["timeout"])))
    if "autoRemoveBad" in settings_data:
        data["settings"]["autoRemoveBad"] = bool(settings_data["autoRemoveBad"])
    if "maxRetryAttempts" in settings_data:
        data["settings"]["maxRetryAttempts"] = max(1, min(10, int(settings_data["maxRetryAttempts"])))
    if "testInterval" in settings_data:
        data["settings"]["testInterval"] = max(10, min(1440, int(settings_data["testInterval"])))
    
    with open(proxy_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    return {"success": True, "message": "Proxy settings saved"}


@router.get("/proxy/config")
async def get_proxy_config(current_user: dict = Depends(get_current_user)):
    """Get proxy configuration including useProxy status"""
    user_id = current_user["user_id"]
    
    proxy_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config"
    proxy_file = proxy_dir / "proxies.json"
    
    if not proxy_file.exists():
        return {"useProxy": False, "configs": {}}
    
    with open(proxy_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return {
        "useProxy": data.get("useProxy", False),
        "configs": data.get("configs", {})
    }


@router.post("/proxy/config/{provider}")
async def save_proxy_config(
    provider: str,
    config_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Save proxy configuration for a specific provider"""
    user_id = current_user["user_id"]
    
    # Validate provider
    valid_providers = ["decodo", "brightdata", "oxylabs", "custom"]
    if provider not in valid_providers:
        raise HTTPException(status_code=400, detail="Invalid proxy provider")
    
    # Ensure proxy-config directory exists
    proxy_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    
    proxy_file = proxy_dir / "proxies.json"
    
    # Load existing data or create new
    if proxy_file.exists():
        with open(proxy_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {
            "useProxy": False,
            "proxies": [],
            "settings": {},
            "configs": {}
        }
    
    # Ensure configs section exists
    if "configs" not in data:
        data["configs"] = {}
    
    # Update config for this provider
    data["configs"][provider] = config_data
    
    with open(proxy_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    return {"success": True, "message": f"{provider} proxy configuration saved"}


@router.post("/proxy/test/{provider}")
async def test_proxy_connection(
    provider: str,
    current_user: dict = Depends(get_current_user)
):
    """Test proxy connection for a specific provider"""
    user_id = current_user["user_id"]
    
    # Validate provider
    valid_providers = ["decodo", "brightdata", "oxylabs", "custom"]
    if provider not in valid_providers:
        raise HTTPException(status_code=400, detail="Invalid proxy provider")
    
    # Load proxy config
    proxy_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config"
    proxy_file = proxy_dir / "proxies.json"
    
    if not proxy_file.exists():
        return {"success": False, "error": "No proxy configuration found"}
    
    with open(proxy_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    config = data.get("configs", {}).get(provider, {})
    
    if not config:
        return {"success": False, "error": "No configuration for this provider"}
    
    # Test based on provider
    if provider == "decodo":
        # Test Decodo API connection
        from functions import test_decodo_proxy
        result = test_decodo_proxy(config)
        return result
    elif provider == "brightdata":
        # Test Bright Data proxy
        from functions import test_brightdata_proxy
        result = test_brightdata_proxy(config)
        return result
    elif provider == "oxylabs":
        # Test Oxylabs proxy
        from functions import test_oxylabs_proxy
        result = test_oxylabs_proxy(config)
        return result
    elif provider == "custom":
        # Test custom proxy
        from functions import test_custom_proxy
        result = test_custom_proxy(config)
        return result
    
    return {"success": False, "error": "Unknown provider"}


@router.post("/proxy/import/{provider}")
async def import_proxies(
    provider: str,
    import_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Import proxies for a specific provider - also auto-detects provider from host domain"""
    user_id = current_user["user_id"]
    
    # Auto-detect provider from host domain if provider is "auto"
    proxies = import_data.get("proxies", [])
    
    if not proxies:
        return {"success": False, "error": "No proxies provided"}
    
    # Auto-detect provider based on host domain
    detected_provider = provider
    if provider == "auto" or provider == "custom":
        for proxy in proxies:
            host = proxy.get("host", "")
            # Bright Data detection
            if "brd.superproxy.io" in host or "brightdata" in host.lower() or "superproxy" in host.lower():
                proxy["type"] = "brightdata"
                detected_provider = "brightdata"
            # Decodo detection
            elif "decodo" in host.lower():
                proxy["type"] = "decodo"
                detected_provider = "decodo"
            # Oxylabs detection
            elif "oxylabs" in host.lower() or "geonode" in host.lower():
                proxy["type"] = "oxylabs"
                detected_provider = "oxylabs"
            else:
                proxy["type"] = "custom"
                detected_provider = "custom"
    
    # Validate provider if not auto
    valid_providers = ["decodo", "brightdata", "oxylabs", "custom", "auto"]
    if provider not in valid_providers:
        raise HTTPException(status_code=400, detail="Invalid proxy provider")
    
    # Ensure proxy-config directory exists
    proxy_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    
    proxy_file = proxy_dir / "proxies.json"
    
    # Load existing data or create new
    if proxy_file.exists():
        with open(proxy_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {
            "useProxy": False,
            "proxies": [],
            "settings": {},
            "configs": {}
        }
    
    # Add new proxies with type and status (use detected provider if auto)
    for proxy in proxies:
        proxy["type"] = detected_provider
        proxy["status"] = "active"
        proxy["lastTest"] = datetime.now().isoformat()
        data["proxies"].append(proxy)
    
    with open(proxy_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    return {
        "success": True,
        "message": f"Imported {len(proxies)} proxies as {detected_provider}",
        "proxies": data["proxies"],
        "detected_provider": detected_provider
    }


@router.get("/proxy/proxies")
async def get_proxies(current_user: dict = Depends(get_current_user)):
    """Get all imported proxies"""
    user_id = current_user["user_id"]
    
    proxy_file = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config" / "proxies.json"
    
    if not proxy_file.exists():
        return {"proxies": []}
    
    with open(proxy_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return {"proxies": data.get("proxies", [])}


@router.put("/proxy/proxies")
async def update_proxies(
    proxies_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Update all proxies (replace existing list)"""
    user_id = current_user["user_id"]
    
    proxies = proxies_data.get("proxies", [])
    
    # Ensure proxy-config directory exists
    proxy_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    
    proxy_file = proxy_dir / "proxies.json"
    
    # Load existing data or create new
    if proxy_file.exists():
        with open(proxy_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {
            "useProxy": False,
            "settings": {},
            "configs": {}
        }
    
    # Update proxies
    data["proxies"] = proxies
    
    with open(proxy_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    return {"success": True, "message": "Proxies updated", "proxies": proxies}


@router.post("/proxy/test-all")
async def test_all_proxies(
    test_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Test all proxies"""
    user_id = current_user["user_id"]
    
    proxies = test_data.get("proxies", [])
    
    if not proxies:
        return {"success": False, "error": "No proxies to test"}
    
    # Test each proxy
    passed = 0
    failed = 0
    results = []
    
    for proxy in proxies:
        from functions import test_single_proxy
        result = test_single_proxy(proxy)
        
        if result.get("success"):
            passed += 1
            proxy["status"] = "active"
        else:
            failed += 1
            proxy["status"] = "failed"
        
        # Copy test result details to proxy object
        proxy["lastTest"] = datetime.now().isoformat()
        if result.get("latency") is not None:
            proxy["latency"] = result.get("latency")
        if result.get("ip"):
            proxy["testIp"] = result.get("ip")
        if result.get("country"):
            proxy["country"] = result.get("country")
        if result.get("isp"):
            proxy["isp"] = result.get("isp")
        
        results.append(result)
    
    # Update proxy file with results
    proxy_dir = USERS_BASE_PATH / f"fv-s-{user_id}" / "proxy-config"
    proxy_file = proxy_dir / "proxies.json"
    
    if proxy_file.exists():
        with open(proxy_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {"useProxy": False, "settings": {}, "configs": {}}
    
    # Update proxies
    data["proxies"] = proxies
    
    with open(proxy_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    return {
        "success": True,
        "passed": passed,
        "failed": failed,
        "proxies": proxies,
        "results": results
    }


@router.post("/proxy/test-single")
async def test_single_proxy_endpoint(
    test_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Test a single proxy"""
    proxy = test_data.get("proxy", {})
    
    if not proxy:
        return {"success": False, "error": "No proxy provided"}
    
    from functions import test_single_proxy
    result = test_single_proxy(proxy)
    
    return result


# =============================================================================
# INBOX / MAILBOX MANAGEMENT ENDPOINTS
# =============================================================================

# Note: CORS preflight is handled automatically by CORSMiddleware in main.py
# Explicit OPTIONS handlers are removed to prevent routing conflicts

@inbox_router.get("/status")
async def get_inbox_status():
    """Get inbox feature status"""
    return {"inbox_enabled": Inbox_state, "status": "active" if Inbox_state else "disabled"}


@inbox_router.get("/mailboxes")
async def get_all_mailboxes(current_user: dict = Depends(get_current_user)):
    """Get all mailboxes for the current user"""
    if not Inbox_state:
        raise HTTPException(status_code=503, detail="Inbox functionality is currently disabled. Coming Soon!")
    user_id = current_user["user_id"]
    mailboxes = get_user_mailboxes(user_id, USERS_BASE_PATH)
    return {"mailboxes": mailboxes, "total": len(mailboxes)}


@inbox_router.post("/mailboxes")
async def create_mailbox(
    mailbox_data: InboxMailboxCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new mailbox connection"""
    user_id = current_user["user_id"]
    
    # Build mailbox config dict
    config_dict = {
        "provider": mailbox_data.provider,
        "email_address": mailbox_data.email_address,
        "display_name": mailbox_data.display_name,
        "imap_config": mailbox_data.imap_config.model_dump(),
        "smtp_config": mailbox_data.smtp_config.model_dump() if mailbox_data.smtp_config else None,
        "is_primary": mailbox_data.is_primary
    }
    
    success, mailbox_id, message = save_mailbox_config(user_id, USERS_BASE_PATH, config_dict)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"success": True, "mailbox_id": mailbox_id, "message": message}


# =============================================================================
# STATIC MAILBOX ROUTES (must come BEFORE dynamic routes like {mailbox_id})
# =============================================================================

@inbox_router.get("/mailboxes/sync-status")
async def get_sync_status(current_user: dict = Depends(get_current_user)):
    """Get sync status for all mailboxes"""
    user_id = current_user["user_id"]
    
    mailboxes = get_user_mailboxes(user_id, USERS_BASE_PATH)
    
    sync_status = []
    for mailbox in mailboxes:
        sync_status.append({
            "mailbox_id": mailbox.get('id'),
            "email": mailbox.get('email'),
            "display_name": mailbox.get('display_name'),
            "sync_status": mailbox.get('sync_status', 'unknown'),
            "unread_count": mailbox.get('unread_count', 0),
            "last_sync": mailbox.get('last_sync'),
            "is_primary": mailbox.get('is_primary', False)
        })
    
    return {
        "mailboxes": sync_status,
        "total": len(sync_status)
    }


@inbox_router.get("/mailboxes/unread-count")
async def get_total_unread_count(current_user: dict = Depends(get_current_user)):
    """Get total unread email count across all mailboxes"""
    user_id = current_user["user_id"]
    
    mailboxes = get_user_mailboxes(user_id, USERS_BASE_PATH)
    
    total_unread = 0
    for mailbox in mailboxes:
        total_unread += mailbox.get('unread_count', 0)
    
    return {
        "total_unread": total_unread,
        "total_mailboxes": len(mailboxes)
    }


# =============================================================================
# DYNAMIC MAILBOX ROUTES (come AFTER static routes)
# =============================================================================

@inbox_router.get("/mailboxes/{mailbox_id}")
async def get_mailbox(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get a specific mailbox by ID"""
    user_id = current_user["user_id"]
    
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id)
    
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    return mailbox


@inbox_router.put("/mailboxes/{mailbox_id}")
async def update_mailbox(
    mailbox_id: str,
    updates: InboxMailboxUpdate,
    current_user: dict = Depends(get_current_user)
):
    """Update a mailbox configuration"""
    user_id = current_user["user_id"]
    
    # Check if mailbox exists
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    # Build updates dict
    updates_dict = {}
    
    if updates.display_name is not None:
        updates_dict["display_name"] = updates.display_name
    
    if updates.is_primary is not None:
        updates_dict["is_primary"] = updates.is_primary
    
    if updates.sync_enabled is not None:
        updates_dict["sync_enabled"] = updates.sync_enabled
    
    if updates.imap_config is not None:
        updates_dict["imap_config"] = updates.imap_config.model_dump()
    
    if updates.smtp_config is not None:
        updates_dict["smtp_config"] = updates.smtp_config.model_dump()
    
    success, message = update_mailbox_config(user_id, USERS_BASE_PATH, mailbox_id, updates_dict)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"success": True, "message": message}


@inbox_router.delete("/mailboxes/{mailbox_id}")
async def delete_mailbox(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a mailbox"""
    user_id = current_user["user_id"]
    
    success, message = delete_mailbox_config(user_id, USERS_BASE_PATH, mailbox_id)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"success": True, "message": message}


@inbox_router.post("/mailboxes/{mailbox_id}/set-primary")
async def set_primary(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Set a mailbox as primary"""
    user_id = current_user["user_id"]
    
    success, message = set_primary_mailbox(user_id, USERS_BASE_PATH, mailbox_id)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"success": True, "message": message}


@inbox_router.get("/mailboxes/primary")
async def get_primary(current_user: dict = Depends(get_current_user)):
    """Get the primary mailbox"""
    user_id = current_user["user_id"]
    mailbox = get_primary_mailbox(user_id, USERS_BASE_PATH, include_sensitive=True)
    
    if not mailbox:
        return {"mailbox": None, "message": "No primary mailbox set"}
    
    return {"mailbox": mailbox}


@inbox_router.post("/mailboxes/test-connection")
async def test_new_mailbox_connection(
    test_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Test connections for a new mailbox before saving"""
    imap_config = test_data.get("imap_config", {})
    smtp_config = test_data.get("smtp_config")
    
    if not imap_config:
        raise HTTPException(status_code=400, detail="IMAP config required")
    
    # Test IMAP
    imap_result = await test_imap_connection(
        host=imap_config.get("host"),
        port=int(imap_config.get("port", 993)),
        username=imap_config.get("username"),
        password=imap_config.get("password"),
        use_ssl=imap_config.get("use_ssl", True),
        validate_cert=imap_config.get("validate_cert", True)
    )
    
    # Test SMTP if provided
    smtp_result = None
    if smtp_config and smtp_config.get("host"):
        smtp_result = await test_smtp_connection_simple(
            host=smtp_config.get("host"),
            port=int(smtp_config.get("port", 587)),
            username=smtp_config.get("username"),
            password=smtp_config.get("password"),
            use_starttls=smtp_config.get("use_starttls", True),
            use_ssl=smtp_config.get("use_ssl", False)
        )
    
    return {
        "imap_success": imap_result.get("success", False),
        "imap_message": imap_result.get("message", ""),
        "imap_response_time": imap_result.get("response_time"),
        "smtp_success": smtp_result.get("success", False) if smtp_result else False,
        "smtp_message": smtp_result.get("message", "") if smtp_result else "",
        "smtp_response_time": smtp_result.get("response_time") if smtp_result else None,
        "overall_success": imap_result.get("success", False)
    }


# =============================================================================
# ASYNC IMAP ENDPOINTS (NEW - using connection pool + async)
# =============================================================================
# These endpoints use the new async architecture with:
# - IMAP connection pooling (imap_manager)
# - SQLAlchemy database helpers (db_helpers)
# - Non-blocking aioimaplib operations
#
# Endpoints are namespaced under /inbox/async/ to coexist with
# the legacy synchronous endpoints above.

@inbox_router.post("/async/test-connection")
async def async_test_connection(
    test_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Test IMAP connection using async connection pool
    
    Body:
    - host: IMAP server host
    - port: IMAP server port (default 993)
    - username: Email address
    - password: Email password
    - use_ssl: Use SSL (default true)
    - timeout: Connection timeout (default 20)
    - mailbox_id: Optional mailbox ID for pooling
    """
    host = test_data.get("host")
    port = int(test_data.get("port", 993))
    username = test_data.get("username")
    password = test_data.get("password", "")
    use_ssl = test_data.get("use_ssl", True)
    timeout = int(test_data.get("timeout", 20))
    mailbox_id = test_data.get("mailbox_id")
    
    if not host or not username:
        raise HTTPException(status_code=400, detail="Host and username are required")
    
    result = await test_imap_connection_async(
        host=host,
        port=port,
        username=username,
        password=password,
        use_ssl=use_ssl,
        timeout=timeout,
        mailbox_id=mailbox_id
    )
    
    return result


@inbox_router.get("/async/mailboxes/{mailbox_id}/folders")
async def async_get_folders(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get IMAP folders using async connection pool
    """
    user_id = current_user["user_id"]
    
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id, include_sensitive=True)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    result = await get_imap_folders_async(
        host=mailbox.get("imap_host"),
        port=int(mailbox.get("imap_port", 993)),
        username=mailbox.get("email"),
        password=mailbox.get("password", ""),
        use_ssl=mailbox.get("use_ssl", True),
        timeout=30,
        mailbox_id=mailbox_id
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Failed to list folders"))
    
    return result


@inbox_router.get("/async/mailboxes/{mailbox_id}/folders/{folder}/status")
async def async_get_folder_status(
    mailbox_id: str,
    folder: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get folder status (total/unread) using async connection pool
    """
    user_id = current_user["user_id"]
    
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id, include_sensitive=True)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    result = await get_folder_status_async(
        host=mailbox.get("imap_host"),
        port=int(mailbox.get("imap_port", 993)),
        username=mailbox.get("email"),
        password=mailbox.get("password", ""),
        folder=folder,
        use_ssl=mailbox.get("use_ssl", True),
        timeout=30,
        mailbox_id=mailbox_id
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Failed to get folder status"))
    
    return result


@inbox_router.get("/async/mailboxes/{mailbox_id}/folders/{folder}/emails")
async def async_fetch_emails(
    mailbox_id: str,
    folder: str,
    limit: int = 200,
    offset: int = 0,
    nocache: int = 0,
    current_user: dict = Depends(get_current_user)
):
    """
    Fetch emails from a folder using async connection pool with pagination.

    Caching:
        The endpoint consults a per-user SQLite-backed envelope cache
        (under {user_folder}/cache/email_cache.db) when offset==0 and
        nocache==0. On a cache miss it fetches from IMAP, parses the
        envelope, writes it back, and returns the live data. Pass
        `?nocache=1` to bypass the cache.
    """
    user_id = current_user["user_id"]

    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id, include_sensitive=True)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    # --- Cache lookup (only on the first page, when not bypassed) ---
    cache_hit = False
    cache_mgr = None
    if offset == 0 and not nocache:
        try:
            from email_cache import get_email_cache_manager_for_user
            cache_mgr = await get_email_cache_manager_for_user(user_id)
            cached_envelopes = await cache_mgr.get_envelopes(
                mailbox_id=mailbox_id, folder=folder, max_age_seconds=300
            )
            if cached_envelopes and len(cached_envelopes) >= min(limit, len(cached_envelopes)):
                cache_hit = True
                return {
                    "success": True,
                    "emails": cached_envelopes[:limit],
                    "message": f"Fetched {min(limit, len(cached_envelopes))} emails (cache)",
                    "total": len(cached_envelopes),
                    "has_more": len(cached_envelopes) > limit,
                    "from_cache": True
                }
        except Exception as e:
            # Cache failures must not break the main flow
            logger.warning(f"Email cache lookup failed: {e}")
            cache_mgr = None

    # --- IMAP fetch (cache miss or bypass) ---
    result = await fetch_emails_async(
        host=mailbox.get("imap_host"),
        port=int(mailbox.get("imap_port", 993)),
        username=mailbox.get("email"),
        password=mailbox.get("password", ""),
        folder=folder,
        use_ssl=mailbox.get("use_ssl", True),
        limit=limit,
        offset=offset,
        timeout=30,
        mailbox_id=mailbox_id
    )

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Failed to fetch emails"))

    # --- Write back to cache (best-effort) ---
    if not cache_hit and offset == 0 and cache_mgr is None and not nocache:
        try:
            from email_cache import get_email_cache_manager_for_user
            cache_mgr = await get_email_cache_manager_for_user(user_id)
        except Exception as e:
            logger.warning(f"Could not open cache for writeback: {e}")
            cache_mgr = None

    if cache_mgr is not None and offset == 0 and not nocache:
        try:
            envelopes = []
            for env in result.get("emails", []):
                # Each entry has uid + raw_data (bytes list). Try to parse
                # the headers so the cached envelope view is useful.
                uid = env.get("uid")
                raw = env.get("raw_data")
                parsed = _parse_raw_imap_message(raw) if raw else {}
                envelopes.append({
                    "uid": uid,
                    "subject": parsed.get("subject", ""),
                    "sender": parsed.get("from", ""),
                    "date": parsed.get("date", ""),
                    "flags": parsed.get("flags", ""),
                    "message_id": parsed.get("message_id", ""),
                    "is_read": parsed.get("is_read", False),
                })
            if envelopes:
                await cache_mgr.put_envelopes(mailbox_id, folder, envelopes)
        except Exception as e:
            logger.warning(f"Email cache writeback failed: {e}")

    return result


def _parse_raw_imap_message(raw_data) -> dict:
    """
    Helper: extract subject/from/date/flags/message_id/is_read from the
    raw aioimaplib `fetch` response.

    `raw_data` is typically a list of bytes/str tokens. The first token
    contains the UID/FLAGS, the remaining bytes contain the headers.
    """
    parsed = {
        "subject": "",
        "from": "",
        "date": "",
        "flags": "",
        "message_id": "",
        "is_read": False,
    }
    try:
        # Concatenate everything into one string for simple scanning
        if isinstance(raw_data, list):
            parts = []
            for item in raw_data:
                if isinstance(item, bytes):
                    parts.append(item.decode("utf-8", errors="replace"))
                else:
                    parts.append(str(item))
            joined = "\n".join(parts)
        elif isinstance(raw_data, bytes):
            joined = raw_data.decode("utf-8", errors="replace")
        else:
            joined = str(raw_data or "")

        # Flags live in parentheses; the headers come on a body line.
        # aioimaplib puts a tuple like (b'1 (FLAGS (\\Seen) UID 1234)', b' header...')
        # We look for key substrings.
        if "FLAGS" in joined:
            try:
                flags_start = joined.index("FLAGS (") + len("FLAGS (")
                flags_end = joined.index(")", flags_start)
                parsed["flags"] = joined[flags_start:flags_end].strip()
                parsed["is_read"] = "\\Seen" in parsed["flags"]
            except Exception:
                pass

        # Headers as a block after the first blank line or just on one line
        for header_name, key in [
            ("subject", "subject"),
            ("from", "from"),
            ("date", "date"),
            ("message-id", "message_id"),
        ]:
            # Match header at start-of-line (after \r\n) optionally followed by space (continuation)
            m = _re.search(r"(?im)^" + _re.escape(header_name) + r":\s*(.+?)\r?\n", joined)
            if m:
                parsed[key] = m.group(1).strip()
    except Exception:
        pass
    return parsed


@inbox_router.post("/async/mailboxes/{mailbox_id}/emails/search")
async def async_search_emails(
    mailbox_id: str,
    search_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Search emails in a folder using async connection pool
    
    Body:
    - folder: Folder name (default INBOX)
    - criteria: IMAP search criteria (e.g. 'UNSEEN', 'FROM "user@example.com"')
    - limit: Max results (default 200)
    """
    user_id = current_user["user_id"]
    
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id, include_sensitive=True)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    folder = search_data.get("folder", "INBOX")
    criteria = search_data.get("criteria", "ALL")
    limit = int(search_data.get("limit", 200))
    
    result = await search_emails_async(
        host=mailbox.get("imap_host"),
        port=int(mailbox.get("imap_port", 993)),
        username=mailbox.get("email"),
        password=mailbox.get("password", ""),
        folder=folder,
        search_criteria=criteria,
        use_ssl=mailbox.get("use_ssl", True),
        limit=limit,
        timeout=30,
        mailbox_id=mailbox_id
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Search failed"))
    
    return result


@inbox_router.get("/async/mailboxes/{mailbox_id}/sync-state")
async def async_get_sync_state(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get sync state for a mailbox from the database
    """
    result = await get_sync_state_async(mailbox_id=mailbox_id)
    return result


@inbox_router.post("/async/mailboxes/{mailbox_id}/sync-state")
async def async_update_sync_state(
    mailbox_id: str,
    state_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Update sync state for a mailbox in the database
    
    Body (all optional):
    - last_uid: Last synced UID
    - is_syncing: Whether sync is in progress
    - status: Sync status (syncing, completed, error, idle)
    - error: Error message if any
    """
    result = await update_sync_state_async(
        mailbox_id=mailbox_id,
        last_uid=state_data.get("last_uid"),
        is_syncing=state_data.get("is_syncing"),
        status=state_data.get("status"),
        error=state_data.get("error")
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Update failed"))
    
    return result


@inbox_router.post("/async/mailboxes/{mailbox_id}/emails/{folder}/{email_uid}/mark-read")
async def async_mark_email_read(
    mailbox_id: str,
    folder: str,
    email_uid: str,
    current_user: dict = Depends(get_current_user)
):
    """Mark an email as read using async connection pool"""
    user_id = current_user["user_id"]
    
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id, include_sensitive=True)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    result = await mark_email_read_async(
        host=mailbox.get("imap_host"),
        port=int(mailbox.get("imap_port", 993)),
        username=mailbox.get("email"),
        password=mailbox.get("password", ""),
        folder=folder,
        email_uid=email_uid,
        is_read=True,
        use_ssl=mailbox.get("use_ssl", True),
        timeout=30,
        mailbox_id=mailbox_id
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Failed to mark read"))
    
    return result


@inbox_router.post("/async/mailboxes/{mailbox_id}/emails/{folder}/{email_uid}/mark-unread")
async def async_mark_email_unread(
    mailbox_id: str,
    folder: str,
    email_uid: str,
    current_user: dict = Depends(get_current_user)
):
    """Mark an email as unread using async connection pool"""
    user_id = current_user["user_id"]
    
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id, include_sensitive=True)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    result = await mark_email_read_async(
        host=mailbox.get("imap_host"),
        port=int(mailbox.get("imap_port", 993)),
        username=mailbox.get("email"),
        password=mailbox.get("password", ""),
        folder=folder,
        email_uid=email_uid,
        is_read=False,
        use_ssl=mailbox.get("use_ssl", True),
        timeout=30,
        mailbox_id=mailbox_id
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Failed to mark unread"))
    
    return result


# CORS preflight handlers for the new async endpoints
@inbox_router.options("/async/test-connection")
async def options_async_test_connection():
    """Handle CORS preflight for async test-connection"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@inbox_router.options("/async/mailboxes/{mailbox_id}/folders")
async def options_async_folders(mailbox_id: str):
    """Handle CORS preflight for async folders"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@inbox_router.options("/async/mailboxes/{mailbox_id}/folders/{folder}/status")
async def options_async_folder_status(mailbox_id: str, folder: str):
    """Handle CORS preflight for async folder status"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@inbox_router.options("/async/mailboxes/{mailbox_id}/folders/{folder}/emails")
async def options_async_folder_emails(mailbox_id: str, folder: str):
    """Handle CORS preflight for async folder emails"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@inbox_router.options("/async/mailboxes/{mailbox_id}/emails/search")
async def options_async_search(mailbox_id: str):
    """Handle CORS preflight for async search"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@inbox_router.options("/async/mailboxes/{mailbox_id}/sync-state")
async def options_async_sync_state(mailbox_id: str):
    """Handle CORS preflight for async sync-state"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@inbox_router.options("/async/mailboxes/{mailbox_id}/emails/{folder}/{email_uid}/mark-read")
async def options_async_mark_read(mailbox_id: str, folder: str, email_uid: str):
    """Handle CORS preflight for async mark-read"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


@inbox_router.options("/async/mailboxes/{mailbox_id}/emails/{folder}/{email_uid}/mark-unread")
async def options_async_mark_unread(mailbox_id: str, folder: str, email_uid: str):
    """Handle CORS preflight for async mark-unread"""
    return JSONResponse(
        content={"status": "ok"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )


# =============================================================================
# FOLDER ENDPOINTS
# =============================================================================

@inbox_router.get("/mailboxes/{mailbox_id}/folders")
async def get_mailbox_folders(
    mailbox_id: str,
    nocache: int = 0,
    current_user: dict = Depends(get_current_user)
):
    """
    Get list of folders in a mailbox.

    The folder list (with unread/total counts) is cached in a per-user
    SQLite-backed cache for 5 minutes. Pass `?nocache=1` to bypass.
    """
    user_id = current_user["user_id"]

    # Cache lookup
    if not nocache:
        try:
            from email_cache import get_email_cache_manager_for_user
            cache_mgr = await get_email_cache_manager_for_user(user_id)
            cached = await cache_mgr.get_cached_folders(mailbox_id)
            # The cache stores CachedFolder rows; check freshness (5 min TTL)
            if cached:
                import time as _time
                freshest = max((f.cached_at for f in cached), default=0)
                if _time.time() - freshest < 300:
                    folders = [
                        {
                            "name": f.name,
                            "path": f.name,
                            "unread_count": f.unread_count,
                            "total_count": f.total_emails,
                        }
                        for f in cached
                    ]
                    return {"folders": folders, "total": len(folders), "from_cache": True}
        except Exception as e:
            logger.warning(f"Folder cache lookup failed: {e}")

    success, folders, message = get_folder_list(user_id, USERS_BASE_PATH, mailbox_id)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    # Write back to cache (best-effort)
    try:
        from email_cache import get_email_cache_manager_for_user
        cache_mgr = await get_email_cache_manager_for_user(user_id)
        # The get_folder_list returns dicts with "name", "path", "unread_count", "total_count"
        await cache_mgr.cache_folders(mailbox_id, [
            {
                "name": f.get("name", ""),
                "total": f.get("total_count", 0),
                "unread": f.get("unread_count", 0),
            }
            for f in (folders or [])
        ])
    except Exception as e:
        logger.warning(f"Folder cache writeback failed: {e}")

    return {"folders": folders, "total": len(folders)}


@inbox_router.get("/mailboxes/{mailbox_id}/folders/{folder:path}/emails")
async def get_folder_emails(
    mailbox_id: str,
    folder: str,
    limit: int = 200,
    offset: int = 0,
    unread_only: bool = False,
    refresh: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """Get emails from a specific folder. Set refresh=true to force IMAP fetch (bypasses cache)."""
    user_id = current_user["user_id"]

    success, emails, message, total = fetch_emails_from_folder(
        user_id, USERS_BASE_PATH, mailbox_id, folder, limit, offset, unread_only, force_refresh=refresh
    )

    if not success:
        logger.warning(f"Fetch failed for {mailbox_id}/{folder}: {message}")
        raise HTTPException(status_code=400, detail=message)

    return {"emails": emails or [], "total": total if total else len(emails or []), "loading": False}


@inbox_router.get("/mailboxes/{mailbox_id}/folders/{folder:path}/sync")
async def sync_folder(
    mailbox_id: str,
    folder: str,
    full_sync: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """Sync emails from a folder"""
    user_id = current_user["user_id"]
    
    result = await sync_mailbox_folder(
        user_id, USERS_BASE_PATH, mailbox_id, folder, full_sync
    )
    
    return result


# =============================================================================
# EMAIL OPERATION ENDPOINTS
# =============================================================================

@inbox_router.get("/mailboxes/{mailbox_id}/emails/{folder:path}/{email_uid}/attachments/{filename:path}")
async def download_email_attachment(
    mailbox_id: str,
    folder: str,
    email_uid: int,
    filename: str,
    current_user: dict = Depends(get_current_user)
):
    """Download an email attachment to the local machine."""
    user_id = current_user["user_id"]
    success, data, message = get_email_attachment(
        user_id, USERS_BASE_PATH, mailbox_id, folder, email_uid, filename
    )
    if not success or not data:
        raise HTTPException(status_code=404, detail=message)

    safe_name = filename.replace('"', "'")
    return Response(
        content=data["content"],
        media_type=data.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@inbox_router.get("/mailboxes/{mailbox_id}/emails/{folder:path}/{email_uid}")
async def get_email(
    mailbox_id: str,
    folder: str,
    email_uid: int,
    current_user: dict = Depends(get_current_user)
):
    """Get full email body"""
    user_id = current_user["user_id"]
    
    success, email_data, message = get_email_body(
        user_id, USERS_BASE_PATH, mailbox_id, folder, email_uid
    )
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return email_data


@inbox_router.post("/mailboxes/{mailbox_id}/emails/search")
async def search_emails(
    mailbox_id: str,
    query: EmailSearchQuery,
    current_user: dict = Depends(get_current_user)
):
    """Search emails with various criteria"""
    user_id = current_user["user_id"]
    
    query_dict = query.model_dump()
    query_dict["mailbox_id"] = mailbox_id
    
    success, emails, message = search_emails_in_mailbox(
        user_id, USERS_BASE_PATH, mailbox_id, query_dict
    )
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"emails": emails or [], "total": len(emails or [])}


@inbox_router.post("/mailboxes/{mailbox_id}/search")
async def search_mailbox(
    mailbox_id: str,
    search_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Smart full-folder email search.

    Runs a server-side IMAP SEARCH so results span the ENTIRE folder (not
    just the first page), then returns clean, list-ready email entries with
    subject, sender, recipients, date, flags, size, attachment flag and a
    text preview. Supports text + field (all/subject/from/to/body), unread
    filter, has-attachments filter, date range, and offset/limit pagination.

    Pass `folders` (list) instead of `folder` to search across multiple
    folders ("All folders").
    """
    user_id = current_user["user_id"]

    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id, include_sensitive=True)
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    imap_config = mailbox.get("imap_config") or {}
    host = imap_config.get("host")
    username = imap_config.get("username")
    if not host or not username:
        raise HTTPException(status_code=400, detail="Mailbox IMAP configuration is incomplete")

    limit = int(search_data.get("limit", 50) or 50)
    offset = int(search_data.get("offset", 0) or 0)

    folders_param = search_data.get("folders")
    if isinstance(folders_param, list) and folders_param:
        folder_list = [str(f) for f in folders_param if f]
    else:
        folder_list = [search_data.get("folder") or "INBOX"]

    def _sort_key(item):
        d = item.get("date") if isinstance(item, dict) else None
        if not d:
            return 0
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(d)
            return dt.timestamp() if dt else 0
        except Exception:
            return 0

    merged = []
    total = 0
    has_more = False
    errors = []

    for folder in folder_list:
        result = await search_mailbox_async(
            host=host,
            port=int(imap_config.get("port", 993)),
            username=username,
            password=imap_config.get("password", ""),
            folder=folder,
            search_text=search_data.get("search_text", ""),
            search_field=search_data.get("search_field", "all"),
            unread_only=bool(search_data.get("unread_only")),
            has_attachments=bool(search_data.get("has_attachments")),
            since_date=search_data.get("since_date"),
            before_date=search_data.get("before_date"),
            use_ssl=imap_config.get("use_ssl", True),
            limit=limit,
            offset=offset,
            timeout=30,
            mailbox_id=mailbox_id,
        )
        if not result.get("success"):
            errors.append(f"{folder}: {result.get('message')}")
            continue
        total += int(result.get("total", 0))
        has_more = has_more or bool(result.get("has_more"))
        merged.extend(result.get("emails", []) or [])

    if merged:
        merged.sort(key=_sort_key, reverse=True)

    if not merged and errors and total == 0:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    return {
        "success": True,
        "emails": merged,
        "total": total,
        "has_more": has_more,
        "folders_searched": folder_list,
        "errors": errors or None,
    }


@inbox_router.post("/mailboxes/{mailbox_id}/emails/{folder:path}/{email_uid}/action")
async def email_action(
    mailbox_id: str,
    folder: str,
    email_uid: int,
    action_request: EmailActionRequest,
    current_user: dict = Depends(get_current_user)
):
    """Perform action on an email (mark read/unread, delete, move)"""
    user_id = current_user["user_id"]
    
    action = action_request.action
    destination = action_request.destination_folder
    
    if action == "mark_read":
        success, message = mark_email_as_read(user_id, USERS_BASE_PATH, mailbox_id, folder, email_uid)
    elif action == "mark_unread":
        success, message = mark_email_as_unread(user_id, USERS_BASE_PATH, mailbox_id, folder, email_uid)
    elif action == "move" and destination:
        success, message = move_email_to_folder(user_id, USERS_BASE_PATH, mailbox_id, folder, email_uid, destination)
    elif action == "delete":
        success, message = delete_email_permanently(user_id, USERS_BASE_PATH, mailbox_id, folder, email_uid)
    else:
        raise HTTPException(status_code=400, detail="Invalid action or missing destination")
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"success": True, "message": message}


# =============================================================================
# EMAIL SENDING ENDPOINTS
# =============================================================================

@inbox_router.post("/mailboxes/{mailbox_id}/send")
async def send_email(
    mailbox_id: str,
    email_data: ComposeEmailRequest,
    current_user: dict = Depends(get_current_user)
):
    """Send email using mailbox SMTP"""
    user_id = current_user["user_id"]
    
    email_dict = email_data.model_dump()
    
    success, message = send_email_from_mailbox(
        user_id, USERS_BASE_PATH, mailbox_id, email_dict
    )
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {"success": True, "message": message}


# =============================================================================
# BULK IMPORT/EXPORT ENDPOINTS
# =============================================================================

@inbox_router.post("/mailboxes/import")
async def import_mailboxes(
    import_request: MailboxImportRequest,
    current_user: dict = Depends(get_current_user)
):
    """Bulk import mailboxes"""
    user_id = current_user["user_id"]
    
    entries = [entry.model_dump() for entry in import_request.entries]
    test_only = import_request.validate_only
    
    results = await import_mailboxes_bulk(user_id, USERS_BASE_PATH, entries, test_only)
    
    return results


@inbox_router.post("/mailboxes/{mailbox_id}/sync-all")
async def sync_single_mailbox_all_folders(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Sync all folders for a specific mailbox - saves all emails to server"""
    user_id = current_user["user_id"]
    
    # Get mailbox
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id)
    
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    total_new = 0
    total_unread = 0
    results = []
    
    # Get folders for this mailbox
    try:
        folders_result = await get_mailbox_folders(mailbox_id, current_user)
        folders = folders_result.get("folders", [])
        
        for folder_info in folders:
            folder = folder_info.get('name', 'INBOX')
            
            # Sync the folder - this saves emails to server
            sync_result = await sync_mailbox_folder(
                user_id, USERS_BASE_PATH, mailbox_id, folder, full_sync=True
            )
            
            if sync_result.get('success'):
                total_new += sync_result.get('new_emails', 0)
                total_unread += sync_result.get('unread_count', 0)
            
            results.append({
                "folder": folder,
                "success": sync_result.get('success', False),
                "total_emails": sync_result.get('total_emails', 0),
                "unread_count": sync_result.get('unread_count', 0),
                "message": sync_result.get('message', '')
            })
    except Exception as e:
        return {
            "success": False,
            "message": f"Error syncing: {str(e)}",
            "results": results
        }
    
    return {
        "success": True,
        "mailbox_id": mailbox_id,
        "mailbox_email": mailbox.get('email'),
        "total_folders_synced": len(results),
        "total_emails": total_new,
        "unread_count": total_unread,
        "results": results
    }


@inbox_router.post("/mailboxes/import/text")
async def import_mailboxes_from_text_endpoint(
    import_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Import mailboxes from raw text content"""
    user_id = current_user["user_id"]
    
    content = import_data.get("content", "")
    format_type = import_data.get("format_type", "auto")
    test_only = import_data.get("test_only", False)
    skip_connection_test = import_data.get("skip_connection_test", False)
    
    if not content:
        raise HTTPException(status_code=400, detail="Content is required")
    
    results = await import_mailboxes_from_text(
        user_id, USERS_BASE_PATH, content, format_type, test_only, skip_connection_test
    )
    
    return results


@inbox_router.get("/mailboxes/export")
async def export_mailboxes(
    include_passwords: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """Export all mailboxes to JSON format"""
    user_id = current_user["user_id"]
    
    if not include_passwords:
        # Verify user wants to export (passwords included)
        pass
    
    export_data = export_mailboxes_to_json(user_id, USERS_BASE_PATH, include_passwords)
    
    return {
        "format": "json",
        "count": len(json.loads(export_data)),
        "data": export_data
    }


@inbox_router.get("/providers/detect")
async def detect_provider(email_address: str):
    """Detect provider from email address"""
    if not email_address or '@' not in email_address:
        raise HTTPException(status_code=400, detail="Valid email_address is required")
    config = get_imap_config_from_email(email_address)
    return config


@inbox_router.post("/providers/config")
async def get_provider_config(request_data: dict):
    """Get full IMAP/SMTP configuration from email address"""
    email_address = request_data.get("email", "")
    if not email_address or '@' not in email_address:
        raise HTTPException(status_code=400, detail="Valid email is required")
    return get_imap_config_from_email(email_address)


@inbox_router.post("/providers/config/batch")
async def get_provider_config_batch(request_data: dict):
    """Batch detect IMAP/SMTP configuration for multiple emails"""
    emails = request_data.get("emails", [])
    if not emails:
        raise HTTPException(status_code=400, detail="emails array is required")
    configs = detect_imap_configs_batch(emails)
    return {"count": len(configs), "configs": configs}


@inbox_router.get("/providers")
async def get_email_providers():
    """Get all available email provider configurations from smtp-imap.json"""
    import json
    from pathlib import Path
    
    smtp_imap_path = Path(__file__).parent / "smtp-imap.json"
    
    if smtp_imap_path.exists():
        with open(smtp_imap_path, 'r') as f:
            data = json.load(f)
            return {
                "providers": data.get("providers", {}),
                "common_ports": data.get("common_ports", {}),
                "tips": data.get("tips", {})
            }
    
    # Fallback to hardcoded defaults if file doesn't exist
    return {
        "providers": {
            "gmail": {"name": "Gmail / Google Workspace", "imap": {"host": "imap.gmail.com", "port": 993, "use_ssl": True}, "smtp": {"host": "smtp.gmail.com", "port": 587, "use_starttls": True}},
            "yahoo": {"name": "Yahoo Mail", "imap": {"host": "imap.mail.yahoo.com", "port": 993, "use_ssl": True}, "smtp": {"host": "smtp.mail.yahoo.com", "port": 587, "use_starttls": True}},
            "outlook": {"name": "Microsoft Outlook", "imap": {"host": "outlook.office365.com", "port": 993, "use_ssl": True}, "smtp": {"host": "smtp.office365.com", "port": 587, "use_starttls": True}},
            "icloud": {"name": "iCloud Mail", "imap": {"host": "imap.icloud.com", "port": 993, "use_ssl": True}, "smtp": {"host": "smtp.mail.me.com", "port": 587, "use_starttls": True}},
            "zoho": {"name": "Zoho Mail", "imap": {"host": "imap.zoho.com", "port": 993, "use_ssl": True}, "smtp": {"host": "smtp.zoho.com", "port": 587, "use_starttls": True}},
            "custom": {"name": "Custom IMAP/SMTP Server", "imap": {"host": "", "port": 993, "use_ssl": True}, "smtp": {"host": "", "port": 587, "use_starttls": True}}
        },
        "common_ports": {
            "imap": {"993": "IMAP over SSL (Recommended)", "143": "IMAP without encryption"},
            "smtp": {"587": "SMTP with STARTTLS (Recommended)", "465": "SMTP over SSL", "25": "SMTP without encryption"}
        },
        "tips": {
            "security": "Always use SSL/TLS connections when available.",
            "app_passwords": "Most email services require App-Specific Passwords when 2FA is enabled."
        }
    }


@inbox_router.get("/mailboxes/{mailbox_id}/sync-status")
async def get_mailbox_sync_status(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get sync status for a specific mailbox - check for new emails without full sync"""
    user_id = current_user["user_id"]
    
    # Get mailbox
    mailbox = get_mailbox_by_id(user_id, USERS_BASE_PATH, mailbox_id)
    
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    # Get storage path and check if emails exist
    storage_path = get_mailbox_storage_path(user_id, mailbox_id, USERS_BASE_PATH)
    
    has_saved_emails = False
    total_saved = 0
    
    if storage_path.exists():
        # Count all email files across folders
        for folder_path in storage_path.iterdir():
            if folder_path.is_dir():
                email_files = list(folder_path.glob("email_*.json"))
                total_saved += len(email_files)
        
        has_saved_emails = total_saved > 0
    
    # Get unread count from mailbox config
    unread_count = mailbox.get('unread_count', 0)
    
    return {
        "mailbox_id": mailbox_id,
        "has_new_emails": False,  # Will be updated after sync check
        "new_email_count": 0,
        "total_saved_emails": total_saved,
        "unread_count": unread_count,
        "last_sync": mailbox.get('last_sync'),
        "status": mailbox.get('status', 'active'),
        "has_server_cache": has_saved_emails
    }


@inbox_router.post("/mailboxes/sync-all")
async def sync_all_mailboxes(
    folders_only: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """Sync all mailboxes for the current user (background sync)"""
    user_id = current_user["user_id"]
    
    mailboxes = get_user_mailboxes(user_id, USERS_BASE_PATH)
    
    results = []
    total_new = 0
    total_unread = 0
    
    for mailbox in mailboxes:
        mailbox_id = mailbox.get('id')
        
        # Get folders for this mailbox
        try:
            folders_result = await get_mailbox_folders(mailbox_id, current_user)
            folders = folders_result.get("folders", [])
            
            for folder_info in folders:
                folder = folder_info.get('name', 'INBOX')
                
                if folders_only:
                    # Just get folder info, don't sync emails
                    continue
                
                # Sync the folder
                sync_result = await sync_mailbox_folder(
                    user_id, USERS_BASE_PATH, mailbox_id, folder, full_sync=False
                )
                
                if sync_result.get('success'):
                    total_new += sync_result.get('new_emails', 0)
                    total_unread += sync_result.get('unread_count', 0)
                
                results.append({
                    "mailbox_id": mailbox_id,
                    "mailbox_email": mailbox.get('email'),
                    "folder": folder,
                    "success": sync_result.get('success', False),
                    "new_emails": sync_result.get('new_emails', 0),
                    "unread_count": sync_result.get('unread_count', 0),
                    "message": sync_result.get('message', '')
                })
        except Exception as e:
            results.append({
                "mailbox_id": mailbox_id,
                "mailbox_email": mailbox.get('email'),
                "folder": "INBOX",
                "success": False,
                "error": str(e)
            })
    
    return {
        "success": True,
        "total_mailboxes": len(mailboxes),
        "total_new_emails": total_new,
        "total_unread": total_unread,
        "results": results
    }


# =============================================================================
# BACKGROUND SYNC WORKER ENDPOINTS
# =============================================================================

@inbox_router.post("/workers/start/{mailbox_id}")
async def start_mailbox_worker(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Start a background sync worker for a mailbox"""
    from workers import get_worker_manager
    
    user_id = current_user["user_id"]
    
    # Get mailbox config
    mailboxes = get_user_mailboxes(user_id, USERS_BASE_PATH)
    mailbox = next((m for m in mailboxes if m.get('id') == mailbox_id), None)
    
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    
    manager = await get_worker_manager()
    
    success = await manager.start_worker(
        mailbox_id=mailbox_id,
        user_id=user_id,
        host=mailbox.get('imap_host'),
        port=int(mailbox.get('imap_port', 993)),
        username=mailbox.get('email'),
        password=mailbox.get('password'),
        use_ssl=mailbox.get('use_ssl', True),
        folders=["INBOX"]  # Default to INBOX, can be expanded
    )
    
    if success:
        return {"success": True, "message": f"Worker started for mailbox {mailbox_id}"}
    
    raise HTTPException(status_code=500, detail="Failed to start worker")


@inbox_router.post("/workers/stop/{mailbox_id}")
async def stop_mailbox_worker(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Stop a background sync worker for a mailbox"""
    from workers import get_worker_manager
    
    manager = await get_worker_manager()
    await manager.stop_worker(mailbox_id)
    
    return {"success": True, "message": f"Worker stopped for mailbox {mailbox_id}"}


@inbox_router.get("/workers/status")
async def get_workers_status(current_user: dict = Depends(get_current_user)):
    """Get status of all background workers"""
    from workers import get_worker_manager
    
    manager = await get_worker_manager()
    stats = manager.get_stats()
    
    return {
        "success": True,
        "stats": stats
    }


@inbox_router.get("/workers/status/{mailbox_id}")
async def get_worker_status(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get status of a specific worker"""
    from workers import get_worker_manager
    
    manager = await get_worker_manager()
    status = manager.get_worker_status(mailbox_id)
    
    if not status:
        raise HTTPException(status_code=404, detail="Worker not found for mailbox")
    
    return {
        "success": True,
        "status": status
    }


# =============================================================================
# SYSTEM STATUS ENDPOINTS
# =============================================================================

@inbox_router.get("/system/status")
async def get_system_status(current_user: dict = Depends(get_current_user)):
    """Get status of all system components"""
    from imap_manager import get_connection_manager
    from workers import get_worker_manager
    from websocket_manager import get_websocket_manager
    from redis_manager import get_redis_manager
    from webhook_manager import get_webhook_manager
    from email_cache import get_email_cache_manager
    
    # Get stats from all managers
    imap_mgr = await get_connection_manager()
    worker_mgr = await get_worker_manager()
    ws_mgr = await get_websocket_manager()
    redis_mgr = await get_redis_manager()
    webhook_mgr = await get_webhook_manager()
    cache_mgr = await get_email_cache_manager()
    
    return {
        "success": True,
        "components": {
            "imap_connection_manager": imap_mgr.get_stats(),
            "sync_workers": worker_mgr.get_stats(),
            "websocket_manager": ws_mgr.get_stats(),
            "redis_manager": redis_mgr.get_stats(),
            "webhook_manager": webhook_mgr.get_stats(),
            "email_cache_manager": cache_mgr.get_stats()
        }
    }


@inbox_router.get("/system/health")
async def get_system_health(current_user: dict = Depends(get_current_user)):
    """Quick health check for all services"""
    from imap_manager import get_connection_manager
    from websocket_manager import get_websocket_manager
    from redis_manager import get_redis_manager
    from webhook_manager import get_webhook_manager
    from email_cache import get_email_cache_manager
    
    health = {
        "status": "healthy",
        "services": {}
    }
    
    # Check IMAP
    try:
        imap_mgr = await get_connection_manager()
        imap_stats = imap_mgr.get_stats()
        health["services"]["imap"] = {
            "connected": imap_stats.get("connected", False),
            "pools": imap_stats.get("total_pools", 0),
            "active_connections": imap_stats.get("active_connections", 0)
        }
    except Exception as e:
        health["services"]["imap"] = {"error": str(e)}
        health["status"] = "degraded"
    
    # Check Email Cache
    try:
        cache_mgr = await get_email_cache_manager()
        cache_stats = cache_mgr.get_stats()
        health["services"]["email_cache"] = {
            "running": True,
            "memory_items": cache_stats.get("memory_items", 0),
            "hit_rate": f"{cache_stats.get('hit_rate', 0):.2%}"
        }
    except Exception as e:
        health["services"]["email_cache"] = {"error": str(e)}
        health["status"] = "degraded"
    
    # Check WebSocket
    try:
        ws_mgr = await get_websocket_manager()
        ws_stats = ws_mgr.get_stats()
        health["services"]["websocket"] = {
            "running": ws_stats.get("running", False),
            "connections": ws_stats.get("active_connections", 0)
        }
    except Exception as e:
        health["services"]["websocket"] = {"error": str(e)}
        health["status"] = "degraded"
    
    # Check Webhook
    try:
        webhook_mgr = await get_webhook_manager()
        webhook_stats = webhook_mgr.get_stats()
        health["services"]["webhook"] = {
            "running": True,
            "webhooks_registered": webhook_stats.get("webhooks_registered", 0),
            "total_deliveries": webhook_stats.get("total_deliveries", 0)
        }
    except Exception as e:
        health["services"]["webhook"] = {"error": str(e), "optional": True}
    
    # Check Redis
    try:
        redis_mgr = await get_redis_manager()
        redis_stats = redis_mgr.get_stats()
        health["services"]["redis"] = {
            "connected": redis_stats.get("connected", False),
            "messages_sent": redis_stats.get("messages_sent", 0),
            "messages_received": redis_stats.get("messages_received", 0)
        }
    except Exception as e:
        health["services"]["redis"] = {"error": str(e), "optional": True}
        # Redis is optional, don't mark as degraded
    
    return health


# =============================================================================
# WEBHOOK MANAGEMENT ENDPOINTS
# =============================================================================

@inbox_router.post("/webhooks/register")
async def register_webhook(
    webhook_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Register a new webhook
    
    Request body:
    - url: Webhook endpoint URL (required)
    - events: List of event types (required)
    - secret: Optional HMAC secret
    - mailbox_id: Optional mailbox filter
    """
    from webhook_manager import register_user_webhook, WebhookEventType
    
    user_id = current_user["user_id"]
    
    url = webhook_data.get("url")
    events = webhook_data.get("events", [])
    secret = webhook_data.get("secret")
    mailbox_id = webhook_data.get("mailbox_id")
    
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    
    if not events:
        raise HTTPException(status_code=400, detail="At least one event is required")
    
    # Validate events
    valid_events = [e.value for e in WebhookEventType]
    for event in events:
        if event not in valid_events:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid event type: {event}. Valid types: {valid_events}"
            )
    
    webhook_id = await register_user_webhook(
        user_id=user_id,
        url=url,
        events=events,
        secret=secret
    )
    
    # Save webhook config to user data
    from functions import read_user_data, write_user_data
    
    webhooks = read_user_data(user_id, USERS_BASE_PATH, "webhooks.json", [])
    
    webhook_config = {
        "webhook_id": webhook_id,
        "url": url,
        "events": events,
        "secret": secret,
        "mailbox_id": mailbox_id,
        "enabled": True
    }
    
    webhooks.append(webhook_config)
    write_user_data(user_id, USERS_BASE_PATH, "webhooks.json", webhooks)
    
    return {
        "success": True,
        "webhook_id": webhook_id,
        "message": "Webhook registered successfully"
    }


@inbox_router.get("/webhooks")
async def list_webhooks(current_user: dict = Depends(get_current_user)):
    """List all webhooks for the current user"""
    from functions import read_user_data
    
    user_id = current_user["user_id"]
    webhooks = read_user_data(user_id, USERS_BASE_PATH, "webhooks.json", [])
    
    return {
        "success": True,
        "webhooks": webhooks,
        "total": len(webhooks)
    }


@inbox_router.delete("/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a webhook"""
    from webhook_manager import get_webhook_manager
    from functions import read_user_data, write_user_data
    
    user_id = current_user["user_id"]
    
    manager = await get_webhook_manager()
    
    # Unregister from manager
    success = await manager.unregister_webhook(webhook_id)
    
    # Remove from user data
    webhooks = read_user_data(user_id, USERS_BASE_PATH, "webhooks.json", [])
    webhooks = [w for w in webhooks if w.get("webhook_id") != webhook_id]
    write_user_data(user_id, USERS_BASE_PATH, "webhooks.json", webhooks)
    
    return {
        "success": True,
        "message": f"Webhook {webhook_id} deleted"
    }


@inbox_router.patch("/webhooks/{webhook_id}")
async def update_webhook(
    webhook_id: str,
    updates: dict,
    current_user: dict = Depends(get_current_user)
):
    """Update webhook configuration"""
    from webhook_manager import get_webhook_manager
    from functions import read_user_data, write_user_data
    
    user_id = current_user["user_id"]
    
    manager = await get_webhook_manager()
    
    # Update in manager
    await manager.update_webhook(webhook_id, updates)
    
    # Update in user data
    webhooks = read_user_data(user_id, USERS_BASE_PATH, "webhooks.json", [])
    for w in webhooks:
        if w.get("webhook_id") == webhook_id:
            w.update(updates)
            break
    write_user_data(user_id, USERS_BASE_PATH, "webhooks.json", webhooks)
    
    return {
        "success": True,
        "message": f"Webhook {webhook_id} updated"
    }


@inbox_router.get("/webhooks/deliveries")
async def get_webhook_deliveries(
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """Get recent webhook delivery history"""
    from webhook_manager import get_webhook_manager
    
    manager = await get_webhook_manager()
    deliveries = manager.get_recent_deliveries(limit)
    
    return {
        "success": True,
        "deliveries": deliveries,
        "total": len(deliveries)
    }


@inbox_router.get("/webhooks/stats")
async def get_webhook_stats(current_user: dict = Depends(get_current_user)):
    """Get webhook manager statistics"""
    from webhook_manager import get_webhook_manager
    
    manager = await get_webhook_manager()
    stats = manager.get_stats()
    
    return {
        "success": True,
        "stats": stats
    }


# =============================================================================
# EMAIL CACHE MANAGEMENT ENDPOINTS
# =============================================================================

@inbox_router.get("/cache/status")
async def get_cache_status(current_user: dict = Depends(get_current_user)):
    """Get email cache statistics"""
    from email_cache import get_email_cache_manager
    
    cache_mgr = await get_email_cache_manager()
    stats = cache_mgr.get_stats()
    
    return {
        "success": True,
        "cache_stats": stats
    }


@inbox_router.post("/cache/clear/{mailbox_id}")
async def clear_mailbox_cache(
    mailbox_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Clear cache for a specific mailbox"""
    from email_cache import get_email_cache_manager
    
    cache_mgr = await get_email_cache_manager()
    success = await cache_mgr.clear_mailbox_cache(mailbox_id)
    
    return {
        "success": success,
        "message": f"Cache cleared for mailbox {mailbox_id}" if success else "Failed to clear cache"
    }


@inbox_router.post("/cache/clear-all")
async def clear_all_cache(current_user: dict = Depends(get_current_user)):
    """Clear all cached data"""
    from email_cache import get_email_cache_manager
    
    cache_mgr = await get_email_cache_manager()
    cleared = await cache_mgr.clear_expired(max_age=0)
    
    return {
        "success": True,
        "entries_cleared": cleared
    }


@inbox_router.get("/cache/search")
async def search_cache(
    query: str,
    mailbox_id: str = None,
    current_user: dict = Depends(get_current_user)
):
    """Search cached emails"""
    from email_cache import get_email_cache_manager
    
    cache_mgr = await get_email_cache_manager()
    results = await cache_mgr.search_cached(query, mailbox_id)
    
    return {
        "success": True,
        "query": query,
        "results": results,
        "count": len(results)
    }


@inbox_router.get("/cache/email/{mailbox_id}/{folder}/{uid}")
async def get_cached_email(
    mailbox_id: str,
    folder: str,
    uid: int,
    current_user: dict = Depends(get_current_user)
):
    """Get cached email metadata"""
    from email_cache import get_email_cache_manager
    
    cache_mgr = await get_email_cache_manager()
    cached = await cache_mgr.get_cached_email(mailbox_id, folder, uid)
    
    if cached:
        return {
            "success": True,
            "cached": True,
            "email": {
                "uid": cached.uid,
                "subject": cached.subject,
                "sender": cached.sender,
                "date": cached.date,
                "is_read": cached.is_read,
                "cached_at": cached.cached_at
            }
        }
    
    return {
        "success": True,
        "cached": False,
        "message": "Email not in cache"
    }


@inbox_router.post("/cache/email/batch")
async def cache_emails_batch(
    request_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Cache multiple emails at once
    
    Request body:
    - mailbox_id: Mailbox identifier
    - folder: Folder name
    - emails: List of email metadata dicts
    """
    from email_cache import get_email_cache_manager
    
    mailbox_id = request_data.get("mailbox_id")
    folder = request_data.get("folder")
    emails = request_data.get("emails", [])
    
    if not mailbox_id or not folder:
        raise HTTPException(status_code=400, detail="mailbox_id and folder are required")
    
    cache_mgr = await get_email_cache_manager()
    cached_count = 0
    
    for email in emails:
        success = await cache_mgr.cache_email(
            mailbox_id=mailbox_id,
            folder=folder,
            uid=email.get("uid"),
            subject=email.get("subject", ""),
            sender=email.get("sender", ""),
            date=email.get("date", ""),
            size=email.get("size", 0),
            flags=email.get("flags", ""),
            message_id=email.get("message_id", ""),
            is_read=email.get("is_read", False),
            extra_data=email
        )
        if success:
            cached_count += 1
    
    return {
        "success": True,
        "cached": cached_count,
        "total": len(emails)
    }


# =============================================================================
# VALIDATOR ROUTER
# =============================================================================

validator_router = APIRouter(prefix="/validator", tags=["Validator"])


# =============================================================================
# VALIDATOR EMAIL ENDPOINTS
# =============================================================================

@validator_router.get("/email/status")
async def get_email_validator_status(current_user: dict = Depends(get_current_user)):
    """Check if there's a running email validation process"""
    user_id = current_user["user_id"]
    
    active = get_active_validator_session(user_id, USERS_BASE_PATH, "email")

    # Nothing drives validation server-side (the UI polls), so a session left
    # mid-run by a closed tab would otherwise sit at "running" forever. If the
    # queue is drained, close it out so the panel is honest.
    if active and not active.get("queue"):
        result = await asyncio.to_thread(
            process_email_validation_batch, user_id, USERS_BASE_PATH, active["session_id"]
        )
        if result.get("completed") or result.get("success"):
            active = get_active_validator_session(user_id, USERS_BASE_PATH, "email")
    
    if active:
        return {
            "running": True,
            "session_id": active.get("session_id"),
            "status": active.get("status"),
            "list_name": active.get("list_name"),
            "progress": {
                "total": active.get("total_items"),
                "processed": active.get("processed"),
                "valid": active.get("valid"),
                "invalid": active.get("invalid"),
                "failed": active.get("failed"),
                "verified_per_minute": active.get("verified_per_minute"),
                "elapsed": active.get("elapsed_seconds", 0)
            }
        }
    
    return {"running": False}


@validator_router.get("/email/lists")
async def get_email_lists_for_validation(current_user: dict = Depends(get_current_user)):
    """Get email lists that can be used for validation.

    Lists are stored one file per list under ``emails/`` (see
    ``list_email_lists``), which is also what ``/email/start`` reads, so this
    endpoint must use the same source or the picker and the start call drift.
    """
    user_id = current_user["user_id"]
    email_lists = list_email_lists(user_id, USERS_BASE_PATH)

    return {"lists": email_lists, "total": len(email_lists)}


@validator_router.post("/email/start")
async def start_email_validation(
    request_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Start email validation process"""
    user_id = current_user["user_id"]
    
    # Check if validation already running
    active = get_active_validator_session(user_id, USERS_BASE_PATH, "email")
    if active:
        raise HTTPException(
            status_code=400,
            detail="A validation process is already running for this user. Please wait for it to complete."
        )
    
    # Get parameters. Decodo is the only transport, so the caller no longer
    # chooses a proxy pool or a browser backend.
    email_list_id = request_data.get("email_list_id")
    import_content = request_data.get("import_content")
    list_name = request_data.get("list_name")
    batch_size = request_data.get("batch_size", DEFAULT_VALIDATION_BATCH_SIZE)
    
    emails = []
    is_import = False
    
    if email_list_id:
        # Use existing email list - email lists are stored as individual files in emails directory
        email_list_data = get_email_list(user_id, USERS_BASE_PATH, email_list_id)
        if not email_list_data:
            raise HTTPException(status_code=404, detail="Email list not found")
        emails = [e.get("email") for e in email_list_data.get("emails", [])]
        list_name = email_list_data.get("name")
    
    elif import_content:
        # Create new list from import
        emails = [line.strip() for line in import_content.strip().split('\n') if line.strip()]
        is_import = True
        
        if not list_name:
            list_name = f"Import {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    
    else:
        raise HTTPException(status_code=400, detail="Either email_list_id or import_content is required")
    
    # Create session
    result = create_email_validation_session(
        user_id=user_id,
        base_path=USERS_BASE_PATH,
        emails=emails,
        list_id=email_list_id,
        list_name=list_name,
        is_import=is_import,
        batch_size=batch_size
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    
    return {
        "success": True,
        "session_id": result.get("session_id"),
        "total_emails": len(emails),
        "list_name": list_name,
        "batch_size": batch_size
    }


@validator_router.get("/email/session/{session_id}")
async def get_email_validator_session(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get email validation session details"""
    user_id = current_user["user_id"]
    session = get_validator_session(user_id, USERS_BASE_PATH, session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Only allow access if session belongs to user
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return {
        "session_id": session.get("session_id"),
        "status": session.get("status"),
        "list_name": session.get("list_name"),
        "progress": {
            "total": session.get("total_items"),
            "processed": session.get("processed"),
            "valid": session.get("valid"),
            "invalid": session.get("invalid"),
            "failed": session.get("failed"),
            "verified_per_minute": session.get("verified_per_minute"),
            "elapsed": session.get("elapsed_seconds", 0)
        },
        "queue_count": len(session.get("queue", [])),
        "current_batch": session.get("current_batch", []),
        "last_batch_results": session.get("last_batch_results", []),
        "recent_results": session.get("results", [])[-20:] if session.get("results") else []
    }


@validator_router.post("/email/validate-batch/{session_id}")
async def validate_email_batch(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Process next batch of emails"""
    user_id = current_user["user_id"]
    
    result = await process_email_validation_batch(user_id, USERS_BASE_PATH, session_id)
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    
    return result


@validator_router.post("/email/stop/{session_id}")
async def stop_email_validation(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Stop email validation process"""
    user_id = current_user["user_id"]
    
    updates = {"status": "stopped"}
    success = update_validator_progress(user_id, USERS_BASE_PATH, session_id, updates)
    
    if success:
        # Clear active session state
        clear_active_session(user_id, USERS_BASE_PATH, "email")
        return {"success": True, "message": "Validation stopped"}
    
    raise HTTPException(status_code=400, detail="Failed to stop validation")


@validator_router.post("/email/pause/{session_id}")
async def pause_email_validation(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Pause email validation process"""
    user_id = current_user["user_id"]
    
    updates = {"status": "paused"}
    success = update_validator_progress(user_id, USERS_BASE_PATH, session_id, updates)
    
    if success:
        return {"success": True, "message": "Validation paused"}
    
    raise HTTPException(status_code=400, detail="Failed to pause validation")


@validator_router.post("/email/resume/{session_id}")
async def resume_email_validation(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Resume email validation process"""
    user_id = current_user["user_id"]
    
    updates = {"status": "running"}
    success = update_validator_progress(user_id, USERS_BASE_PATH, session_id, updates)
    
    if success:
        return {"success": True, "message": "Validation resumed"}
    
    raise HTTPException(status_code=400, detail="Failed to resume validation")


# =============================================================================
# VALIDATOR PHONE ENDPOINTS
# =============================================================================

@validator_router.get("/phone/status")
async def get_phone_validator_status(current_user: dict = Depends(get_current_user)):
    """Check if there's a running phone validation process"""
    user_id = current_user["user_id"]
    
    active = get_active_validator_session(user_id, USERS_BASE_PATH, "phone")
    
    if active:
        return {
            "running": True,
            "session_id": active.get("session_id"),
            "status": active.get("status"),
            "list_name": active.get("list_name"),
            "progress": {
                "total": active.get("total_items"),
                "processed": active.get("processed"),
                "valid": active.get("valid"),
                "invalid": active.get("invalid"),
                "failed": active.get("failed"),
                "verified_per_minute": active.get("verified_per_minute"),
                "elapsed": active.get("elapsed_seconds", 0)
            }
        }
    
    return {"running": False}


@validator_router.get("/phone/lists")
async def list_phone_lists(current_user: dict = Depends(get_current_user)):
    """Get all phone number lists"""
    user_id = current_user["user_id"]
    lists = get_phone_lists(user_id, USERS_BASE_PATH)
    
    return {"lists": lists, "total": len(lists)}


@validator_router.get("/phone/lists/{list_id}")
async def get_phone_list_details(
    list_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get details of a specific phone list"""
    user_id = current_user["user_id"]
    phone_list = get_phone_list_numbers(user_id, USERS_BASE_PATH, list_id)
    
    if not phone_list:
        phone_list = get_phone_list_by_id(user_id, USERS_BASE_PATH, list_id)
    
    if not phone_list:
        raise HTTPException(status_code=404, detail="Phone list not found")
    
    return phone_list


@validator_router.delete("/phone/lists/{list_id}")
async def delete_phone_list(
    list_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a phone list"""
    user_id = current_user["user_id"]
    
    success = delete_phone_list(user_id, USERS_BASE_PATH, list_id)
    
    if success:
        return {"success": True, "message": "Phone list deleted"}
    
    raise HTTPException(status_code=400, detail="Failed to delete phone list")


@validator_router.post("/phone/import")
async def import_phone_list(
    request_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Import phone numbers and create a new phone list"""
    user_id = current_user["user_id"]
    raw_numbers = request_data.get("numbers", [])
    content = request_data.get("content", "")
    list_name = request_data.get("name") or request_data.get("list_name")
    country_code = request_data.get("country_code")

    if content and not raw_numbers:
        raw_numbers = [
            line.strip()
            for line in content.replace(',', '\n').split('\n')
            if line.strip()
        ]

    if not raw_numbers:
        raise HTTPException(status_code=400, detail="numbers or content is required")

    import_result = import_phone_numbers(
        user_id, USERS_BASE_PATH, raw_numbers, list_name, country_code
    )

    if not import_result.get("success"):
        raise HTTPException(status_code=400, detail=import_result.get("error", "Import failed"))

    numbers_for_list = []
    for entry in import_result.get("numbers", []):
        numbers_for_list.append({
            "number": entry.get("cleaned") or entry.get("original"),
            "country_code": entry.get("country_code"),
            "formatted_number": entry.get("cleaned") or entry.get("original"),
            "valid": False,
            "added_at": datetime.now().isoformat()
        })

    list_metadata = create_phone_list(
        user_id,
        USERS_BASE_PATH,
        import_result.get("list_name", list_name or "Imported List"),
        numbers_for_list,
        country_code
    )

    return {
        "success": True,
        "list": list_metadata,
        "total_numbers": import_result.get("total_numbers", len(numbers_for_list)),
        "country_codes": import_result.get("country_codes", []),
        "needs_country_confirmation": import_result.get("needs_country_confirmation", False)
    }


@validator_router.put("/phone/lists/{list_id}")
async def rename_phone_list(
    list_id: str,
    update_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Rename a phone list"""
    user_id = current_user["user_id"]
    new_name = update_data.get("name")
    
    if not new_name:
        raise HTTPException(status_code=400, detail="Name is required")
    
    success = rename_phone_list(user_id, USERS_BASE_PATH, list_id, new_name)
    
    if success:
        return {"success": True, "message": "Phone list renamed"}
    
    raise HTTPException(status_code=400, detail="Failed to rename phone list")


@validator_router.post("/phone/start")
async def start_phone_validation(
    request_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Start phone validation process"""
    user_id = current_user["user_id"]
    
    # Check if validation already running
    active = get_active_validator_session(user_id, USERS_BASE_PATH, "phone")
    if active:
        raise HTTPException(
            status_code=400,
            detail="A validation process is already running for this user. Please wait for it to complete."
        )
    
    phone_numbers = request_data.get("phone_numbers", [])
    list_id = request_data.get("list_id")
    list_name = request_data.get("list_name")
    country_code = request_data.get("country_code")
    batch_size = request_data.get("batch_size", DEFAULT_VALIDATION_BATCH_SIZE)

    list_data = None
    if list_id and not phone_numbers:
        list_data = get_phone_list_numbers(user_id, USERS_BASE_PATH, list_id)
        if not list_data:
            raise HTTPException(status_code=404, detail="Phone list not found")
        list_name = list_name or list_data.get("name")
        country_code = country_code or list_data.get("country_code")
        for entry in list_data.get("numbers", []):
            num = entry.get("number") or entry.get("formatted_number") or entry.get("phone")
            if num:
                phone_numbers.append(str(num))

    if not phone_numbers:
        raise HTTPException(status_code=400, detail="phone_numbers or list_id is required")
    
    # Keep the numbers as submitted. format_phone_number() strips the dial code
    # (and, for a bare "07069977249", even the trunk zero) which used to be
    # queued instead of the real number -- "+23469977249" for three different
    # inputs. Normalisation now happens once, in to_e164(), right before the
    # lookup, and only as a hint.
    formatted_numbers = [str(phone).strip() for phone in phone_numbers if str(phone).strip()]

    # If no country hint came with the request, infer one rather than sending
    # national digits to veriphone with no context.
    if not country_code:
        for entry in (list_data or {}).get("numbers", []):
            candidate = (entry.get("country_code") or "").strip().upper()
            if candidate:
                country_code = candidate
                break
    if not country_code:
        for candidate in formatted_numbers:
            detected, _ = format_phone_number(candidate)
            if detected and detected not in ("UNKNOWN", ""):
                country_code = detected
                break
    
    # Create session
    result = create_phone_validation_session(
        user_id=user_id,
        base_path=USERS_BASE_PATH,
        phone_numbers=formatted_numbers,
        list_name=list_name,
        country_code=country_code,
        batch_size=batch_size
    )
    
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    
    return {
        "success": True,
        "session_id": result.get("session_id"),
        "total_numbers": len(formatted_numbers),
        "list_name": list_name,
        "country_code": country_code,
        "batch_size": batch_size,
        "site": "veriphone",
        "transport": result.get("session", {}).get("transport"),
    }


@validator_router.get("/phone/session/{session_id}")
async def get_phone_validator_session(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get phone validation session details"""
    user_id = current_user["user_id"]
    session = get_validator_session(user_id, USERS_BASE_PATH, session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return {
        "session_id": session.get("session_id"),
        "status": session.get("status"),
        "list_name": session.get("list_name"),
        "site": session.get("site", "veriphone"),
        "transport": session.get("transport"),
        "batch_size": session.get("batch_size"),
        "progress": {
            "total": session.get("total_items"),
            "processed": session.get("processed"),
            "valid": session.get("valid"),
            "invalid": session.get("invalid"),
            "failed": session.get("failed"),
            "verified_per_minute": session.get("verified_per_minute"),
            "elapsed": session.get("elapsed_seconds", 0)
        },
        "queue_count": len(session.get("queue", [])),
        "current_number": session.get("current_number"),
        # Per-number detail for the live log (mirrors the email session).
        "last_batch_results": session.get("last_batch_results", [])
    }


@validator_router.post("/phone/validate/{session_id}")
async def validate_phone_number(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Process next phone number"""
    user_id = current_user["user_id"]
    
    # Runs the batch through Decodo off the event loop.
    result = await process_phone_validation(user_id, USERS_BASE_PATH, session_id)
    
    if not result.get("success") and not result.get("completed"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    
    return result


@validator_router.post("/phone/stop/{session_id}")
async def stop_phone_validation(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Stop phone validation process"""
    user_id = current_user["user_id"]
    
    updates = {"status": "stopped"}
    success = update_validator_progress(user_id, USERS_BASE_PATH, session_id, updates)
    
    if success:
        # Clear active session state
        clear_active_session(user_id, USERS_BASE_PATH, "phone")
        return {"success": True, "message": "Validation stopped"}
    
    raise HTTPException(status_code=400, detail="Failed to stop validation")


@validator_router.post("/phone/pause/{session_id}")
async def pause_phone_validation(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Pause phone validation process"""
    user_id = current_user["user_id"]
    
    updates = {"status": "paused"}
    success = update_validator_progress(user_id, USERS_BASE_PATH, session_id, updates)
    
    if success:
        return {"success": True, "message": "Validation paused"}
    
    raise HTTPException(status_code=400, detail="Failed to pause")


@validator_router.post("/phone/resume/{session_id}")
async def resume_phone_validation(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Resume phone validation process"""
    user_id = current_user["user_id"]
    
    updates = {"status": "running"}
    success = update_validator_progress(user_id, USERS_BASE_PATH, session_id, updates)
    
    if success:
        return {"success": True, "message": "Validation resumed"}
    
    raise HTTPException(status_code=400, detail="Failed to resume validation")


# =============================================================================
# VALIDATOR PROXY CONFIG ENDPOINTS
# =============================================================================

@validator_router.get("/decodo/config")
async def get_decodo_validator_config(current_user: dict = Depends(get_current_user)):
    """
    Get the user's Decodo configuration.

    The API key is never returned -- only whether one is configured and which
    key source supplies it (user config vs. server environment).
    """
    user_id = current_user["user_id"]

    from email_validator import load_decodo_settings

    settings = load_decodo_settings(user_id, USERS_BASE_PATH)
    has_key = bool(settings.get("api_key"))

    return {
        "configured": has_key,
        "api_url": settings.get("api_url"),
        "target_url": settings.get("target_url"),
        "target_type": settings.get("target_type"),
        "source": "user" if (USERS_BASE_PATH / f"fv-s-{user_id}" / "validator-config" / "decodo.json").exists() else ("environment" if has_key else None),
    }


@validator_router.get("/proxy/config")
async def get_validator_proxy_config(current_user: dict = Depends(get_current_user)):
    """Deprecated alias of GET /validator/decodo/config."""
    return await get_decodo_validator_config(current_user)


@validator_router.post("/decodo/config")
async def save_decodo_validator_config(
    config_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Save Decodo settings for this user.

    Accepted keys: ``api_key`` (write-only, encrypted at rest), ``api_url``,
    ``target_url``, ``target_type``. Omitting ``api_key`` leaves the stored
    key untouched.
    """
    user_id = current_user["user_id"]

    from email_validator import save_decodo_settings

    saved = save_decodo_settings(user_id, USERS_BASE_PATH, config_data)
    return {
        "success": True,
        "message": "Decodo configuration saved",
        **saved,
    }


@validator_router.post("/proxy/config")
async def save_validator_proxy_config(
    config_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """Deprecated alias of POST /validator/decodo/config."""
    return await save_decodo_validator_config(config_data, current_user)


@validator_router.get("/veriphone/config")
async def get_veriphone_config(current_user: dict = Depends(get_current_user)):
    """
    Get the user's veriphone API configuration.

    When an API key is present the phone validator calls the JSON API
    directly (one GET, no browser). Without one it falls back to scraping
    veriphone.io through Decodo.
    """
    user_id = current_user["user_id"]

    from email_validator import load_veriphone_settings

    settings = load_veriphone_settings(user_id, USERS_BASE_PATH)
    has_key = bool(settings.get("api_key"))
    cfg = USERS_BASE_PATH / f"fv-s-{user_id}" / "validator-config" / "veriphone.json"

    return {
        "configured": has_key,
        "api_url": settings.get("api_url"),
        "transport": "veriphone-api" if has_key else "decodo-scrape",
        "source": "user" if cfg.exists() else ("environment" if has_key else None),
    }


@validator_router.post("/veriphone/config")
async def save_veriphone_config(
    config_data: dict,
    current_user: dict = Depends(get_current_user)
):
    """
    Save a veriphone API key (write-only, encrypted at rest).

    Omitting ``api_key`` leaves the stored key untouched.
    """
    user_id = current_user["user_id"]

    from email_validator import save_veriphone_settings

    saved = save_veriphone_settings(user_id, USERS_BASE_PATH, config_data)
    return {
        "success": True,
        "message": "veriphone configuration saved",
        "transport": "veriphone-api" if saved.get("has_api_key") else "decodo-scrape",
        **saved,
    }


@validator_router.post("/veriphone/test")
async def test_veriphone_config(current_user: dict = Depends(get_current_user)):
    """Run one real API lookup to prove the key works."""
    user_id = current_user["user_id"]

    from email_validator import load_veriphone_settings, validate_phone

    settings = load_veriphone_settings(user_id, USERS_BASE_PATH)
    if not settings.get("api_key"):
        raise HTTPException(
            status_code=400,
            detail="No veriphone API key configured. Set FV_S_VERIPHONE_API_KEY or save one here.",
        )

    result = validate_phone("+14155552671", "US", settings=settings)
    return {
        "success": not result.get("error"),
        "error": result.get("error"),
        "transport": "veriphone-api",
        "status": result.get("status"),
        "valid": result.get("valid"),
        "carrier": result.get("carrier"),
        "line_type": result.get("line_type"),
    }


@validator_router.post("/decodo/test")
async def test_decodo_config(current_user: dict = Depends(get_current_user)):
    """Run one real lookup to prove the configured key and site work."""
    user_id = current_user["user_id"]

    from email_validator import load_decodo_settings, validate_one

    settings = load_decodo_settings(user_id, USERS_BASE_PATH)
    if not settings.get("api_key"):
        raise HTTPException(
            status_code=400,
            detail="No Decodo API key configured. Set FV_S_DECODO_API_KEY or save one here.",
        )

    result = validate_one("test@example.com", settings, check_dns=False)
    return {
        "success": not result.get("error"),
        "error": result.get("error"),
        "status": result.get("status"),
        "site": result.get("site"),
    }


# =============================================================================
# VALIDATOR UTILITY ENDPOINTS
# =============================================================================

@validator_router.get("/check-active")
async def check_any_validator_running(current_user: dict = Depends(get_current_user)):
    """Check if any validator (email or phone) is running"""
    user_id = current_user["user_id"]
    
    email_active = get_active_validator_session(user_id, USERS_BASE_PATH, "email")
    phone_active = get_active_validator_session(user_id, USERS_BASE_PATH, "phone")
    
    return {
        "email_running": email_active is not None,
        "phone_running": phone_active is not None,
        "any_running": email_active is not None or phone_active is not None,
        "email_session": email_active if email_active else None,
        "phone_session": phone_active if phone_active else None
    }
