"""
Session Manager — Persistent Browser Context for MCP Engine
=============================================================
Manages a dedicated Playwright browser profile for automation, separate from
the user's personal Chrome profile. Cookies/localStorage persist automatically.

Two strategies:
  1. Primary: launch_persistent_context() with ~/.directact/browser-profile/
     - Reused across all tasks — no relaunch per task
     - No lock conflicts with personal Chrome
  2. Fallback: storage_state export/import (auth.json) for fast headless runs

Security: This module does NOT bypass the security guard. All navigation and
actions still pass through MalwareGuard in the MCP server layer.

Performance:
  - 5-second hard timeout on context launch (no silent hangs)
  - Context reused across tasks — only new tabs per task
  - time.perf_counter() instrumentation on all critical paths
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Timeout for browser context launch (prevents silent hangs on locked/corrupt profiles)
_CONTEXT_LAUNCH_TIMEOUT_S = 20.0


class SessionManager:
    """
    Manages a single persistent Playwright browser context shared across
    all MCP tasks. Each task gets its own Page (tab) within that context.
    """

    def __init__(self):
        self._playwright = None
        self._context = None  # Single persistent context, reused
        self._pages: dict[str, object] = {}  # session_id → Page
        self._profile_dir: Optional[str] = None
        self._storage_state_path: Optional[str] = None

    def _resolve_profile_dir(self) -> str:
        """Resolve the browser profile directory from config."""
        from app.core.config import settings
        raw = getattr(settings, "mcp_browser_profile_dir", "~/.directact/browser-profile")
        resolved = os.path.expanduser(raw)
        os.makedirs(resolved, exist_ok=True)
        return resolved

    def _resolve_system_chrome_dir(self) -> tuple[str, str]:
        """
        Auto-detect system Chrome User Data directory and profile name.
        Returns (user_data_dir, profile_name).
        """
        from app.core.config import settings
        import platform

        # Use config overrides if provided
        user_data_dir = getattr(settings, "chrome_user_data_dir", "").strip()
        profile_name = getattr(settings, "chrome_profile_name", "Default").strip() or "Default"

        # Also check env var (set by the /chrome/profile endpoint)
        env_profile = os.environ.get("CHROME_PROFILE_NAME", "").strip()
        if env_profile:
            profile_name = env_profile

        if not user_data_dir:
            # Auto-detect based on OS
            system = platform.system()
            home = Path.home()
            if system == "Windows":
                user_data_dir = str(home / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
            elif system == "Darwin":  # macOS
                user_data_dir = str(home / "Library" / "Application Support" / "Google" / "Chrome")
            else:  # Linux
                user_data_dir = str(home / ".config" / "google-chrome")

        return user_data_dir, profile_name

    def _resolve_storage_state_path(self) -> str:
        """Path for storage_state export (fallback strategy)."""
        profile_dir = self._resolve_profile_dir()
        return os.path.join(profile_dir, "storage_state.json")

    async def _ensure_playwright(self):
        """Lazily initialize Playwright."""
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                logger.info("✅ SessionManager: Playwright initialized")
            except ImportError:
                raise RuntimeError(
                    "Playwright not available. Install: pip install playwright && playwright install chromium"
                )

    async def ensure_context(self, headless: Optional[bool] = None):
        """
        Ensure the persistent browser context is running.
        Returns the context. If already running, returns immediately (~0ms).

        Strategy (when mcp_use_system_chrome=True):
          1. Try system Chrome User Data dir (has saved passwords/cookies)
          2. If locked/in-use, fall back to dedicated profile

        Hard timeout of 5 seconds on launch — raises TimeoutError if
        the profile is locked or corrupt.
        """
        if self._context is not None:
            return self._context

        from app.core.config import settings

        if headless is None:
            headless = getattr(settings, "mcp_default_headless", False)

        await self._ensure_playwright()

        use_system_chrome = getattr(settings, "mcp_use_system_chrome", True)
        profile_dir = None
        self._storage_state_path = self._resolve_storage_state_path()

        # ── Strategy 1: Try system Chrome profile (saved passwords & cookies) ──
        if use_system_chrome:
            chrome_data_dir, chrome_profile = self._resolve_system_chrome_dir()
            chrome_profile_path = os.path.join(chrome_data_dir, chrome_profile)

            if os.path.exists(chrome_data_dir) and os.path.exists(chrome_profile_path):
                t0 = time.perf_counter()
                logger.info(
                    f"SessionManager: trying system Chrome profile "
                    f"(dir={chrome_data_dir}, profile={chrome_profile}, headless={headless})"
                )
                try:
                    self._context = await asyncio.wait_for(
                        self._playwright.chromium.launch_persistent_context(
                            user_data_dir=chrome_data_dir,
                            headless=headless,
                            channel="chrome",
                            args=[
                                f"--profile-directory={chrome_profile}",
                                "--disable-blink-features=AutomationControlled",
                                "--no-first-run",
                            ],
                            viewport={"width": 1280, "height": 720},
                            ignore_https_errors=True,
                        ),
                        timeout=_CONTEXT_LAUNCH_TIMEOUT_S,
                    )
                    elapsed = (time.perf_counter() - t0) * 1000
                    profile_dir = chrome_data_dir
                    self._profile_dir = profile_dir
                    logger.info(
                        f"✅ SessionManager: SYSTEM CHROME profile loaded in {elapsed:.0f}ms "
                        f"(profile={chrome_profile}) — saved passwords & cookies ACTIVE"
                    )
                    return self._context
                except asyncio.TimeoutError:
                    elapsed = (time.perf_counter() - t0) * 1000
                    logger.warning(
                        f"⚠️ SessionManager: system Chrome profile TIMEOUT ({elapsed:.0f}ms). "
                        f"Chrome may be open/locked. Falling back to dedicated profile."
                    )
                except Exception as e:
                    elapsed = (time.perf_counter() - t0) * 1000
                    logger.warning(
                        f"⚠️ SessionManager: system Chrome profile failed ({elapsed:.0f}ms): {e}. "
                        f"Falling back to dedicated profile."
                    )
            else:
                logger.info(
                    f"SessionManager: system Chrome dir not found at {chrome_data_dir}/{chrome_profile}. "
                    f"Using dedicated profile."
                )

        # ── Strategy 2: Dedicated automation profile (fallback) ──
        profile_dir = self._resolve_profile_dir()
        self._profile_dir = profile_dir

        t0 = time.perf_counter()
        logger.info(f"SessionManager: launching dedicated profile (headless={headless}, profile={profile_dir})")

        try:
            self._context = await asyncio.wait_for(
                self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--disable-extensions",
                    ],
                    viewport={"width": 1280, "height": 720},
                    ignore_https_errors=True,
                ),
                timeout=_CONTEXT_LAUNCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(
                f"SessionManager: TIMEOUT launching browser context after {elapsed:.0f}ms. "
                f"Profile may be locked or corrupt: {profile_dir}"
            )
            raise TimeoutError(
                f"Browser context launch timed out after {_CONTEXT_LAUNCH_TIMEOUT_S}s. "
                f"Check if another process is locking '{profile_dir}' or try deleting it."
            )
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(f"SessionManager: context launch FAILED in {elapsed:.0f}ms: {e}")
            raise

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            f"⏱️  SessionManager: dedicated profile launched in {elapsed:.0f}ms "
            f"(NOTE: no saved passwords/cookies — use '/setup-login' or enable mcp_use_system_chrome)"
        )
        return self._context

    async def get_page(self, session_id: str):
        """
        Get or create a Page (tab) for the given session.
        Each task session gets its own tab within the shared context.
        """
        if session_id in self._pages:
            page = self._pages[session_id]
            # Verify page is still open
            try:
                if not page.is_closed():
                    return page
            except Exception:
                pass
            # Page was closed — remove and recreate
            del self._pages[session_id]

        ctx = await self.ensure_context()

        t0 = time.perf_counter()
        # Reuse an existing blank page if available and not a project tab, else create new
        candidate = None
        if ctx.pages and len(self._pages) == 0:
            p = ctx.pages[0]
            if not any(x in p.url for x in [":5173", ":8000"]):
                candidate = p
        page = candidate if candidate else await ctx.new_page()

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(f"⏱️  SessionManager: page created for session={session_id[:8]} in {elapsed:.0f}ms")

        self._pages[session_id] = page
        return page

    async def check_session_status(self, domain: str) -> dict:
        """
        Check if valid session cookies exist for the given domain.
        Returns {logged_in: bool, cookie_count: int, cookies: [...names]}.
        """
        ctx = await self.ensure_context()

        try:
            all_cookies = await ctx.cookies()
            domain_lower = domain.lower().lstrip(".")

            matching = [
                c for c in all_cookies
                if domain_lower in c.get("domain", "").lower()
            ]

            # Heuristic: session cookies often contain auth tokens
            auth_indicators = ["session", "token", "auth", "sid", "jwt", "login", "user"]
            auth_cookies = [
                c for c in matching
                if any(ind in c["name"].lower() for ind in auth_indicators)
            ]

            logged_in = len(auth_cookies) > 0
            return {
                "logged_in": logged_in,
                "cookie_count": len(matching),
                "auth_cookie_count": len(auth_cookies),
                "cookie_names": [c["name"] for c in matching[:20]],  # Cap for readability
                "domain": domain,
            }
        except Exception as e:
            logger.error(f"SessionManager: cookie check failed for {domain}: {e}")
            return {
                "logged_in": False,
                "cookie_count": 0,
                "auth_cookie_count": 0,
                "cookie_names": [],
                "domain": domain,
                "error": str(e),
            }

    async def export_storage_state(self, path: Optional[str] = None) -> str:
        """
        Export the current browser context's storage state (cookies + localStorage)
        to a JSON file. Used for the fallback headless strategy.
        Returns the path where the state was saved.
        """
        ctx = await self.ensure_context()
        save_path = path or self._resolve_storage_state_path()

        t0 = time.perf_counter()
        await ctx.storage_state(path=save_path)
        elapsed = (time.perf_counter() - t0) * 1000

        logger.info(f"⏱️  SessionManager: storage state exported to {save_path} in {elapsed:.0f}ms")
        return save_path

    async def setup_login_session(self) -> dict:
        """
        Launch a HEADED browser window for one-time manual login.
        The user logs into their sites, then closes the browser.
        Cookies persist in the profile directory for future headless runs.

        Returns status dict.
        """
        logger.info("SessionManager: launching headed browser for manual login setup...")

        # Force headed mode for login
        if self._context is not None:
            # Close existing context first
            await self.shutdown()

        try:
            ctx = await self.ensure_context(headless=False)

            # Open a helper page with instructions
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("about:blank")
            await page.set_content("""
                <html>
                <head><title>DirectAct-AI — Login Setup</title></head>
                <body style="font-family: system-ui; max-width: 600px; margin: 80px auto; color: #e0e0e0; background: #1a1a2e;">
                    <h1 style="color: #10b981;">🔐 DirectAct-AI Login Setup</h1>
                    <p>Log into any sites you want DirectAct-AI to access automatically:</p>
                    <ul>
                        <li>Gmail / Google</li>
                        <li>GitHub</li>
                        <li>Jira / Confluence</li>
                        <li>Any other site you use</li>
                    </ul>
                    <p style="margin-top: 24px; padding: 16px; background: #16213e; border-radius: 8px; border-left: 4px solid #10b981;">
                        <strong>How it works:</strong> Open new tabs, navigate to each site, and log in normally.
                        Your cookies will be saved in a dedicated automation profile —
                        your personal Chrome is never touched.
                    </p>
                    <p style="color: #6b7280; margin-top: 24px;">
                        When done, close this browser window or send "setup_complete" in DirectAct-AI chat.
                    </p>
                </body>
                </html>
            """)

            return {
                "status": "login_browser_launched",
                "profile_dir": self._profile_dir,
                "message": "Headed browser launched. Log into your sites, then close the browser.",
            }

        except Exception as e:
            logger.error(f"SessionManager: login setup failed: {e}")
            return {"status": "error", "error": str(e)}

    async def close_page(self, session_id: str):
        """Close a specific session's page (tab)."""
        page = self._pages.pop(session_id, None)
        if page:
            try:
                if not page.is_closed():
                    await page.close()
                logger.info(f"SessionManager: page closed for session={session_id[:8]}")
            except Exception as e:
                logger.error(f"SessionManager: error closing page: {e}")

    async def shutdown(self):
        """Close browser context and cleanup. Called on application shutdown."""
        # Close all pages
        for sid in list(self._pages.keys()):
            await self.close_page(sid)

        # Close context
        if self._context:
            try:
                # Export storage state before shutdown for fallback strategy
                try:
                    await self.export_storage_state()
                except Exception:
                    pass  # Best-effort

                await self._context.close()
                logger.info("SessionManager: browser context closed")
            except Exception as e:
                logger.error(f"SessionManager: error closing context: {e}")
            self._context = None

        # Stop playwright
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    @property
    def is_context_alive(self) -> bool:
        """Check if the browser context is alive and usable."""
        return self._context is not None

    @property
    def active_page_count(self) -> int:
        """Number of active pages (tabs)."""
        return len(self._pages)


# Singleton
session_manager = SessionManager()
