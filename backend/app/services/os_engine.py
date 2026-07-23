"""
OS Automation Engine — Strategy Pattern — Phase 1.2
=====================================================
Per-OS engine implementations behind a common abstract interface.
The Orchestrator always calls this interface; the concrete implementation
is selected at startup by OSEngineFactory based on platform detection.

All engines receive typed Command objects from the action vocabulary.
They NEVER receive raw strings — the LLM is constrained to the typed
vocabulary by the PromptConstraintLayer before the Command reaches here.

The Malware Guard validates every Command BEFORE the engine executes it.
The engine itself does no security checking — that is the Guard's job.

Implementations:
  - WindowsAutomationEngine: PowerShell 7 + Win32 + Registry app discovery
  - LinuxAutomationEngine:   bash + D-Bus + AT-SPI (stub — Phase 1 stubs)
  - MacOSAutomationEngine:   AppleScript/JXA + Accessibility API (stub)
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from app.schemas.action_vocabulary import (
    BaseCommand, CommandType, CommandResult,
    LaunchApp, CloseApp, FocusApp,
    CreateFile, DeleteFile, MoveFile, CopyFile, RenameFile, ReadFile,
    ListDirectory, CreateDirectory,
    GetSystemInfo, TakeScreenshot, GetClipboard, SetClipboard,
    RunApprovedScript, OpenURL, QueryLLM,
)
from app.core.config import settings

logger = logging.getLogger(__name__)
MAX_OUTPUT = 10_000  # chars


# ──────────────────────────────────────────────────────────────────────────────
# Abstract Engine Interface
# ──────────────────────────────────────────────────────────────────────────────

class OSAutomationEngine(ABC):
    """Abstract Strategy interface for per-OS automation engines."""

    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @abstractmethod
    async def execute(self, command: BaseCommand, session_id: str = "") -> CommandResult: ...

    def _result(self, command: BaseCommand, success: bool, output: str = "",
                error: str = "", duration_ms: float = 0.0, **meta) -> CommandResult:
        return CommandResult(
            command_type=command.command_type,
            success=success,
            output=output[:MAX_OUTPUT] if output else None,
            error=error or None,
            duration_ms=duration_ms,
            metadata=meta,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Windows Engine
# ──────────────────────────────────────────────────────────────────────────────

# Registry-based app name → executable path mapping
_WIN_APP_MAP_CACHE: Optional[dict] = None


def _build_win_app_map() -> dict:
    """Build app map from registry + well-known paths. Cached."""
    global _WIN_APP_MAP_CACHE
    if _WIN_APP_MAP_CACHE is not None:
        return _WIN_APP_MAP_CACHE

    windir = os.environ.get("WINDIR", "C:\\Windows")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")

    well_known = {
        "notepad": os.path.join(windir, "System32", "notepad.exe"),
        "calculator": os.path.join(windir, "System32", "calc.exe"),
        "calc": os.path.join(windir, "System32", "calc.exe"),
        "explorer": os.path.join(windir, "explorer.exe"),
        "cmd": os.path.join(windir, "System32", "cmd.exe"),
        "powershell": os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "paint": os.path.join(windir, "System32", "mspaint.exe"),
        "wordpad": os.path.join(windir, "System32", "write.exe"),
        "chrome": os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
        "vscode": os.path.join(local_appdata, "Programs", "Microsoft VS Code", "Code.exe"),
        "code": os.path.join(local_appdata, "Programs", "Microsoft VS Code", "Code.exe"),
        "msedge": os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
        "edge": os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
        "firefox": os.path.join(program_files, "Mozilla Firefox", "firefox.exe"),
        "winrar": os.path.join(program_files, "WinRAR", "WinRAR.exe"),
        "7zip": os.path.join(program_files, "7-Zip", "7z.exe"),
        "vlc": os.path.join(program_files, "VideoLAN", "VLC", "vlc.exe"),
        "teams": os.path.join(local_appdata, "Microsoft", "Teams", "current", "Teams.exe"),
        "zoom": os.path.join(local_appdata, "Zoom", "bin", "Zoom.exe"),
        "discord": os.path.join(local_appdata, "Discord", "Update.exe"),
        "spotify": os.path.join(local_appdata, "Microsoft", "WindowsApps", "Spotify.exe"),
        "slack": os.path.join(local_appdata, "slack", "slack.exe"),
        "task manager": os.path.join(windir, "System32", "Taskmgr.exe"),
        "taskmgr": os.path.join(windir, "System32", "Taskmgr.exe"),
        "regedit": os.path.join(windir, "regedit.exe"),
        "control panel": os.path.join(windir, "System32", "control.exe"),
        "snipping tool": os.path.join(windir, "System32", "SnippingTool.exe"),
    }

    # Try App Paths registry key for additional apps
    try:
        import winreg
        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, name) as sub:
                        try:
                            path = winreg.QueryValue(sub, "")
                            app_name = name.replace(".exe", "").lower()
                            if path and os.path.exists(path):
                                well_known[app_name] = path
                        except Exception:
                            pass
                    i += 1
                except OSError:
                    break
    except Exception:
        pass

    _WIN_APP_MAP_CACHE = well_known
    return well_known


class WindowsAutomationEngine(OSAutomationEngine):
    """Windows automation via PowerShell 7 + Win32 APIs."""

    @property
    def platform_name(self) -> str:
        return "windows"

    async def execute(self, command: BaseCommand, session_id: str = "") -> CommandResult:
        t0 = datetime.utcnow()

        try:
            if isinstance(command, LaunchApp):
                return await self._launch_app(command, session_id)
            elif isinstance(command, CloseApp):
                return await self._close_app(command, session_id)
            elif isinstance(command, FocusApp):
                return await self._focus_app(command, session_id)
            elif isinstance(command, CreateFile):
                return await self._create_file(command)
            elif isinstance(command, DeleteFile):
                return await self._delete_file(command)
            elif isinstance(command, MoveFile):
                return await self._move_file(command)
            elif isinstance(command, CopyFile):
                return await self._copy_file(command)
            elif isinstance(command, RenameFile):
                return await self._rename_file(command)
            elif isinstance(command, ReadFile):
                return await self._read_file(command)
            elif isinstance(command, ListDirectory):
                return await self._list_directory(command)
            elif isinstance(command, CreateDirectory):
                return await self._create_directory(command)
            elif isinstance(command, GetSystemInfo):
                return await self._get_system_info(command)
            elif isinstance(command, TakeScreenshot):
                return await self._take_screenshot(command)
            elif isinstance(command, GetClipboard):
                return await self._get_clipboard(command)
            elif isinstance(command, SetClipboard):
                return await self._set_clipboard(command)
            elif isinstance(command, RunApprovedScript):
                return await self._run_script(command)
            elif isinstance(command, QueryLLM):
                return self._result(command, True, "Query handled by LLM — no OS action")
            elif isinstance(command, OpenURL):
                return await self._open_url(command)
            else:
                return self._result(command, False, error=f"Unhandled command type: {command.command_type}")
        except Exception as e:
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            logger.error(f"WindowsEngine error executing {command.command_type}: {e}", exc_info=True)
            return self._result(command, False, error=str(e), duration_ms=duration_ms)

    # ── PowerShell helper ────────────────────────────────────────────────────

    async def _ps(self, script: str, timeout: int = 30) -> tuple[str, str, int]:
        """Run a PowerShell command; return (stdout, stderr, exit_code)."""
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return "", f"Timeout after {timeout}s", -1
        return (
            out.decode("utf-8", errors="replace")[:MAX_OUTPUT],
            err.decode("utf-8", errors="replace")[:2000],
            proc.returncode or 0,
        )

    # ── App lifecycle ─────────────────────────────────────────────────────────

    async def _launch_app(self, cmd: LaunchApp, session_id: str) -> CommandResult:
        t0 = datetime.utcnow()
        app_map = _build_win_app_map()
        lower = cmd.app_id.lower().strip()
        exe_path = app_map.get(lower)

        def _do_launch():
            if exe_path and os.path.exists(exe_path):
                subprocess.Popen([exe_path] + cmd.arguments)
                return True, f"Launched via path: {exe_path}"
            # Start-Process fallback
            args_str = " ".join(f'"{a}"' for a in cmd.arguments) if cmd.arguments else ""
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Start-Process '{cmd.app_id}' {args_str} -ErrorAction Stop"],
                capture_output=True, text=True, timeout=10,
            )
            if res.returncode == 0:
                return True, f"Launched via Start-Process"
            # cmd /c start fallback
            subprocess.Popen(["cmd.exe", "/c", f"start {cmd.app_id}"], shell=True)
            return True, "Launched via cmd start"

        loop = asyncio.get_running_loop()
        success, msg = await loop.run_in_executor(None, _do_launch)
        duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
        return self._result(cmd, success, output=msg if success else "", duration_ms=duration_ms)

    async def _close_app(self, cmd: CloseApp, session_id: str) -> CommandResult:
        t0 = datetime.utcnow()
        proc_name = cmd.app_id.strip().lower().replace(".exe", "")
        force_flag = "-Force" if cmd.force else ""

        script = (
            f"Stop-Process -Name '{proc_name}' {force_flag} -ErrorAction SilentlyContinue; "
            f"taskkill /IM '{proc_name}.exe' /F 2>$null; echo 'done'"
        )
        out, err, code = await self._ps(script, timeout=15)
        duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
        return self._result(cmd, True, output=f"Closed {cmd.app_id}", duration_ms=duration_ms)

    async def _focus_app(self, cmd: FocusApp, session_id: str) -> CommandResult:
        script = (
            f"$app = Get-Process '{cmd.app_id}' -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($app) { $app.MainWindowHandle } else { 'not found' }"
        )
        out, err, code = await self._ps(script, timeout=10)
        return self._result(cmd, code == 0, output=f"Focused {cmd.app_id}")

    async def _open_url(self, cmd: OpenURL) -> CommandResult:
        """Open a URL in the default browser."""
        script = f"Start-Process '{cmd.url}'"
        out, err, code = await self._ps(script, timeout=10)
        return self._result(cmd, code == 0, output=f"Opened {cmd.url}", error=err if err else None)

    # ── File operations ───────────────────────────────────────────────────────

    async def _create_file(self, cmd: CreateFile) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            path = os.path.expanduser(cmd.path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            if os.path.exists(path) and not cmd.overwrite:
                return self._result(cmd, False, error=f"File already exists: {path}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(cmd.content)
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=f"Created: {path}", duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    async def _delete_file(self, cmd: DeleteFile) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            path = os.path.expanduser(cmd.path)
            if not os.path.exists(path):
                return self._result(cmd, False, error=f"Path not found: {path}")
            if os.path.isfile(path):
                os.remove(path)
            else:
                import shutil
                shutil.rmtree(path)
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=f"Deleted: {path}", duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    async def _move_file(self, cmd: MoveFile) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            import shutil
            shutil.move(os.path.expanduser(cmd.source), os.path.expanduser(cmd.destination))
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=f"Moved: {cmd.source} → {cmd.destination}", duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    async def _copy_file(self, cmd: CopyFile) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            import shutil
            src = os.path.expanduser(cmd.source)
            dst = os.path.expanduser(cmd.destination)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=f"Copied: {cmd.source} → {cmd.destination}", duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    async def _rename_file(self, cmd: RenameFile) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            path = os.path.expanduser(cmd.path)
            new_path = os.path.join(os.path.dirname(path), cmd.new_name)
            os.rename(path, new_path)
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=f"Renamed: {path} → {new_path}", duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    async def _read_file(self, cmd: ReadFile) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            path = os.path.expanduser(cmd.path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(cmd.max_bytes)
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=content, duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    async def _list_directory(self, cmd: ListDirectory) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            path = os.path.expanduser(cmd.path)
            entries = os.listdir(path)
            if not cmd.include_hidden:
                entries = [e for e in entries if not e.startswith(".")]
            output = "\n".join(sorted(entries))
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=output, duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    async def _create_directory(self, cmd: CreateDirectory) -> CommandResult:
        t0 = datetime.utcnow()
        try:
            path = os.path.expanduser(cmd.path)
            os.makedirs(path, exist_ok=True)
            duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return self._result(cmd, True, output=f"Created directory: {path}", duration_ms=duration_ms)
        except Exception as e:
            return self._result(cmd, False, error=str(e))

    # ── System commands ───────────────────────────────────────────────────────

    async def _get_system_info(self, cmd: GetSystemInfo) -> CommandResult:
        t0 = datetime.utcnow()
        script = """
$info = @{
    ComputerName = $env:COMPUTERNAME
    UserName = $env:USERNAME
    OS = (Get-CimInstance Win32_OperatingSystem).Caption
    OSVersion = (Get-CimInstance Win32_OperatingSystem).Version
    Architecture = $env:PROCESSOR_ARCHITECTURE
    RAM_GB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB, 1)
    CPU = (Get-CimInstance Win32_Processor).Name
} | ConvertTo-Json -Compress
$info
""".strip()
        out, err, code = await self._ps(script, timeout=20)
        duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
        return self._result(cmd, code == 0, output=out, error=err if err and code != 0 else None, duration_ms=duration_ms)

    async def _take_screenshot(self, cmd: TakeScreenshot) -> CommandResult:
        t0 = datetime.utcnow()
        out_dir = settings.screenshot_path
        os.makedirs(out_dir, exist_ok=True)
        filename = f"screenshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.png"
        save_path = cmd.save_to or os.path.join(out_dir, filename)
        save_path_ps = save_path.replace("\\", "\\\\")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
$screen = [System.Windows.Forms.Screen]::PrimaryScreen
$bitmap = New-Object System.Drawing.Bitmap($screen.Bounds.Width, $screen.Bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($screen.Bounds.X, $screen.Bounds.Y, 0, 0, $screen.Bounds.Size)
$bitmap.Save('{save_path_ps}')
Write-Output '{save_path_ps}'
""".strip()
        out, err, code = await self._ps(script, timeout=15)
        duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
        return self._result(cmd, code == 0, output=out.strip(), error=err if code != 0 else None, duration_ms=duration_ms)

    async def _get_clipboard(self, cmd: GetClipboard) -> CommandResult:
        out, err, code = await self._ps("Get-Clipboard", timeout=5)
        return self._result(cmd, code == 0, output=out, error=err if code != 0 else None)

    async def _set_clipboard(self, cmd: SetClipboard) -> CommandResult:
        safe_content = cmd.content.replace("'", "''")
        out, err, code = await self._ps(f"Set-Clipboard -Value '{safe_content}'", timeout=5)
        return self._result(cmd, code == 0, output="Clipboard updated" if code == 0 else "", error=err if code != 0 else None)

    async def _run_script(self, cmd: RunApprovedScript) -> CommandResult:
        """Run an approved script in a constrained environment.
        Note: SandboxDecisionCheck will have routed this to Windows Sandbox if available."""
        t0 = datetime.utcnow()
        if cmd.interpreter not in ("powershell", "python", "bash"):
            return self._result(cmd, False, error=f"Unsupported interpreter: {cmd.interpreter}")
        out, err, code = await self._ps(cmd.script_content, timeout=cmd.timeout_seconds)
        duration_ms = (datetime.utcnow() - t0).total_seconds() * 1000
        return self._result(cmd, code == 0, output=out, error=err if code != 0 else None, duration_ms=duration_ms)


# ──────────────────────────────────────────────────────────────────────────────
# Linux Engine (Phase 1 Stub)
# ──────────────────────────────────────────────────────────────────────────────

class LinuxAutomationEngine(OSAutomationEngine):
    """Linux automation stub — full implementation in Phase 2."""

    @property
    def platform_name(self) -> str:
        return "linux"

    async def execute(self, command: BaseCommand, session_id: str = "") -> CommandResult:
        if isinstance(command, LaunchApp):
            proc = await asyncio.create_subprocess_exec(
                command.app_id, *command.arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return self._result(command, True, output=f"Launched {command.app_id} (PID {proc.pid})")
        elif isinstance(command, CreateFile):
            path = os.path.expanduser(command.path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(command.content)
            return self._result(command, True, output=f"Created {path}")
        elif isinstance(command, ReadFile):
            with open(os.path.expanduser(command.path)) as f:
                return self._result(command, True, output=f.read(command.max_bytes))
        elif isinstance(command, ListDirectory):
            entries = os.listdir(os.path.expanduser(command.path))
            return self._result(command, True, output="\n".join(sorted(entries)))
        elif isinstance(command, GetSystemInfo):
            import platform as _pl
            return self._result(command, True, output=str({
                "os": _pl.platform(), "user": os.environ.get("USER", ""), "arch": _pl.machine()
            }))
        return self._result(command, False, error=f"Linux stub: {command.command_type.value} not yet fully implemented")


# ──────────────────────────────────────────────────────────────────────────────
# macOS Engine (Phase 1 Stub)
# ──────────────────────────────────────────────────────────────────────────────

class MacOSAutomationEngine(OSAutomationEngine):
    """macOS automation stub — full implementation in Phase 2."""

    @property
    def platform_name(self) -> str:
        return "macos"

    async def execute(self, command: BaseCommand, session_id: str = "") -> CommandResult:
        if isinstance(command, LaunchApp):
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", command.app_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            return self._result(command, proc.returncode == 0, output=f"Opened {command.app_id}",
                                error=err.decode() if proc.returncode != 0 else None)
        elif isinstance(command, GetSystemInfo):
            result = await asyncio.create_subprocess_exec(
                "sw_vers", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await result.communicate()
            return self._result(command, True, output=out.decode())
        return self._result(command, False, error=f"macOS stub: {command.command_type.value} not yet fully implemented")


# ──────────────────────────────────────────────────────────────────────────────
# Factory — selects the right engine at startup
# ──────────────────────────────────────────────────────────────────────────────

def create_os_engine() -> OSAutomationEngine:
    """Factory function: return the correct engine for the current OS."""
    system = platform.system().lower()
    if system == "windows":
        logger.info("OSEngineFactory: selected WindowsAutomationEngine")
        return WindowsAutomationEngine()
    elif system == "darwin":
        logger.info("OSEngineFactory: selected MacOSAutomationEngine (stub)")
        return MacOSAutomationEngine()
    elif system == "linux":
        logger.info("OSEngineFactory: selected LinuxAutomationEngine (stub)")
        return LinuxAutomationEngine()
    else:
        logger.warning(f"OSEngineFactory: unknown OS '{system}' — using Linux stub")
        return LinuxAutomationEngine()


# Singleton — selected at startup
os_engine: OSAutomationEngine = create_os_engine()
