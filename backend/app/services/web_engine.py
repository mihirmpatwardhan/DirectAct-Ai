"""
Web Automation Engine — Powered by browser-use & System Chrome Profiles
========================================================================
Executes web automation tasks using browser-use and Playwright with:
  - User's REAL Chrome profile (saved logins, cookies, passwords, chosen Google account)
  - Active profile switching (supports "Default", "Profile 1", "Profile 2", etc.)
  - Interactive multi-step actions (e.g. search YouTube, open playlists, select specific videos & play)
  - Autonomous agent-driven execution via browser-use Agent
  - Real-time screenshot streaming to frontend Viewport via WebSocket
  - Security guard validation for all navigations
"""
import asyncio
import base64
import json
import logging
import os
import platform
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from urllib.parse import quote_plus

from app.core.websocket_manager import manager
from app.core.config import settings
from app.services.security_guard import security_guard
from app.services.live_chrome_bridge import live_chrome_bridge

logger = logging.getLogger(__name__)


def _get_chrome_user_data_dir() -> Path:
    """Get the standard Chrome User Data directory for the current OS."""
    # The profile selector updates the process environment at runtime, while
    # the initial selection comes from Settings/.env.  Both must be honored;
    # otherwise every fresh backend process silently falls back to Default.
    override = (
        os.getenv("CHROME_USER_DATA_DIR", "").strip()
        or str(getattr(settings, "chrome_user_data_dir", "") or "").strip()
    )
    if override:
        return Path(override)

    system = platform.system()
    home = Path.home()
    if system == "Windows":
        local_app_data = os.getenv("LOCALAPPDATA", "")
        if local_app_data:
            return Path(local_app_data) / "Google" / "Chrome" / "User Data"
        return home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return home / ".config" / "google-chrome"


def _find_chrome_executable() -> Optional[str]:
    """Find installed Google Chrome binary."""
    override = os.getenv("CHROME_EXECUTABLE_PATH", "").strip()
    if override and os.path.exists(override):
        return override

    system = platform.system()
    if system == "Windows":
        candidates = [
            Path(os.getenv("ProgramFiles", "C:\\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.getenv("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        for p in candidates:
            if p.exists():
                return str(p)
    elif system == "Darwin":
        p = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if p.exists():
            return str(p)
    else:
        for cmd in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
            found = shutil.which(cmd)
            if found:
                return found

    return None


def _get_active_profile_name() -> str:
    """Get the currently selected Chrome profile directory name."""
    return (
        os.getenv("CHROME_PROFILE_NAME", "").strip()
        or str(getattr(settings, "chrome_profile_name", "Default") or "Default").strip()
        or "Default"
    )


def _get_dedicated_profile_dir(session_id: str) -> Path:
    """Return a persistent, session-isolated profile directory."""
    configured = str(getattr(settings, "mcp_browser_profile_dir", "~/.directact/browser-profile"))
    base = Path(os.path.expanduser(configured))
    # A separate profile per chat prevents one open session from locking all
    # other sessions on Windows while still preserving cookies within a chat.
    return base / (session_id or "default")


def _copy_chrome_profile_for_automation(user_data_dir: Path, profile_name: str, session_id: str) -> Path:
    """Prepare a non-default Chrome data directory with the selected profile.

    Recent Chrome versions reject Playwright's remote-debugging pipe when it
    targets the default user-data directory. Copying the selected profile to a
    private, non-default directory lets the installed Google Chrome binary run
    with the user's saved session without changing or locking the live profile.
    """
    automation_root = (
        Path("./logs/chrome_profiles") / (session_id or "default") / "User Data"
    ).resolve()
    profile_source = user_data_dir / profile_name
    profile_target = automation_root / profile_name

    if not profile_source.exists():
        raise RuntimeError(f"Chrome profile '{profile_name}' was not found in {user_data_dir}")

    if not (automation_root / "Local State").exists():
        automation_root.mkdir(parents=True, exist_ok=True)
        local_state = user_data_dir / "Local State"
        if local_state.exists():
            shutil.copy2(local_state, automation_root / "Local State")

        ignored_entries = {
            "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
            "Crashpad", "DawnCache", "Service Worker\\CacheStorage",
        }

        def ignore_profile_entries(_directory: str, names: list[str]):
            return {name for name in names if name in ignored_entries}

        shutil.copytree(
            profile_source,
            profile_target,
            copy_function=shutil.copy2,
            ignore=ignore_profile_entries,
            dirs_exist_ok=True,
        )

    return automation_root


def parse_youtube_request(text: str) -> tuple[str, int]:
    """Extract a YouTube search query and target video index (0-based).

    This intentionally handles short Marathi/Hinglish command words too. The
    automation path must not send a natural-language command such as
    ``youtube la ja ... play kar`` to YouTube as the search query.
    """
    lowered = text.lower()

    # Determine 0-based target index
    index = 0
    if re.search(r"\b(1st|first|1|pahila|pahili)\b\s+(?:video|result|one)", lowered):
        index = 0
    elif (
        re.search(r"\b(2nd|second|2|dusra|dusari)\b\s+(?:video|result|one|nd video)", lowered)
        or "2 nd video" in lowered
        or "2nd video" in lowered
        or "second video" in lowered
        or "dusra video" in lowered
    ):
        index = 1
    elif (
        re.search(r"\b(3rd|third|3)\b\s+(?:video|result|one|rd video)", lowered)
        or "3 rd video" in lowered
        or "3rd video" in lowered
        or "third video" in lowered
    ):
        index = 2
    elif re.search(r"\b(4th|fourth|4)\b\s+(?:video|result|one|th video)", lowered):
        index = 3
    elif re.search(r"\b(5th|fifth|5)\b\s+(?:video|result|one|th video)", lowered):
        index = 4

    is_playlist_request = bool(re.search(
        r"\b(playlist|channel|madhil|madhla|madhun|from)\b", lowered
    ))

    if is_playlist_request:
        # Structured requests contain the useful names between the YouTube
        # target and the ordinal/action tail. Keep those names and remove
        # navigation filler in both English and Marathi transliteration.
        query = re.sub(r"^.*?\byoutube\b", "", text, flags=re.I).strip()
        query = re.sub(
            r"\b(?:1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth|pahila|pahili|dusra|dusari|\d+)\s+(?:video|result|one)\b",
            " ",
            query,
            flags=re.I,
        )
        query = re.sub(
            r"\b(?:play|watch|listen to|search for|search|find|open|launch|go|to|and|ani|on|in|the|from|of|la|ja|kar|karo|madhil|madhla|madhun|channel|playlist|please)\b",
            " ",
            query,
            flags=re.I,
        )
    else:
        patterns = [
            r"(?:play|watch|listen to|search for|search|find)\s+(.+?)(?:\s+on youtube|\s+in youtube|$)",
            r"youtube\s+(?:and\s+)?(?:play|watch|search for|search|find)\s+(.+)",
            r"(?:open|go to)\s+youtube\s+(?:and\s+)?(?:play|watch|search for|search|find)\s+(.+)",
        ]
        query = ""
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                query = match.group(1).strip()
                break
        if not query:
            query = text

        query = re.sub(
            r"\b(?:1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth|pahila|pahili|dusra|dusari|\d+\s*nd|\d+\s*rd|\d+\s*th|\d+)\s+video\b",
            "",
            query,
            flags=re.I,
        )
        query = re.sub(r"\b(?:on|in)\s+youtube\b", "", query, flags=re.I)
        query = re.sub(r"\b(?:open|launch)\s+youtube(?:\s+and)?\b", "", query, flags=re.I)
        query = re.sub(r"\b(?:play|watch|search for|search|find)\b", "", query, flags=re.I)

    clean_query = re.sub(r"\bplayalist\b", "playlist", query, flags=re.I)
    clean_query = re.sub(r"[,\u2013\u2014]+", " ", clean_query)
    clean_query = re.sub(r"\s+", " ", clean_query).strip(" ,.-")

    return clean_query or query, index


def _get_browser_use_llm():
    """Instantiate the LLM for browser-use Agent."""
    try:
        from browser_use.llm import ChatGoogle, ChatOpenAI
    except ImportError:
        logger.warning("browser-use LLM wrappers not available")
        return None

    gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if gemini_key:
        model = (os.getenv("GEMINI_MODEL") or "gemini-2.0-flash").strip()
        try:
            return ChatGoogle(model=model, api_key=gemini_key)
        except Exception as e:
            logger.warning(f"Failed to initialize ChatGoogle: {e}")

    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if openai_key:
        model = (os.getenv("OPENAI_MODEL") or "gpt-4o").strip()
        try:
            return ChatOpenAI(model=model, api_key=openai_key)
        except Exception as e:
            logger.warning(f"Failed to initialize ChatOpenAI: {e}")

    return None


class WebAutomationEngine:
    """
    Browser automation engine powered by Playwright and browser-use.
    Uses an isolated Playwright Chromium profile by default. The user's real Chrome
    profile is opt-in because Chrome locks it while the desktop app is running.
    """

    def __init__(self):
        self._browsers: dict[str, Any] = {}   # session_id -> context
        self._pages: dict[str, Any] = {}      # session_id -> page
        self._playwright = None

    async def _ensure_playwright(self):
        """Lazily initialize Playwright."""
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                logger.info("✅ Playwright initialized")
            except ImportError:
                logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
                raise RuntimeError("Playwright not available. Install it with: pip install playwright")

    async def start_session(self, session_id: str, headless: bool = False) -> bool:
        """
        Launch an automation browser for this session.

        The dedicated Playwright Chromium profile is the safe/reliable default.
        Reusing a live Chrome profile on Windows makes Chromium attach to the
        existing browser and immediately close, which breaks the viewport stream.
        The real profile remains available when explicitly enabled in settings.
        """
        try:
            if bool(getattr(settings, "mcp_use_system_chrome", False)):
                # System-Chrome mode is intentionally live-tab-only. Never
                # launch a copied profile here: that creates a second,
                # Guest-like browser and cannot guarantee the user's account.
                logger.info("System Chrome mode uses the live extension bridge; no copied browser will be launched")
                return False

            await self._ensure_playwright()

            user_data_dir = _get_chrome_user_data_dir()
            profile_name = _get_active_profile_name()
            exe_path = _find_chrome_executable()
            use_system_profile = bool(getattr(settings, "mcp_use_system_chrome", False))

            logger.info(
                f"Starting browser session: mode={'system Chrome' if use_system_profile else 'isolated Chromium'}, "
                f"headless={headless}"
            )

            launch_args = [
                f"--profile-directory={profile_name}",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-sandbox",
                "--disable-infobars",
                "--start-maximized",
            ]

            browser = None
            # Use the real Chrome profile only when explicitly enabled. It cannot
            # be safely shared with an already-running desktop Chrome process.
            if use_system_profile and user_data_dir.exists():
                try:
                    # Chrome 136+ blocks remote debugging against the default
                    # user-data directory. Use the installed Chrome executable
                    # with a private copy of the selected real profile instead.
                    automation_user_data_dir = _copy_chrome_profile_for_automation(
                        user_data_dir, profile_name, session_id
                    )
                    real_kwargs = dict(
                        user_data_dir=str(automation_user_data_dir),
                        headless=headless,
                        args=launch_args,
                        viewport=None,
                        ignore_default_args=["--enable-automation"],
                    )
                    if exe_path:
                        real_kwargs["executable_path"] = exe_path
                    browser = await self._playwright.chromium.launch_persistent_context(**real_kwargs)
                    logger.info(
                        f"✅ Installed Google Chrome profile '{profile_name}' loaded "
                        f"with saved session copy: {automation_user_data_dir}"
                    )
                except Exception as lock_err:
                    raise RuntimeError(
                        "Actual Chrome profile is locked or unavailable. "
                        "Close all Google Chrome windows and retry; "
                        "DirectAct-AI will then use your selected Chrome profile "
                        "and saved session. Guest Chromium fallback is disabled."
                    ) from lock_err
            elif use_system_profile:
                raise RuntimeError(
                    f"Chrome User Data directory was not found: {user_data_dir}"
                )

            if browser is None:
                dedicated_dir = _get_dedicated_profile_dir(session_id)
                dedicated_dir.mkdir(parents=True, exist_ok=True)
                dedicated_args = [arg for arg in launch_args if not arg.startswith("--profile-directory=")]
                dedicated_kwargs = dict(
                    user_data_dir=str(dedicated_dir),
                    headless=headless,
                    args=dedicated_args,
                    viewport={"width": 1280, "height": 720},
                    ignore_default_args=["--enable-automation"],
                )
                try:
                    # No executable_path here: Playwright's bundled Chromium is
                    # independent from the user's running Chrome and its locks.
                    browser = await self._playwright.chromium.launch_persistent_context(**dedicated_kwargs)
                    logger.info(f"✅ Isolated Chromium profile ready: {dedicated_dir}")
                except Exception as dedicated_err:
                    # A stale/corrupt dedicated profile should not make all web
                    # automation unavailable. Use a clean session profile once.
                    clean_dir = Path("./logs/browser_profiles") / session_id
                    clean_dir.mkdir(parents=True, exist_ok=True)
                    dedicated_kwargs["user_data_dir"] = str(clean_dir)
                    browser = await self._playwright.chromium.launch_persistent_context(**dedicated_kwargs)
                    logger.warning(
                        f"Dedicated browser profile was unavailable ({dedicated_err}); "
                        f"using clean session profile {clean_dir}"
                    )

            self._browsers[session_id] = browser
            page = browser.pages[0] if browser.pages else await browser.new_page()
            self._pages[session_id] = page
            return True

        except Exception as e:
            logger.error(f"Failed to start browser session {session_id}: {e}", exc_info=True)
            return False

    async def get_or_create_page(self, session_id: str):
        """Ensure an active browser and page for the session."""
        if session_id not in self._browsers or session_id not in self._pages:
            ok = await self.start_session(session_id)
            if not ok:
                raise RuntimeError("Failed to start Chrome browser session")
        page = self._pages.get(session_id)
        if not page or page.is_closed():
            browser = self._browsers.get(session_id)
            page = await browser.new_page()
            self._pages[session_id] = page
        return page

    # ── Smart wait helper ─────────────────────────────────────────────────────

    async def _smart_wait(
        self,
        check_fn,
        max_seconds: float = 10.0,
        poll_interval: float = 0.1,
        description: str = "",
    ) -> bool:
        """
        Poll `check_fn()` (async callable returning bool-ish) every `poll_interval`
        seconds and return True as soon as it is truthy, or False after `max_seconds`.
        This replaces hardcoded `asyncio.sleep(N)` with adaptive waiting.
        """
        elapsed = 0.0
        while elapsed < max_seconds:
            try:
                result = await check_fn()
                if result:
                    return True
            except Exception as wait_err:
                logger.debug(f"_smart_wait check error ({description}): {wait_err}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        logger.debug(f"_smart_wait timed out after {max_seconds}s: {description}")
        return False

    async def run_task(self, task_query: str, url: str = "", session_id: str = "", task_id: str = "") -> dict:
        """
        Main entry point for web automation.

        Every non-trivial request goes through the same agentic path. The URL
        is only an optional starting location; the task text remains intact so
        the agent can complete multi-step work on any site.
        """
        use_live = bool(getattr(settings, "mcp_use_system_chrome", False))

        if use_live:
            if not live_chrome_bridge.connected:
                return {
                    "success": False,
                    "error": (
                        "Live Chrome extension is not connected. "
                        "Load chrome-extension/ in Chrome as an unpacked extension and keep Chrome open."
                    ),
                }
            return await self._run_live_chrome_agent_task(
                session_id, task_query or url, start_url=url or None, task_id=task_id
            )

        # ── Isolated Playwright path ──────────────────────────────────────────
        try:
            page = await self.get_or_create_page(session_id)
        except Exception as e:
            logger.error(f"Could not initialize page for task: {e}")
            return {"success": False, "error": str(e)}

        task = (task_query or url).strip()
        # A URL-only request is navigation. A URL plus any other intent is an
        # automation task that must continue through the agent.
        navigation_only = False
        if url:
            host_path = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
            navigation_only = bool(re.fullmatch(
                rf"\s*(?:open|go\s+to|navigate\s+to|visit)?\s*(?:https?://)?{re.escape(host_path)}\s*",
                task,
                re.IGNORECASE,
            ))
        if url and (task.casefold() in {url.casefold(), f"open {url}".casefold()} or navigation_only):
            return await self.navigate(session_id, url)

        return await self.run_agent_task(
            task_query=task or "Complete the requested browser task",
            session_id=session_id,
            start_url=url or None,
        )

    # ── General Live-Chrome Agentic Engine ────────────────────────────────────

    async def _run_live_chrome_agent_task(
        self, session_id: str, task_query: str, start_url: str | None = None, task_id: str = ""
    ) -> dict:
        """
        Drive ANY website through the user's live Chrome tab using an LLM as the
        decision-making brain. Works for BookMyShow, redBus, IRCTC, MakeMyTrip, Amazon, etc.
        without any site-specific hardcoding.

        Agent loop:
          1. Navigate to starting URL (if provided/inferred)
          2. Capture page DOM snapshot + screenshot
          3. Ask LLM: "Given this DOM, what is the next action to take?"
          4. Execute the LLM's action (navigate / click / type / scroll / read)
          5. Stream step progress to Timeline & screenshot to Viewport
          6. Repeat until LLM signals DONE or max_steps is reached
        """
        from app.services.llm_service import llm_service

        max_steps = 20
        step_results: list[str] = []
        last_url = ""

        async def _emit_step_start(step_idx: int, cmd_type: str, desc: str):
            if not task_id:
                return
            try:
                await manager.send_to_session(session_id, {
                    "type": "task_step_started",
                    "session_id": session_id,
                    "task_id": task_id,
                    "step_index": step_idx,
                    "total_steps": max_steps,
                    "command_type": cmd_type,
                    "description": desc,
                    "risk_level": "low",
                    "timestamp": datetime.utcnow().isoformat(),
                })
            except Exception as emit_err:
                logger.debug(f"Failed to emit task_step_started: {emit_err}")

        async def _emit_step_completed(step_idx: int, cmd_type: str, success: bool, duration_ms: float, output: str = "", error: str = ""):
            if not task_id:
                return
            try:
                await manager.send_to_session(session_id, {
                    "type": "task_step_completed",
                    "session_id": session_id,
                    "task_id": task_id,
                    "step_index": step_idx,
                    "command_type": cmd_type,
                    "success": success,
                    "duration_ms": duration_ms,
                    "output_summary": output[:120] if output else None,
                    "error": error if error else None,
                    "timestamp": datetime.utcnow().isoformat(),
                })
            except Exception as emit_err:
                logger.debug(f"Failed to emit task_step_completed: {emit_err}")

        async def _take_live_shot(label: str):
            """Capture a live Chrome screenshot and stream it to the frontend."""
            try:
                shot = await live_chrome_bridge.request("screenshot", timeout=10)
                tab  = await live_chrome_bridge.request("tab", timeout=5)
                await manager.send_to_session(session_id, {
                    "type": "viewport_screenshot",
                    "label": label,
                    "data": shot.get("data", "") if shot else "",
                    "format": "jpeg",
                    "timestamp": datetime.utcnow().isoformat(),
                })
                return tab.get("url", "") if tab else ""
            except Exception as shot_err:
                logger.debug(f"Live screenshot error: {shot_err}")
                return ""

        async def _get_dom_snapshot() -> str:
            """Return a rich DOM snapshot with explicit popup/modal detection."""
            try:
                snap = await live_chrome_bridge.request(
                    "evaluate",
                    {"expression": r"""
                        (() => {
                          const tag = (el) => {
                            const t = el.tagName.toLowerCase();
                            const attrs = ['id','class','name','type','aria-label','placeholder','href','role','value','data-testid']
                              .filter(a => el.getAttribute(a))
                              .map(a => `${a}="${(el.getAttribute(a)||'').slice(0,70)}"`);
                            const text = (el.innerText || el.textContent || el.value || '').trim().slice(0, 90);
                            return `<${t} ${attrs.join(' ')}>${text}</${t}>`;
                          };

                          // 1. Detect visible modals, dialogs, popups, or cookie banners
                          const modalSelectors = [
                            'dialog[open]',
                            '[role="dialog"]',
                            '[role="alertdialog"]',
                            '[aria-modal="true"]',
                            '.modal:not([style*="display: none"])',
                            '.popup:not([style*="display: none"])',
                            '.overlay:not([style*="display: none"])',
                            '[class*="modal" i]:not([style*="display: none"])',
                            '[class*="dialog" i]:not([style*="display: none"])',
                            '[class*="consent" i]:not([style*="display: none"])',
                            '[class*="cookie" i]:not([style*="display: none"])',
                            '[class*="banner" i]:not([style*="display: none"])',
                            '[id*="consent" i]',
                            '[id*="cookie" i]',
                          ];
                          const visibleModals = Array.from(document.querySelectorAll(modalSelectors.join(', '))).filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 30 && r.height > 20;
                          });

                          let popupReport = '';
                          let modalElements = [];
                          if (visibleModals.length > 0) {
                            const topModal = visibleModals[0];
                            const modalButtons = Array.from(topModal.querySelectorAll(
                              'button, [role="button"], a, input[type="button"], input[type="submit"], [class*="btn" i]'
                            )).filter(b => {
                              const r = b.getBoundingClientRect();
                              return r.width > 0 && r.height > 0;
                            });
                            const btnDescriptions = modalButtons.map(b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim()).filter(Boolean);
                            const modalText = (topModal.innerText || '').slice(0, 400).replace(/\s+/g, ' ');
                            popupReport = `⚠️ ACTIVE POPUP/MODAL DETECTED:\nModal Content: "${modalText}"\nButtons inside popup: [${btnDescriptions.join(' | ')}]\n(CRITICAL: You MUST click a popup button such as "Continue" or "Accept" or "Close" before interacting with background elements!)\n`;
                            modalElements = modalButtons.slice(0, 30).map(tag);
                          }

                          // 2. Collect general interactive elements
                          const allInteractive = Array.from(document.querySelectorAll(
                            'a,button,input,select,textarea,[role="button"],[role="link"],[role="menuitem"],[role="option"],[role="tab"],h1,h2,h3,[tabindex]'
                          )).filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                          }).slice(0, 120).map(tag);

                          // Combine modal elements first, then deduplicate
                          const combined = Array.from(new Set([...modalElements, ...allInteractive]));

                          return {
                            url: location.href,
                            title: document.title,
                            popup_report: popupReport,
                            dom: combined.join('\n'),
                            body_text: (document.body.innerText || '').slice(0, 2500)
                          };
                        })()
                    """},
                    timeout=10,
                )
                if snap:
                    popup = snap.get('popup_report', '')
                    base_str = (
                        f"URL: {snap.get('url', '')}\n"
                        f"Title: {snap.get('title', '')}\n"
                    )
                    if popup:
                        base_str += f"\n{popup}\n"
                    base_str += (
                        f"Body text:\n{snap.get('body_text', '')[:1000]}\n\n"
                        f"Interactive elements:\n{snap.get('dom', '')}"
                    )
                    return base_str
            except Exception as snap_err:
                logger.debug(f"DOM snapshot error: {snap_err}")
            return "(DOM snapshot unavailable)"

        async def _auto_dismiss_popups() -> list[str]:
            """
            Proactively detect and dismiss visible popups, modal dialogs, cookie
            banners, or consent overlays before the LLM agent inspects the DOM.
            before the LLM agent inspects the DOM.
            """
            dismiss_js = r"""
            (() => {
              const isVisible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) return false;
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
              };

              const modalSelectors = [
                'dialog[open]',
                '[role="dialog"]',
                '[role="alertdialog"]',
                '[aria-modal="true"]',
                '[class*="modal" i]:not([style*="display: none"])',
                '[class*="dialog" i]:not([style*="display: none"])',
                '[class*="popup" i]:not([style*="display: none"])',
                '[class*="consent" i]:not([style*="display: none"])',
                '[class*="cookie" i]:not([style*="display: none"])',
                '[class*="banner" i]:not([style*="display: none"])',
                '[class*="overlay" i]:not([style*="display: none"])',
                '[id*="consent" i]',
                '[id*="cookie" i]',
                '[id*="banner" i]',
                '[id*="onetrust" i]',
                '[id*="sp_message_container" i]',
              ];

              const isConsentDomain = window.location.hostname.includes('consent') ||
                                      document.title.toLowerCase().includes('before you continue');

              let scopes = [];
              if (isConsentDomain) {
                scopes = [document.body || document.documentElement];
              } else {
                scopes = Array.from(document.querySelectorAll(modalSelectors.join(','))).filter(isVisible);
              }

              if (scopes.length === 0) return null;

              const positiveTerms = [
                'continue', 'accept all', 'accept all cookies', 'accept', 'allow all', 'allow',
                'i agree', 'agree', 'got it', 'ok', 'okay', 'dismiss', 'close', 'proceed',
                'confirm', 'understood', 'reject all', 'reject'
              ];

              const buttonSelectors = 'button, [role="button"], a, input[type="button"], input[type="submit"], [class*="btn" i], [class*="button" i]';

              let candidates = [];
              for (const scope of scopes) {
                const btns = Array.from(scope.querySelectorAll(buttonSelectors)).filter(isVisible);
                for (const b of btns) {
                  const tag = b.tagName.toLowerCase();
                  if (tag === 'dialog' || b.getAttribute('role') === 'dialog' || b.getAttribute('role') === 'alertdialog') continue;

                  const text = (b.innerText || b.value || b.getAttribute('aria-label') || b.getAttribute('title') || '').trim().toLowerCase();
                  if (!text) continue;

                  let matchedTerm = null;
                  let score = 0;
                  for (const term of positiveTerms) {
                    if (text === term) {
                      matchedTerm = term;
                      score = 1000 - text.length;
                      break;
                    } else if (text.startsWith(term + ' ') || text.endsWith(' ' + term) || text.includes(' ' + term + ' ')) {
                      matchedTerm = term;
                      score = 500 - text.length;
                      break;
                    } else if (text.includes(term) && text.length < 50) {
                      matchedTerm = term;
                      score = 200 - text.length;
                      break;
                    }
                  }

                  if (matchedTerm) {
                    if (tag === 'button' || tag === 'input') score += 100;
                    candidates.push({ el: b, text, matchedTerm, score });
                  }
                }
              }

              if (candidates.length === 0) return null;

              candidates.sort((a, b) => b.score - a.score);
              const best = candidates[0].el;
              const bestText = candidates[0].text;

              best.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'center' });
              best.focus?.();

              const rect = best.getBoundingClientRect();
              const evtOpts = { bubbles: true, cancelable: true, view: window, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
              best.dispatchEvent(new PointerEvent('pointerdown', evtOpts));
              best.dispatchEvent(new MouseEvent('mousedown', evtOpts));
              best.dispatchEvent(new PointerEvent('pointerup', evtOpts));
              best.dispatchEvent(new MouseEvent('mouseup', evtOpts));
              best.click();

              return {
                dismissed: true,
                buttonText: bestText,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2
              };
            })()
            """
            dismissed: list[str] = []
            try:
                res = await live_chrome_bridge.request("evaluate", {"expression": dismiss_js}, timeout=6)
                if res and isinstance(res, dict) and res.get("dismissed"):
                    btn_text = res.get("buttonText") or "popup button"
                    dismissed.append(btn_text)
                    logger.info(f"Auto-dismissed popup button: '{btn_text}'")
                    x = res.get("x")
                    y = res.get("y")
                    if x is not None and y is not None:
                        try:
                            await live_chrome_bridge.request("click", {"x": x, "y": y}, timeout=3)
                        except Exception:
                            pass
                    await asyncio.sleep(0.5)
            except Exception as dismiss_err:
                logger.debug(f"Auto-dismiss check error: {dismiss_err}")
            return dismissed

        async def _highlight_live_target(target: str) -> bool:
            """Draw a visible target box/crosshair in the live page before acting."""
            expression = f"""
                (() => {{
                  const requested = {json.dumps(target or '')};
                  const needle = requested.toLowerCase().trim();
                  document.getElementById('directact-ai-target-highlight')?.remove();
                  let el = null;
                  try {{ el = document.querySelector(requested); }} catch (_) {{}}
                  if (!el && needle) {{
                    el = Array.from(document.querySelectorAll(
                      'a,button,input,select,textarea,[role="button"],[role="link"],[role="menuitem"],[role="option"]'
                    )).filter((candidate) => {{
                      const r = candidate.getBoundingClientRect();
                      return r.width > 0 && r.height > 0;
                    }}).find((candidate) => {{
                      const text = (candidate.innerText || candidate.value || candidate.getAttribute('aria-label') || candidate.getAttribute('placeholder') || '').toLowerCase();
                      return text.includes(needle);
                    }});
                  }}
                  if (!el) return false;
                  el.scrollIntoView({{behavior: 'auto', block: 'center'}});
                  const r = el.getBoundingClientRect();
                  const box = document.createElement('div');
                  box.id = 'directact-ai-target-highlight';
                  box.style.cssText = `position:fixed;left:${{r.left}}px;top:${{r.top}}px;width:${{r.width}}px;height:${{r.height}}px;z-index:2147483647;pointer-events:none;border:3px solid #ff375f;border-radius:6px;box-shadow:0 0 0 3px rgba(255,55,95,.25),0 0 22px rgba(255,55,95,.8);`;
                  const cursor = document.createElement('div');
                  cursor.style.cssText = `position:absolute;left:50%;top:50%;width:22px;height:22px;transform:translate(-50%,-50%);border:3px solid #ffe45c;border-radius:50%;box-shadow:0 0 0 2px rgba(0,0,0,.45);`;
                  box.appendChild(cursor);
                  const label = document.createElement('div');
                  label.textContent = 'AI target';
                  label.style.cssText = 'position:absolute;left:-3px;top:-29px;background:#ff375f;color:white;padding:4px 8px;border-radius:4px;font:600 12px/1.1 system-ui,sans-serif;white-space:nowrap;';
                  box.appendChild(label);
                  document.documentElement.appendChild(box);
                  setTimeout(() => box.remove(), 1800);
                  return true;
                }})()
            """
            try:
                return bool(await live_chrome_bridge.request("evaluate", {"expression": expression}, timeout=10))
            except Exception as highlight_err:
                logger.debug(f"Live target highlight failed: {highlight_err}")
                return False

        # An explicit URL is the only automatic starting point. Guessing a
        # website from task keywords made flows site-specific and could send a
        # task to the wrong place; the agent now chooses its first navigation.

        # ── Step 0: Navigate to starting URL if given ────────────────────────
        if start_url and (start_url.startswith("http://") or start_url.startswith("https://")):
            t_nav0 = asyncio.get_event_loop().time()
            await _emit_step_start(0, "navigate", f"Open {start_url}")
            try:
                await live_chrome_bridge.request("navigate", {"url": start_url}, timeout=20)
                await self._smart_wait(
                    lambda: live_chrome_bridge.request("evaluate", {"expression": "document.readyState === 'complete'"}, timeout=5),
                    max_seconds=8, description="initial page load"
                )
                last_url = await _take_live_shot(f"🌐 Navigated: {start_url}")
                dur_ms = (asyncio.get_event_loop().time() - t_nav0) * 1000
                await _emit_step_completed(0, "navigate", True, dur_ms, output=f"Opened {start_url}")
            except Exception as nav_err:
                logger.warning(f"Initial navigation failed: {nav_err}")
                dur_ms = (asyncio.get_event_loop().time() - t_nav0) * 1000
                await _emit_step_completed(0, "navigate", False, dur_ms, error=str(nav_err))

        # ── Agent loop ───────────────────────────────────────────────────────
        for step_num in range(1, max_steps + 1):
            # Proactively dismiss any popup / consent / cookie banner before LLM sees DOM
            auto_dismissed = await _auto_dismiss_popups()
            if auto_dismissed:
                step_results.append(f"Step {step_num}: auto-dismissed popup: {', '.join(auto_dismissed)}")
                await _take_live_shot(f"🚫 Auto-dismissed: {', '.join(auto_dismissed[:2])}")

            dom_context = await _get_dom_snapshot()


            # Build the agent prompt
            history_summary = "\n".join(step_results[-5:])  # last 5 steps as context
            agent_prompt = f"""You are an autonomous web automation agent controlling a real Chrome browser.

Original task: {task_query}

What you have done so far:
{history_summary or 'Nothing yet — this is step 1.'}

Current browser state:
{dom_context}

Respond with EXACTLY ONE action in this JSON format (no prose, no markdown):
{{"action": "<action_type>", "target": "<css_selector_or_url_or_text>", "value": "<text_to_type_if_applicable>", "reason": "<one_sentence_why>"}}

Allowed action_types:
- "navigate" — go to a URL (use target as full URL, value unused)
- "click" — click an element (use CSS selector or visible text as target)
- "type" — clear field and type text (target=selector, value=text to type)
- "scroll" — scroll down the page (target="down" or "up")
- "wait" — pause briefly for page animation (target="1" means 1 second)
- "read" — extract information from the page (target=css_selector or "body", value=what you need)
- "done" — task is complete (target=summary of what was accomplished)
- "fail" — task cannot be completed (target=reason why)

IMPORTANT:
- If ANY popup, modal, cookie banner, or dialog is visible (such as "Continue", "Accept", "OK", "Allow", "Close", "Stay signed out"), you MUST click that button (e.g. action: "click", target: "Continue") first to dismiss the popup before trying to interact with the background page!
- STOP IMMEDIATELY before any payment, OTP, or irreversible action — use "done" with a note
- Use "click" with visible button text when you don't have a precise CSS selector
- Fill forms field by field using "type" actions
- After each navigation, wait for the page to load before acting"""

            # Ask LLM for next action
            action_data: dict = {}
            try:
                action_data = await asyncio.wait_for(
                    llm_service.complete_json(agent_prompt),
                    timeout=60.0,
                )
            except Exception as llm_err:
                logger.error(f"Agent LLM call failed at step {step_num}: {llm_err}")
                break

            if not action_data or "action" not in action_data:
                logger.warning(f"Agent step {step_num}: provider returned no usable action")
                step_results.append(f"Step {step_num}: (parse error — skipped)")
                continue

            action  = action_data.get("action", "")
            target  = action_data.get("target", "")
            value   = action_data.get("value", "")
            reason  = action_data.get("reason", "")

            logger.info(f"Agent step {step_num}: action={action} target={target[:80]} reason={reason[:80]}")
            step_results.append(f"Step {step_num}: {action} on '{target[:60]}' — {reason[:80]}")

            step_desc = f"{action.capitalize()}: {target[:50]}"
            if reason:
                step_desc += f" ({reason[:50]})"
            await _emit_step_start(step_num, action, step_desc)
            t_step0 = asyncio.get_event_loop().time()

            # ── Execute action ────────────────────────────────────────────────
            if action == "done":
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                last_url = await _take_live_shot(f"✅ Done (step {step_num}): {target[:60]}")
                await _emit_step_completed(step_num, "done", True, dur_ms, output=target)
                return {"success": True, "output": target, "url": last_url, "steps": step_num}

            elif action == "fail":
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                last_url = await _take_live_shot(f"❌ Agent: {target[:60]}")
                await _emit_step_completed(step_num, "fail", False, dur_ms, error=target)
                return {"success": False, "error": target, "url": last_url, "steps": step_num}

            elif action == "navigate":
                step_ok = True
                step_err = ""
                nav_url = target if target.startswith("http") else f"https://{target}"
                try:
                    await live_chrome_bridge.request("navigate", {"url": nav_url}, timeout=20)
                    await self._smart_wait(
                        lambda: live_chrome_bridge.request("evaluate", {"expression": "document.readyState === 'complete'"}, timeout=5),
                        max_seconds=10, description=f"navigate to {nav_url[:50]}"
                    )
                    last_url = await _take_live_shot(f"🌐 Step {step_num}: Navigated to {nav_url[:50]}")
                except Exception as nav_err:
                    step_ok = False
                    step_err = str(nav_err)
                    step_results.append(f"Step {step_num}: navigate failed — {nav_err}")
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "navigate", step_ok, dur_ms, output=f"Navigated to {nav_url[:60]}", error=step_err)

            elif action == "click":
                step_ok = True
                step_err = ""
                highlighted = await _highlight_live_target(target)
                if highlighted:
                    last_url = await _take_live_shot(f"🎯 Step {step_num}: About to click '{target[:50]}'")
                    await asyncio.sleep(0.2)

                clicked_ok = False
                # Strategy 1: native 'interact' command (v1.3+ extension)
                if "interact" in live_chrome_bridge._capabilities:
                    try:
                        click_res = await live_chrome_bridge.request(
                            "interact",
                            {"action": "click", "target": target},
                            timeout=10,
                        )
                        if click_res and isinstance(click_res, dict) and click_res.get("clicked"):
                            clicked_ok = True
                    except Exception:
                        pass

                # Strategy 2: JS evaluate fallback (works with any extension version)
                if not clicked_ok:
                    _t = json.dumps(target)
                    js_click = f"""
                        (() => {{
                          const raw = {_t};
                          const needle = raw.toLowerCase().trim();
                          const isVis = el => {{
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                          }};
                          let el = null;
                          try {{ el = document.querySelector(raw); }} catch(_) {{}}
                          if (!el || !isVis(el)) {{
                            el = Array.from(document.querySelectorAll(
                              'a,button,input[type="submit"],input[type="button"],[role="button"],[role="link"],[role="menuitem"],[role="tab"],[role="option"]'
                            )).filter(isVis).find(e => {{
                              const t = (e.innerText||e.value||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').toLowerCase();
                              return t === needle || t.includes(needle);
                            }});
                          }}
                          if (!el || !isVis(el)) return false;
                          el.scrollIntoView({{behavior:'auto',block:'center'}});
                          const r = el.getBoundingClientRect();
                          const cx = r.left + r.width/2, cy = r.top + r.height/2;
                          const opts = {{bubbles:true,cancelable:true,view:window,clientX:cx,clientY:cy}};
                          el.dispatchEvent(new PointerEvent('pointerdown',opts));
                          el.dispatchEvent(new MouseEvent('mousedown',opts));
                          el.dispatchEvent(new PointerEvent('pointerup',opts));
                          el.dispatchEvent(new MouseEvent('mouseup',opts));
                          el.click();
                          return {{clicked:true, x:cx, y:cy}};
                        }})()
                    """
                    try:
                        res = await live_chrome_bridge.request("evaluate", {"expression": js_click}, timeout=10)
                        if res and isinstance(res, dict) and res.get("clicked"):
                            clicked_ok = True
                            x_c, y_c = res.get("x"), res.get("y")
                            if x_c is not None:
                                try:
                                    await live_chrome_bridge.request("click", {"x": x_c, "y": y_c}, timeout=5)
                                except Exception:
                                    pass
                    except Exception as js_err:
                        step_err = str(js_err)

                if clicked_ok:
                    await asyncio.sleep(0.5)
                    last_url = await _take_live_shot(f"👆 Step {step_num}: Clicked '{target[:50]}'")
                else:
                    step_ok = False
                    if not step_err:
                        step_err = f"Target not found: {target[:60]}"
                    step_results.append(f"Step {step_num}: click failed — {step_err}")
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "click", step_ok, dur_ms, output=f"Clicked '{target[:50]}'", error=step_err)

            elif action == "type":
                step_ok = True
                step_err = ""
                highlighted = await _highlight_live_target(target)
                if highlighted:
                    last_url = await _take_live_shot(f"🎯 Step {step_num}: About to type in '{target[:50]}'")
                    await asyncio.sleep(0.25)
                type_js = f"""
                    (() => {{
                      let el = null;
                      try {{ el = document.querySelector({__import__('json').dumps(target)}); }} catch(_) {{}}
                      if (!el) {{
                        const label = {__import__('json').dumps(target.lower())};
                        el = Array.from(document.querySelectorAll('input,textarea,select'))
                          .filter(e => e.getBoundingClientRect().width > 0)
                          .find(e => (e.name + ' ' + e.id + ' ' + (e.placeholder||'') + ' ' + (e.getAttribute('aria-label')||'')).toLowerCase().includes(label));
                      }}
                      if (!el) return false;
                      const owner = el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : el instanceof HTMLSelectElement
                          ? HTMLSelectElement.prototype
                          : HTMLInputElement.prototype;
                      const setter = Object.getOwnPropertyDescriptor(owner, 'value')?.set;
                      if (setter) setter.call(el, {__import__('json').dumps(value)});
                      else el.value = {__import__('json').dumps(value)};
                      el.dispatchEvent(new Event('input', {{bubbles:true}}));
                      el.dispatchEvent(new Event('change', {{bubbles:true}}));
                      el.focus();
                      return true;
                    }})()
                """
                try:
                    typed = await live_chrome_bridge.request("evaluate", {"expression": type_js}, timeout=10)
                    await asyncio.sleep(0.3)
                    last_url = await _take_live_shot(f"⌨️ Step {step_num}: Typed into '{target[:50]}'")
                    if not typed:
                        step_results.append(f"Step {step_num}: type target not found: {target[:60]}")
                        step_ok = False
                        step_err = f"Target not found: {target[:60]}"
                except Exception as type_err:
                    step_ok = False
                    step_err = str(type_err)
                    step_results.append(f"Step {step_num}: type error — {type_err}")
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "type", step_ok, dur_ms, output=f"Typed into '{target[:50]}'", error=step_err)

            elif action == "scroll":
                direction = "window.scrollBy(0, 600)" if target.lower() != "up" else "window.scrollBy(0, -600)"
                try:
                    await live_chrome_bridge.request("evaluate", {"expression": direction}, timeout=5)
                    await asyncio.sleep(0.5)
                    last_url = await _take_live_shot(f"📜 Step {step_num}: Scrolled {target}")
                except Exception:
                    pass
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "scroll", True, dur_ms, output=f"Scrolled {target}")

            elif action == "wait":
                secs = 1.0
                try:
                    secs = float(target) if target else 1.0
                    await asyncio.sleep(min(secs, 5.0))
                    last_url = await _take_live_shot(f"⏳ Step {step_num}: Waited {secs}s")
                except Exception:
                    pass
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "wait", True, dur_ms, output=f"Waited {secs}s")

            elif action == "read":
                read_js = f"""
                    (() => {{
                      let el = null;
                      try {{ el = document.querySelector({__import__('json').dumps(target)}); }} catch(_) {{}}
                      if (!el && {__import__('json').dumps(target.lower())} === 'body') el = document.body;
                      return el ? (el.innerText || el.textContent || '').slice(0, 1000) : '(not found)';
                    }})()
                """
                read_summary = ""
                step_ok = True
                step_err = ""
                try:
                    read_result = await live_chrome_bridge.request("evaluate", {"expression": read_js}, timeout=10)
                    read_summary = str(read_result)[:200]
                    step_results.append(f"Step {step_num}: read result — {read_summary}")
                    last_url = await _take_live_shot(f"🔍 Step {step_num}: Read '{target[:40]}'")
                except Exception as read_err:
                    step_ok = False
                    step_err = str(read_err)
                    step_results.append(f"Step {step_num}: read error — {read_err}")
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "read", step_ok, dur_ms, output=read_summary, error=step_err)

            else:
                logger.warning(f"Agent step {step_num}: unknown action '{action}' — skipping")

        # Max steps reached
        last_url = await _take_live_shot(f"⚠️ Agent: max steps ({max_steps}) reached")
        return {
            "success": False,
            "error": f"Agent reached max steps ({max_steps}) without completing the task",
            "url": last_url,
            "steps": max_steps,
            "partial_progress": step_results,
        }

    async def _handle_live_chrome_bus_task(self, session_id: str, task_query: str) -> dict:
        """Run the bus planning or booking flow in the user's live Chrome tab."""
        lowered = task_query.lower()
        if "book" in lowered and "do not book" not in lowered:
            return await self._handle_live_chrome_bus_booking_task(session_id, task_query)

        if not live_chrome_bridge.connected:
            return {
                "success": False,
                "error": "Live Chrome is not connected; no copied or Guest browser will be opened",
            }

        search_url = (
            "https://www.google.com/search?q="
            + quote_plus("Pune to Mumbai bus cheapest fare redBus AbhiBus")
        )
        try:
            await live_chrome_bridge.request("navigate", {"url": search_url}, timeout=20)
            await asyncio.sleep(1.0)
            visible_text = ""
            for _ in range(20):
                state = await live_chrome_bridge.request(
                    "evaluate",
                    {
                        "expression": """
                            ({
                              ready: document.readyState,
                              title: document.title,
                              text: (document.body && document.body.innerText || '').slice(0, 5000)
                            })
                        """,
                    },
                    timeout=10,
                )
                if state and state.get("ready") == "complete" and state.get("text"):
                    visible_text = state["text"]
                    break
                await asyncio.sleep(0.5)

            tab = await live_chrome_bridge.request("tab", timeout=10)
            shot = await live_chrome_bridge.request("screenshot", timeout=15)
            await manager.send_to_session(session_id, {
                "type": "viewport_screenshot",
                "label": f"🚌 Live Chrome: {tab.get('title', 'Pune to Mumbai bus search') if tab else 'Bus fare search'}",
                "data": shot.get("data") if shot else "",
                "format": "jpeg",
                "timestamp": datetime.utcnow().isoformat(),
            })
            if not visible_text:
                return {"success": False, "error": "Bus fare search did not load in live Chrome"}
            return {
                "success": True,
                "title": tab.get("title", "") if tab else "",
                "url": tab.get("url", search_url) if tab else search_url,
                "output": (
                    "Pune to Mumbai bus fare search opened in your live Chrome account. "
                    "No booking or payment was performed. Exact cheapest fare requires a travel date."
                ),
            }
        except Exception as error:
            logger.error(f"Live Chrome bus task failed: {error}", exc_info=True)
            return {"success": False, "error": str(error)}

    async def _handle_live_chrome_bus_booking_task(self, session_id: str, task_query: str) -> dict:
        """Book the cheapest matching bus, stopping before payment/QR/OTP."""
        if not live_chrome_bridge.connected:
            return {"success": False, "error": "Live Chrome is not connected; booking was not started"}

        def field(name: str, default: str = "") -> str:
            match = re.search(rf"{re.escape(name)}=([^;]+)", task_query, re.I)
            return match.group(1).strip() if match else default

        date_value = field("date", "01/09/2026")
        try:
            date_obj = datetime.strptime(date_value, "%d/%m/%Y")
            date_aria_label = f"{date_obj:%A}, {date_obj:%B} {date_obj.day}, {date_obj:%Y}"
        except ValueError:
            date_aria_label = "Tuesday, September 1, 2026"
        passenger_1 = field("passenger1_name")
        passenger_2 = field("passenger2_name")
        age_1 = field("passenger1_age", "20")
        age_2 = field("passenger2_age", "21")
        email_1 = field("passenger1_email")
        email_2 = field("passenger2_email")
        phone_1 = field("passenger1_phone")
        phone_2 = field("passenger2_phone")

        if not passenger_1 or not passenger_2 or not phone_1:
            return {"success": False, "error": "Real passenger names and contact details are required; dummy data is disabled"}

        homepage = "https://www.redbus.in/"
        try:
            await live_chrome_bridge.request("navigate", {"url": homepage}, timeout=20)
            await asyncio.sleep(1.0)

            form_state = None
            for _ in range(20):
                form_state = await live_chrome_bridge.request(
                    "evaluate",
                    {
                        "expression": f"""
                        (() => {{
                          const inputs = Array.from(document.querySelectorAll('input'));
                          const find = (terms) => inputs.find(el => terms.some(term =>
                            ((el.id || '') + ' ' + (el.name || '') + ' ' + (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase().includes(term)));
                          const setValue = (el, value) => {{
                            if (!el) return false;
                            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(el, value); else el.value = value;
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                          }};
                          const source = document.querySelector('#srcinput') || find(['source', 'from', 'origin', 'src']);
                          const destination = document.querySelector('#destinput') || find(['destination', 'to', 'dest']);
                          const date = find(['onward', 'travel date', 'journey date', 'date']);
                          source?.focus();
                          if (source) source.value = '';
                          if (destination) destination.value = '';
                          return {{
                            source: !!source,
                            destination: !!destination,
                            date: !!date
                          }};
                        }})()
                        """,
                    },
                    timeout=15,
                )
                if form_state and form_state.get("source") and form_state.get("destination"):
                    break
                await asyncio.sleep(0.5)
            if not form_state or not form_state.get("source") or not form_state.get("destination"):
                return {"success": False, "error": "redBus source/destination form was not available"}

            await live_chrome_bridge.request(
                "evaluate",
                {"expression": "document.querySelector('#srcinput')?.focus(); document.execCommand('insertText', false, 'Pune'); true"},
                timeout=10,
            )

            await asyncio.sleep(1.2)
            await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const exact = (value) => Array.from(document.querySelectorAll('[role="option"], li'))
                            .filter(el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0)
                            .find(el => (el.innerText || '').trim().toLowerCase().startsWith(value));
                          const source = exact('pune to mumbai bus') || exact('pune');
                          if (source) source.click();
                          return !!source;
                        })()
                    """,
                },
                timeout=10,
            )
            await asyncio.sleep(1.2)
            await live_chrome_bridge.request(
                "evaluate",
                {"expression": "document.querySelector('#destinput')?.focus(); true"},
                timeout=10,
            )
            await live_chrome_bridge.request(
                "evaluate",
                {"expression": "document.querySelector('#destinput')?.focus(); document.execCommand('insertText', false, 'Mumbai'); true"},
                timeout=10,
            )
            await asyncio.sleep(1.2)
            await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const exact = (value) => Array.from(document.querySelectorAll('[role="option"], li'))
                            .filter(el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0)
                            .find(el => (el.innerText || '').trim().toLowerCase().startsWith(value));
                          const destination = exact('mumbai') || exact('mumbai to pune bus');
                          if (destination) destination.click();
                          return !!destination;
                        })()
                    """,
                },
                timeout=10,
            )

            await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const control = document.querySelector('[aria-label="Select date of journey"]');
                          if (control) { control.click(); return true; }
                          return false;
                        })()
                    """,
                },
                timeout=10,
            )
            await asyncio.sleep(0.5)
            await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const next = Array.from(document.querySelectorAll('button, [role="button"]'))
                            .find(el => /next|forward|arrow-right/i.test((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')));
                          if (next) next.click();
                          return !!next;
                        })()
                    """,
                },
                timeout=10,
            )
            await asyncio.sleep(0.5)
            await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const target = '{date_aria_label}';
                          const cell = document.querySelector(`[aria-label="${target}"]`)
                            || Array.from(document.querySelectorAll('.calendarDate, [role="gridcell"], [role="option"]'))
                              .find(el => (el.getAttribute('aria-label') || '').trim() === target);
                          if (cell && cell.getBoundingClientRect().width > 0) {
                            (cell.parentElement?.matches('li') ? cell.parentElement : cell).click();
                            return true;
                          }
                          return false;
                        })()
                    """,
                },
                timeout=10,
            )

            await asyncio.sleep(0.8)
            # Build dynamic date strings from actual date_obj (Fix 1 — no more hardcoded date)
            _expected_date      = f"{date_obj.day} {date_obj.strftime('%b')}, {date_obj.year}"
            _expected_date_zero = f"{date_obj.strftime('%d %b, %Y')}"
            date_state = await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": f"""
                        (() => {{
                          const text = (document.querySelector('[aria-label="Select date of journey"]')?.innerText || document.body?.innerText || '');
                          return {{
                              date_text: text.slice(0, 160),
                              target_present: text.includes('{_expected_date}') || text.includes('{_expected_date_zero}')
                          }};
                        }})()
                    """,
                },
                timeout=10,
            )
            if not date_state or not date_state.get("target_present"):
                return {"success": False, "error": f"redBus did not accept the requested journey date ({date_value}); booking stopped before search"}

            await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const buttons = Array.from(document.querySelectorAll('button, input[type="submit"]'));
                          const search = buttons.find(el => /search buses|search/i.test((el.innerText || el.value || '').trim()));
                          if (search) search.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                          return !!search;
                        })()
                    """,
                },
                timeout=10,
            )
            # Smart wait — poll every 100ms for bus list to appear, max 6s (Fix 4)
            await self._smart_wait(
                lambda: live_chrome_bridge.request(
                    "evaluate",
                    {"expression": r"""!!document.querySelector('button[class*="view"], button[class*="select"], button[class*="seat"], .bus-card, .bus-item, .rb-tabs-head')"""},
                    timeout=5,
                ),
                max_seconds=6.0,
                description="bus search results loaded",
            )

            selected_bus = await live_chrome_bridge.request(
                "evaluate",
                {
                    # Fix 2 — raw string r""" so \s does not produce a SyntaxWarning
                    "expression": r"""
                        (() => {
                          const buttons = Array.from(document.querySelectorAll('button'))
                            .filter(el => /view seats|select seats/i.test((el.innerText || '').trim()));
                          const candidates = buttons.map(button => {
                            let card = button;
                            for (let i = 0; i < 6 && card.parentElement; i++) {
                              if (/\u20b9\s?[\d,]+/.test(card.innerText || '')) break;
                              card = card.parentElement;
                            }
                            const match = (card.innerText || '').match(/\u20b9\s?([\d,]+)/);
                            return { button, fare: match ? Number(match[1].replace(/,/g, '')) : Number.MAX_SAFE_INTEGER };
                          }).sort((a, b) => a.fare - b.fare)[0];
                          if (candidates?.button) { candidates.button.click(); return candidates.fare; }
                          return null;
                        })()
                    """,
                },
                timeout=15,
            )
            if selected_bus is None:
                return {"success": False, "error": "No bus seat-selection button was found"}
            # Smart wait — check every 100ms for seat map, max 5 seconds (Fix 4)
            await self._smart_wait(
                lambda: live_chrome_bridge.request(
                    "evaluate",
                    {"expression": r"""!!document.querySelector('[class*=\"seat\"], [data-testid*=\"seat\"]')"""},
                    timeout=5,
                ),
                max_seconds=5.0,
                description="seat map loaded",
            )

            seat_selected = await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const seats = Array.from(document.querySelectorAll('[class*="seat"], [data-testid*="seat"]'))
                            .filter(el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0)
                            .filter(el => !/sold|unavailable|booked|selected/i.test(el.className + ' ' + (el.getAttribute('aria-label') || '')));
                          if (seats[0]) { seats[0].click(); return true; }
                          return false;
                        })()
                    """,
                },
                timeout=15,
            )
            if not seat_selected:
                return {"success": False, "error": "No available seat could be selected automatically"}

            await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const buttons = Array.from(document.querySelectorAll('button'));
                          const next = buttons.find(el => /continue|proceed/i.test((el.innerText || '').trim()));
                          if (next) next.click();
                          return !!next;
                        })()
                    """,
                },
                timeout=10,
            )
            await asyncio.sleep(2.0)

            filled = await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": f"""
                        (() => {{
                          const inputs = Array.from(document.querySelectorAll('input'));
                          const setValue = (el, value) => {{
                            if (!el) return false;
                            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(el, value); else el.value = value;
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                          }};
                          const textInputs = inputs.filter(el => /text|email|tel|number/.test(el.type || 'text'));
                          const by = (terms) => inputs.find(el => terms.some(term =>
                            ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase().includes(term)));
                          let count = 0;
                          count += setValue(by(['email']), '{email_1}') ? 1 : 0;
                          count += setValue(by(['mobile', 'phone', 'contact']), '{phone_1}') ? 1 : 0;
                          const names = inputs.filter(el => /name|passenger/.test((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '')).toLowerCase());
                          count += setValue(names[0], '{passenger_1}') ? 1 : 0;
                          count += setValue(names[1], '{passenger_2}') ? 1 : 0;
                          const ages = inputs.filter(el => /age/.test((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '')).toLowerCase());
                          count += setValue(ages[0], '{age_1}') ? 1 : 0;
                          count += setValue(ages[1], '{age_2}') ? 1 : 0;
                          return count;
                        }})()
                    """,
                },
                timeout=15,
            )

            const_state = await live_chrome_bridge.request(
                "evaluate",
                {
                    "expression": """
                        (() => {
                          const text = (document.body?.innerText || '').slice(0, 12000);
                          const qr = /\bqr\b|scan.*pay|upi|payment/i.test(text);
                          return { qr, title: document.title, text: text.slice(0, 1200) };
                        })()
                    """,
                },
                timeout=10,
            )
            const_state = const_state or {}
            const_shot = await live_chrome_bridge.request("screenshot", timeout=15)
            await manager.send_to_session(session_id, {
                "type": "viewport_screenshot",
                "label": "🚌 Live Chrome booking stopped before payment/QR",
                "data": const_shot.get("data") if const_shot else "",
                "format": "jpeg",
                "timestamp": datetime.utcnow().isoformat(),
            })
            if const_state.get("qr"):
                return {"success": True, "title": const_state.get("title", ""), "output": "QR/payment page reached; automation stopped before OTP or payment"}
            return {"success": True, "title": const_state.get("title", ""), "output": "Passenger details filled in live Chrome; automation stopped before payment/QR"}
        except Exception as error:
            logger.error(f"Live Chrome bus booking failed: {error}", exc_info=True)
            return {"success": False, "error": str(error)}

    async def _handle_live_chrome_youtube_task(self, session_id: str, task_query: str) -> dict:
        """Run the YouTube flow in the user's real, already-open Chrome tab."""
        if not live_chrome_bridge.connected:
            return {
                "success": False,
                "error": (
                    "Live Chrome is not connected. Load chrome-extension/ in Chrome as an unpacked extension "
                    "and keep the signed-in Chrome window open."
                ),
            }

        query, target_index = parse_youtube_request(task_query)
        logger.info(f"Live Chrome YouTube request: query='{query}', target_index={target_index}")
        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        try:
            await live_chrome_bridge.request("navigate", {"url": search_url}, timeout=15)
            await asyncio.sleep(1.5)

            results = None
            for _ in range(30):
                results = await live_chrome_bridge.request(
                    "evaluate",
                    {
                        "expression": """
                            (() => {
                              // Auto-dismiss any Google / YouTube consent popup or "Continue" dialog
                              const consentBtns = Array.from(document.querySelectorAll(
                                'button, [role="button"], a, input[type="submit"]'
                              )).filter(b => {
                                const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().toLowerCase();
                                return t === 'accept all' || t === 'i agree' || t === 'agree' || t === 'continue' || t === 'reject all' || t.includes('accept all') || t === 'stay signed out';
                              });
                              if (consentBtns.length > 0) {
                                consentBtns[0].click();
                              }

                              const playlists = [];
                              // 1. Specific YouTube playlist renderers
                              document.querySelectorAll('ytd-playlist-renderer').forEach(renderer => {
                                const titleEl = renderer.querySelector('#video-title, h3 a');
                                const fullLink = renderer.querySelector('a[href*="/playlist?list="]');
                                const watchLink = renderer.querySelector('a[href*="list="]');
                                const title = (titleEl?.innerText || titleEl?.getAttribute('title') || '').trim();
                                const href = fullLink?.href || watchLink?.href || '';
                                if (title && href) {
                                  playlists.push({ title, href });
                                }
                              });
                              // 2. Any link containing list=
                              document.querySelectorAll('a[href*="list="]').forEach(a => {
                                const title = (a.innerText || a.getAttribute('title') || a.getAttribute('aria-label') || '').trim();
                                const href = a.href;
                                if (title && href && !title.toLowerCase().includes('view full playlist') && !playlists.some(p => p.href === href)) {
                                  playlists.push({ title, href });
                                }
                              });

                              const videos = [];
                              document.querySelectorAll('ytd-video-renderer, #contents ytd-video-renderer').forEach(renderer => {
                                const titleEl = renderer.querySelector('#video-title');
                                if (titleEl && titleEl.href && !titleEl.href.includes('/shorts/')) {
                                  const title = (titleEl.innerText || titleEl.getAttribute('title') || '').trim();
                                  if (title) videos.push({ title, href: titleEl.href });
                                }
                              });
                              if (videos.length === 0) {
                                document.querySelectorAll('a#video-title[href*="watch?v="]').forEach(a => {
                                  const title = (a.innerText || a.getAttribute('title') || '').trim();
                                  if (title && !a.href.includes('/shorts/')) videos.push({ title, href: a.href });
                                });
                              }

                              return {
                                ready: document.readyState,
                                playlists,
                                videos,
                              };
                            })()
                        """,
                    },
                    timeout=10,
                )
                if results and (results.get("playlists") or results.get("videos")):
                    break
                await asyncio.sleep(0.5)

            if not results:
                return {"success": False, "error": "Live Chrome returned no YouTube results"}

            playlist_request = bool(re.search(r"\b(playlist|playlists|channel)\b", task_query, re.I))
            query_tokens = [token for token in query.lower().split() if len(token) > 2]
            playlists = results.get("playlists") or []
            videos = results.get("videos") or []

            logger.info(f"YouTube parsed: {len(playlists)} playlists, {len(videos)} videos (is_playlist={playlist_request})")

            if playlist_request and playlists:
                chosen = next(
                    (
                        item for item in playlists
                        if sum(token in item["title"].lower() for token in query_tokens) >= max(1, min(2, len(query_tokens)))
                    ),
                    playlists[0],
                )
                list_match = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", chosen["href"])
                playlist_url = f"https://www.youtube.com/playlist?list={list_match.group(1)}" if list_match else chosen["href"]
                logger.info(f"Opening playlist URL: {playlist_url}")
                await live_chrome_bridge.request("navigate", {"url": playlist_url}, timeout=15)
                await asyncio.sleep(1.5)

                playlist_items = None
                for _ in range(30):
                    playlist_items = await live_chrome_bridge.request(
                        "evaluate",
                        {
                            "expression": """
                            (() => {
                              const items = [];
                              const seen = new Set();
                              document.querySelectorAll('ytd-playlist-video-renderer').forEach(renderer => {
                                const titleEl = renderer.querySelector('#video-title, a.yt-simple-endpoint[href*="watch?v="]');
                                if (!titleEl) return;
                                const href = titleEl.href || '';
                                const match = href.match(/watch\\?v=([a-zA-Z0-9_-]+)/);
                                const videoId = match ? match[1] : href;
                                if (videoId && !seen.has(videoId)) {
                                  seen.add(videoId);
                                  const title = (titleEl.innerText || titleEl.getAttribute('title') || '').trim();
                                  items.push({ title, href, videoId });
                                }
                              });
                              if (items.length === 0) {
                                document.querySelectorAll('a[href*="watch?v="]').forEach(a => {
                                  const href = a.href || '';
                                  const match = href.match(/watch\\?v=([a-zA-Z0-9_-]+)/);
                                  const videoId = match ? match[1] : '';
                                  const title = (a.innerText || a.getAttribute('title') || '').trim();
                                  if (videoId && title && !seen.has(videoId) && !href.includes('/shorts/')) {
                                    seen.add(videoId);
                                    items.push({ title, href, videoId });
                                  }
                                });
                              }
                              return items;
                            })()
                            """,
                        },
                        timeout=10,
                    )
                    if playlist_items and len(playlist_items) > target_index:
                        break
                    await asyncio.sleep(0.5)

                if not playlist_items:
                    return {"success": False, "error": "Live Chrome opened the playlist but no videos were found"}

                selected_idx = min(target_index, len(playlist_items) - 1)
                selected = playlist_items[selected_idx]
                logger.info(f"Selected playlist video index {selected_idx}: {selected['title']} ({selected['href']})")
                await live_chrome_bridge.request("navigate", {"url": selected["href"]}, timeout=15)
                selected_title = selected.get("title", "")
            else:
                if not videos:
                    return {"success": False, "error": f"No YouTube videos found for '{query}'"}
                selected_idx = min(target_index, len(videos) - 1)
                selected = videos[selected_idx]
                logger.info(f"Selected search video index {selected_idx}: {selected['title']} ({selected['href']})")
                await live_chrome_bridge.request("navigate", {"url": selected["href"]}, timeout=15)
                selected_title = selected.get("title", "")

            await asyncio.sleep(1.5)
            playing = False
            for _ in range(20):
                playing = await live_chrome_bridge.request(
                    "evaluate",
                    {
                        "expression": """
                            (async () => {
                              const video = document.querySelector('video');
                              if (!video) return false;
                              video.muted = false;
                              if (video.paused) { try { await video.play(); } catch (_) {} }
                              const button = document.querySelector('.ytp-play-button');
                              if (video.paused && button) button.click();
                              return !video.paused;
                            })()
                        """,
                    },
                    timeout=10,
                )
                if playing:
                    break
                await asyncio.sleep(0.5)

            tab = await live_chrome_bridge.request("tab", timeout=10)
            shot = await live_chrome_bridge.request("screenshot", timeout=15)
            label = f"▶️ Live Chrome: {tab.get('title', '') if tab else selected_title}"
            await manager.send_to_session(session_id, {
                "type": "viewport_screenshot",
                "label": label,
                "data": shot.get("data") if shot else "",
                "format": "jpeg",
                "timestamp": datetime.utcnow().isoformat(),
            })
            return {
                "success": True,
                "title": tab.get("title", "") if tab else selected_title,
                "url": tab.get("url", "") if tab else selected.get("href", ""),
                "output": f"Now playing video #{target_index + 1} ({selected_title}) in YouTube playlist in live Chrome",
            }
        except Exception as error:
            logger.error(f"Live Chrome YouTube task failed: {error}", exc_info=True)
            return {"success": False, "error": str(error)}

    async def _handle_youtube_task(self, session_id: str, page: Any, task_query: str) -> dict:
        """
        Execute YouTube search, find playlists/videos, select specified video index, and play.
        """
        query, target_index = parse_youtube_request(task_query)
        logger.info(f"YouTube automation: query='{query}', target_video_index={target_index}")

        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"

        # 1. Navigate to YouTube Search
        await self._stream_screenshot(session_id, page, f"🔍 Searching YouTube: '{query}'")
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.5)
            await self._stream_screenshot(session_id, page, f"🔍 Loaded results for '{query}'")
        except Exception as e:
            logger.error(f"YouTube search navigation error: {e}")
            return {"success": False, "error": f"YouTube navigation failed: {e}"}

        # 2. Find and select the target video / playlist item
        try:
            # Wait for search results
            await page.wait_for_selector(
                "ytd-video-renderer, ytd-playlist-renderer, a#video-title, #video-title",
                timeout=15000
            )
            # For playlist requests, select the playlist result itself first;
            # only then select the requested item inside that playlist.
            playlist_request = bool(re.search(r"\b(playlist|channel|madhil)\b", task_query, re.I))
            playlist_elements = await page.query_selector_all(
                "ytd-playlist-renderer a#video-title, ytd-playlist-renderer a#thumbnail"
            )
            valid_playlists = []
            for element in playlist_elements:
                try:
                    title = (await element.inner_text()).strip()
                    if title:
                        valid_playlists.append((element, title))
                except Exception:
                    continue

            elements = await page.query_selector_all("ytd-video-renderer a#video-title, a#video-title")
            valid_videos = []
            for element in elements:
                try:
                    title = (await element.inner_text()).strip()
                    if title:
                        valid_videos.append((element, title))
                except Exception:
                    continue

            logger.info(
                f"YouTube search found {len(valid_videos)} videos and {len(valid_playlists)} playlists"
            )

            chosen_from_playlist = playlist_request and bool(valid_playlists)
            if chosen_from_playlist:
                # Prefer a title containing the requested subject, otherwise
                # the first playlist result is the deterministic fallback.
                query_tokens = [token for token in query.lower().split() if len(token) > 2]
                target_element, video_title = next(
                    (
                        (element, title)
                        for element, title in valid_playlists
                        if sum(token in title.lower() for token in query_tokens) >= max(1, min(2, len(query_tokens)))
                    ),
                    valid_playlists[0],
                )
                chosen_idx = 0
            else:
                valid_elements = valid_videos
                if not valid_elements:
                    return {
                        "success": False,
                        "error": f"No YouTube results found for '{query}'",
                        "url": page.url,
                    }
                chosen_idx = target_index if target_index < len(valid_elements) else 0
                target_element, video_title = valid_elements[chosen_idx]

            # Highlight element
            try:
                await page.evaluate(
                    """(el) => {
                        if (el) {
                            el.style.outline = '4px solid #ef4444';
                            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        }
                    }""",
                    target_element,
                )
            except Exception:
                pass

            selection_label = f"playlist result: {video_title}" if chosen_from_playlist else f"video #{chosen_idx+1}: {video_title}"
            await self._stream_screenshot(session_id, page, f"👆 Selecting {selection_label}")

            # Click on the chosen video/playlist
            await target_element.click()
            await asyncio.sleep(1.0)

            # 3. If navigated into a playlist overview page, select the
            # requested video inside it (the user's ordinal applies here).
            current_url = page.url
            if "/playlist?list=" in current_url:
                await self._stream_screenshot(session_id, page, f"📂 Playlist opened, selecting video #{target_index+1}")
                try:
                    await page.wait_for_selector(
                        "ytd-playlist-video-renderer a#video-title, ytd-playlist-video-renderer a#thumbnail",
                        timeout=10000,
                    )
                    pl_videos = await page.query_selector_all(
                        "ytd-playlist-video-renderer a#video-title"
                    )
                    if not pl_videos:
                        pl_videos = await page.query_selector_all(
                            "ytd-playlist-video-renderer a#thumbnail"
                        )
                    if not pl_videos:
                        return {"success": False, "error": "Playlist opened but contains no videos", "url": current_url}
                    pl_idx = target_index if target_index < len(pl_videos) else 0
                    await pl_videos[pl_idx].click()
                    await asyncio.sleep(1.0)
                except Exception as pl_err:
                    logger.warning(f"Playlist sub-selection failed: {pl_err}")
                    return {"success": False, "error": f"Could not select playlist video: {pl_err}", "url": page.url}

            # 4. Trigger video playback & unpause.
            try:
                await page.wait_for_selector("video", timeout=10000)
                playing = await page.evaluate(
                    """async () => {
                        const video = document.querySelector('video');
                        if (!video) return false;
                        video.muted = false;
                        if (video.paused) {
                            try { await video.play(); } catch (_) {}
                        }
                        const playBtn = document.querySelector('.ytp-play-button');
                        if (video.paused && playBtn) playBtn.click();
                        return !video.paused;
                    }"""
                )
            except Exception as play_err:
                logger.warning(f"YouTube playback trigger failed: {play_err}")
                playing = False

            await asyncio.sleep(0.7)
            final_title = await page.title()
            await self._stream_screenshot(session_id, page, f"▶️ Playing: {final_title}")
            return {
                "success": bool(playing),
                "title": final_title,
                "url": page.url,
                "output": f"Now playing video #{target_index+1} ({video_title}) on YouTube" if playing else "Video opened but playback did not start",
                "error": None if playing else "YouTube video opened but is paused",
            }

        except Exception as sel_err:
            logger.warning(f"Interactive element selection error on YouTube: {sel_err}")

        # Do not report success when the requested result could not be found;
        # otherwise the UI claims the task ran while nothing was executed.
        final_title = await page.title()
        await self._stream_screenshot(session_id, page, f"YouTube: {final_title}")
        return {"success": False, "title": final_title, "url": page.url, "error": "Could not locate the requested YouTube result"}

    async def navigate(self, session_id: str, url: str) -> dict:
        """Navigate to a URL and stream a screenshot."""
        try:
            page = await self.get_or_create_page(session_id)
        except Exception as e:
            return {"success": False, "error": str(e)}

        # Security check
        verdict = security_guard.scan_url(url)
        if not verdict.allowed:
            return {"success": False, "error": f"URL blocked: {verdict.reason}"}

        try:
            logger.info(f"Navigating to {url} (session={session_id})")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.0)
            title = await page.title()
            await self._stream_screenshot(session_id, page, f"Navigated to: {title}")
            return {"success": True, "url": url, "title": title}
        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return {"success": False, "error": str(e)}

    async def run_agent_task(
        self,
        task_query: str,
        session_id: str,
        start_url: Optional[str] = None,
    ) -> dict:
        """
        Run a full browser-use Agent task with user's selected Chrome profile.
        Streams step updates and screenshots to the frontend.
        """
        try:
            if bool(getattr(settings, "mcp_use_system_chrome", False)):
                return {
                    "success": False,
                    "error": "Live Chrome bridge is required for system-Chrome automation; copied profiles are disabled",
                }
            from browser_use import Agent, BrowserProfile
        except ImportError:
            logger.error("browser-use package is not installed.")
            return {"success": False, "error": "browser-use is not installed"}

        llm = _get_browser_use_llm()
        if not llm:
            logger.warning("No LLM configured for browser-use Agent; cannot complete an interactive task.")
            return {
                "success": False,
                "error": "No browser-agent LLM is configured for this interactive task",
            }

        use_system_profile = bool(getattr(settings, "mcp_use_system_chrome", False))
        if use_system_profile:
            user_data_dir = _get_chrome_user_data_dir()
            profile_name = _get_active_profile_name()
            exe_path = _find_chrome_executable()
        else:
            user_data_dir = _get_dedicated_profile_dir(session_id)
            profile_name = "Default"
            exe_path = None

        profile = BrowserProfile(
            user_data_dir=str(user_data_dir),
            profile_directory=profile_name,
            executable_path=exe_path,
            channel=None if exe_path else "chromium",
            headless=False,
            highlight_elements=True,
            keep_alive=True,
        )

        async def step_callback(state, output, step_num):
            try:
                if hasattr(state, "screenshot") and state.screenshot:
                    b64 = state.screenshot
                    if isinstance(b64, bytes):
                        b64 = base64.b64encode(b64).decode("utf-8")
                    await manager.send_to_session(session_id, {
                        "type": "viewport_screenshot",
                        "label": f"Step {step_num}: {getattr(output, 'current_state', '')}",
                        "data": b64,
                        "format": "jpeg",
                        "timestamp": datetime.utcnow().isoformat(),
                    })
            except Exception as cb_err:
                logger.debug(f"Step callback error: {cb_err}")

        try:
            logger.info(f"Running browser-use Agent for task: '{task_query}' with profile '{profile_name}'")
            agent_task = task_query
            if start_url:
                agent_task = f"Start at {start_url}. Then complete this task: {task_query}"
            agent = Agent(
                task=agent_task,
                llm=llm,
                browser_profile=profile,
                use_vision=False,
                register_new_step_callback=step_callback,
            )
            history = await agent.run()
            result_text = str(history)
            logger.info(f"✅ browser-use Agent completed task: {result_text[:200]}")
            return {"success": True, "output": result_text}
        except Exception as e:
            logger.error(f"browser-use Agent task failed: {e}", exc_info=True)
            return {"success": False, "error": f"Browser agent failed: {e}"}

    async def click_element(self, session_id: str, selector: str, description: str = "") -> dict:
        """Click an element with visual highlight."""
        page = self._pages.get(session_id)
        if not page:
            return {"success": False, "error": "No active browser session"}

        try:
            await self._highlight_element(page, selector)
            await asyncio.sleep(0.3)
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

    async def _highlight_element(self, page, selector: str):
        """Draw a red target box and yellow crosshair before a browser click."""
        try:
            await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return;
                    document.getElementById('directact-ai-target-highlight')?.remove();
                    el.scrollIntoView({ behavior: 'auto', block: 'center' });
                    const r = el.getBoundingClientRect();
                    const box = document.createElement('div');
                    box.id = 'directact-ai-target-highlight';
                    box.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;z-index:2147483647;pointer-events:none;border:3px solid #ff375f;border-radius:6px;box-shadow:0 0 0 3px rgba(255,55,95,.25),0 0 22px rgba(255,55,95,.8);`;
                    const cursor = document.createElement('div');
                    cursor.style.cssText = 'position:absolute;left:50%;top:50%;width:22px;height:22px;transform:translate(-50%,-50%);border:3px solid #ffe45c;border-radius:50%;box-shadow:0 0 0 2px rgba(0,0,0,.45);';
                    box.appendChild(cursor);
                    document.documentElement.appendChild(box);
                    setTimeout(() => box.remove(), 1800);
                }""",
                selector,
            )
        except Exception:
            pass

    async def _stream_screenshot(self, session_id: str, page, label: str = ""):
        """Take a screenshot and send it to the frontend via WebSocket."""
        try:
            screenshot_bytes = await page.screenshot(type="jpeg", quality=75)
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            await manager.send_to_session(session_id, {
                "type": "viewport_screenshot",
                "label": label,
                "data": b64,
                "format": "jpeg",
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            logger.debug(f"Screenshot streaming failed: {e}")

    async def cleanup_all(self):
        """Close all browser sessions on shutdown."""
        for session_id in list(self._browsers.keys()):
            await self.close_session(session_id)


# Singleton
web_engine = WebAutomationEngine()
