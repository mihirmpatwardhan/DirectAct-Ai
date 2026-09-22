"""
API Router — aggregates all route modules
"""
from fastapi import APIRouter
from app.api.routes import health, sessions, chat, actions, auth, chrome

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router)
api_router.include_router(chrome.router)
api_router.include_router(sessions.router)
api_router.include_router(chat.router)
api_router.include_router(actions.router)
