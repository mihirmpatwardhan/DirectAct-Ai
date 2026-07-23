"""
DirectAct-AI — FastAPI Application Entry Point
"""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.core.config import settings
from app.core.database import init_db
from app.api.router import api_router
from app.api.routes.websocket import router as ws_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — runs setup on startup, teardown on shutdown."""
    # Startup
    logger.info(f"🚀 Starting {settings.app_name} v{settings.app_version}")
    os.makedirs(os.path.dirname(settings.audit_log_path), exist_ok=True)
    os.makedirs(settings.screenshot_path, exist_ok=True)
    await init_db()
    logger.info("✅ Database initialized")
    logger.info(f"🌐 CORS origins: {settings.cors_origins}")
    logger.info(f"🤖 LLM provider: {settings.llm_provider}")
    yield
    # Shutdown
    logger.info(f"🛑 Shutting down {settings.app_name}")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "DirectAct-AI — Define. Direct. Done.\n\n"
        "AI Copilot for Web & Desktop Automation with Security-First Architecture."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(api_router)
app.include_router(ws_router)

# Root redirect to health
@app.get("/", tags=["root"])
async def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "health": "/api/v1/health",
    }
