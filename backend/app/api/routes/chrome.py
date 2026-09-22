"""
DirectAct-AI — Chrome Profile Discovery & Selection
Auto-detects all Chrome profiles on the system with account info.
"""
import json
import logging
import os
import platform
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.core.config import settings
from app.services.live_chrome_bridge import live_chrome_bridge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chrome", tags=["chrome"])


class ChromeProfile(BaseModel):
    """Detected Chrome profile."""
    folder_name: str        # e.g. "Default", "Profile 1"
    display_name: str       # e.g. "Mihir Patil"
    email: str              # e.g. "mihir@gmail.com"
    avatar_index: int       # Chrome avatar icon index
    is_using: bool          # Whether this is the currently active profile


class ChromeProfilesResponse(BaseModel):
    profiles: list[ChromeProfile]
    active_profile: str
    user_data_dir: str


class SetProfileRequest(BaseModel):
    folder_name: str


def _get_chrome_user_data_dir() -> Path:
    """Get the Chrome User Data directory for the current OS."""
    configured = str(getattr(settings, "chrome_user_data_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser()

    system = platform.system()
    home = Path.home()

    if system == "Windows":
        return home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":  # macOS
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    else:  # Linux
        return home / ".config" / "google-chrome"


def _read_profile_info(profile_path: Path) -> Optional[dict]:
    """Read profile display name and email from Preferences file."""
    prefs_file = profile_path / "Preferences"
    if not prefs_file.exists():
        return None

    try:
        with open(prefs_file, "r", encoding="utf-8", errors="replace") as f:
            prefs = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # Extract profile info
    profile_info = prefs.get("profile", {})
    account_info = prefs.get("account_info", [])

    display_name = profile_info.get("name", "")
    avatar_index = profile_info.get("avatar_index", 0)

    # Try to get email from account_info
    email = ""
    if account_info and isinstance(account_info, list):
        for acc in account_info:
            if isinstance(acc, dict) and acc.get("email"):
                email = acc["email"]
                break

    # Fallback: try gaia_info
    if not email:
        gaia_info = profile_info.get("gaia_info_picture_url", "")
        gaia_name = prefs.get("google", {}).get("services", {}).get("signin", {}).get("email", "")
        if gaia_name:
            email = gaia_name

    return {
        "display_name": display_name or profile_path.name,
        "email": email,
        "avatar_index": avatar_index,
    }


def _discover_profiles(user_data_dir: Path) -> list[dict]:
    """Scan Chrome User Data directory for all profiles."""
    profiles = []

    if not user_data_dir.exists():
        logger.warning(f"Chrome User Data directory not found: {user_data_dir}")
        return profiles

    # Check "Default" profile
    default_path = user_data_dir / "Default"
    if default_path.exists():
        info = _read_profile_info(default_path)
        if info:
            profiles.append({"folder_name": "Default", **info})

    # Check "Profile N" directories
    try:
        for entry in sorted(user_data_dir.iterdir()):
            if entry.is_dir() and entry.name.startswith("Profile "):
                info = _read_profile_info(entry)
                if info:
                    profiles.append({"folder_name": entry.name, **info})
    except OSError as e:
        logger.error(f"Error scanning Chrome profiles: {e}")

    return profiles


# In-memory active profile (can be overridden per session)
_active_profile = (
    os.getenv("CHROME_PROFILE_NAME", "").strip()
    or str(getattr(settings, "chrome_profile_name", "Default") or "Default").strip()
    or "Default"
)


@router.get("/profiles", response_model=ChromeProfilesResponse)
async def get_chrome_profiles():
    """Auto-detect all Chrome profiles on this system."""
    global _active_profile

    user_data_dir = _get_chrome_user_data_dir()
    raw_profiles = _discover_profiles(user_data_dir)

    if not raw_profiles:
        # Return at least the configured profile
        return ChromeProfilesResponse(
            profiles=[
                ChromeProfile(
                    folder_name=_active_profile,
                    display_name=_active_profile,
                    email="",
                    avatar_index=0,
                    is_using=True,
                )
            ],
            active_profile=_active_profile,
            user_data_dir=str(user_data_dir),
        )

    profiles = [
        ChromeProfile(
            folder_name=p["folder_name"],
            display_name=p["display_name"],
            email=p["email"],
            avatar_index=p["avatar_index"],
            is_using=(p["folder_name"] == _active_profile),
        )
        for p in raw_profiles
    ]

    return ChromeProfilesResponse(
        profiles=profiles,
        active_profile=_active_profile,
        user_data_dir=str(user_data_dir),
    )


@router.post("/profile")
async def set_active_profile(body: SetProfileRequest):
    """Set the active Chrome profile for browser automation."""
    global _active_profile

    user_data_dir = _get_chrome_user_data_dir()
    profile_path = user_data_dir / body.folder_name

    if not profile_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Chrome profile '{body.folder_name}' not found",
        )

    _active_profile = body.folder_name

    # Also update the environment variable so main.py picks it up
    os.environ["CHROME_PROFILE_NAME"] = body.folder_name

    logger.info(f"🔄 Active Chrome profile changed to: {body.folder_name}")

    return {
        "message": f"Active profile set to '{body.folder_name}'",
        "active_profile": body.folder_name,
    }


@router.get("/bridge-status")
async def chrome_bridge_status():
    """Report whether the live signed-in Chrome bridge is connected."""
    return {
        **live_chrome_bridge.status,
        "mode": "live_chrome_extension",
        "setup_directory": "chrome-extension",
    }


@router.get("/live-state-debug")
async def live_state_debug():
    """Return non-sensitive UI diagnostics for the active live tab."""
    if not live_chrome_bridge.connected:
        return {"connected": False}
    tab = await live_chrome_bridge.request("tab", timeout=10)
    ui = await live_chrome_bridge.request(
        "evaluate",
        {
            "expression": """
                (() => ({
                  ready: document.readyState,
                  title: document.title,
                  url: location.href,
                  inputs: Array.from(document.querySelectorAll('input')).slice(0, 30).map(el => ({id: el.id, name: el.name, placeholder: el.placeholder, type: el.type, value: el.value})),
                  buttons: Array.from(document.querySelectorAll('button')).slice(0, 40).map(el => (el.innerText || '').trim()).filter(Boolean),
                  date_controls: Array.from(document.querySelectorAll('[id], [class], [aria-label]')).filter(el => {
                    const text = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.id || '') + ' ' + (el.className || '')).toLowerCase();
                    return /date|onward|journey|calendar/.test(text) && el.getBoundingClientRect().width > 0;
                  }).slice(0, 20).map(el => ({tag: el.tagName, id: el.id, text: (el.innerText || '').trim().slice(0, 80), aria: el.getAttribute('aria-label')})),
                  visible_calendar_buttons: Array.from(document.querySelectorAll('button, [role="button"], [role="gridcell"], [role="option"]'))
                    .filter(el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0)
                    .filter(el => /calendar|date|month|next|previous|sep|aug|2026|1/.test(((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase()))
                    .slice(0, 80)
                    .map(el => ({tag: el.tagName, text: (el.innerText || '').trim().slice(0, 120), aria: el.getAttribute('aria-label'), title: el.getAttribute('title'), cls: String(el.className || '').slice(0, 160)})),
                  target_date_dom: (() => {
                    const el = document.querySelector('[aria-label="Tuesday, September 1, 2026"]');
                    return el ? {outer: el.outerHTML.slice(0, 1200), parent: el.parentElement?.outerHTML.slice(0, 1600)} : null;
                  })()
                }))()
            """,
        },
        timeout=10,
    )
    return {"connected": True, "tab": tab, "ui": ui}


@router.get("/live-source-options-debug")
async def live_source_options_debug():
    """Inspect only visible city autocomplete labels while fixing live booking."""
    if not live_chrome_bridge.connected:
        return {"connected": False}
    result = await live_chrome_bridge.request(
        "evaluate",
        {
            "expression": """
                (() => {
                  const input = document.querySelector('#srcinput');
                  if (!input) return {value: '', options: []};
                  input.focus();
                  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                  if (setter) setter.call(input, 'Pune'); else input.value = 'Pune';
                  input.dispatchEvent(new Event('input', {bubbles: true}));
                  input.dispatchEvent(new Event('change', {bubbles: true}));
                  return {
                    value: input.value,
                    options: Array.from(document.querySelectorAll('[role="option"], li, [class*="suggest"], [class*="auto"]'))
                      .filter(el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0)
                      .map(el => ({tag: el.tagName, role: el.getAttribute('role'), text: (el.innerText || '').trim().slice(0, 120)}))
                      .filter(x => x.text.toLowerCase().includes('pune'))
                      .slice(0, 20)
                  };
                })()
            """,
        },
        timeout=15,
    )
    return {"connected": True, "result": result}
