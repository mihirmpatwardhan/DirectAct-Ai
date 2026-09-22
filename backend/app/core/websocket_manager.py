"""
WebSocket Connection Manager — Phase 1.4 (Typed EventBus)
===========================================================
Manages real-time bidirectional communication between frontend and backend.

Updated to:
  - Emit typed event objects (from app.schemas.events) via to_ws_payload()
  - Retain all existing helper methods for backward compatibility
  - Route typed events from the internal EventBus to connected clients
"""
import json
import logging
from typing import Dict, Set, Any
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and typed event delivery per session."""

    def __init__(self):
        # session_id → set of websocket connections
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        """Accept and register a new WebSocket connection.

        Only one live socket is kept per session so React StrictMode remounts
        and reconnects cannot double-append the same stream chunks.
        """
        await websocket.accept()
        existing = self.active_connections.get(session_id)
        if existing:
            for old in list(existing):
                if old is websocket:
                    continue
                try:
                    await old.close()
                except Exception:
                    pass
                existing.discard(old)
        if session_id not in self.active_connections:
            self.active_connections[session_id] = set()
        self.active_connections[session_id].add(websocket)
        logger.info(f"WS connected: session={session_id}, conns={len(self.active_connections[session_id])}")

    def disconnect(self, websocket: WebSocket, session_id: str):
        """Remove a WebSocket connection."""
        if session_id in self.active_connections:
            self.active_connections[session_id].discard(websocket)
            if not self.active_connections[session_id]:
                del self.active_connections[session_id]
        logger.info(f"WS disconnected: session={session_id}")

    # ────────────────────────────────────────────────────────────────────────
    # Core: send any dict payload to all connections in a session
    # ────────────────────────────────────────────────────────────────────────

    async def send_to_session(self, session_id: str, message: dict):
        """Broadcast a message dict to all connections in a session."""
        if session_id not in self.active_connections:
            return
        dead: set = set()
        for ws in self.active_connections[session_id]:
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.warning(f"WS send failed: {e}")
                dead.add(ws)
        for ws in dead:
            self.active_connections[session_id].discard(ws)

    async def emit_event(self, session_id: str, event) -> None:
        """Emit a typed BaseEvent to a session. Serializes via to_ws_payload()."""
        try:
            await self.send_to_session(session_id, event.to_ws_payload())
        except Exception as e:
            logger.error(f"EventBus emit failed for {getattr(event, 'type', '?')}: {e}")

    # ────────────────────────────────────────────────────────────────────────
    # Typed convenience senders (backward compat + typed wrappers)
    # ────────────────────────────────────────────────────────────────────────

    async def send_text_chunk(self, session_id: str, chunk: str, message_id: str):
        """Send a streaming text chunk — maps to TextChunkEvent."""
        await self.send_to_session(session_id, {
            "type": "text_chunk",
            "message_id": message_id,
            "content": chunk,
        })

    async def send_stream_end(self, session_id: str, message_id: str):
        """Signal end of streaming — maps to StreamEndEvent."""
        await self.send_to_session(session_id, {
            "type": "stream_end",
            "message_id": message_id,
        })

    async def send_status(self, session_id: str, status: str, data: dict = None):
        """Send a system_status event."""
        await self.send_to_session(session_id, {
            "type": "system_status",
            "status": status,
            "details": data or {},
        })

    async def send_action_update(self, session_id: str, action: dict):
        """Send a legacy action status update (for backward compat with Timeline panel)."""
        await self.send_to_session(session_id, {
            "type": "action_update",
            "action": action,
        })

    async def send_error(self, session_id: str, error: str, details: str = ""):
        """Send an error_occurred event."""
        await self.send_to_session(session_id, {
            "type": "error_occurred",
            "error_code": "GENERAL_ERROR",
            "message": error,
            "details": details,
            "recoverable": True,
        })

    async def send_security_event(self, session_id: str, event_dict: dict):
        """Forward a security pipeline event directly (used by the Guard chain)."""
        await self.send_to_session(session_id, event_dict)

    # ────────────────────────────────────────────────────────────────────────
    # Stats
    # ────────────────────────────────────────────────────────────────────────

    def get_session_count(self) -> int:
        return len(self.active_connections)

    def get_connection_count(self, session_id: str) -> int:
        return len(self.active_connections.get(session_id, set()))

    def is_connected(self, session_id: str) -> bool:
        return bool(self.active_connections.get(session_id))


# Singleton instance
manager = ConnectionManager()
