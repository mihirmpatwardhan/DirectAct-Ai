"""
Web Automation Engine — Powered by browser-use & System Chrome Profiles
========================================================================
Executes web automation tasks using browser-use and Playwright with:
  - User's REAL Chrome profile (saved logins, cookies, passwords, chosen Google account)
  - Active profile switching (supports "Default", "Profile 1", "Profile 2", etc.)
  - Interactive multi-step actions across arbitrary public websites
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


_PAYMENT_STOP_REQUEST = re.compile(
    r"\b(?:stop(?:s|ping|ped)?|halt(?:s|ing|ed)?|pause(?:s|d)?|before|at|until|up\s+to)\b"
    r".{0,100}\b(?:payment|pay|upi|qr|scanner|scan)\b"
    r"|\b(?:payment|pay|upi|qr|scanner|scan)\b.{0,100}"
    r"\b(?:stop(?:s|ping|ped)?|halt(?:s|ing|ed)?|pause(?:s|d)?|before|at|until|up\s+to)\b",
    re.IGNORECASE | re.DOTALL,
)
_PAYMENT_PAGE_MARKERS = re.compile(
    r"\b(?:payment|upi|qr\s*code|scan\s*(?:to\s*)?pay|pay\s*(?:using|via)|payment\s*method)\b",
    re.IGNORECASE,
)
_SENSITIVE_FORM_FIELD = re.compile(
    r"\b(?:passenger|travell?er|full[ _-]?name|first[ _-]?name|last[ _-]?name|"
    r"email|e[ _-]?mail|mobile|phone|contact|address|age|gender|dob|birth|"
    r"card|cvv|upi|otp|pan|passport|identity)\b",
    re.IGNORECASE,
)


def _is_explicitly_authorized_form_value(task_query: str, target: str, value: str) -> bool:
    """Allow personal form data only when the user actually supplied that value.

    This is field- and website-agnostic. It keeps the agent from inventing a
    passenger name, age, email, phone number, or payment credential to force a
    booking flow to continue. Payment credentials are never typed by the agent.
    """
    field = f"{target} {value}"
    if _PAYMENT_PAGE_MARKERS.search(field) or re.search(r"\b(?:otp|cvv|card)\b", field, re.IGNORECASE):
        return False
    if not _SENSITIVE_FORM_FIELD.search(target):
        return True

    normalized_value = re.sub(r"[^a-z0-9]+", "", value.casefold())
    normalized_task = re.sub(r"[^a-z0-9]+", "", task_query.casefold())
    # One-character values (such as a fabricated age of "2") are never a
    # reliable proof that the user supplied a sensitive detail.
    return len(normalized_value) >= 2 and normalized_value in normalized_task


def _completion_rejection_reason(
    task_query: str,
    summary: Any,
    evidence: Any,
    visible_text: Any,
    successful_actions: int,
) -> Optional[str]:
    """Return why an agent's `done` response is not independently provable.

    This is intentionally site-agnostic.  It validates the visible browser
    state and the user's requested safe stop condition, never a vendor URL,
    CSS selector, or booking-provider-specific word.
    """
    completion = str(summary or "").strip()
    proof = str(evidence or "").strip()
    page = re.sub(r"\s+", " ", str(visible_text or "")).strip()

    if successful_actions < 1:
        return "No successful browser action has occurred yet"
    if not completion or completion.casefold() in {"stub", "completed", "done"}:
        return "The completion summary is empty or generic"
    if len(proof) < 3:
        return "The agent did not provide visible completion evidence"
    if re.sub(r"\s+", " ", proof).casefold() not in page.casefold():
        return "The claimed completion evidence is not visible in the browser"
    if _PAYMENT_STOP_REQUEST.search(task_query) and not _PAYMENT_PAGE_MARKERS.search(page):
        return "The requested payment/scan stopping point is not visible yet"

    # The visible page should also still contain at least one specific term
    # from the requested task. This prevents a generic banner such as "Done"
    # or "Search" from becoming false completion evidence on an unrelated page.
    ignored_terms = {
        "about", "after", "before", "book", "browser", "cheapest", "click", "from", "into", "open",
        "page", "payment", "please", "price", "scanner", "search", "stop", "task", "that", "the", "then",
        "this", "ticket", "until", "with", "your",
    }
    requested_terms = {
        word.casefold()
        for word in re.findall(r"[A-Za-z0-9]{3,}", task_query)
        if word.casefold() not in ignored_terms
    }
    if requested_terms and not any(re.search(rf"\b{re.escape(term)}\b", page, re.IGNORECASE) for term in requested_terms):
        return "The visible page no longer contains a specific term from the requested task"
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
            if bool(getattr(settings, "mcp_use_system_chrome", False)) and live_chrome_bridge.connected:
                # System-Chrome mode uses live extension bridge when connected.
                logger.info("System Chrome mode uses the live extension bridge; no copied browser will be launched")
                return False

            await self._ensure_playwright()

            user_data_dir = _get_chrome_user_data_dir()
            profile_name = _get_active_profile_name()
            exe_path = _find_chrome_executable()
            use_system_profile = bool(getattr(settings, "mcp_use_system_chrome", False)) and live_chrome_bridge.connected

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

        if use_live and live_chrome_bridge.connected:
            return await self._run_live_chrome_agent_task(
                session_id, task_query or url, start_url=url or None, task_id=task_id
            )
        elif use_live and not live_chrome_bridge.connected:
            logger.info(
                "Live Chrome bridge is not connected. Seamlessly falling back to Playwright autonomous browser..."
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
        decision-making brain without any site-specific hardcoding.

        Agent loop:
          1. Navigate to starting URL (if provided/inferred)
          2. Capture page DOM snapshot + screenshot
          3. Ask LLM: "Given this DOM, what is the next action to take?"
          4. Execute the LLM's action (navigate / click / type / scroll / read)
          5. Stream step progress to Timeline & screenshot to Viewport
          6. Repeat until LLM signals DONE or max_steps is reached
        """
        from app.services.llm_service import llm_service

        cfg_max = getattr(settings, "max_agent_steps", 100)
        max_steps = 1000 if cfg_max <= 0 else cfg_max
        step_results: list[str] = []
        successful_actions = 0
        invalid_or_failed_streak = 0
        last_url = ""
        agent_failure = ""
        last_attempted_step = 0

        async def _emit_step_start(step_idx: int, cmd_type: str, desc: str):
            if not task_id:
                return
            try:
                await manager.send_to_session(session_id, {
                    "type": "task_step_started",
                    "session_id": session_id,
                    "task_id": task_id,
                    "step_index": step_idx,
                    "total_steps": max_steps if max_steps <= 100 else 100,
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

        async def _get_visible_page_text() -> str:
            """Read enough rendered text to prove a requested terminal state."""
            try:
                text = await live_chrome_bridge.request(
                    "evaluate",
                    {"expression": "(document.body && document.body.innerText || '').slice(0, 12000)"},
                    timeout=6,
                )
                return str(text or "")
            except Exception as page_text_err:
                logger.debug(f"Completion evidence read failed: {page_text_err}")
                return ""

        async def _auto_dismiss_popups() -> list[str]:
            """
            Proactively detect and dismiss visible popups, modal dialogs, cookie
            banners, or consent overlays before the LLM agent inspects the DOM.
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

              // A payment/OTP dialog is the requested stopping boundary for
              // many workflows.  It is not a disposable popup, so leave it
              // visible for the completion verifier instead of clicking past it.
              const paymentBoundary = /\b(?:payment|upi|qr\s*code|scan\s*(?:to\s*)?pay|otp|cvv|card number)\b/i;
              scopes = scopes.filter(scope => !paymentBoundary.test((scope.innerText || '').replace(/\s+/g, ' ')));
              if (scopes.length === 0) return null;

              const positiveTerms = [
                'continue', 'accept all', 'accept all cookies', 'accept', 'allow all', 'allow',
                'i agree', 'agree', 'got it', 'ok', 'okay', 'dismiss', 'close', 'proceed',
                'confirm', 'understood', 'reject all', 'reject', 'cancel', 'not now', 'no thanks'
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
                    # The page-side click has already fired.  Sending another
                    # CDP click here used to double-submit buttons or click the
                    # page beneath a closing modal.
                    await asyncio.sleep(0.25)
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
                verdict = security_guard.scan_url(start_url)
                if not verdict.allowed:
                    raise RuntimeError(f"URL blocked: {verdict.reason}")
                await live_chrome_bridge.request("navigate", {"url": start_url}, timeout=20)
                await self._smart_wait(
                    lambda: live_chrome_bridge.request("evaluate", {"expression": "document.readyState !== 'loading'"}, timeout=5),
                    max_seconds=5, description="initial page load"
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
            last_attempted_step = step_num
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
{{"action": "<action_type>", "target": "<css_selector_or_url_or_text>", "value": "<text_to_type_if_applicable>", "reason": "<one_sentence_why>", "evidence": "<exact visible page text that proves done; required only for done>"}}

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
- Never use "done" just because an action was sent. Use it only when the current visible page proves the user's requested end state. For "stop at/before payment, QR, UPI, or scanner", the visible page must show that payment/scan boundary.
- For "done", set evidence to an exact short phrase currently visible on the page; do not invent it.
- Use "click" with visible button text when you don't have a precise CSS selector
- Fill forms field by field using "type" actions
- After each navigation, wait for the page to load before acting"""

            # Ask LLM for next action
            action_data: dict = {}
            try:
                action_data = await asyncio.wait_for(
                    llm_service.complete_json(agent_prompt),
                    timeout=float(settings.agent_llm_timeout_seconds),
                )
            except Exception as llm_err:
                logger.error(f"Agent LLM call failed at step {step_num}: {llm_err}")
                agent_failure = f"Agent LLM request failed at step {step_num}: {llm_err or 'no response'}"
                break

            if not action_data or "action" not in action_data:
                logger.warning(f"Agent step {step_num}: provider returned no usable action")
                step_results.append(f"Step {step_num}: (parse error — skipped)")
                invalid_or_failed_streak += 1
                if invalid_or_failed_streak >= 3:
                    return {
                        "success": False,
                        "error": "LLM returned no usable action three times; task stopped without claiming completion.",
                        "url": last_url,
                        "steps": step_num,
                        "partial_progress": step_results,
                    }
                continue

            action  = str(action_data.get("action", "")).strip().lower()
            target  = str(action_data.get("target", ""))
            value   = str(action_data.get("value", ""))
            reason  = str(action_data.get("reason", ""))
            evidence = action_data.get("evidence", "")

            logger.info(f"Agent step {step_num}: action={action} target={target[:80]} reason={reason[:80]}")
            step_results.append(f"Step {step_num}: {action} on '{target[:60]}' — {reason[:80]}")

            step_desc = f"{action.capitalize()}: {target[:50]}"
            if reason:
                step_desc += f" ({reason[:50]})"
            await _emit_step_start(step_num, action, step_desc)
            t_step0 = asyncio.get_event_loop().time()

            # ── Execute action ────────────────────────────────────────────────
            if action == "done":
                completion_reason = _completion_rejection_reason(
                    task_query, target, evidence, await _get_visible_page_text(), successful_actions
                )
                if completion_reason:
                    invalid_or_failed_streak += 1
                    step_results.append(f"Step {step_num}: rejected premature completion — {completion_reason}")
                    dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                    await _emit_step_completed(
                        step_num, "done", False, dur_ms, error=completion_reason
                    )
                    if invalid_or_failed_streak >= 3:
                        return {
                            "success": False,
                            "error": f"Agent repeatedly claimed completion without proof: {completion_reason}",
                            "url": last_url,
                            "steps": step_num,
                            "partial_progress": step_results,
                        }
                    continue
                invalid_or_failed_streak = 0
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
                    verdict = security_guard.scan_url(nav_url)
                    if not verdict.allowed:
                        raise RuntimeError(f"URL blocked: {verdict.reason}")
                    await live_chrome_bridge.request("navigate", {"url": nav_url}, timeout=20)
                    await self._smart_wait(
                        lambda: live_chrome_bridge.request("evaluate", {"expression": "document.readyState !== 'loading'"}, timeout=5),
                        max_seconds=6, description=f"navigate to {nav_url[:50]}"
                    )
                    last_url = await _take_live_shot(f"🌐 Step {step_num}: Navigated to {nav_url[:50]}")
                    successful_actions += 1
                    invalid_or_failed_streak = 0
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
                    except Exception as js_err:
                        step_err = str(js_err)

                if clicked_ok:
                    await asyncio.sleep(0.5)
                    last_url = await _take_live_shot(f"👆 Step {step_num}: Clicked '{target[:50]}'")
                    successful_actions += 1
                    invalid_or_failed_streak = 0
                else:
                    step_ok = False
                    if not step_err:
                        step_err = f"Target not found: {target[:60]}"
                    step_results.append(f"Step {step_num}: click failed — {step_err}")
                    invalid_or_failed_streak += 1
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "click", step_ok, dur_ms, output=f"Clicked '{target[:50]}'", error=step_err)

            elif action == "type":
                step_ok = True
                step_err = ""
                if not _is_explicitly_authorized_form_value(task_query, target, value):
                    step_ok = False
                    step_err = "Refusing to invent personal or payment information not supplied in the task"
                    invalid_or_failed_streak += 1
                    step_results.append(f"Step {step_num}: blocked unsafe form entry for '{target[:60]}'")
                    dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                    await _emit_step_completed(step_num, "type", False, dur_ms, error=step_err)
                    if invalid_or_failed_streak >= 3:
                        return {
                            "success": False,
                            "error": step_err,
                            "url": last_url,
                            "steps": step_num,
                            "partial_progress": step_results,
                        }
                    continue
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
                        invalid_or_failed_streak += 1
                    else:
                        successful_actions += 1
                        invalid_or_failed_streak = 0
                except Exception as type_err:
                    step_ok = False
                    step_err = str(type_err)
                    step_results.append(f"Step {step_num}: type error — {type_err}")
                    invalid_or_failed_streak += 1
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "type", step_ok, dur_ms, output=f"Typed into '{target[:50]}'", error=step_err)

            elif action == "scroll":
                direction = "window.scrollBy(0, 600)" if target.lower() != "up" else "window.scrollBy(0, -600)"
                try:
                    await live_chrome_bridge.request("evaluate", {"expression": direction}, timeout=5)
                    await asyncio.sleep(0.5)
                    last_url = await _take_live_shot(f"📜 Step {step_num}: Scrolled {target}")
                    successful_actions += 1
                    invalid_or_failed_streak = 0
                except Exception:
                    invalid_or_failed_streak += 1
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "scroll", True, dur_ms, output=f"Scrolled {target}")

            elif action == "wait":
                secs = 1.0
                try:
                    secs = float(target) if target else 1.0
                    await asyncio.sleep(min(secs, 5.0))
                    last_url = await _take_live_shot(f"⏳ Step {step_num}: Waited {secs}s")
                    invalid_or_failed_streak = 0
                except Exception:
                    invalid_or_failed_streak += 1
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
                    successful_actions += 1
                    invalid_or_failed_streak = 0
                except Exception as read_err:
                    step_ok = False
                    step_err = str(read_err)
                    step_results.append(f"Step {step_num}: read error — {read_err}")
                    invalid_or_failed_streak += 1
                dur_ms = (asyncio.get_event_loop().time() - t_step0) * 1000
                await _emit_step_completed(step_num, "read", step_ok, dur_ms, output=read_summary, error=step_err)

            else:
                logger.warning(f"Agent step {step_num}: unknown action '{action}' — skipping")
                invalid_or_failed_streak += 1

            if invalid_or_failed_streak >= 3:
                return {
                    "success": False,
                    "error": "Agent made three consecutive invalid or failed actions; stopping instead of falsely completing.",
                    "url": last_url,
                    "steps": step_num,
                    "partial_progress": step_results,
                }

        # A model/provider failure is not a max-step exhaustion. Keeping these
        # states separate prevents misleading progress and completion messages.
        if agent_failure:
            last_url = await _take_live_shot(f"❌ Agent stopped: {agent_failure[:60]}")
            return {
                "success": False,
                "error": agent_failure,
                "url": last_url,
                "steps": last_attempted_step,
                "partial_progress": step_results,
            }

        # Max steps reached
        last_url = await _take_live_shot(f"⚠️ Agent: max steps ({max_steps}) reached")
        return {
            "success": False,
            "error": f"Agent reached max steps ({max_steps}) without completing the task",
            "url": last_url,
            "steps": max_steps,
            "partial_progress": step_results,
        }

    # All browser tasks use the generic live-Chrome agent above. Keeping no
    # vendor-specific route here prevents one site's selectors or data model
    # from changing how an unrelated website is automated.
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
        task_id: str = "",
    ) -> dict:
        """
        Playwright-native LLM agent loop.
        Replaces the browser-use Agent which deadlocks on Windows (CDP 30s timeout).
        Uses the existing Playwright session (get_or_create_page) and drives the browser
        step-by-step via llm_service.complete_json — no extra dependencies required.
        """
        from app.services.llm_service import llm_service

        cfg_max = getattr(settings, "max_agent_steps", 100)
        max_steps = 1000 if cfg_max <= 0 else cfg_max
        step_results: list[str] = []
        successful_actions = 0
        invalid_or_failed_streak = 0

        # ── Ensure a Playwright page is available ─────────────────────────────
        try:
            page = await self.get_or_create_page(session_id)
        except Exception as page_err:
            return {"success": False, "error": f"Could not start browser: {page_err}"}

        async def _take_shot(label: str) -> str:
            try:
                await self._stream_screenshot(session_id, page, label)
            except Exception:
                pass
            return page.url

        async def _get_dom() -> str:
            try:
                return await page.evaluate(r"""
                (() => {
                  const tag = (el) => {
                    const t = el.tagName.toLowerCase();
                    const attrs = ['id','class','name','type','aria-label','placeholder','href','role','value','data-testid']
                      .filter(a => el.getAttribute(a))
                      .map(a => `${a}="${(el.getAttribute(a)||'').slice(0,60)}"`);
                    const text = (el.innerText || el.textContent || el.value || '').trim().slice(0, 80);
                    return `<${t} ${attrs.join(' ')}>${text}</${t}>`;
                  };
                  const els = Array.from(document.querySelectorAll(
                    'a,button,input,select,textarea,h1,h2,h3,[role="button"],[role="link"],[role="option"],[role="menuitem"],[role="tab"],[role="dialog"],[role="alertdialog"],[aria-modal]'
                  )).filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  }).slice(0, 80);
                  return `URL: ${location.href}\nTitle: ${document.title}\n\n` + els.map(tag).join('\n');
                })()
                """)
            except Exception as dom_err:
                return f"URL: {page.url}\n(DOM snapshot failed: {dom_err})"

        async def _get_visible_page_text() -> str:
            try:
                return await page.locator("body").inner_text(timeout=6000)
            except Exception as page_text_err:
                logger.debug(f"Playwright completion evidence read failed: {page_text_err}")
                return ""

        async def _dismiss_playwright_popups() -> list[str]:
            """Dismiss non-transactional overlays without relying on a website name."""
            try:
                dismissed = await page.evaluate(r"""
                    (() => {
                      const visible = (el) => {
                        if (!el) return false;
                        const box = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return box.width > 20 && box.height > 15 && style.display !== 'none' &&
                          style.visibility !== 'hidden' && style.opacity !== '0';
                      };
                      const transaction = /\b(?:payment|upi|qr\s*code|scan\s*(?:to\s*)?pay|otp|cvv|card number)\b/i;
                      const scopes = Array.from(document.querySelectorAll([
                        'dialog[open]', '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]',
                        '[class*="modal" i]', '[class*="dialog" i]', '[class*="popup" i]',
                        '[class*="consent" i]', '[class*="cookie" i]', '[class*="banner" i]',
                        '[class*="overlay" i]', '[id*="consent" i]', '[id*="cookie" i]'
                      ].join(','))).filter(visible).filter(scope => !transaction.test(scope.innerText || ''));
                      if (!scopes.length) return null;

                      const dismissLabels = new Set(['cancel', 'close', 'dismiss', 'not now', 'no thanks', 'reject', 'reject all', 'ok', 'okay']);
                      const controls = 'button, [role="button"], a, input[type="button"], input[type="submit"]';
                      const candidates = [];
                      for (const scope of scopes) {
                        const isConsent = /\b(?:cookie|consent|privacy|preferences)\b/i.test(scope.innerText || '');
                        for (const control of scope.querySelectorAll(controls)) {
                          if (!visible(control)) continue;
                          const label = (control.innerText || control.value || control.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
                          const normalized = label.toLowerCase();
                          const isDismissal = dismissLabels.has(normalized);
                          const isCookieAcceptance = isConsent && /^(?:accept|accept all|allow all|agree|i agree)$/i.test(label);
                          if (!isDismissal && !isCookieAcceptance) continue;
                          candidates.push({ control, label, score: (isDismissal ? 100 : 50) - label.length });
                        }
                      }
                      if (!candidates.length) return null;
                      candidates.sort((a, b) => b.score - a.score);
                      candidates[0].control.scrollIntoView({ block: 'center', inline: 'center' });
                      candidates[0].control.click();
                      return candidates[0].label || 'popup';
                    })()
                """)
                if dismissed:
                    await page.wait_for_timeout(250)
                    return [str(dismissed)]
            except Exception as popup_err:
                logger.debug(f"Playwright popup dismissal failed: {popup_err}")
            return []

        async def _handle_js_dialog(dialog) -> None:
            """Accept ordinary alerts, but never advance a payment/OTP prompt."""
            try:
                prompt_text = str(dialog.message or "")
                is_sensitive = _PAYMENT_PAGE_MARKERS.search(prompt_text) or re.search(
                    r"\b(?:otp|cvv|card number)\b", prompt_text, re.IGNORECASE
                )
                if dialog.type == "prompt" or is_sensitive:
                    await dialog.dismiss()
                else:
                    await dialog.accept()
            except Exception as dialog_err:
                logger.debug(f"Playwright JS dialog handling failed: {dialog_err}")

        # Native JavaScript alerts/confirmations otherwise block all page actions.
        page.on("dialog", lambda dialog: asyncio.create_task(_handle_js_dialog(dialog)))

        # ── Step 0: Navigate to starting URL if given ─────────────────────────
        last_url = page.url
        if start_url and (start_url.startswith("http://") or start_url.startswith("https://")):
            try:
                verdict = security_guard.scan_url(start_url)
                if not verdict.allowed:
                    raise RuntimeError(f"URL blocked: {verdict.reason}")
                await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1.0)
                last_url = await _take_shot(f"🌐 Opened: {start_url}")
            except Exception as nav0_err:
                logger.warning(f"Initial navigation to {start_url} failed: {nav0_err}")

        logger.info(f"▶ Playwright agent starting — task='{task_query[:120]}' max_steps={max_steps}")

        # ── Agent loop ────────────────────────────────────────────────────────
        for step_num in range(1, max_steps + 1):
            auto_dismissed = await _dismiss_playwright_popups()
            if auto_dismissed:
                step_results.append(f"Step {step_num}: auto-dismissed popup: {', '.join(auto_dismissed)}")
                last_url = await _take_shot(f"🚫 Auto-dismissed: {', '.join(auto_dismissed[:2])}")
            dom_context = await _get_dom()
            history_summary = "\n".join(step_results[-5:])

            agent_prompt = f"""You are an autonomous web automation agent controlling a real browser via Playwright.

Original task: {task_query}

What you have done so far:
{history_summary or 'Nothing yet — this is step 1.'}

Current browser state:
{dom_context}

Respond with EXACTLY ONE action in this JSON format (no prose, no markdown fences):
{{"action": "<action_type>", "target": "<css_selector_or_url_or_text>", "value": "<text_to_type_if_applicable>", "reason": "<one_sentence_why>", "evidence": "<exact visible page text that proves done; required only for done>"}}

Allowed action_types:
- "navigate"  — go to a URL (target = full URL)
- "click"     — click an element (target = CSS selector or visible button/link text)
- "type"      — clear and type text (target = CSS selector of input, value = text)
- "scroll"    — scroll page (target = "down" or "up")
- "wait"      — pause (target = seconds as string, e.g. "2")
- "read"      — extract visible text (target = CSS selector or "body")
- "done"      — task complete (target = summary of what was accomplished)
- "fail"      — task cannot be completed (target = reason why)

CRITICAL RULES:
- STOP before any payment/OTP/irreversible action — use "done" with a note
- If a popup/modal/cookie banner is visible, dismiss it first with "click"
- Never use "done" just because an action was sent. The visible page must prove the requested result; for a payment/QR/UPI/scan stopping point, that boundary must be visible.
- For "done", set evidence to an exact short phrase currently visible on the page; do not invent it.
- Prefer clicking by visible text label over complex CSS selectors
- After navigating, always wait 1-2 seconds before the next action"""

            action_data: dict = {}
            try:
                action_data = await asyncio.wait_for(
                    llm_service.complete_json(agent_prompt),
                    timeout=float(settings.agent_llm_timeout_seconds),
                )
            except Exception as llm_err:
                logger.error(f"Agent LLM call failed at step {step_num}: {llm_err}")
                break

            if not action_data or "action" not in action_data:
                step_results.append(f"Step {step_num}: (no action returned — skipped)")
                invalid_or_failed_streak += 1
                if invalid_or_failed_streak >= 3:
                    return {
                        "success": False,
                        "error": "LLM returned no usable action three times; task stopped without claiming completion.",
                        "url": last_url,
                        "steps": step_num,
                        "partial_progress": step_results,
                    }
                continue

            action = str(action_data.get("action", "")).strip().lower()
            target = str(action_data.get("target", ""))
            value  = str(action_data.get("value", ""))
            reason = str(action_data.get("reason", ""))
            evidence = action_data.get("evidence", "")

            logger.info(f"Agent step {step_num}: {action} | target={target[:80]} | {reason[:80]}")
            step_results.append(f"Step {step_num}: {action} on '{target[:60]}' — {reason[:80]}")

            if action == "done":
                completion_reason = _completion_rejection_reason(
                    task_query, target, evidence, await _get_visible_page_text(), successful_actions
                )
                if completion_reason:
                    invalid_or_failed_streak += 1
                    step_results.append(f"Step {step_num}: rejected premature completion — {completion_reason}")
                    if invalid_or_failed_streak >= 3:
                        return {
                            "success": False,
                            "error": f"Agent repeatedly claimed completion without proof: {completion_reason}",
                            "url": last_url,
                            "steps": step_num,
                            "partial_progress": step_results,
                        }
                    continue
                invalid_or_failed_streak = 0
                last_url = await _take_shot(f"✅ Done (step {step_num}): {target[:60]}")
                return {"success": True, "output": target, "url": last_url, "steps": step_num}

            elif action == "fail":
                last_url = await _take_shot(f"❌ Failed (step {step_num}): {target[:60]}")
                return {"success": False, "error": target, "url": last_url, "steps": step_num}

            elif action == "navigate":
                try:
                    verdict = security_guard.scan_url(target)
                    if not verdict.allowed:
                        raise RuntimeError(f"URL blocked: {verdict.reason}")
                    await page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1.5)
                    last_url = await _take_shot(f"🌐 Step {step_num}: Navigated to {target[:50]}")
                    successful_actions += 1
                    invalid_or_failed_streak = 0
                except Exception as e:
                    step_results.append(f"  → navigate failed: {e}")
                    invalid_or_failed_streak += 1

            elif action == "click":
                clicked = False
                # Try CSS selector first, then visible text
                try:
                    await page.click(target, timeout=6000)
                    clicked = True
                except Exception:
                    pass
                if not clicked:
                    try:
                        await page.get_by_text(target, exact=False).first.click(timeout=6000)
                        clicked = True
                    except Exception:
                        pass
                if not clicked:
                    try:
                        await page.get_by_role("button", name=target).first.click(timeout=4000)
                        clicked = True
                    except Exception:
                        pass
                await asyncio.sleep(0.8)
                last_url = await _take_shot(f"🖱 Step {step_num}: Clicked '{target[:40]}'")
                if not clicked:
                    step_results.append(f"  → click failed: element not found")
                    invalid_or_failed_streak += 1
                else:
                    successful_actions += 1
                    invalid_or_failed_streak = 0

            elif action == "type":
                if not _is_explicitly_authorized_form_value(task_query, target, value):
                    invalid_or_failed_streak += 1
                    step_results.append(f"Step {step_num}: blocked unsafe form entry for '{target[:60]}'")
                    if invalid_or_failed_streak >= 3:
                        return {
                            "success": False,
                            "error": "Refusing to invent personal or payment information not supplied in the task",
                            "url": last_url,
                            "steps": step_num,
                            "partial_progress": step_results,
                        }
                    continue
                try:
                    await page.fill(target, "")
                    await page.type(target, value, delay=40)
                    await asyncio.sleep(0.5)
                    successful_actions += 1
                    invalid_or_failed_streak = 0
                except Exception as e:
                    try:
                        await page.get_by_placeholder(target).fill(value)
                        successful_actions += 1
                        invalid_or_failed_streak = 0
                    except Exception:
                        try:
                            await page.get_by_label(target, exact=False).fill(value)
                            successful_actions += 1
                            invalid_or_failed_streak = 0
                        except Exception:
                            step_results.append(f"  → type failed: {e}")
                            invalid_or_failed_streak += 1
                last_url = await _take_shot(f"⌨️ Step {step_num}: Typed into '{target[:40]}'")

            elif action == "scroll":
                direction = 1 if (target or "down").lower() != "up" else -1
                await page.evaluate(f"window.scrollBy(0, {direction * 600})")
                await asyncio.sleep(0.5)
                last_url = await _take_shot(f"↕ Step {step_num}: Scrolled {target or 'down'}")
                successful_actions += 1
                invalid_or_failed_streak = 0

            elif action == "wait":
                secs = min(float(target or "2"), 10.0)
                await asyncio.sleep(secs)
                last_url = await _take_shot(f"⏳ Step {step_num}: Waited {secs}s")
                invalid_or_failed_streak = 0

            elif action == "read":
                try:
                    text = await page.inner_text(target if target else "body")
                    step_results.append(f"  → read: {text[:200]}")
                    successful_actions += 1
                    invalid_or_failed_streak = 0
                except Exception as e:
                    step_results.append(f"  → read failed: {e}")
                    invalid_or_failed_streak += 1

            else:
                invalid_or_failed_streak += 1

            if invalid_or_failed_streak >= 3:
                return {
                    "success": False,
                    "error": "Agent made three consecutive invalid or failed actions; stopping instead of falsely completing.",
                    "url": last_url,
                    "steps": step_num,
                    "partial_progress": step_results,
                }

        return {
            "success": False,
            "error": f"Agent reached max steps ({max_steps}) without completing the task",
            "url": last_url,
            "steps": max_steps,
        }

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
