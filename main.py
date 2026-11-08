"""
FV-S Admin Panel Backend - FastAPI Server
Automatic module installation and server initialization
"""

import uvicorn
import socket
import sys
import subprocess
import time
import os
import logging

# =============================================================================
# AUTOMATIC MODULE INSTALLATION
# =============================================================================

# pip package name -> Python import module name (they differ for some packages)
REQUIRED_PACKAGES = {
    "fastapi": "fastapi",
    "uvicorn[standard]": "uvicorn",
    "starlette": "starlette",
    "python-multipart": "multipart",
    "jinja2": "jinja2",
    "aiosqlite": "aiosqlite",
    "sqlalchemy": "sqlalchemy",
    "aioimaplib": "aioimaplib",
    "redis": "redis",
    "aiohttp": "aiohttp",
    "python-jose[cryptography]": "jose",
    "passlib[bcrypt]": "passlib",
    "pydantic": "pydantic",
    "email-validator": "email_validator",
    "requests": "requests",
    "dnspython": "dns",
}

def install_missing_packages():
    """Check and install missing required packages"""
    print("=" * 60)
    print("FV-S Admin Panel - Checking Dependencies")
    print("=" * 60)
    
    missing = []
    
    for install_name, import_name in REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
            print(f"[OK] {install_name}")
        except ImportError:
            missing.append(install_name)
            print(f"[MISSING] {install_name} - will install")
    
    if missing:
        print("\n" + "=" * 60)
        print(f"Installing {len(missing)} missing package(s)...")
        print("=" * 60)
        
        for package in missing:
            print(f"Installing: {package}")
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", package, "-q"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                print(f"  [OK] {package} installed")
            except Exception as e:
                print(f"  [ERROR] Failed to install {package}: {e}")
        
        print("\n" + "=" * 60)
        print("Dependency check complete!")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("All dependencies installed!")
        print("=" * 60)

def ensure_dependencies():
    """Run dependency check once per process tree (avoids duplicate checks under uvicorn reload)."""
    if os.environ.get("FV_S_DEPS_CHECKED") == "1":
        return
    install_missing_packages()
    os.environ["FV_S_DEPS_CHECKED"] = "1"


ensure_dependencies()

# Now import the rest
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from config import config, USERS_BASE_PATH
from database import DatabaseManager
from api import (
    router,
    smtp_router,
    api_router,
    auth_router,
    user_router,
    inbox_router,
    validator_router,
    ws_router
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    DatabaseManager.init()
    print(f"Database initialized: {DatabaseManager.get_db_path()}")

    # Seed the default admin on first run, then recover any campaign sessions
    # that were left "running"/"paused" by a previous process crash.
    from functions import ensure_default_admin, recover_interrupted_sessions
    ensure_default_admin(USERS_BASE_PATH)
    recovered = recover_interrupted_sessions(USERS_BASE_PATH)
    if recovered:
        print(f"Recovered {recovered} interrupted campaign session(s)")

    from imap_manager import get_connection_manager
    await get_connection_manager()
    print("IMAP Connection Manager initialized")

    from workers import get_worker_manager
    await get_worker_manager()
    print("Sync Manager initialized")

    from websocket_manager import get_websocket_manager
    ws_manager = await get_websocket_manager()
    print("WebSocket Manager initialized")

    from redis_manager import get_redis_manager, initialize_event_bridge
    redis_mgr = await get_redis_manager()
    if redis_mgr.is_connected:
        print("Redis Manager initialized")
        await initialize_event_bridge(ws_manager)
        print("Event Bridge initialized")
    else:
        print("Redis unavailable - running without pub/sub event bridge")

    from webhook_manager import get_webhook_manager
    await get_webhook_manager()
    print("Webhook Manager initialized")

    from email_cache import get_email_cache_manager
    await get_email_cache_manager()
    print("Email Cache Manager initialized")

    yield

    from email_cache import shutdown_email_cache_manager, shutdown_user_cache_managers
    await shutdown_user_cache_managers()
    print("Per-user Email Cache Managers shutdown complete")
    await shutdown_email_cache_manager()
    print("Email Cache Manager shutdown complete")

    from webhook_manager import shutdown_webhook_manager
    await shutdown_webhook_manager()
    print("Webhook Manager shutdown complete")

    from redis_manager import shutdown_redis_manager
    await shutdown_redis_manager()
    print("Redis Manager shutdown complete")

    from workers import shutdown_worker_manager
    await shutdown_worker_manager()
    print("Sync Manager shutdown complete")

    from websocket_manager import shutdown_websocket_manager
    await shutdown_websocket_manager()
    print("WebSocket Manager shutdown complete")

    from imap_manager import shutdown_connection_manager
    await shutdown_connection_manager()
    print("IMAP Connection Manager shutdown complete")


# Initialize FastAPI app
app = FastAPI(
    title="FV-S Admin Panel API",
    description="Backend API for FV-S Admin Panel",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS Middleware - allow all origins for cross-domain requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    # Authentication is via the "Authorization: Bearer ..." header (not
    # cookies), so credentialed CORS is not required. allow_credentials=True
    # combined with allow_origins=["*"] is an invalid combination that
    # browsers reject, so it is kept off for permissive origins.
    allow_credentials=bool(config.cors_credentials),
    allow_methods=config.cors_methods,
    allow_headers=config.cors_headers,
    expose_headers=["*"],
)

# Include all API routers
app.include_router(router)
app.include_router(smtp_router)
app.include_router(api_router)
app.include_router(auth_router)
app.include_router(user_router)
app.include_router(inbox_router)
app.include_router(validator_router)
app.include_router(ws_router)


def init_database():
    """Initialize database on server startup"""
    db_path = DatabaseManager.get_db_path()
    DatabaseManager.init(db_path)
    print(f"Database initialized: {db_path}")


def is_port_in_use(port):
    """Check if a port is in use"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', port))
    sock.close()
    return result == 0


def kill_process_on_port(port):
    """Kill process on port (Windows)"""
    try:
        result = subprocess.run(
            f'netstat -ano | findstr :{port}',
            shell=True,
            capture_output=True,
            text=True
        )
        if result.stdout:
            for line in result.stdout.strip().split('\n'):
                if 'LISTENING' in line:
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part == 'LISTENING' and i > 0:
                            pid = parts[i - 1]
                            print(f"Killing process {pid} on port {port}...")
                            subprocess.run(f'taskkill /PID {pid} /F', shell=True)
                            time.sleep(1)
                            return True
    except Exception:
        pass
    return False


def find_available_port(start_port):
    """Find an available port starting from start_port"""
    for port in range(start_port, start_port + 10):
        if not is_port_in_use(port):
            return port
    return None


if __name__ == "__main__":
    port = config.port
    
    # Check if port is in use
    if is_port_in_use(port):
        print(f"Port {port} is in use")
        response = input("Kill existing server and restart? (y/n): ").lower().strip()
        if response == 'y':
            kill_process_on_port(port)
            time.sleep(2)
        else:
            print("Exiting...")
            sys.exit(0)
    
    # Verify port is now free
    if is_port_in_use(port):
        port = find_available_port(8001)
        if not port:
            print("ERROR: No available port found")
            sys.exit(1)
        print(f"Using port {port}")
    
    print(f"FV-S Admin Panel Backend")
    print(f"Server running on http://{config.host}:{port}")
    print(f"API Docs: http://{config.host}:{port}/docs")
    print(f"Users: {USERS_BASE_PATH}")
    
    # Initialize database on startup
    init_database()
    
    uvicorn.run(
        "main:app",
        host=config.host,
        port=port,
        reload=False,
        log_level="warning",
        use_colors=False
    )
