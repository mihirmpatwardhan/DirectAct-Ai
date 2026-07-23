"""
Platform Probe — Phase 1.2
============================
Bootstrap-time OS and capability detection service.
Runs once at startup, results cached and exposed to the orchestrator.

Detects:
  - OS family/version, CPU architecture
  - Current user identity and privilege level (admin vs. standard)
  - Available security capabilities (AMSI, Windows Sandbox, Accessibility API)
  - Automation-relevant permissions

Feeds directly into:
  - OSEngineFactory (which engine implementation to load)
  - MalwareGuard (which AV integration is available)
  - UI sidebar "System Status" display

Design: Factory + Strategy (one PlatformProbe implementation per OS,
selected at runtime via platform.system()).
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List

logger = logging.getLogger(__name__)


class OSFamily(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    UNKNOWN = "unknown"


class PrivilegeLevel(str, Enum):
    ADMIN = "admin"       # Administrator / root
    STANDARD = "standard" # Normal user


@dataclass
class PlatformCapabilities:
    """Security and automation capabilities available on this platform."""
    # Windows
    amsi_available: bool = False          # Antimalware Scan Interface
    windows_sandbox_available: bool = False
    uia_available: bool = False           # UI Automation (accessibility)
    powershell_version: Optional[str] = None
    # Linux
    clamav_available: bool = False
    at_spi_available: bool = False        # Assistive Tech Service Provider Interface
    bubblewrap_available: bool = False
    # macOS
    xprotect_available: bool = False
    accessibility_api_available: bool = False
    # Shared
    keyring_available: bool = False       # OS keychain access


@dataclass
class PlatformInfo:
    """Complete platform detection result."""
    os_family: OSFamily
    os_name: str             # e.g. "Windows 11", "Ubuntu 22.04", "macOS 14.0"
    os_version: str
    architecture: str        # e.g. "x86_64", "arm64"
    hostname: str
    username: str
    privilege_level: PrivilegeLevel
    capabilities: PlatformCapabilities = field(default_factory=PlatformCapabilities)
    detection_warnings: List[str] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return self.privilege_level == PrivilegeLevel.ADMIN

    def to_dict(self) -> dict:
        return {
            "os_family": self.os_family.value,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "architecture": self.architecture,
            "hostname": self.hostname,
            "username": self.username,
            "privilege_level": self.privilege_level.value,
            "is_admin": self.is_admin,
            "capabilities": {
                "amsi": self.capabilities.amsi_available,
                "windows_sandbox": self.capabilities.windows_sandbox_available,
                "powershell_version": self.capabilities.powershell_version,
                "clamav": self.capabilities.clamav_available,
                "xprotect": self.capabilities.xprotect_available,
                "keyring": self.capabilities.keyring_available,
            },
            "warnings": self.detection_warnings,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Per-OS Detection Implementations
# ──────────────────────────────────────────────────────────────────────────────

def _detect_windows() -> PlatformInfo:
    caps = PlatformCapabilities()
    warnings = []

    # OS info
    os_name = platform.version()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            product_name = winreg.QueryValueEx(key, "ProductName")[0]
            build = winreg.QueryValueEx(key, "CurrentBuildNumber")[0]
            os_name = f"{product_name} (Build {build})"
    except Exception:
        warnings.append("Could not read detailed Windows version from registry")

    # Privilege level
    try:
        import ctypes
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        is_admin = False
        warnings.append("Could not determine admin status")

    # AMSI availability
    try:
        import ctypes
        amsi = ctypes.windll.LoadLibrary("amsi.dll")
        caps.amsi_available = amsi is not None
    except Exception:
        warnings.append("AMSI not available — file scanning will use extension heuristics only")

    # Windows Sandbox
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM | Select-Object -ExpandProperty State"],
            capture_output=True, text=True, timeout=10
        )
        caps.windows_sandbox_available = "Enabled" in result.stdout
    except Exception:
        pass

    # PowerShell version
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            caps.powershell_version = result.stdout.strip()
    except Exception:
        warnings.append("PowerShell not found — OS automation engine will be unavailable")

    # UI Automation
    try:
        import ctypes.wintypes
        caps.uia_available = True  # Available on all Windows 7+
    except Exception:
        pass

    # Keyring
    try:
        import keyring
        caps.keyring_available = True
    except ImportError:
        warnings.append("keyring not installed — OS keychain unavailable. Run: pip install keyring")

    return PlatformInfo(
        os_family=OSFamily.WINDOWS,
        os_name=os_name,
        os_version=platform.version(),
        architecture=platform.machine(),
        hostname=platform.node(),
        username=os.environ.get("USERNAME", os.environ.get("USER", "unknown")),
        privilege_level=PrivilegeLevel.ADMIN if is_admin else PrivilegeLevel.STANDARD,
        capabilities=caps,
        detection_warnings=warnings,
    )


def _detect_linux() -> PlatformInfo:
    caps = PlatformCapabilities()
    warnings = []

    # OS info from /etc/os-release
    os_name = "Linux"
    try:
        with open("/etc/os-release") as f:
            lines = dict(line.strip().split("=", 1) for line in f if "=" in line)
            os_name = lines.get("PRETTY_NAME", "Linux").strip('"')
    except Exception:
        pass

    # Privilege level
    is_admin = os.geteuid() == 0 if hasattr(os, "geteuid") else False

    # ClamAV
    try:
        result = subprocess.run(["which", "clamdscan"], capture_output=True, timeout=5)
        caps.clamav_available = result.returncode == 0
        if not caps.clamav_available:
            warnings.append("ClamAV not found — install clamav for file scanning")
    except Exception:
        pass

    # AT-SPI
    try:
        result = subprocess.run(["which", "at-spi-bus-launcher"], capture_output=True, timeout=5)
        caps.at_spi_available = result.returncode == 0
    except Exception:
        pass

    # Bubblewrap sandbox
    try:
        result = subprocess.run(["which", "bwrap"], capture_output=True, timeout=5)
        caps.bubblewrap_available = result.returncode == 0
    except Exception:
        pass

    # Keyring
    try:
        import keyring
        caps.keyring_available = True
    except ImportError:
        warnings.append("keyring not installed — OS keychain unavailable")

    return PlatformInfo(
        os_family=OSFamily.LINUX,
        os_name=os_name,
        os_version=platform.release(),
        architecture=platform.machine(),
        hostname=platform.node(),
        username=os.environ.get("USER", "unknown"),
        privilege_level=PrivilegeLevel.ADMIN if is_admin else PrivilegeLevel.STANDARD,
        capabilities=caps,
        detection_warnings=warnings,
    )


def _detect_macos() -> PlatformInfo:
    caps = PlatformCapabilities()
    warnings = []

    # OS info
    mac_ver = platform.mac_ver()
    os_name = f"macOS {mac_ver[0]}" if mac_ver[0] else "macOS"

    # Privilege level
    is_admin = os.geteuid() == 0 if hasattr(os, "geteuid") else False

    # XProtect (always present on macOS 10.6+)
    try:
        result = subprocess.run(["spctl", "--status"], capture_output=True, timeout=5)
        caps.xprotect_available = True
    except Exception:
        warnings.append("spctl not available — Gatekeeper/XProtect integration unavailable")

    # Accessibility API (requires user grant)
    try:
        result = subprocess.run(
            ["osascript", "-e", "tell application \"System Events\" to return name of first process"],
            capture_output=True, timeout=5
        )
        caps.accessibility_api_available = result.returncode == 0
        if not caps.accessibility_api_available:
            warnings.append(
                "Accessibility API permission not granted. "
                "Grant in System Settings > Privacy & Security > Accessibility."
            )
    except Exception:
        pass

    # Keyring
    try:
        import keyring
        caps.keyring_available = True
    except ImportError:
        warnings.append("keyring not installed — OS keychain unavailable")

    return PlatformInfo(
        os_family=OSFamily.MACOS,
        os_name=os_name,
        os_version=platform.version(),
        architecture=platform.machine(),
        hostname=platform.node(),
        username=os.environ.get("USER", "unknown"),
        privilege_level=PrivilegeLevel.ADMIN if is_admin else PrivilegeLevel.STANDARD,
        capabilities=caps,
        detection_warnings=warnings,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

_cached_platform_info: Optional[PlatformInfo] = None


def detect_platform() -> PlatformInfo:
    """Detect the current platform. Result is cached after first call."""
    global _cached_platform_info
    if _cached_platform_info is not None:
        return _cached_platform_info

    system = platform.system().lower()
    logger.info(f"PlatformProbe: detecting OS family '{system}'")

    if system == "windows":
        info = _detect_windows()
    elif system == "darwin":
        info = _detect_macos()
    elif system == "linux":
        info = _detect_linux()
    else:
        info = PlatformInfo(
            os_family=OSFamily.UNKNOWN,
            os_name=system,
            os_version=platform.version(),
            architecture=platform.machine(),
            hostname=platform.node(),
            username=os.environ.get("USER", "unknown"),
            privilege_level=PrivilegeLevel.STANDARD,
            detection_warnings=[f"Unsupported OS: {system}"],
        )

    _cached_platform_info = info

    logger.info(
        f"PlatformProbe: detected {info.os_name} ({info.architecture}), "
        f"user={info.username}, admin={info.is_admin}"
    )
    for warning in info.detection_warnings:
        logger.warning(f"PlatformProbe: {warning}")

    return info


# Convenience singleton
platform_info = detect_platform()
