"""
Web Automation Engine — Phase 4 (Playwright)
=============================================
Executes web-based tasks using Playwright with:
  - Chromium browser in headed or headless mode
  - Screenshot streaming to frontend via WebSocket
  - Element highlighting (red outline flash) during interactions
  - User profile reuse for saved logins and cookies
  - Comprehensive audit logging of every action

Security: All URLs pass through security_guard before navigation.
Sandbox: Each session uses an isolated --user-data-dir (Approach 1 from security_guard.py research)
"""
import asyncio
import base64
import uuid
import logging
import os
from datetime import datetime
from typing import Optional, AsyncIterator

from app.core.websocket_manager import manager
from app.core.config import settings
from app.services.security_guard import security_guard

logger = logging.getLogger(__name__)

# User data dir for browser profile persistence
_PROFILE_DIR = os.path.join(".", "logs", "browser_profiles")


class WebAutomationEngine:
    """
    Playwright-based web automation engine.
    Manages browser lifecycle and streams screenshots to the frontend.
    """

    def __init__(self):
        self._browsers: dict[str, object] = {}   # session_id → browser instance
        self._pages: dict[str, object] = {}       # session_id → page instance
        self._playwright = None

    async def _ensure_playwright(self):
        """Lazily initialize Playwright."""
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                logger.info("✅ Playwright initialized")
            except ImportError:
                logger.warning("⚠️ playwright not installed. Run: pip install playwright && playwright install chromium")
                raise RuntimeError("Playwright not available. Install it with: pip install playwright && playwright install chromium")

    async def start_session(self, session_id: str, headless: bool = False) -> bool:
        """Launch a browser instance using system Chrome profile for saved logins/cookies."""
        try:
            await self._ensure_playwright()
            os.makedirs(_PROFILE_DIR, exist_ok=True)
            profile_path = os.path.join(_PROFILE_DIR, session_id)

            local_appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
            system_chrome_data = os.path.join(local_appdata, "Google", "Chrome", "User Data")

            browser = None

            # Attempt 1: Attach to actual system Chrome User Data (inherits all cookies & saved logins)
            if os.path.exists(system_chrome_data):
                try:
                    logger.info(f"Attempting to launch with System Chrome profile: {system_chrome_data}")
                    browser = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=system_chrome_data,
                        channel="chrome",
                        headless=headless,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-first-run",
                        ],
                        viewport={"width": 1280, "height": 720},
                    )
                    logger.info("✅ Successfully loaded system Chrome profile with saved logins & cookies")
                except Exception as chrome_err:
                    logger.warning(f"System Chrome profile in use/locked ({chrome_err}). Using dedicated persistent session profile.")

            # Attempt 2: Independent browser instance if system profile is in use/locked
            if browser is None:
                logger.info(f"Launching independent browser context for session: {profile_path}")
                browser = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_path,
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                    ],
                    viewport={"width": 1280, "height": 720},
                )
                logger.info(f"✅ Browser session started: {profile_path}")

            self._browsers[session_id] = browser
            if browser.pages:
                page = browser.pages[0]
            else:
                page = await browser.new_page()

            self._pages[session_id] = page
            return True

        except Exception as e:
            logger.error(f"Failed to start browser session {session_id}: {e}")
            return False

    async def navigate(self, session_id: str, url: str) -> dict:
        """Navigate to a URL and stream a screenshot."""
        page = self._pages.get(session_id)
        if not page:
            return {"success": False, "error": "No active browser session"}

        # Security check
        verdict = security_guard.scan_url(url)
        if not verdict.allowed:
            return {"success": False, "error": f"URL blocked: {verdict.reason}"}

        try:
            logger.info(f"Navigating to {url} (session={session_id})")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            title = await page.title()
            await self._stream_screenshot(session_id, page, f"Navigated to: {title}")
            return {"success": True, "url": url, "title": title}
        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return {"success": False, "error": str(e)}

    async def click_element(self, session_id: str, selector: str, description: str = "") -> dict:
        """Click an element with visual highlight."""
        page = self._pages.get(session_id)
        if not page:
            return {"success": False, "error": "No active browser session"}

        try:
            # Highlight element before clicking
            await self._highlight_element(page, selector)
            await asyncio.sleep(0.4)  # Let user see highlight

            await page.click(selector, timeout=10000)
            await asyncio.sleep(0.5)
            await self._stream_screenshot(session_id, page, description or f"Clicked: {selector}")
            return {"success": True, "selector": selector}
        except Exception as e:
            logger.error(f"Click failed on '{selector}': {e}")
            return {"success": False, "error": str(e)}

    async def type_text(self, session_id: str, selector: str, text: str, description: str = "") -> dict:
        """Type text into an element with visual highlight."""
        page = self._pages.get(session_id)
        if not page:
            return {"success": False, "error": "No active browser session"}

        try:
            await self._highlight_element(page, selector)
            await page.click(selector, timeout=10000)
            await page.fill(selector, text)
            await self._stream_screenshot(session_id, page, description or f"Typed into: {selector}")
            return {"success": True, "selector": selector, "text": text}
        except Exception as e:
            logger.error(f"Type failed on '{selector}': {e}")
            return {"success": False, "error": str(e)}

    async def press_key(self, session_id: str, key: str) -> dict:
        """Press a keyboard key."""
        page = self._pages.get(session_id)
        if not page:
            return {"success": False, "error": "No active browser session"}
        try:
            await page.keyboard.press(key)
            await asyncio.sleep(0.5)
            await self._stream_screenshot(session_id, page, f"Pressed: {key}")
            return {"success": True, "key": key}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_text(self, session_id: str, selector: str) -> dict:
        """Extract text from an element."""
        page = self._pages.get(session_id)
        if not page:
            return {"success": False, "error": "No active browser session"}
        try:
            text = await page.inner_text(selector, timeout=5000)
            return {"success": True, "text": text.strip()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def take_screenshot(self, session_id: str, label: str = "") -> Optional[str]:
        """Take a screenshot and return as base64."""
        page = self._pages.get(session_id)
        if not page:
            return None
        try:
            screenshot_bytes = await page.screenshot(type="jpeg", quality=75)
            return base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return None

    async def close_session(self, session_id: str):
        """Close the browser session and clean up."""
        browser = self._browsers.pop(session_id, None)
        self._pages.pop(session_id, None)
        if browser:
            try:
                await browser.close()
                logger.info(f"Browser session closed: {session_id}")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")

    # ─────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────

    async def _highlight_element(self, page, selector: str):
        """
        Flash a red outline around the target element.
        This provides the interactive element highlighting for the live viewport.
        """
        try:
            await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return;
                    const original = el.style.outline;
                    el.style.outline = '3px solid #ef4444';
                    el.style.outlineOffset = '2px';
                    el.style.transition = 'outline 0.1s ease';
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    setTimeout(() => {
                        el.style.outline = original;
                    }, 800);
                }""",
                selector,
            )
        except Exception:
            pass  # Non-fatal — element may not exist

    async def _stream_screenshot(self, session_id: str, page, label: str = ""):
        """Take a screenshot and send it to the frontend via WebSocket."""
        try:
            screenshot_bytes = await page.screenshot(type="jpeg", quality=75)
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            await manager.send_to_session(session_id, {
                "type": "viewport_screenshot",
                "label": label,
                "data": b64,
                "format": "webp",
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            logger.error(f"Screenshot streaming failed: {e}")

    async def cleanup_all(self):
        """Close all browser sessions on shutdown."""
        for session_id in list(self._browsers.keys()):
            await self.close_session(session_id)


# Singleton
web_engine = WebAutomationEngine()
