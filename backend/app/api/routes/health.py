"""
Health & System Routes
"""
import platform
import psutil
from datetime import datetime
from fastapi import APIRouter, Depends
from app.core.config import settings
from app.core.auth_utils import get_current_user
from app.core.websocket_manager import manager
from app.services.llm_service import llm_service
from app.models.models import User

router = APIRouter()


def _get_llm_ready() -> bool:
    return llm_service.is_configured


def _get_active_provider() -> str:
    return llm_service.active_provider


@router.get("/health")
async def health_check():
    """Basic health check endpoint — public, no auth required."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/health/detailed")
async def detailed_health(current_user: User = Depends(get_current_user)):
    """
    Detailed system health including resource usage.
    FIX: Requires authentication — system info (CPU/memory/disk/platform) should
    not be exposed to unauthenticated callers.
    """
    try:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
    except Exception:
        cpu_percent = -1
        mem = None
        disk = None

    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "timestamp": datetime.utcnow().isoformat(),
        "system": {
            "platform": platform.system(),
            "platform_version": platform.version(),
            "python_version": platform.python_version(),
        },
        "resources": {
            "cpu_percent": cpu_percent,
            "memory_used_mb": round(mem.used / 1024 / 1024, 1) if mem else -1,
            "memory_total_mb": round(mem.total / 1024 / 1024, 1) if mem else -1,
            "memory_percent": mem.percent if mem else -1,
            "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 2) if disk else -1,
            "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 2) if disk else -1,
        },
        "websocket": {
            "active_sessions": manager.get_session_count(),
        },
        "llm_provider": settings.llm_provider,
        "gemini_configured": bool(settings.gemini_api_key),
        "openai_configured": bool(settings.openai_api_key),
        "llm_ready": _get_llm_ready(),
        "llm_active_provider": _get_active_provider(),
    }
