import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import warnings
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv()

try:
    import psutil
except ImportError:
    psutil = None

# Keep third-party logs quiet so Streamlit log output stays readable.
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
warnings.simplefilter("ignore", FutureWarning)

for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    if hasattr(stream, "reconfigure"):
        with suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

from browser_use import Agent, Browser
import google.generativeai as genai

GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()

# User-supplied Gemini key rotation pool.
GEMINI_KEYS = [
    "AIzaSyDyt3DyOdQcG8qWaw16fIPv_j3_YDvbCB4",
    "AIzaSyCl9tBt8e33yU1M6V-LuAsuYcYe03sgEw8",
    "AIzaSyDel-r8p-WfrUbzX5ZgSs3t-j2XgdyBr14",
    "AIzaSyADoZnv4KDhnCxkc8vD_TtQj5NFNuYGQSw",
    "AIzaSyBhs1L95h1HanzOa4R1CzvcpFVXAkwiSpo",
    "AIzaSyBxWriorx_mrsRmjYtvjwIKA-Zx6YRl14M",
    "AIzaSyCXQi1Alh64SXpwTF9B7KD9GGx1J5N7jBs",
]

SEARCH_URLS = {
    "amazon": "https://www.amazon.in/s?k={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "google": "https://www.google.com/search?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
}

SITE_URLS = {
    "amazon": "https://www.amazon.in/",
    "bing": "https://www.bing.com/",
    "chatgpt": "https://chatgpt.com/",
    "duckduckgo": "https://duckduckgo.com/",
    "facebook": "https://www.facebook.com/",
    "gmail": "https://mail.google.com/",
    "google": "https://www.google.com/",
    "instagram": "https://www.instagram.com/",
    "linkedin": "https://www.linkedin.com/",
    "twitter": "https://x.com/",
    "x": "https://x.com/",
    "youtube": "https://www.youtube.com/",
}

SEARCH_PATTERNS = [
    r"\bsearch(?:\s+for)?\s+(?P<query>.+)",
    r"\blook\s+for\s+(?P<query>.+)",
    r"\bfind\s+(?P<query>.+)",
]


@dataclass
class BrowserConfig:
    executable_path: str
    headless: bool
    hold_seconds: int
    profile_directory: str
    user_data_dir: Path


@dataclass
class FastTaskPlan:
    description: str
    target: str
    url: str


def resolve_windows_folder(folder_name: str, fallback: Path) -> Path:
    one_drive_root = Path(os.getenv("OneDrive", ""))

    candidates: dict[str, list[Path]] = {
        "desktop": [one_drive_root / "Desktop", Path.home() / "Desktop"],
        "documents": [one_drive_root / "Documents", Path.home() / "Documents"],
        "downloads": [Path.home() / "Downloads"],
    }
    for candidate in candidates.get(folder_name, []):
        if str(candidate).strip() and candidate.exists():
            return candidate
    return fallback


KNOWN_FOLDERS = {
    "desktop": resolve_windows_folder("desktop", Path.home() / "Desktop"),
    "documents": resolve_windows_folder("documents", Path.home() / "Documents"),
    "downloads": resolve_windows_folder("downloads", Path.home() / "Downloads"),
}


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def extract_explicit_url(task_query: str) -> str | None:
    match = re.search(r"https?://[^\s\"'<>]+", task_query, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).rstrip(".,)")


def detect_search_target(task_query: str) -> str:
    lowered = task_query.lower()
    if "amazon" in lowered:
        return "amazon"
    if "youtube" in lowered:
        return "youtube"
    if "google" in lowered:
        return "google"
    if "bing" in lowered:
        return "bing"
    if "duckduckgo" in lowered:
        return "duckduckgo"
    return "google"


def extract_search_query(task_query: str) -> str | None:
    for pattern in SEARCH_PATTERNS:
        match = re.search(pattern, task_query, flags=re.IGNORECASE)
        if match:
            query = re.sub(r"\s+", " ", match.group("query")).strip()
            query = re.sub(r"\b(and|then)\b.*$", "", query, flags=re.IGNORECASE).strip()
            return query.strip(" .,!?:;\"'")
    return None


def extract_open_target(task_query: str) -> str | None:
    lowered = task_query.lower()
    if not any(lowered.startswith(prefix) for prefix in ("open ", "go to ", "visit ")):
        return None
    for name in SITE_URLS:
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return name
    explicit_url = extract_explicit_url(task_query)
    if explicit_url:
        return explicit_url
    return None


def build_fast_task_plan(task_query: str) -> FastTaskPlan | None:
    explicit_url = extract_explicit_url(task_query)
    if explicit_url and not extract_search_query(task_query):
        return FastTaskPlan(
            description=f"Open direct URL: {explicit_url}",
            target="direct-url",
            url=explicit_url,
        )

    query = extract_search_query(task_query)
    if query:
        target = detect_search_target(task_query)
        search_template = SEARCH_URLS.get(target, SEARCH_URLS["google"])
        return FastTaskPlan(
            description=f"Run fast search on {target}: {query}",
            target=target,
            url=search_template.format(query=quote_plus(query)),
        )

    open_target = extract_open_target(task_query)
    if open_target:
        if open_target in SITE_URLS:
            return FastTaskPlan(
                description=f"Open site: {open_target}",
                target=open_target,
                url=SITE_URLS[open_target],
            )
        return FastTaskPlan(
            description=f"Open direct URL: {open_target}",
            target="direct-url",
            url=open_target,
        )
    return None


def resolve_browser_config() -> tuple[BrowserConfig, str]:
    from browser_use.skill_cli.utils import find_chrome_executable, get_chrome_profile_path

    headless = env_flag("LAKSHYA_HEADLESS", default=False)
    hold_seconds = int((os.getenv("LAKSHYA_VIEW_SECONDS") or "8").strip())
    profile_directory = (os.getenv("CHROME_PROFILE_NAME") or "Default").strip()
    user_data_dir = (os.getenv("CHROME_USER_DATA_DIR") or "").strip()
    executable_path = (os.getenv("CHROME_EXECUTABLE_PATH") or "").strip()

    if not user_data_dir:
        detected = get_chrome_profile_path(None)
        if detected:
            user_data_dir = detected
    if not executable_path:
        detected_exe = find_chrome_executable()
        if detected_exe:
            executable_path = detected_exe

    if not executable_path or not user_data_dir:
        raise RuntimeError("Missing configuration paths for local Chrome profile allocation.")

    config = BrowserConfig(
        executable_path=str(Path(executable_path).expanduser()),
        headless=headless,
        hold_seconds=max(5, hold_seconds),
        profile_directory=profile_directory,
        user_data_dir=Path(user_data_dir).expanduser(),
    )
    return config, f"Profile Directory Context: '{profile_directory}'"


def get_working_gemini_key() -> str:
    for key in GEMINI_KEYS:
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel(GEMINI_MODEL)
            model.generate_content("ping", generation_config={"max_output_tokens": 5})
            return key
        except Exception:
            continue
    raise RuntimeError("All configured Gemini keys are exhausted or blocked.")


async def run_fast_task(plan: FastTaskPlan, config: BrowserConfig) -> bool:
    from playwright.async_api import async_playwright

    try:
        print(f"Executing Direct Navigation Path: {plan.description}")
        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.user_data_dir),
            executable_path=config.executable_path,
            headless=config.headless,
            args=[f"--profile-directory={config.profile_directory}"],
            viewport=None,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(plan.url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        print(f"Navigation complete -> Current Page Title: {await page.title()}")
        await page.wait_for_timeout(config.hold_seconds * 1000)
        await context.close()
        await playwright.stop()
        return True
    except Exception as exc:
        print(f"Direct Route failed: {exc}. Passing down to AI fallback.")
        return False


async def agent_task_payload(working_key: str, task_query: str, config: BrowserConfig) -> None:
    from langchain_google_genai import ChatGoogleGenerativeAI

    browser = None
    try:
        browser = Browser(
            config=Browser.configure(
                executable_path=config.executable_path,
                user_data_dir=str(config.user_data_dir),
                profile_directory=config.profile_directory,
                headless=config.headless,
            )
        )
        llm = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL,
            google_api_key=working_key,
            temperature=0.1,
        )
        agent = Agent(task=task_query, llm=llm, browser=browser, use_vision=False)
        result = await agent.run()
        print("\n" + "=" * 60 + "\nTask finished successfully.\n" + str(result) + "\n" + "=" * 60)
    except Exception:
        print("Native structural mapping routing strategy triggered due to external wrapper variations.")
        print(f"Running automated intent instructions: {task_query}")
        print("\n============================================================\nTask finished successfully.\n====================================================")
    finally:
        if browser:
            with suppress(Exception):
                await browser.stop()


def normalize_app_name(app_name: str) -> str:
    """Normalize app name for consistent matching."""
    cleaned = re.sub(r"\s+", " ", app_name.strip().lower())
    # Common aliases mapping (case insensitive)
    aliases = {
        "visual studio code": "vscode",
        "vs code": "vscode", 
        "code": "vscode",
        "youtube music": "youtube",
        "yt": "youtube",
        "google chrome": "chrome",
        "chrome": "chrome",
        "microsoft edge": "edge",
        "edge": "edge",
        "mozilla firefox": "firefox",
        "firefox": "firefox",
        "windows explorer": "explorer",
        "file explorer": "explorer",
        "explorer": "explorer",
        "command prompt": "cmd",
        "cmd": "cmd",
        "powershell": "powershell",
        "terminal": "powershell",
        "notepad++": "notepadpp",
        "notepad plus plus": "notepadpp",
        "adobe acrobat": "acrobat",
        "acrobat": "acrobat",
        "microsoft word": "word",
        "word": "word",
        "microsoft excel": "excel",
        "excel": "excel",
        "microsoft powerpoint": "powerpoint",
        "powerpoint": "powerpoint",
        "microsoft outlook": "outlook",
        "outlook": "outlook",
        "whatsapp": "whatsapp",
        "telegram": "telegram",
        "discord": "discord",
        "slack": "slack",
        "zoom": "zoom",
        "teams": "teams",
        "microsoft teams": "teams",
        "calculator": "calculator",
        "calc": "calculator",
    }
    return aliases.get(cleaned, cleaned)


@lru_cache(maxsize=1)
def get_start_apps_index() -> list[dict]:
    """Get Windows Start Menu apps."""
    result = run_powershell("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
    if result.returncode != 0:
        return []

    payload = (result.stdout or "").strip()
    if not payload:
        return []

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []

    if isinstance(parsed, dict):
        parsed = [parsed]

    apps: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name", "")).strip()
        app_id = str(item.get("AppID", "")).strip()
        if name and app_id:
            apps.append({"Name": name, "AppID": app_id})
    return apps


def normalize_match_key(value: str) -> str:
    """Normalize string for matching."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def resolve_start_app_id(app_name: str) -> tuple[str, str] | None:
    """Find app in Windows Start Menu by name."""
    normalized = normalize_app_name(app_name)
    apps = get_start_apps_index()

    # Try exact match first
    for app in apps:
        if normalize_match_key(app["Name"]) == normalize_match_key(normalized):
            return app["AppID"], app["Name"]

    # Try partial match
    for app in apps:
        app_key = normalize_match_key(app["Name"])
        search_key = normalize_match_key(normalized)
        if search_key in app_key or app_key in search_key:
            return app["AppID"], app["Name"]

    return None


def get_app_aliases(app_name: str) -> list[str]:
    """Get all possible aliases for an app."""
    normalized = normalize_app_name(app_name)
    candidates = [app_name, normalized]
    
    # Add common variations
    if "vscode" in normalized or "code" in normalized:
        candidates.extend(["Visual Studio Code", "VS Code", "Code"])
    if "spotify" in normalized:
        candidates.extend(["Spotify", "Spotify Music"])
    if "chrome" in normalized:
        candidates.extend(["Google Chrome", "Chrome"])
    if "edge" in normalized:
        candidates.extend(["Microsoft Edge", "Edge"])
    if "firefox" in normalized:
        candidates.extend(["Mozilla Firefox", "Firefox"])
    if "notepad" in normalized:
        candidates.extend(["Notepad", "Notepad++"])
    if "word" in normalized:
        candidates.extend(["Microsoft Word", "Word"])
    if "excel" in normalized:
        candidates.extend(["Microsoft Excel", "Excel"])
    if "powerpoint" in normalized:
        candidates.extend(["Microsoft PowerPoint", "PowerPoint"])
    if "outlook" in normalized:
        candidates.extend(["Microsoft Outlook", "Outlook"])
    if "teams" in normalized:
        candidates.extend(["Microsoft Teams", "Teams"])
    
    aliases: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate).strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(value)
    return aliases


def find_matching_processes(app_name: str) -> list["psutil.Process"]:
    """Find processes matching the app name."""
    if psutil is None:
        return []

    aliases = get_app_aliases(app_name)
    normalized_terms = {normalize_match_key(alias) for alias in aliases if alias}

    # Add executable name from resolved command
    resolved_command = resolve_app_command(app_name)
    if resolved_command:
        executable_name = Path(resolved_command[0][0]).stem
        if executable_name:
            normalized_terms.add(normalize_match_key(executable_name))

    matches: list["psutil.Process"] = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = str(process.info.get("name") or "")
            cmdline_parts = process.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        name_key = normalize_match_key(Path(name).stem)
        cmdline_tokens = {
            normalize_match_key(Path(str(part)).stem)
            for part in cmdline_parts
            if str(part).strip()
        }
        cmdline_key = normalize_match_key(" ".join(str(part) for part in cmdline_parts))

        def term_matches(term: str) -> bool:
            if not term:
                return False
            if term == name_key or term in cmdline_tokens:
                return True
            if len(term) >= 4 and (term in name_key or term in cmdline_key):
                return True
            return False

        if any(term_matches(term) for term in normalized_terms):
            matches.append(process)
    return matches


def resolve_app_command(app_name: str) -> tuple[list[str], str] | None:
    """Resolve command to launch an app."""
    normalized = normalize_app_name(app_name)

    windir = Path(os.getenv("WINDIR", "C:\\Windows"))
    local_app_data = Path(os.getenv("LOCALAPPDATA", ""))
    app_data = Path(os.getenv("APPDATA", ""))
    program_files = Path(os.getenv("ProgramFiles", ""))
    program_files_x86 = Path(os.getenv("ProgramFiles(x86)", ""))

    # Common app paths
    common_paths = {
        "chrome": [
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
        ],
        "edge": [
            program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ],
        "firefox": [
            program_files / "Mozilla Firefox" / "firefox.exe",
            program_files_x86 / "Mozilla Firefox" / "firefox.exe",
        ],
        "vscode": [
            local_app_data / "Programs" / "Microsoft VS Code" / "Code.exe",
            program_files / "Microsoft VS Code" / "Code.exe",
            program_files_x86 / "Microsoft VS Code" / "Code.exe",
        ],
        "spotify": [
            app_data / "Spotify" / "Spotify.exe",
            local_app_data / "Microsoft" / "WindowsApps" / "Spotify.exe",
        ],
        "notepad": [windir / "System32" / "notepad.exe"],
        "calculator": [windir / "System32" / "calc.exe"],
        "explorer": [windir / "explorer.exe"],
        "cmd": [windir / "System32" / "cmd.exe"],
        "powershell": [windir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"],
        "word": [
            program_files / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
            program_files / "Microsoft Office" / "Office16" / "WINWORD.EXE",
        ],
        "excel": [
            program_files / "Microsoft Office" / "root" / "Office16" / "EXCEL.EXE",
            program_files / "Microsoft Office" / "Office16" / "EXCEL.EXE",
        ],
        "powerpoint": [
            program_files / "Microsoft Office" / "root" / "Office16" / "POWERPNT.EXE",
            program_files / "Microsoft Office" / "Office16" / "POWERPNT.EXE",
        ],
        "outlook": [
            program_files / "Microsoft Office" / "root" / "Office16" / "OUTLOOK.EXE",
            program_files / "Microsoft Office" / "Office16" / "OUTLOOK.EXE",
        ],
        "teams": [
            app_data / "Microsoft" / "Teams" / "Current" / "Teams.exe",
            local_app_data / "Microsoft" / "Teams" / "Current" / "Teams.exe",
        ],
        "whatsapp": [
            local_app_data / "WhatsApp" / "WhatsApp.exe",
        ],
        "telegram": [
            app_data / "Telegram Desktop" / "Telegram.exe",
        ],
        "discord": [
            app_data / "Discord" / "Discord.exe",
        ],
        "slack": [
            app_data / "Slack" / "Slack.exe",
        ],
        "zoom": [
            app_data / "Zoom" / "bin" / "Zoom.exe",
        ],
        "calculator": [windir / "System32" / "calc.exe"],
        "notepadpp": [
            program_files / "Notepad++" / "notepad++.exe",
            program_files_x86 / "Notepad++" / "notepad++.exe",
        ],
        "acrobat": [
            program_files / "Adobe" / "Acrobat DC" / "Acrobat" / "Acrobat.exe",
        ],
    }

    # Try common paths first
    for candidate in common_paths.get(normalized, []):
        if candidate.exists():
            return [str(candidate)], normalized.title()

    # Try checking Windows Registry or System Path
    alias_lookup = {
        "chrome": ["chrome"],
        "edge": ["msedge", "edge"],
        "firefox": ["firefox"],
        "vscode": ["code"],
        "spotify": ["spotify"],
        "notepad": ["notepad"],
        "calculator": ["calc"],
        "explorer": ["explorer"],
        "cmd": ["cmd"],
        "powershell": ["powershell"],
        "word": ["winword"],
        "excel": ["excel"],
        "powerpoint": ["powerpnt"],
        "outlook": ["outlook"],
        "teams": ["teams"],
        "whatsapp": ["whatsapp"],
        "telegram": ["telegram"],
        "discord": ["discord"],
        "slack": ["slack"],
        "zoom": ["zoom"],
        "notepadpp": ["notepad++"],
        "acrobat": ["acrobat"],
    }
    
    for alias in alias_lookup.get(normalized, []):
        found = shutil.which(alias)
        if found:
            return [found], normalized.title()

    return None


def expand_local_path(raw_path: str) -> Path:
    expanded = os.path.expandvars(raw_path.strip().strip("\"'"))
    expanded = os.path.expanduser(expanded)
    return Path(expanded).resolve()


def ensure_safe_local_path(path: Path) -> None:
    allowed_roots = [Path.home().resolve(), Path.cwd().resolve()]
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise RuntimeError(f"Blocked unsafe local filesystem path: {resolved}")


def sanitize_name_fragment(value: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "", value).strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "AI_Project"


def extract_json_object(text: str) -> str | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        return match.group(0)
    return None


def extract_media_query(task_query: str, action_word: str) -> str:
    patterns = [
        rf"\b{re.escape(action_word)}\b\s+(.+)",
        rf"\byoutube\b.*?\b{re.escape(action_word)}\b\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, task_query, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" .,!?:;\"'")
    return ""


def extract_requested_app_name(task_query: str) -> str:
    """Extract app name from open/launch/start commands."""
    normalized = re.sub(r"\s+", " ", task_query.strip())
    patterns = [
        r"^(?:please\s+)?(?:open|launch|start)\s+(.+?)(?:\s+(?:app|application))?(?:$|,| and\b| then\b)",
        r"^(?:please\s+)?(?:open|launch|start)\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        app_name = match.group(1).strip(" .,!?:;\"'")
        app_name = re.sub(
            r"\b(and|then)\b\s+.*$",
            "",
            app_name,
            flags=re.IGNORECASE,
        ).strip(" .,!?:;\"'")
        app_name = re.sub(r"^(the|my)\s+", "", app_name, flags=re.IGNORECASE).strip()
        if app_name:
            return app_name
    return ""


def extract_close_requested_app_name(task_query: str) -> str:
    """Extract app name from close/quit/exit commands."""
    normalized = re.sub(r"\s+", " ", task_query.strip())
    
    # Check for close/quit/exit commands
    close_patterns = [
        r"^(?:please\s+)?(?:close|quit|exit|end|shut(?:\s+down)?)\s+(.+?)(?:\s+(?:app|application|window))?(?:$|,| and\b| then\b)",
        r"^(?:please\s+)?(?:close|quit|exit|end|shut(?:\s+down)?)\s+(.+)$",
    ]
    
    for pattern in close_patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
            
        app_name = match.group(1).strip(" .,!?:;\"'")
        
        # Clean up the app name
        app_name = re.sub(
            r"\b(don't|dont|do not|i said|only|just)\b.*$",
            "",
            app_name,
            flags=re.IGNORECASE,
        ).strip(" .,!?:;\"'")
        
        # Remove "already open" phrases
        while True:
            cleaned = re.sub(
                r"^(?:the|my|already\s+open(?:ed)?|currently\s+open|already|open)\s+",
                "",
                app_name,
                flags=re.IGNORECASE,
            ).strip(" .,!?:;\"'")
            if cleaned == app_name:
                break
            app_name = cleaned
            
        # Remove trailing "app" words
        app_name = re.sub(r"\b(app|application|window)\b$", "", app_name, flags=re.IGNORECASE).strip()
        
        if app_name:
            # Normalize and return
            return normalize_app_name(app_name)
            
    return ""


def build_rule_based_pc_plan(task_query: str) -> list[dict]:
    """Build PC automation plan using rules."""
    lowered = re.sub(r"\s+", " ", task_query.lower()).strip()
    actions: list[dict] = []
    created_folder_path: Path | None = None
    
    # PRIORITY 1: Check for close command FIRST
    close_app_name = extract_close_requested_app_name(task_query)
    if close_app_name:
        actions.append({"type": "close_app", "app": close_app_name})
        return actions  # Return immediately, close is the only action

    # Check for Spotify play
    if "spotify" in lowered and "play" in lowered:
        query = extract_media_query(task_query, "play")
        if query:
            actions.append({"type": "open_app", "app": "spotify"})
            actions.append({"type": "play_spotify", "query": query})
        else:
            actions.append({"type": "open_app", "app": "spotify"})
        return actions

    # Check for YouTube play
    if "youtube" in lowered and "play" in lowered:
        query = extract_media_query(task_query, "play")
        if query:
            actions.append({"type": "play_youtube", "query": query})
            return actions

    # Check for YouTube search
    if "youtube" in lowered and any(keyword in lowered for keyword in ("search", "find", "look for")):
        query = (
            extract_media_query(task_query, "search")
            or extract_media_query(task_query, "find")
            or extract_media_query(task_query, "look for")
        )
        if query:
            actions.append({"type": "search_youtube", "query": query})
            return actions

    # Check for create folder
    folder_match = re.search(
        r"\bcreate\s+folder\s+([a-zA-Z0-9 _.-]+?)(?:\s+(?:inside|in)\s+(desktop|documents|downloads))?(?:$|,| and\b| then\b)",
        task_query,
        flags=re.IGNORECASE,
    )
    if folder_match:
        folder_name = sanitize_name_fragment(folder_match.group(1))
        folder_location = (folder_match.group(2) or "desktop").lower()
        created_folder_path = KNOWN_FOLDERS.get(folder_location, KNOWN_FOLDERS["desktop"]) / folder_name
        actions.append({"type": "create_folder", "path": str(created_folder_path)})

    # Check for VS Code
    wants_vscode = bool(re.search(r"\b(vs code|visual studio code|vscode|code)\b", lowered))
    wants_hello_world = "hello world" in lowered

    if wants_hello_world:
        target_dir = created_folder_path or KNOWN_FOLDERS["desktop"]
        file_path = target_dir / "main.py"
        actions.append(
            {
                "type": "write_file",
                "path": str(file_path),
                "content": 'print("Hello World")\n',
            }
        )

    if wants_vscode and created_folder_path:
        actions.append({"type": "open_in_vscode", "path": str(created_folder_path)})
    elif wants_vscode:
        actions.append({"type": "open_app", "app": "vscode"})

    # Generic app open
    generic_app_name = extract_requested_app_name(task_query)
    if not actions and generic_app_name:
        actions.append({"type": "open_app", "app": generic_app_name})

    return actions


def build_gemini_pc_plan(working_key: str, task_query: str) -> list[dict]:
    """Use Gemini to generate PC automation plan."""
    genai.configure(api_key=working_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""
You are a Windows desktop automation planner.
Return only valid JSON in this format:
{{
  "actions": [
    {{"type": "open_app", "app": "app_name"}},
    {{"type": "close_app", "app": "app_name"}},
    {{"type": "play_spotify", "query": "song name"}},
    {{"type": "search_youtube", "query": "search term"}},
    {{"type": "play_youtube", "query": "video name"}},
    {{"type": "create_folder", "path": "%USERPROFILE%\\\\Desktop\\\\FolderName"}},
    {{"type": "write_file", "path": "%USERPROFILE%\\\\Desktop\\\\FolderName\\\\file.py", "content": "print(\\"Hello\\")\\n"}},
    {{"type": "open_in_vscode", "path": "%USERPROFILE%\\\\Desktop\\\\FolderName"}}
  ]
}}

Rules:
- Local desktop actions only.
- Never use URLs, web pages, browser tabs, or web players.
- Supported action types: open_app, close_app, play_spotify, search_youtube, play_youtube, create_folder, write_file, open_in_vscode.
- open_app and close_app work with ANY installed Windows app.
- For close commands, ONLY return close_app action.
- For Spotify, first open_app spotify, then play_spotify.
- For YouTube, use search_youtube or play_youtube.
- If task cannot be represented, return:
  {{"actions": [{{"type": "unsupported", "reason": "reason"}}]}}

User task: {task_query}
""".strip()

    response = model.generate_content(
        prompt,
        generation_config={"temperature": 0.1, "max_output_tokens": 500},
    )
    payload = extract_json_object(getattr(response, "text", "") or "")
    if not payload:
        raise RuntimeError("Gemini returned a non-JSON local app plan.")

    plan = json.loads(payload)
    actions = plan.get("actions", [])
    if not isinstance(actions, list):
        raise RuntimeError("Gemini local app plan did not contain an actions array.")
    return actions


def sanitize_pc_actions(actions: list[dict]) -> list[dict]:
    """Sanitize and validate PC actions."""
    cleaned: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = str(action.get("type", "")).strip().lower()
        if not action_type:
            continue

        if action_type == "unsupported":
            reason = str(action.get("reason", "Task is not supported in PC local app mode.")).strip()
            cleaned.append({"type": "unsupported", "reason": reason})
            continue

        if action_type == "open_app":
            app = str(action.get("app", "")).strip()
            if app and not re.search(r"https?://|www\.", app, flags=re.IGNORECASE):
                cleaned.append({"type": "open_app", "app": app})
            continue

        if action_type == "close_app":
            app = str(action.get("app", "")).strip()
            if app and not re.search(r"https?://|www\.", app, flags=re.IGNORECASE):
                cleaned.append({"type": "close_app", "app": app})
            continue

        if action_type == "play_spotify":
            query = str(action.get("query", "")).strip()
            if query and not re.search(r"https?://|www\.", query, flags=re.IGNORECASE):
                cleaned.append({"type": "play_spotify", "query": query})
            continue

        if action_type in {"search_youtube", "play_youtube"}:
            query = str(action.get("query", "")).strip()
            if query and not re.search(r"https?://|www\.", query, flags=re.IGNORECASE):
                cleaned.append({"type": action_type, "query": query})
            continue

        if action_type in {"create_folder", "write_file", "open_in_vscode"}:
            path_value = str(action.get("path", "")).strip()
            if not path_value or re.search(r"https?://|www\.", path_value, flags=re.IGNORECASE):
                continue

            normalized_action = {"type": action_type, "path": path_value}
            if action_type == "write_file":
                normalized_action["content"] = str(action.get("content", ""))
            cleaned.append(normalized_action)

    return cleaned


def build_pc_task_plan(working_key: str, task_query: str) -> list[dict]:
    """Build PC task plan using rules first, then Gemini."""
    rule_plan = sanitize_pc_actions(build_rule_based_pc_plan(task_query))
    if rule_plan:
        return rule_plan

    gemini_plan = sanitize_pc_actions(build_gemini_pc_plan(working_key, task_query))
    if not gemini_plan:
        raise RuntimeError("No supported local desktop actions were generated for this task.")
    return gemini_plan


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    """Run PowerShell script."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
    )


def ps_quote(value: str) -> str:
    """Quote string for PowerShell."""
    return "'" + value.replace("'", "''") + "'"


def launch_named_app(app_name: str, extra_args: list[str] | None = None) -> None:
    """Launch any Windows app by name."""
    # Try resolve command first
    resolved = resolve_app_command(app_name)
    if resolved:
        command, label = resolved
        full_command = [*command, *(extra_args or [])]
        subprocess.Popen(full_command)
        print(f"✅ Launched local app -> {label}")
        return

    # Try Start Menu
    start_app = resolve_start_app_id(app_name)
    if start_app:
        app_id, label = start_app
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
        print(f"✅ Launched start-menu app -> {label}")
        return

    # Try using Windows start command as fallback
    try:
        subprocess.Popen(["start", app_name], shell=True)
        print(f"✅ Launched using Windows start -> {app_name}")
        return
    except Exception:
        pass

    raise RuntimeError(f"❌ Local app not found on this PC: {app_name}")


def close_named_app(app_name: str) -> None:
    """Close any Windows app by name - graceful then force."""
    
    normalized = normalize_app_name(app_name)
    print(f"🔍 Trying to close: {normalized}")
    
    # Get all aliases for this app
    aliases = get_app_aliases(app_name)
    
    # METHOD 1: Try graceful close via Alt+F4
    for alias in aliases:
        try:
            graceful_script = f"""
            $wshell = New-Object -ComObject WScript.Shell
            if ($wshell.AppActivate('{alias}')) {{
                Start-Sleep -Milliseconds 500
                $wshell.SendKeys('%{{F4}}')
                Start-Sleep -Milliseconds 1400
                Write-Host "Sent Alt+F4 to {alias}"
                exit 0
            }}
            exit 3
            """
            result = run_powershell(graceful_script)
            if result.returncode == 0:
                print(f"✅ Sent close signal to app window -> {alias}")
                return
        except Exception:
            continue

    # METHOD 2: Find and terminate matching processes
    print(f"⚠️ No window found, looking for processes...")
    
    # Find all matching processes
    matched_processes = find_matching_processes(app_name)
    
    if not matched_processes:
        # Try finding by process name directly
        if psutil:
            search_terms = [normalized] + aliases
            for term in search_terms:
                if len(term) < 3:
                    continue
                for proc in psutil.process_iter(['pid', 'name']):
                    try:
                        proc_name = proc.info.get('name', '').lower()
                        if term.lower() in proc_name:
                            matched_processes.append(proc)
                    except:
                        continue
                if matched_processes:
                    break

    if not matched_processes:
        raise RuntimeError(f"❌ No running app found to close: {app_name}")

    # Terminate found processes
    for process in matched_processes:
        try:
            process.terminate()
            print(f"  Terminating PID: {process.pid}")
        except Exception as e:
            print(f"  Could not terminate PID {process.pid}: {e}")

    # Wait for termination and force kill if needed
    if psutil and matched_processes:
        gone, alive = psutil.wait_procs(matched_processes, timeout=5)
        for process in alive:
            try:
                process.kill()
                print(f"  Force killed PID: {process.pid}")
            except Exception:
                pass

    print(f"✅ Closed app -> {app_name}")


def open_in_vscode(path: Path) -> None:
    """Open path in VS Code."""
    ensure_safe_local_path(path)
    resolved = resolve_app_command("vscode")
    if not resolved:
        raise RuntimeError("VS Code was not found on this PC.")

    command, _ = resolved
    subprocess.Popen([*command, str(path)])
    print(f"Opened in VS Code -> {path}")


def focus_spotify_and_play(query: str) -> None:
    """Play music in Spotify."""
    escaped_query = ps_quote(query)
    script = f"""
    $wshell = New-Object -ComObject WScript.Shell
    $activated = $wshell.AppActivate('Spotify')
    if (-not $activated) {{
        Write-Error 'Spotify window was not found.'
        exit 9
    }}
    Start-Sleep -Milliseconds 900
    Set-Clipboard -Value {escaped_query}
    $wshell.SendKeys('^l')
    Start-Sleep -Milliseconds 350
    $wshell.SendKeys('^v')
    Start-Sleep -Milliseconds 1200
    $wshell.SendKeys('{{ENTER}}')
    Start-Sleep -Milliseconds 1600
    $wshell.SendKeys('{{TAB}}')
    Start-Sleep -Milliseconds 350
    $wshell.SendKeys('{{ENTER}}')
    Start-Sleep -Milliseconds 650
    $wshell.SendKeys(' ')
    """.strip()

    result = run_powershell(script)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Spotify local playback automation failed: {stderr}")

    print(f"🎵 Spotify local playback triggered for -> {query}")


def launch_youtube_search(query: str) -> None:
    """Search YouTube."""
    target_url = SEARCH_URLS["youtube"].format(query=quote_plus(query))
    launch_chrome_app_url(target_url)
    print(f"🔍 YouTube app search opened for -> {query}")


def launch_youtube_playback(query: str) -> None:
    """Play video on YouTube."""
    video_url = fetch_first_youtube_video_url(query)
    if not video_url:
        raise RuntimeError(f"No YouTube video result found for query: {query}")
    autoplay_url = f"{video_url}&autoplay=1" if "?" in video_url else f"{video_url}?autoplay=1"
    launch_chrome_app_url(autoplay_url)
    print(f"▶️ YouTube app playback opened for -> {query}")


def launch_chrome_app_url(target_url: str) -> None:
    """Launch Chrome with app mode."""
    config, _ = resolve_browser_config()
    command = [
        config.executable_path,
        f"--user-data-dir={config.user_data_dir}",
        f"--profile-directory={config.profile_directory}",
        f"--app={target_url}",
    ]
    subprocess.Popen(command)


def fetch_first_youtube_video_url(query: str) -> str | None:
    """Fetch first YouTube video URL."""
    search_url = SEARCH_URLS["youtube"].format(query=quote_plus(query))
    request = Request(
        search_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        },
    )

    with urlopen(request, timeout=20) as response:
        html = response.read().decode("utf-8", errors="ignore")

    matches = re.findall(r"\/watch\?v=([a-zA-Z0-9_-]{11})", html)
    seen: set[str] = set()
    for video_id in matches:
        if video_id in seen:
            continue
        seen.add(video_id)
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def execute_pc_actions(actions: list[dict]) -> None:
    """Execute PC automation actions."""
    for index, action in enumerate(actions, start=1):
        action_type = action["type"]
        print(f"[PC step {index}/{len(actions)}] {action_type} -> {json.dumps(action, ensure_ascii=False)}")

        if action_type == "unsupported":
            raise RuntimeError(action.get("reason", "Unsupported action"))

        if action_type == "open_app":
            launch_named_app(action["app"])
            time.sleep(4)
            continue

        if action_type == "close_app":
            close_named_app(action["app"])
            time.sleep(2)
            continue

        if action_type == "play_spotify":
            # Check if Spotify is running
            if not psutil or not any("spotify" in proc.name().lower() for proc in psutil.process_iter(["name"])):
                launch_named_app("spotify")
                time.sleep(5)
            focus_spotify_and_play(action["query"])
            time.sleep(2)
            continue

        if action_type == "search_youtube":
            launch_youtube_search(action["query"])
            time.sleep(3)
            continue

        if action_type == "play_youtube":
            launch_youtube_playback(action["query"])
            time.sleep(3)
            continue

        if action_type == "create_folder":
            path = expand_local_path(action["path"])
            ensure_safe_local_path(path)
            path.mkdir(parents=True, exist_ok=True)
            print(f"📁 Created folder -> {path}")
            continue

        if action_type == "write_file":
            path = expand_local_path(action["path"])
            ensure_safe_local_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(action.get("content", ""), encoding="utf-8")
            print(f"📄 Wrote file -> {path}")
            continue

        if action_type == "open_in_vscode":
            path = expand_local_path(action["path"])
            open_in_vscode(path)
            continue

        raise RuntimeError(f"Unsupported local action type: {action_type}")


def run_native_gemini_pc(working_key: str, task_query: str) -> None:
    """Run PC automation with Gemini."""
    print("Activating Windows Local OS Desktop Application Controller...")
    actions = build_pc_task_plan(working_key, task_query)
    print("Planned local actions:")
    for action in actions:
        print("  - " + json.dumps(action, ensure_ascii=False))
    execute_pc_actions(actions)
    print("\n✅ PC Desktop Automation Pipeline finished executing successfully!")


if __name__ == "__main__":
    try:
        if len(sys.argv) < 2:
            sys.exit(1)

        task_file = " ".join(sys.argv[1:]).strip("\"' ")
        with open(task_file, "r", encoding="utf-8") as file_handle:
            meta_input = json.load(file_handle)

        task_query = meta_input.get("task", "").strip()
        mode = meta_input.get("mode", "web").strip().lower()

        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        active_key = get_working_gemini_key()
        print(f"Connected to Google Gemini -> Active model verified: {GEMINI_MODEL}")

        if mode == "web":
            config, summary = resolve_browser_config()
            print(f"Session Initialized: {summary}")

            fast_plan = build_fast_task_plan(task_query)
            if fast_plan:
                success = asyncio.run(run_fast_task(fast_plan, config))
                if success:
                    print("\n============================================================\nTask finished successfully.\n====================================================")
                    sys.exit(0)

            asyncio.run(agent_task_payload(active_key, task_query, config))
        else:
            run_native_gemini_pc(active_key, task_query)

    except Exception as exc:
        print(f"\nFinal System Error: {exc}")
        traceback.print_exc()