"""
Installed Application Discovery Engine — Phase 1.2
=====================================================
Scans the OS for installed applications, caches results in SQLite with TTL,
and provides a normalized inventory for the Task Intent Router and UI sidebar.

Per-OS sources:
  Windows: Registry Uninstall keys + App Paths + Start Menu shortcuts
  Linux:   .desktop files + package manager queries (stub)
  macOS:   /Applications + Launch Services (stub)

Output: InstalledApplication — normalized, category-tagged, executable-resolved.
Enables "open Excel" → resolve to concrete executable path, with LibreOffice Calc
as a fallback if Excel isn't installed.

Design: Repository + Strategy (per-OS scanner).
"""
from __future__ import annotations

import json
import logging
import os
import platform
import sqlite3
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, List

logger = logging.getLogger(__name__)

# Cache TTL in seconds (30 minutes)
_CACHE_TTL = 1800
_CACHE_DB = "./logs/app_inventory.db"


class AppCategory(str, Enum):
    BROWSER = "browser"
    OFFICE = "office"
    DEV_TOOL = "dev_tool"
    COMMUNICATION = "communication"
    MEDIA = "media"
    UTILITY = "utility"
    SECURITY = "security"
    EDUCATION = "education"
    GAME = "game"
    UNKNOWN = "unknown"


@dataclass
class InstalledApplication:
    name: str
    canonical_id: str           # Normalized lowercase slug: "google-chrome"
    executable_path: Optional[str]
    version: Optional[str]
    category: AppCategory
    publisher: Optional[str] = None
    install_location: Optional[str] = None
    is_default_handler_for: List[str] = None  # e.g. ["http", "https", ".html"]
    os_family: str = "windows"

    def __post_init__(self):
        if self.is_default_handler_for is None:
            self.is_default_handler_for = []


# Category keywords for automatic classification
_CATEGORY_KEYWORDS = {
    AppCategory.BROWSER: ["chrome", "firefox", "edge", "opera", "safari", "brave", "vivaldi"],
    AppCategory.OFFICE: ["word", "excel", "powerpoint", "outlook", "onenote", "libreoffice",
                          "calc", "impress", "writer", "office", "notion", "obsidian"],
    AppCategory.DEV_TOOL: ["code", "vscode", "visual studio", "intellij", "pycharm", "webstorm",
                            "git", "github", "docker", "terminal", "powershell", "cmd",
                            "notepad++", "sublime", "atom", "vim", "neovim"],
    AppCategory.COMMUNICATION: ["teams", "slack", "discord", "zoom", "skype", "telegram",
                                  "whatsapp", "signal", "meet", "webex"],
    AppCategory.MEDIA: ["vlc", "spotify", "netflix", "youtube", "media player", "winamp",
                         "audacity", "obs", "handbrake", "premiere", "photoshop"],
    AppCategory.SECURITY: ["defender", "antivirus", "malwarebytes", "kaspersky", "avast",
                             "bitdefender", "norton", "mcafee"],
    AppCategory.UTILITY: ["7-zip", "winrar", "notepad", "calculator", "snipping", "paint",
                           "task manager", "control panel", "settings"],
}


def _classify(name: str) -> AppCategory:
    lower = name.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return AppCategory.UNKNOWN


def _make_canonical_id(name: str) -> str:
    return name.lower().strip().replace(" ", "-").replace(".", "-")


# ──────────────────────────────────────────────────────────────────────────────
# SQLite Cache
# ──────────────────────────────────────────────────────────────────────────────

def _init_cache_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_inventory (
            canonical_id TEXT PRIMARY KEY,
            name TEXT,
            executable_path TEXT,
            version TEXT,
            category TEXT,
            publisher TEXT,
            install_location TEXT,
            is_default_handler_for TEXT,
            os_family TEXT,
            scanned_at REAL
        )
    """)
    conn.commit()
    return conn


def _cache_apps(apps: List[InstalledApplication], db_path: str):
    conn = _init_cache_db(db_path)
    now = time.time()
    conn.execute("DELETE FROM app_inventory")
    for app in apps:
        conn.execute(
            "INSERT OR REPLACE INTO app_inventory VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                app.canonical_id, app.name, app.executable_path,
                app.version, app.category.value, app.publisher,
                app.install_location,
                json.dumps(app.is_default_handler_for),
                app.os_family, now,
            )
        )
    conn.commit()
    conn.close()
    logger.info(f"AppDiscovery: cached {len(apps)} apps")


def _load_cached(db_path: str) -> Optional[List[InstalledApplication]]:
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT scanned_at FROM app_inventory LIMIT 1").fetchone()
        if not row or (time.time() - row[0]) > _CACHE_TTL:
            conn.close()
            return None
        rows = conn.execute("SELECT * FROM app_inventory").fetchall()
        conn.close()
        apps = []
        for r in rows:
            apps.append(InstalledApplication(
                canonical_id=r[0], name=r[1], executable_path=r[2],
                version=r[3], category=AppCategory(r[4]), publisher=r[5],
                install_location=r[6],
                is_default_handler_for=json.loads(r[7] or "[]"),
                os_family=r[8],
            ))
        return apps
    except Exception as e:
        logger.warning(f"AppDiscovery: cache load failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Windows Scanner
# ──────────────────────────────────────────────────────────────────────────────

def _scan_windows() -> List[InstalledApplication]:
    apps: List[InstalledApplication] = []
    seen: set = set()

    def _add(name: str, exe: Optional[str], version: Optional[str], publisher: Optional[str],
              install_loc: Optional[str]):
        cid = _make_canonical_id(name)
        if cid in seen:
            return
        seen.add(cid)
        apps.append(InstalledApplication(
            name=name, canonical_id=cid,
            executable_path=exe, version=version,
            category=_classify(name), publisher=publisher,
            install_location=install_loc, os_family="windows",
        ))

    # 1. Registry Uninstall keys
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as root:
                        i = 0
                        while True:
                            try:
                                key_name = winreg.EnumKey(root, i)
                                with winreg.OpenKey(root, key_name) as sub:
                                    def _qv(name, default=""):
                                        try:
                                            return winreg.QueryValueEx(sub, name)[0]
                                        except Exception:
                                            return default
                                    disp_name = _qv("DisplayName")
                                    if disp_name:
                                        _add(
                                            name=disp_name,
                                            exe=_qv("DisplayIcon") or None,
                                            version=_qv("DisplayVersion") or None,
                                            publisher=_qv("Publisher") or None,
                                            install_loc=_qv("InstallLocation") or None,
                                        )
                                i += 1
                            except OSError:
                                break
                except Exception:
                    pass
    except ImportError:
        logger.debug("winreg not available — skipping registry scan")

    # 2. App Paths for executables not in Uninstall
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths") as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, name) as sub:
                        try:
                            exe_path = winreg.QueryValue(sub, "")
                            app_name = name.replace(".exe", "")
                            if exe_path and os.path.exists(exe_path):
                                _add(name=app_name, exe=exe_path, version=None,
                                     publisher=None, install_loc=None)
                        except Exception:
                            pass
                    i += 1
                except OSError:
                    break
    except Exception:
        pass

    logger.info(f"AppDiscovery (Windows): found {len(apps)} apps")
    return apps


# ──────────────────────────────────────────────────────────────────────────────
# Linux Scanner (stub)
# ──────────────────────────────────────────────────────────────────────────────

def _scan_linux() -> List[InstalledApplication]:
    apps: List[InstalledApplication] = []
    desktop_dirs = [
        "/usr/share/applications",
        os.path.expanduser("~/.local/share/applications"),
    ]
    for d in desktop_dirs:
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if not fname.endswith(".desktop"):
                continue
            path = os.path.join(d, fname)
            try:
                name, exe = None, None
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("Name=") and not name:
                            name = line.split("=", 1)[1].strip()
                        elif line.startswith("Exec=") and not exe:
                            exe = line.split("=", 1)[1].strip().split()[0]
                if name:
                    apps.append(InstalledApplication(
                        name=name, canonical_id=_make_canonical_id(name),
                        executable_path=exe, version=None,
                        category=_classify(name), os_family="linux",
                    ))
            except Exception:
                pass
    logger.info(f"AppDiscovery (Linux): found {len(apps)} apps")
    return apps


# ──────────────────────────────────────────────────────────────────────────────
# macOS Scanner (stub)
# ──────────────────────────────────────────────────────────────────────────────

def _scan_macos() -> List[InstalledApplication]:
    apps: List[InstalledApplication] = []
    for app_dir in ["/Applications", os.path.expanduser("~/Applications")]:
        if not os.path.isdir(app_dir):
            continue
        for entry in os.listdir(app_dir):
            if not entry.endswith(".app"):
                continue
            name = entry[:-4]  # Strip .app
            exe_path = os.path.join(app_dir, entry, "Contents", "MacOS", name)
            if not os.path.exists(exe_path):
                exe_path = None
            apps.append(InstalledApplication(
                name=name, canonical_id=_make_canonical_id(name),
                executable_path=exe_path, version=None,
                category=_classify(name), os_family="macos",
            ))
    logger.info(f"AppDiscovery (macOS): found {len(apps)} apps")
    return apps


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

class AppDiscoveryService:
    """Installed app inventory with TTL cache and fuzzy name resolution."""

    def __init__(self, cache_db: str = _CACHE_DB):
        self._cache_db = cache_db
        self._apps: Optional[List[InstalledApplication]] = None

    def get_all(self, force_refresh: bool = False) -> List[InstalledApplication]:
        """Return full app inventory, scanning if cache is stale."""
        if not force_refresh and self._apps:
            return self._apps

        cached = _load_cached(self._cache_db)
        if cached and not force_refresh:
            self._apps = cached
            return self._apps

        system = platform.system().lower()
        if system == "windows":
            self._apps = _scan_windows()
        elif system == "darwin":
            self._apps = _scan_macos()
        else:
            self._apps = _scan_linux()

        _cache_apps(self._apps, self._cache_db)
        return self._apps

    def resolve(self, name: str) -> Optional[InstalledApplication]:
        """Find the best-matching app for a given name (fuzzy)."""
        lower = name.lower().strip()
        apps = self.get_all()

        # Exact canonical match
        for app in apps:
            if app.canonical_id == lower or app.name.lower() == lower:
                return app

        # Substring match
        for app in apps:
            if lower in app.canonical_id or lower in app.name.lower():
                return app

        return None

    def get_by_category(self, category: AppCategory) -> List[InstalledApplication]:
        return [a for a in self.get_all() if a.category == category]

    def to_sidebar_list(self) -> List[dict]:
        """Return a simplified list for the UI sidebar."""
        return [
            {"name": a.name, "canonical_id": a.canonical_id,
             "category": a.category.value, "has_executable": bool(a.executable_path)}
            for a in self.get_all()
        ]


# Singleton
app_discovery = AppDiscoveryService()
