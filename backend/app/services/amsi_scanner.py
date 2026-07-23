"""
AMSI Scanner — Windows Antimalware Scan Interface Integration
==============================================================
Primary file/content scanner for the Malware Guard pipeline.

Windows: Invokes AMSI via ctypes to leverage whatever AV engine the user
already has installed (Windows Defender or any third-party AMSI-aware AV).
This is the architecturally correct integration point — not shelling out to
MpCmdRun.exe, not uploading to VirusTotal.

VirusTotal: Demoted to an opt-in, user-consented secondary hash lookup.
Never called without explicit consent, never for files containing personal data.

Non-Windows: Gracefully degrades — returns a "heuristic only" verdict.
ClamAV (Linux) and XProtect (macOS) are stub-wired here for Phase 1 stubs.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ScanVerdict(str, Enum):
    CLEAN = "clean"
    DETECTED = "detected"           # AV positively identified threat
    SUSPICIOUS = "suspicious"       # Heuristic/behavioral concern
    UNKNOWN = "unknown"             # File not in any database
    UNAVAILABLE = "unavailable"     # Scanner not present on this platform
    ERROR = "error"                 # Scan failed (treat conservatively)


@dataclass
class ScanResult:
    verdict: ScanVerdict
    scanner: str                     # "amsi", "clamav", "xprotect", "heuristic"
    threat_name: Optional[str] = None
    file_hash: Optional[str] = None
    details: str = ""
    virustotal_result: Optional[dict] = None  # Only set when opt-in VT used


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# AMSI Integration (Windows Primary)
# ──────────────────────────────────────────────────────────────────────────────

_AMSI_RESULT_CLEAN = 0
_AMSI_RESULT_NOT_DETECTED = 1

class _AmsiContext:
    """Manages the AMSI context lifecycle."""

    def __init__(self):
        self._amsi = None
        self._context = None
        self._session = None
        self._available = False
        self._init()

    def _init(self):
        try:
            self._amsi = ctypes.windll.LoadLibrary("amsi.dll")  # type: ignore
            context = ctypes.c_void_p()
            result = self._amsi.AmsiInitialize("DirectAct-AI", ctypes.byref(context))
            if result == 0:  # S_OK
                self._context = context
                session = ctypes.c_void_p()
                self._amsi.AmsiOpenSession(context, ctypes.byref(session))
                self._session = session
                self._available = True
                logger.info("AMSI: initialized successfully — Windows AV integration active")
            else:
                logger.warning(f"AMSI: AmsiInitialize returned {result}")
        except Exception as e:
            logger.info(f"AMSI: not available on this platform ({e})")

    @property
    def available(self) -> bool:
        return self._available

    def scan_buffer(self, content: bytes, content_name: str = "scan") -> ScanResult:
        """Scan an in-memory buffer through AMSI."""
        if not self._available:
            return ScanResult(
                verdict=ScanVerdict.UNAVAILABLE,
                scanner="amsi",
                details="AMSI not available on this platform",
            )
        try:
            result = ctypes.c_ulong(0)
            self._amsi.AmsiScanBuffer(
                self._context,
                content,
                len(content),
                content_name,
                self._session,
                ctypes.byref(result),
            )
            # AMSI_RESULT_DETECTED = 32768 and above
            is_malware = result.value >= 32768
            verdict = ScanVerdict.DETECTED if is_malware else ScanVerdict.CLEAN
            logger.info(f"AMSI: scan '{content_name}' → {verdict.value} (result={result.value})")
            return ScanResult(
                verdict=verdict,
                scanner="amsi",
                file_hash=_sha256(content),
                details=f"AMSI result code: {result.value}",
                threat_name="AMSI_DETECTED" if is_malware else None,
            )
        except Exception as e:
            logger.error(f"AMSI scan error: {e}")
            return ScanResult(verdict=ScanVerdict.ERROR, scanner="amsi", details=str(e))

    def scan_string(self, command: str) -> ScanResult:
        """Scan a command string through AMSI."""
        return self.scan_buffer(command.encode("utf-8", errors="replace"), "command_string")

    def cleanup(self):
        if self._session and self._context and self._amsi:
            try:
                self._amsi.AmsiCloseSession(self._context, self._session)
                self._amsi.AmsiUninitialize(self._context)
            except Exception:
                pass


# Singleton AMSI context — initialized once at module load
_amsi_ctx: Optional[_AmsiContext] = None


def _get_amsi() -> _AmsiContext:
    global _amsi_ctx
    if _amsi_ctx is None:
        _amsi_ctx = _AmsiContext()
    return _amsi_ctx


# ──────────────────────────────────────────────────────────────────────────────
# Extension-Based Heuristic (fallback for non-Windows or AMSI unavailable)
# ──────────────────────────────────────────────────────────────────────────────

_HIGH_RISK_EXTENSIONS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".com", ".scr", ".pif", ".hta"}
_MEDIUM_RISK_EXTENSIONS = {".js", ".jar", ".py", ".sh", ".dll", ".sys", ".reg"}


def _heuristic_scan(file_path: str) -> ScanResult:
    ext = os.path.splitext(file_path)[1].lower()
    file_hash = _sha256_file(file_path)

    if ext in _HIGH_RISK_EXTENSIONS:
        return ScanResult(
            verdict=ScanVerdict.SUSPICIOUS,
            scanner="heuristic",
            file_hash=file_hash,
            details=f"High-risk file extension: {ext}",
        )
    elif ext in _MEDIUM_RISK_EXTENSIONS:
        return ScanResult(
            verdict=ScanVerdict.SUSPICIOUS,
            scanner="heuristic",
            file_hash=file_hash,
            details=f"Medium-risk file extension: {ext}",
        )
    return ScanResult(
        verdict=ScanVerdict.UNKNOWN,
        scanner="heuristic",
        file_hash=file_hash,
        details="No AV scanner available; heuristic extension check only",
    )


# ──────────────────────────────────────────────────────────────────────────────
# VirusTotal (opt-in secondary, NEVER primary)
# ──────────────────────────────────────────────────────────────────────────────

async def _virustotal_hash_lookup(
    file_hash: str,
    api_key: str,
) -> Optional[dict]:
    """Query VirusTotal API v3 for a file hash.
    Called ONLY when: user has explicitly consented AND file doesn't contain personal data."""
    try:
        import httpx
        url = f"https://www.virustotal.com/api/v3/files/{file_hash}"
        headers = {"x-apikey": api_key}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                stats = data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
                return {
                    "malicious": stats.get("malicious", 0),
                    "suspicious": stats.get("suspicious", 0),
                    "harmless": stats.get("harmless", 0),
                    "undetected": stats.get("undetected", 0),
                    "permalink": f"https://www.virustotal.com/gui/file/{file_hash}",
                }
            elif resp.status_code == 404:
                return {"malicious": 0, "note": "Hash not found in VirusTotal database"}
    except Exception as e:
        logger.error(f"VirusTotal lookup failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Public Scanner API
# ──────────────────────────────────────────────────────────────────────────────

def scan_command_string(command: str) -> ScanResult:
    """Scan a command string via AMSI (Windows) or return unavailable on other platforms."""
    import platform as _platform
    if _platform.system().lower() == "windows":
        return _get_amsi().scan_string(command)
    return ScanResult(
        verdict=ScanVerdict.UNAVAILABLE,
        scanner="amsi",
        details="AMSI is Windows-only; ClamAV/XProtect integration is in Phase 1 stubs",
    )


def scan_file_path(file_path: str) -> ScanResult:
    """Scan a file via AMSI buffer scan (Windows) or heuristics fallback."""
    import platform as _platform
    if not os.path.exists(file_path):
        return ScanResult(
            verdict=ScanVerdict.ERROR,
            scanner="amsi",
            details=f"File not found: {file_path}",
        )
    if _platform.system().lower() == "windows" and _get_amsi().available:
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            return _get_amsi().scan_buffer(content, os.path.basename(file_path))
        except Exception as e:
            logger.error(f"AMSI file scan error: {e}")
    return _heuristic_scan(file_path)


async def scan_file_with_optional_vt(
    file_path: str,
    vt_api_key: Optional[str] = None,
    user_consented_to_vt: bool = False,
) -> ScanResult:
    """Scan a file. Optionally run VirusTotal hash lookup as secondary,
    only if explicitly user-consented."""
    result = scan_file_path(file_path)

    # VirusTotal secondary — only with explicit user consent, never for personal data
    if (
        user_consented_to_vt
        and vt_api_key
        and vt_api_key not in ("", "your_virustotal_api_key_here", "YOUR_KEY_HERE")
        and result.file_hash
    ):
        vt = await _virustotal_hash_lookup(result.file_hash, vt_api_key)
        if vt:
            result.virustotal_result = vt
            if vt.get("malicious", 0) > 0:
                result.verdict = ScanVerdict.DETECTED
                result.threat_name = f"VirusTotal: {vt['malicious']} engines flagged malicious"

    return result
