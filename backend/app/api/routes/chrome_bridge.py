"""WebSocket endpoint used by the DirectAct-AI Chrome extension."""
from fastapi import APIRouter, WebSocket

from app.services.live_chrome_bridge import live_chrome_bridge

router = APIRouter()


@router.websocket("/ws/chrome-bridge")
async def chrome_bridge_endpoint(websocket: WebSocket):
    await live_chrome_bridge.serve(websocket)
