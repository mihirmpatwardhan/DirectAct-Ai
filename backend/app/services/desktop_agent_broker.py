"""
Desktop Agent Broker — Interactive-Session Worker
===================================================
This script is launched as a **visible foreground process** by the backend so it
runs inside the user's interactive Windows desktop session.  All pywinauto /
Win32 accessibility calls live here; the backend communicates via newline-
delimited JSON on stdin/stdout.

Protocol
--------
Request  (backend → broker, one JSON line):
  {"id": "<str>", "cmd": "<action>", "target": "<str>", "value": "<str>", "task": "<str>"}

Response (broker → backend, one JSON line per request):
  {"id": "<str>", "ok": true|false, "msg": "<str>", "rect": <dict|null>, "snapshot": "<str|null>", "screenshot": "<str|null>"}

Commands
--------
  launch    – launch an application by name
  focus     – bring a window to the front
  click     – click a UIA control
  type      – type text into a window / control
  keys      – send raw keystrokes
  wait      – pause N seconds (target = seconds as string)
  snapshot  – return the accessibility snapshot text
  screenshot– return a base64 JPEG screenshot
  ping      – heartbeat (always ok=true)
  exit      – shut down gracefully

All errors are caught; the broker never crashes — it returns ok=false with an
error message so the backend can decide what to do next.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Optional

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("broker")

# ─── Interactive desktop attachment ───────────────────────────────────────────
# When launched as a subprocess from a non-interactive process (e.g. uvicorn),
# the thread is attached to the wrong window station / desktop and cannot
# enumerate or interact with user windows.  Explicitly switching to the
# "Default" input desktop gives us access to all visible windows.
def _attach_interactive_desktop() -> bool:
    try:
        import ctypes
        user32 = ctypes.windll.user32
        GENERIC_ALL = 0x10000000
        desk = user32.OpenDesktopW("Default", 0, False, GENERIC_ALL)
        if desk:
            result = user32.SetThreadDesktop(desk)
            if result:
                logger.debug("Attached to interactive desktop")
                return True
    except Exception as exc:
        logger.debug("SetThreadDesktop failed: %s", exc)
    return False

_attach_interactive_desktop()

# ─── pywinauto import ──────────────────────────────────────────────────────────
try:
    from pywinauto import Desktop
    from pywinauto.keyboard import send_keys
    _HAS_PYWINAUTO = True
except ImportError:
    _HAS_PYWINAUTO = False
    logger.warning("pywinauto not available – desktop automation disabled")

# ─── PIL import ────────────────────────────────────────────────────────────────
try:
    from PIL import ImageGrab, ImageDraw
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

_EDIT_CONTROL_TYPES = {"edit", "document", "datagrid", "text"}


def _get_desktop(backend: str = "uia"):
    if not _HAS_PYWINAUTO:
        raise RuntimeError("pywinauto not installed")
    try:
        return Desktop(backend=backend)
    except Exception:
        return Desktop(backend="win32")


def _control_process_name(control: Any) -> str:
    try:
        pid = int(control.element_info.process_id)
    except Exception:
        return ""
    try:
        import psutil
        return (psutil.Process(pid).name() or "").lower()
    except Exception:
        return ""


def _desktop_text(control: Any) -> str:
    values: list[str] = []
    for getter in (
        lambda: control.element_info.name,
        lambda: control.element_info.automation_id,
        lambda: control.element_info.control_type,
    ):
        try:
            v = str(getter() or "").strip()
        except Exception:
            v = ""
        if v and v not in values:
            values.append(v)
    if not values:
        for getter in (lambda: control.window_text(), lambda: control.friendly_class_name()):
            try:
                v = str(getter() or "").strip()
            except Exception:
                v = ""
            if v and v not in values:
                values.append(v)
    return " | ".join(values)


def _desktop_rect(control: Any) -> Optional[dict]:
    try:
        r = control.rectangle()
        left, top, right, bottom = int(r.left), int(r.top), int(r.right), int(r.bottom)
        if right <= left or bottom <= top:
            return None
        return {"left": left, "top": top, "right": right, "bottom": bottom,
                "width": right - left, "height": bottom - top}
    except Exception:
        return None


def _walk_controls(root: Any, max_depth: int = 8, max_seen: int = 280):
    yield root
    queue: list[tuple[Any, int]] = [(root, 0)]
    seen = 0
    while queue and seen < max_seen:
        parent, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = parent.children()
        except Exception:
            children = []
        for control in children:
            seen += 1
            yield control
            if seen >= max_seen:
                break
            queue.append((control, depth + 1))


def _desktop_windows(visible_only: bool = True):
    """Return all top-level windows using BOTH uia and win32 backends."""
    seen_handles: set = set()
    for backend_name in ("uia", "win32"):
        try:
            d = Desktop(backend=backend_name)
            for w in d.windows(visible_only=visible_only):
                try:
                    h = int(w.handle)
                except Exception:
                    h = id(w)
                if h not in seen_handles:
                    seen_handles.add(h)
                    yield w
        except Exception:
            continue


def _force_foreground(control: Any) -> None:
    try:
        control.restore()
    except Exception:
        pass
    try:
        control.set_focus()
    except Exception:
        pass
    hwnd = 0
    try:
        hwnd = int(control.handle)
    except Exception:
        hwnd = 0
    if not hwnd:
        return
    try:
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        u32.ShowWindow(hwnd, 9)
        u32.BringWindowToTop(hwnd)
        if u32.SetForegroundWindow(hwnd):
            return
        fg = u32.GetForegroundWindow()
        cur = k32.GetCurrentThreadId()
        fg_t = u32.GetWindowThreadProcessId(fg, None)
        tgt_t = u32.GetWindowThreadProcessId(hwnd, None)
        u32.AttachThreadInput(cur, fg_t, True)
        u32.AttachThreadInput(cur, tgt_t, True)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(cur, tgt_t, False)
        u32.AttachThreadInput(cur, fg_t, False)
    except Exception:
        pass


def _find_window(target: str) -> tuple[Any | None, Optional[dict]]:
    needle = re.sub(r"\s+(?:application|app)$", "", (target or "").strip().strip("\"'").lower()).strip()
    if not needle:
        return None, None
    best: tuple[int, Any, Optional[dict]] | None = None
    for window in _desktop_windows():
        try:
            title = (window.window_text() or "").lower()
        except Exception:
            title = ""
        text = _desktop_text(window).lower()
        blob = f"{title} {text}"
        if not blob.strip():
            continue
        if title == needle or text == needle:
            score = 0
        elif needle in title:
            score = 1
        elif needle in text:
            score = 2
        else:
            continue
        rect = _desktop_rect(window)
        if best is None or score < best[0]:
            best = (score, window, rect)
        if score == 0:
            break
    if not best:
        return None, None
    return best[1], best[2]


def _wait_for_window(target: str, timeout_s: float = 10.0) -> tuple[Any | None, Optional[dict]]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        window, rect = _find_window(target)
        if window is not None:
            _force_foreground(window)
            return window, rect
        time.sleep(0.35)
    return None, None


def _find_edit_control(window: Any) -> tuple[Any | None, Optional[dict]]:
    for control in _walk_controls(window):
        role = ""
        try:
            role = str(control.element_info.control_type or "").lower()
        except Exception:
            role = ""
        if role not in _EDIT_CONTROL_TYPES:
            continue
        rect = _desktop_rect(control)
        if rect and rect["width"] > 40 and rect["height"] > 16:
            return control, rect
    return None, None


def _escape_send_keys(text: str) -> str:
    return re.sub(r"([+^%~{}])", r"{\1}", text or "")


def _win32_click(x: int, y: int) -> bool:
    """Perform a left-click at screen coordinates using Win32 mouse events.
    Works even when pywinauto click methods raise ComError / element not visible."""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        # Move cursor
        u32.SetCursorPos(x, y)
        time.sleep(0.05)
        # Mouse down + up  (MOUSEEVENTF_LEFTDOWN=0x0002, LEFTUP=0x0004)
        u32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        u32.mouse_event(0x0004, 0, 0, 0, 0)
        return True
    except Exception as exc:
        logger.debug("win32_click failed: %s", exc)
        return False


def _center(rect: Optional[dict]) -> tuple[int, int]:
    """Return screen center of a rect dict."""
    if not rect:
        return (0, 0)
    return (
        rect["left"] + rect["width"] // 2,
        rect["top"] + rect["height"] // 2,
    )


def _find_control(target: str, root: Any | None = None) -> tuple[Any | None, Optional[dict]]:
    needle = (target or "").strip().strip("\"'").lower()
    if not needle:
        return None, None
    candidates: list[tuple[int, Any, Optional[dict]]] = []
    source = _walk_controls(root) if root is not None else _iter_all_controls()
    for control in source:
        rect = _desktop_rect(control)
        if not rect:
            continue
        text = _desktop_text(control).lower()
        if not text:
            continue
        if text == needle:
            score = 0
        elif needle in text:
            score = 1
        elif any(p and len(p) >= 3 and p in text for p in needle.split()):
            score = 2
        else:
            continue
        candidates.append((score, control, rect))
        if score == 0:
            break
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]


def _iter_all_controls():
    for window in _desktop_windows():
        yield from _walk_controls(window, max_depth=4, max_seen=100)


def _type_into_window(window: Any, value: str) -> tuple[bool, str, Optional[dict]]:
    _force_foreground(window)
    edit, rect = _find_edit_control(window)
    if edit is not None:
        try:
            edit.set_focus()
        except Exception:
            pass
        try:
            edit.set_edit_text(value)
            return True, "Typed into document", rect
        except Exception:
            try:
                edit.type_keys(_escape_send_keys(value), with_spaces=True, pause=0.01)
                return True, "Typed into document", rect
            except Exception:
                pass
    send_keys(_escape_send_keys(value), with_spaces=True, pause=0.01)
    return True, "Typed into focused window", _desktop_rect(window)


def _launch(target: str) -> tuple[bool, str]:
    """Launch any desktop app or open any URL dynamically."""
    clean = re.sub(r"^the\s+", "", (target or "").strip(), flags=re.IGNORECASE)
    clean = re.sub(r"\s+(?:application|app)$", "", clean, flags=re.IGNORECASE).strip()
    if not clean:
        return False, "No application name or URL provided"

    # 0. URL detection -> universal os.startfile
    is_url = bool(re.match(r"^https?://", clean, re.I) or re.match(r"^www\.", clean, re.I) or re.search(r"\.[a-zA-Z]{2,}(?:/.*)?$", clean))
    if is_url:
        url = clean if clean.startswith("http") else f"https://{clean}"
        try:
            os.startfile(url)
            return True, f"Opened URL {url}"
        except Exception as exc:
            logger.debug("os.startfile url failed: %s", exc)

    # 1. App discovery (dynamic registry + Start Menu + AppData scan)
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from app.services.app_discovery import app_discovery
        resolved = app_discovery.resolve(clean)
        if resolved and resolved.app_user_model_id:
            safe = resolved.app_user_model_id.replace("'", "''")
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"Start-Process 'shell:AppsFolder\\{safe}'"],
                capture_output=True, text=True, timeout=15,
            )
            if res.returncode == 0:
                return True, f"Launched {resolved.name}"
        if resolved and resolved.executable_path:
            exe = resolved.executable_path
            if exe.lower().endswith(".exe") and os.path.exists(exe):
                subprocess.Popen([exe])
                return True, f"Launched {resolved.name}"
    except Exception as exc:
        logger.debug("app_discovery failed: %s", exc)

    # 2. Start-Process (handles most apps)
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Start-Process '{clean}' -ErrorAction Stop"],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode == 0:
            return True, f"Launched {clean}"
    except Exception:
        pass

    # 3. Shell execute fallback
    try:
        proc = subprocess.Popen(clean, shell=True)
        time.sleep(0.3)
        if proc.poll() is None:
            return True, f"Launched {clean}"
    except Exception as exc:
        return False, f"Could not launch {clean}: {exc}"

    return False, f"Windows could not find an application named {clean}"


def _snapshot(root: Any | None = None) -> str:
    try:
        lines: list[str] = []
        source = _walk_controls(root) if root is not None else _iter_all_controls()
        for control in source:
            rect = _desktop_rect(control)
            text = _desktop_text(control)
            if not rect or not text:
                continue
            role = ""
            try:
                role = str(control.element_info.control_type or "")
            except Exception:
                pass
            lines.append(
                f"- {text[:180]} [{role}] "
                f"rect=({rect['left']},{rect['top']},{rect['width']},{rect['height']})"
            )
            if len(lines) >= 180:
                break
        return "\n".join(lines)[:18_000] or "(No visible accessible controls found)"
    except Exception as exc:
        return f"(Snapshot unavailable: {exc})"


def _screenshot(highlight: Optional[dict] = None) -> Optional[str]:
    if not _HAS_PIL:
        return None
    try:
        image = ImageGrab.grab(all_screens=True)
        if highlight:
            try:
                import ctypes
                u32 = ctypes.windll.user32
                vx = int(u32.GetSystemMetrics(76))
                vy = int(u32.GetSystemMetrics(77))
            except Exception:
                vx, vy = 0, 0
            x1 = highlight["left"] - vx
            y1 = highlight["top"] - vy
            x2 = highlight["right"] - vx
            y2 = highlight["bottom"] - vy
            draw = ImageDraw.Draw(image)
            draw.rectangle((x1, y1, x2, y2), outline=(255, 55, 95), width=5)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), outline=(255, 235, 80), width=4)
            draw.line((cx - 20, cy, cx + 20, cy), fill=(255, 235, 80), width=2)
            draw.line((cx, cy - 20, cx, cy + 20), fill=(255, 235, 80), width=2)
        max_w = 1280
        if image.width > max_w:
            ratio = max_w / image.width
            image = image.resize((max_w, max(1, int(image.height * ratio))))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=70, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        logger.debug("Screenshot failed: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Command dispatcher
# ══════════════════════════════════════════════════════════════════════════════

# Track the last focused window per session (single process, so global is fine)
_active_window: Any | None = None


def _dispatch(req: dict) -> dict:
    global _active_window
    rid = req.get("id", "")
    cmd = (req.get("cmd") or "").lower().strip()
    target = (req.get("target") or "").strip()
    value = (req.get("value") or "").strip()

    def _ok(msg: str = "", rect: Optional[dict] = None, **extra) -> dict:
        return {"id": rid, "ok": True, "msg": msg, "rect": rect, **extra}

    def _err(msg: str, rect: Optional[dict] = None) -> dict:
        return {"id": rid, "ok": False, "msg": msg, "rect": rect}

    try:
        # ── ping ──────────────────────────────────────────────────────────────
        if cmd == "ping":
            return _ok("pong")

        # ── exit ──────────────────────────────────────────────────────────────
        if cmd == "exit":
            return _ok("bye")

        # ── launch ────────────────────────────────────────────────────────────
        if cmd == "launch":
            ok, msg = _launch(target)
            if ok:
                # Wait for window to appear
                window, rect = _wait_for_window(target, timeout_s=10.0)
                if window is not None:
                    _active_window = window
                    return _ok(msg, rect)
                # Window not yet visible but process launched — still ok
                return _ok(f"{msg} (window not yet visible)", None)
            return _err(msg)

        # ── focus ─────────────────────────────────────────────────────────────
        if cmd == "focus":
            window, rect = _find_window(target)
            if window is None:
                return _err(f"Window not found: {target}")
            _force_foreground(window)
            _active_window = window
            return _ok(f"Focused {target}", rect)

        # ── snapshot ──────────────────────────────────────────────────────────
        if cmd == "snapshot":
            snap = _snapshot(_active_window)
            return _ok("snapshot", snapshot=snap)

        # ── screenshot ────────────────────────────────────────────────────────
        if cmd == "screenshot":
            highlight = req.get("rect") if req.get("rect") else None
            img = _screenshot(highlight)
            return _ok("screenshot", screenshot=img)

        # ── click ─────────────────────────────────────────────────────────────
        if cmd == "click":
            control, rect = _find_control(target, _active_window)
            if control is None:
                control, rect = _find_control(target)  # global fallback
            if control is None:
                return _err(f"Control not found: {target}")
            _force_foreground(_active_window or control)
            time.sleep(0.1)
            # Strategy 1: UIA Invoke pattern (works for buttons/menu items)
            clicked = False
            try:
                control.invoke()
                clicked = True
            except Exception:
                pass
            # Strategy 2: pywinauto click_input (simulated mouse)
            if not clicked:
                try:
                    control.click_input()
                    clicked = True
                except Exception:
                    pass
            # Strategy 3: pywinauto click (legacy)
            if not clicked:
                try:
                    control.click()
                    clicked = True
                except Exception:
                    pass
            # Strategy 4: Win32 coordinate-based mouse click
            if not clicked and rect:
                cx, cy = _center(rect)
                clicked = _win32_click(cx, cy)
            if not clicked:
                return _err(f"All click methods failed for: {target}", rect)
            return _ok(f"Clicked {target}", rect)

        # ── type ──────────────────────────────────────────────────────────────
        if cmd == "type":
            # Retry a few times to find the window
            window = _active_window
            if window is None and target and target.lower() not in {"focused", "active", ""}:
                for delay in (0.3, 0.7, 1.2, 2.0):
                    time.sleep(delay)
                    window, _ = _find_window(target)
                    if window is not None:
                        _active_window = window
                        break
            if window is not None:
                # Try to click edit control first to ensure focus
                edit, erect = _find_edit_control(window)
                if edit is not None and erect:
                    cx, cy = _center(erect)
                    _win32_click(cx, cy)
                    time.sleep(0.15)
                ok_t, msg_t, rect_t = _type_into_window(window, value)
                return (_ok(msg_t, rect_t) if ok_t else _err(msg_t, rect_t))
            # Fallback: type into whatever has focus
            send_keys(_escape_send_keys(value), with_spaces=True, pause=0.01)
            return _ok("Typed into focused control")

        # ── keys ──────────────────────────────────────────────────────────────
        if cmd == "keys":
            window = _active_window
            if target and target.lower() not in {"focused", "active", ""}:
                w2, _ = _find_window(target)
                if w2 is not None:
                    _force_foreground(w2)
                    _active_window = w2
                    window = w2
            elif window is not None:
                _force_foreground(window)
            keystr = value or target
            try:
                send_keys(keystr, with_spaces=True)
            except Exception as e:
                return _err(f"Keys failed: {e}")
            rect = _desktop_rect(window) if window else None
            return _ok(f"Sent keys: {keystr[:80]}", rect)

        # ── wait ──────────────────────────────────────────────────────────────
        if cmd == "wait":
            try:
                secs = min(float(target or value or "0.5"), 10.0)
            except ValueError:
                secs = 0.5
            time.sleep(secs)
            return _ok(f"Waited {secs}s")

        # ── scroll ────────────────────────────────────────────────────────────
        if cmd == "scroll":
            """Scroll in the active window or a named control.
            target = 'up' | 'down' (default 'down'), value = lines (default 3)"""
            direction = (target or "down").lower()
            try:
                lines = int(value or 3)
            except ValueError:
                lines = 3
            wheel_delta = lines if direction == "down" else -lines
            try:
                import ctypes
                u32 = ctypes.windll.user32
                # Scroll at center of active window or screen center
                w = _active_window
                if w:
                    r = _desktop_rect(w)
                    cx, cy = _center(r) if r else (960, 540)
                else:
                    cx, cy = 960, 540
                u32.SetCursorPos(cx, cy)
                # MOUSEEVENTF_WHEEL = 0x0800; WHEEL_DELTA = 120
                u32.mouse_event(0x0800, 0, 0, -wheel_delta * 120, 0)
                return _ok(f"Scrolled {direction} {lines} lines")
            except Exception as exc:
                return _err(f"Scroll failed: {exc}")

        return _err(f"Unknown command: {cmd}")

    except Exception as exc:
        return _err(f"{cmd} error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # Signal ready
    ready = json.dumps({"id": "init", "ok": True, "msg": "broker_ready"})
    sys.stdout.write(ready + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"id": "", "ok": False, "msg": f"JSON parse error: {e}"}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        resp = _dispatch(req)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

        if req.get("cmd") == "exit":
            break


if __name__ == "__main__":
    main()
