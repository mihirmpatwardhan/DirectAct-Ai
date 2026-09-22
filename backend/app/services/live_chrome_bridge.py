"""Connection to the user's already-running Chrome via the Chrome extension API.

This intentionally does not read cookies, passwords, or profile files. The
extension attaches to the user's active tab with Chrome's official
``chrome.debugger`` API, so the page keeps the user's real signed-in session.
"""
import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


CURRENT_EXTENSION_VERSION = "1.3.0"
REQUIRED_CAPABILITIES = frozenset({"evaluate", "navigate", "screenshot", "click", "interact"})


class LiveChromeBridge:
    def __init__(self) -> None:
        self._socket: Optional[WebSocket] = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._ready = False
        self._extension_version = ""
        self._capabilities: set[str] = set()

    @property
    def connected(self) -> bool:
        """Whether a compatible extension completed its hello handshake."""
        return self._socket is not None and self._ready

    @property
    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "handshake_complete": self._ready,
            "extension_version": self._extension_version,
            "capabilities": sorted(self._capabilities),
        }

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending.clear()

    async def serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        old_socket = self._socket
        self._socket = websocket
        self._ready = False
        self._extension_version = ""
        self._capabilities.clear()
        if old_socket is not None:
            self._fail_pending("Live Chrome bridge replaced by a newer extension connection")
            try:
                await old_socket.close(code=1012)
            except Exception:
                pass
        logger.info("Live Chrome bridge connected")
        try:
            while True:
                message = json.loads(await websocket.receive_text())
                request_id = message.get("request_id")
                if request_id and request_id in self._pending:
                    future = self._pending.pop(request_id)
                    if not future.done():
                        if message.get("ok", False):
                            future.set_result(message.get("result"))
                        else:
                            future.set_exception(RuntimeError(message.get("error", "Chrome bridge request failed")))
                elif message.get("type") == "hello":
                    version = message.get("version", "1.0.0")
                    capabilities = set(message.get("capabilities") or [])
                    self._extension_version = version
                    self._capabilities = capabilities
                    # Accept any version that has the minimum required capabilities.
                    # Only reload if critical capabilities are missing entirely.
                    # A version mismatch alone should NOT trigger a reload loop.
                    min_required = frozenset({"evaluate", "navigate", "screenshot"})
                    has_min = min_required.issubset(capabilities)
                    has_full = REQUIRED_CAPABILITIES.issubset(capabilities)
                    if not has_min:
                        logger.info(
                            "Chrome bridge missing minimum capabilities "
                            "(version=%s, capabilities=%s) — sending auto-reload",
                            version,
                            sorted(capabilities),
                        )
                        await self._send({"type": "reload"}, require_ready=False)
                    else:
                        self._ready = True
                        if not has_full:
                            logger.info(
                                "Live Chrome extension connected (v%s) with partial capabilities %s — "
                                "click/interact will use JS fallback",
                                version, sorted(capabilities),
                            )
                        else:
                            logger.info(f"Live Chrome extension fully verified (v{version})")
                        await self._send({"type": "hello_ack", "request_id": message.get("request_id")})
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            if self._socket is websocket:
                self._socket = None
                self._ready = False
                self._extension_version = ""
                self._capabilities.clear()
                self._fail_pending("Live Chrome bridge disconnected")
                logger.info("Live Chrome bridge disconnected")

    async def _send(self, message: dict[str, Any], *, require_ready: bool = True) -> None:
        if self._socket is None or (require_ready and not self.connected):
            raise RuntimeError(
                "Live Chrome is not ready. Load the DirectAct-AI Chrome extension, keep Chrome open, and wait for it to connect."
            )
        async with self._send_lock:
            await self._socket.send_json(message)

    async def request(self, command: str, payload: Optional[dict[str, Any]] = None, timeout: float = 20) -> Any:
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({
                "type": "command",
                "request_id": request_id,
                "command": command,
                "payload": payload or {},
            })
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)


live_chrome_bridge = LiveChromeBridge()
