"""
Desktop Agent Client — Backend-Side IPC Bridge
================================================
Manages a single persistent desktop_agent_broker.py subprocess that lives in
the interactive Windows desktop session.  All UI automation commands are sent
over stdin/stdout JSON so the broker (which has desktop access) does the
actual work.

Usage (async):
    from app.services.desktop_agent_client import desktop_client

    result = await desktop_client.send(cmd="launch", target="notepad")
    snap   = await desktop_client.snapshot()
    img    = await desktop_client.screenshot()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BROKER_SCRIPT = os.path.join(os.path.dirname(__file__), "desktop_agent_broker.py")


class DesktopAgentClient:
    """
    Async client for the interactive-session desktop automation broker.

    The broker subprocess is started on first use and kept alive for the
    lifetime of the backend process.  A per-command asyncio.Lock prevents
    interleaved I/O.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def _ensure_started(self) -> bool:
        """Start the broker if not already running.  Returns True on success."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            # Launch broker as a regular interactive process so it inherits the
            # user's desktop window station.  We pipe stdin/stdout; stderr goes
            # to DEVNULL (broker logs to stderr but we don't need it here).
            # Uvicorn's Windows reload loop can run on a selector event loop,
            # where asyncio subprocess pipes raise NotImplementedError. Use
            # the normal Windows process API and perform the small blocking I/O
            # operations in a worker thread instead.
            self._proc = await asyncio.to_thread(
                subprocess.Popen,
                [sys.executable, _BROKER_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=0,
            )
            # Wait for the broker's "ready" line
            try:
                ready_line = await asyncio.wait_for(
                    asyncio.to_thread(self._proc.stdout.readline), timeout=15.0
                )
                data = json.loads(ready_line.decode().strip())
                if not data.get("ok"):
                    raise RuntimeError(f"Broker unhealthy: {data}")
                logger.info("Desktop broker ready (pid=%s)", self._proc.pid)
            except asyncio.TimeoutError:
                logger.error("Desktop broker did not send ready signal in 15s")
                await self._kill()
                return False
            self._started = True
            return True
        except Exception as exc:
            logger.error("Failed to start desktop broker: %s", exc)
            self._proc = None
            return False

    async def _kill(self):
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            try:
                if proc.poll() is None:
                    await asyncio.to_thread(proc.kill)
                    await asyncio.to_thread(proc.wait, 3)
            except Exception:
                pass

    async def _restart(self) -> bool:
        logger.warning("Desktop broker died — restarting")
        await self._kill()
        return await self._ensure_started()

    async def start(self) -> bool:
        """Ensure the broker is available without issuing a desktop action."""
        async with self._lock:
            return await self._ensure_started()

    async def keep_alive(self, retry_seconds: float = 5.0) -> None:
        """Keep the desktop worker ready and recreate it after an unexpected exit."""
        while True:
            try:
                await self.start()
            except Exception as exc:
                logger.warning("Desktop broker health check failed: %s", exc)
            await asyncio.sleep(retry_seconds)

    # ── core send ─────────────────────────────────────────────────────────────

    async def send(self, cmd: str, target: str = "", value: str = "",
                   timeout: float = 30.0, **extra) -> dict:
        """Send one command to the broker; return its response dict."""
        async with self._lock:
            for attempt in range(2):
                ok = await self._ensure_started()
                if not ok:
                    return {"ok": False, "msg": "Desktop broker unavailable", "rect": None}

                req_id = str(uuid.uuid4())[:8]
                req = {"id": req_id, "cmd": cmd, "target": target, "value": value, **extra}
                line = json.dumps(req) + "\n"

                try:
                    def write_request() -> None:
                        assert self._proc is not None and self._proc.stdin is not None
                        self._proc.stdin.write(line.encode())
                        self._proc.stdin.flush()

                    await asyncio.to_thread(write_request)
                except Exception as exc:
                    if attempt == 0:
                        await self._restart()
                        continue
                    return {"ok": False, "msg": f"Broker write failed: {exc}", "rect": None}

                try:
                    resp_line = await asyncio.wait_for(
                        asyncio.to_thread(self._proc.stdout.readline), timeout=timeout
                    )
                    if not resp_line:
                        raise EOFError("Broker stdout closed")
                    resp = json.loads(resp_line.decode().strip())
                    return resp
                except (asyncio.TimeoutError, EOFError, json.JSONDecodeError) as exc:
                    if attempt == 0:
                        await self._restart()
                        continue
                    return {"ok": False, "msg": f"Broker read failed: {exc}", "rect": None}

            return {"ok": False, "msg": "Desktop broker: max retries exceeded", "rect": None}

    # ── convenience wrappers ──────────────────────────────────────────────────

    async def launch(self, target: str) -> dict:
        return await self.send("launch", target=target, timeout=20.0)

    async def focus(self, target: str) -> dict:
        return await self.send("focus", target=target, timeout=10.0)

    async def click(self, target: str) -> dict:
        return await self.send("click", target=target, timeout=15.0)

    async def type_text(self, target: str, value: str) -> dict:
        return await self.send("type", target=target, value=value, timeout=20.0)

    async def keys(self, target: str, value: str) -> dict:
        return await self.send("keys", target=target, value=value, timeout=10.0)

    async def wait(self, seconds: float) -> dict:
        return await self.send("wait", target=str(seconds), timeout=seconds + 5)

    async def snapshot(self) -> str:
        resp = await self.send("snapshot", timeout=15.0)
        return resp.get("snapshot") or "(No snapshot)"

    async def screenshot(self, highlight: Optional[dict] = None) -> Optional[str]:
        resp = await self.send("screenshot", rect=highlight, timeout=20.0)
        return resp.get("screenshot")

    async def ping(self) -> bool:
        resp = await self.send("ping", timeout=5.0)
        return bool(resp.get("ok"))

    async def close(self):
        async with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    proc = self._proc

                    def send_exit() -> None:
                        assert proc.stdin is not None
                        proc.stdin.write(b'{"id":"bye","cmd":"exit"}\n')
                        proc.stdin.flush()

                    await asyncio.to_thread(send_exit)
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            await self._kill()


# Singleton — shared across all requests
desktop_client = DesktopAgentClient()
