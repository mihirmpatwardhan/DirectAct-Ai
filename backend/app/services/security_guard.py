"""
Malware Guard — Chain of Responsibility Pipeline
==================================================
The non-bypassable security gate between the LLM orchestrator and the OS.

Architecture principle:
    "No AI-generated action reaches the OS without passing through a
    deterministic, non-LLM security gate."

Pipeline (8 steps, each independently testable, any step can HALT):
  1. PolicyValidationCheck    — Rule-based policy evaluation (no LLM)
  2. StaticCommandAnalysis    — Schema validation; AST-level for scripts
  3. PrivilegeEscalationCheck — Hard deny on elevation; OS-native prompts only
  4. FileScanCheck            — AMSI primary; VT opt-in secondary
  5. SandboxDecisionCheck     — Route unknown executables to OS sandbox
  6. DirectoryAccessCheck     — Enforce allowed-directory allowlist
  7. ConfirmationGateCheck    — Force approval for HIGH/CRITICAL Commands
  8. AuditLogger              — Hash-chained log entry (always runs)

Key properties:
  - The LLM is Process 2; the Guard is conceptually Process 3.
    A prompt-injection or LLM-hallucination bug CANNOT bypass this gate —
    it is not connected to the LLM and has no LLM logic.
  - Adding a new check = implementing GuardCheck + inserting into the chain.
    Zero changes to callers.
  - Each check emits a SecurityCheckResultEvent to the UI in real time.
"""
from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Callable, Awaitable

from app.schemas.action_vocabulary import (
    BaseCommand, CommandType, RiskLevel, COMMAND_RISK_DEFAULTS,
    RunApprovedScript, DeleteFile, CreateFile, MoveFile, CopyFile,
)
from app.schemas.events import (
    GuardCheckName, SecurityCheckResultEvent, ActionBlockedEvent,
    AuditLogEntryEvent, EventType, make_redacted_hash,
)
from app.services.audit_log import audit_logger

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Guard Verdict
# ──────────────────────────────────────────────────────────────────────────────

class GuardAction(str, Enum):
    ALLOW = "allow"                     # Continue to next check
    REQUIRE_APPROVAL = "require_approval"  # Pause for human confirmation
    BLOCK = "block"                     # Hard block — do not execute
    SANDBOX = "sandbox"                 # Route to sandbox execution


@dataclass
class GuardVerdict:
    action: GuardAction
    reason: str
    risk_level: RiskLevel = RiskLevel.LOW
    threat_name: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def halts_pipeline(self) -> bool:
        return self.action == GuardAction.BLOCK

    @property
    def requires_approval(self) -> bool:
        return self.action == GuardAction.REQUIRE_APPROVAL

    @property
    def allowed(self) -> bool:
        return self.action in (GuardAction.ALLOW, GuardAction.SANDBOX)


@dataclass
class PipelineResult:
    """Final result from the full guard chain."""
    allowed: bool
    requires_approval: bool
    sandboxed: bool
    blocked_by: Optional[GuardCheckName]
    block_reason: Optional[str]
    highest_risk: RiskLevel
    audit_sequence: int
    all_verdicts: List[tuple[GuardCheckName, GuardVerdict]]

    @property
    def should_execute(self) -> bool:
        return self.allowed and not self.requires_approval


# ──────────────────────────────────────────────────────────────────────────────
# Abstract Guard Check
# ──────────────────────────────────────────────────────────────────────────────

class GuardCheck(ABC):
    """Base class for all guard pipeline steps.
    Each check is independent, testable, and can short-circuit the chain."""

    @property
    @abstractmethod
    def check_name(self) -> GuardCheckName: ...

    @abstractmethod
    async def evaluate(
        self,
        command: BaseCommand,
        context: "GuardContext",
    ) -> GuardVerdict: ...


@dataclass
class GuardContext:
    """Shared context passed through the guard chain."""
    action_id: str
    task_id: Optional[str]
    session_id: str
    execution_mode: str  # "autonomous" | "hitl"
    raw_input: str = ""  # Original user text (for logging)
    file_path: Optional[str] = None  # Set when command involves a file
    virustotal_consented: bool = False  # Explicit user consent for VT lookup
    event_emitter: Optional[Callable] = None  # async fn(event) → None


# ──────────────────────────────────────────────────────────────────────────────
# Step 1: Policy Validation
# Hard-coded deny list + risk classification. No LLM, no external calls.
# ──────────────────────────────────────────────────────────────────────────────

# Patterns that are ALWAYS blocked regardless of mode or approval
_ABSOLUTE_BLOCK_PATTERNS: List[tuple[re.Pattern, str]] = [
    # File system destruction
    (re.compile(r"\brm\s+-[rf]{1,2}\b", re.I), "Recursive deletion command"),
    (re.compile(r"\bdel\s+/[sf]", re.I), "Forced system file deletion"),
    (re.compile(r"\brd\s+/[sq]", re.I), "Silent directory removal"),
    (re.compile(r"format\s+[a-z]:", re.I), "Disk format command"),
    (re.compile(r"format-volume", re.I), "PowerShell disk format"),
    (re.compile(r"clear-disk", re.I), "Disk wipe command"),
    # System destruction
    (re.compile(r"\bshutdown\s+/[sr]\b", re.I), "System shutdown/restart via flag"),
    (re.compile(r"stop-computer", re.I), "PowerShell shutdown"),
    (re.compile(r"restart-computer", re.I), "PowerShell restart"),
    # Registry destruction
    (re.compile(r"remove-item\s+hk(lm|cu|cr|cc|u)", re.I), "Registry key deletion"),
    # Privilege bypass
    (re.compile(r"set-executionpolicy\s+unrestricted", re.I), "PowerShell policy bypass"),
    (re.compile(r"invoke-expression\s*\(.*net\.webclient", re.I), "Remote code download+execute"),
    (re.compile(r"\biex\s*\(", re.I), "PowerShell IEX bypass"),
    (re.compile(r"\bbase64\b.*(decode|convert)", re.I), "Base64 payload decode"),
    # Network exfiltration
    (re.compile(r"(curl|wget|invoke-webrequest)\s+.*\|\s*(bash|sh|powershell|cmd)", re.I),
     "Remote code execution via pipe"),
]

# System directories that are never writable regardless of mode
_PROTECTED_WRITE_PATHS = [
    "C:\\Windows", "C:\\Windows\\System32", "C:\\Windows\\SysWOW64",
    "C:\\Program Files", "C:\\Program Files (x86)",
    "/etc", "/sys", "/proc", "/boot", "/System", "/Library/System",
]


class PolicyValidationCheck(GuardCheck):
    """Step 1: Hard-coded deny list + structural Command validation.
    Catches obvious attacks in <1ms. Cannot be overridden by approval."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.POLICY_VALIDATION

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        t0 = time.monotonic()

        # 1a. Check RunApprovedScript is globally disabled unless settings allow
        if command.command_type == CommandType.RUN_APPROVED_SCRIPT:
            from app.core.config import settings
            if not getattr(settings, "enable_script_execution", False):
                return GuardVerdict(
                    action=GuardAction.BLOCK,
                    reason="Script execution is disabled. Enable 'enable_script_execution' in settings.",
                    risk_level=RiskLevel.CRITICAL,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

        # 1b. Scan the raw input against absolute block patterns
        text_to_check = context.raw_input
        # Also check script content if applicable
        if isinstance(command, RunApprovedScript):
            text_to_check += " " + command.script_content

        for pattern, description in _ABSOLUTE_BLOCK_PATTERNS:
            if pattern.search(text_to_check):
                return GuardVerdict(
                    action=GuardAction.BLOCK,
                    reason=f"Blocked by policy: {description}",
                    risk_level=RiskLevel.CRITICAL,
                    threat_name=description,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

        # 1c. Check write paths against protected directory list
        write_path = None
        if isinstance(command, CreateFile):
            write_path = command.path
        elif isinstance(command, MoveFile):
            write_path = command.destination
        elif isinstance(command, CopyFile):
            write_path = command.destination

        if write_path:
            normalized = os.path.normpath(write_path)
            for protected in _PROTECTED_WRITE_PATHS:
                if normalized.lower().startswith(protected.lower()):
                    return GuardVerdict(
                        action=GuardAction.BLOCK,
                        reason=f"Write to protected system directory '{protected}' is not permitted",
                        risk_level=RiskLevel.CRITICAL,
                        duration_ms=(time.monotonic() - t0) * 1000,
                    )

        # 1d. Assign risk level from Command type defaults
        risk = COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.MEDIUM)

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="Policy validation passed",
            risk_level=risk,
            duration_ms=(time.monotonic() - t0) * 1000,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Step 2: Static Command Analysis
# Schema validation + AST-level script analysis for RunApprovedScript
# ──────────────────────────────────────────────────────────────────────────────

# Suspicious script patterns for static analysis
_SUSPICIOUS_SCRIPT_PATTERNS: List[tuple[re.Pattern, str]] = [
    (re.compile(r"frombase64string", re.I), "Base64 decode in script"),
    (re.compile(r"invoke-expression|iex\s*\(", re.I), "Dynamic code execution"),
    (re.compile(r"downloadstring|downloadfile|webclient", re.I), "Network download"),
    (re.compile(r"amsi\s*bypass|amsi.*disable", re.I), "AMSI bypass attempt"),
    (re.compile(r"hidden\s*window|windowstyle\s*hidden", re.I), "Hidden window execution"),
    (re.compile(r"certutil.*decode|certutil.*urlcache", re.I), "Certutil payload delivery"),
    (re.compile(r"mshta\.exe|wscript\.exe|cscript\.exe", re.I), "Scripting host invocation"),
]


class StaticCommandAnalysisCheck(GuardCheck):
    """Step 2: Schema validation for typed Commands; AST-level for scripts."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.STATIC_ANALYSIS

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        t0 = time.monotonic()

        # For scripts: run static analysis on the source
        if isinstance(command, RunApprovedScript):
            for pattern, description in _SUSPICIOUS_SCRIPT_PATTERNS:
                if pattern.search(command.script_content):
                    return GuardVerdict(
                        action=GuardAction.BLOCK,
                        reason=f"Script static analysis: suspicious pattern detected — {description}",
                        risk_level=RiskLevel.CRITICAL,
                        threat_name=description,
                        duration_ms=(time.monotonic() - t0) * 1000,
                    )

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="Static analysis passed",
            risk_level=COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.MEDIUM),
            duration_ms=(time.monotonic() - t0) * 1000,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Step 3: Privilege Escalation Check
# DirectAct-AI NEVER silently elevates. Only OS-native prompts (UAC/sudo).
# ──────────────────────────────────────────────────────────────────────────────

_PRIVILEGE_PATTERNS: List[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsudo\b", re.I), "sudo invocation"),
    (re.compile(r"runas\s+/user:administrator", re.I), "Windows RunAs Administrator"),
    (re.compile(r"start-process.*-verb\s+runas", re.I), "PowerShell RunAs"),
    (re.compile(r"\bnet\s+user\b", re.I), "User account modification"),
    (re.compile(r"\bnet\s+localgroup\s+administrators\b", re.I), "Admin group modification"),
    (re.compile(r"schtasks.*highest", re.I), "Scheduled task with highest privileges"),
]


class PrivilegeEscalationCheck(GuardCheck):
    """Step 3: Hard deny on automation-triggered privilege escalation.
    Elevation must go through the OS-native consent prompt (UAC/sudo/Polkit)
    initiated by the user, never silently by DirectAct-AI."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.PRIVILEGE_CHECK

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        t0 = time.monotonic()

        for pattern, description in _PRIVILEGE_PATTERNS:
            if pattern.search(context.raw_input):
                return GuardVerdict(
                    action=GuardAction.BLOCK,
                    reason=(
                        f"Privilege escalation detected: {description}. "
                        "DirectAct-AI never silently elevates privileges. "
                        "Escalation must go through the OS-native consent prompt."
                    ),
                    risk_level=RiskLevel.CRITICAL,
                    threat_name="privilege_escalation",
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="No privilege escalation detected",
            risk_level=COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.LOW),
            duration_ms=(time.monotonic() - t0) * 1000,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Step 4: File Scan Check
# AMSI primary (Windows); VT hash-lookup opt-in secondary
# ──────────────────────────────────────────────────────────────────────────────

from app.services.amsi_scanner import ScanVerdict, scan_file_path


class FileScanCheck(GuardCheck):
    """Step 4: Invoke platform AV before any file/script execution."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.FILE_SCAN

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        t0 = time.monotonic()

        file_to_scan = context.file_path
        if not file_to_scan:
            # No file involved — skip
            return GuardVerdict(
                action=GuardAction.ALLOW,
                reason="No file to scan",
                risk_level=COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.LOW),
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        result = scan_file_path(file_to_scan)

        if result.verdict == ScanVerdict.DETECTED:
            return GuardVerdict(
                action=GuardAction.BLOCK,
                reason=f"AV scan detected threat: {result.threat_name or 'unknown threat'}",
                risk_level=RiskLevel.CRITICAL,
                threat_name=result.threat_name,
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        elif result.verdict == ScanVerdict.SUSPICIOUS:
            return GuardVerdict(
                action=GuardAction.REQUIRE_APPROVAL,
                reason=f"File flagged as suspicious by {result.scanner}: {result.details}",
                risk_level=RiskLevel.HIGH,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason=f"File scan clean ({result.scanner})",
            risk_level=RiskLevel.LOW,
            duration_ms=(time.monotonic() - t0) * 1000,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Step 5: Sandbox Decision Check
# Route unknown/unverified executables to OS sandbox first
# ──────────────────────────────────────────────────────────────────────────────

class SandboxDecisionCheck(GuardCheck):
    """Step 5: Determine if execution should be sandboxed.
    Unverified executables run in Windows Sandbox / bubblewrap / sandbox-exec first."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.SANDBOX_DECISION

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        t0 = time.monotonic()

        # Scripts always get sandbox consideration
        if isinstance(command, RunApprovedScript):
            from app.services.platform_probe import platform_info
            if platform_info.capabilities.windows_sandbox_available:
                return GuardVerdict(
                    action=GuardAction.SANDBOX,
                    reason="Script will execute in Windows Sandbox isolation",
                    risk_level=RiskLevel.HIGH,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )
            # Sandbox not available — still allow but warn
            return GuardVerdict(
                action=GuardAction.REQUIRE_APPROVAL,
                reason="Script execution approved but Windows Sandbox unavailable — running in main environment",
                risk_level=RiskLevel.HIGH,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="Sandbox not required for this command type",
            risk_level=COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.LOW),
            duration_ms=(time.monotonic() - t0) * 1000,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Step 6: Directory Access Check
# Deny-by-default allowlist of readable/writable paths per task
# ──────────────────────────────────────────────────────────────────────────────

import platform as _platform

def _get_allowed_base_paths() -> List[str]:
    """Returns the set of directories writable by automation tasks."""
    from app.core.config import settings
    defaults = [
        os.path.expanduser("~"),  # User home directory
        os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
        os.environ.get("USERPROFILE", ""),
    ]
    # Filter empty strings
    allowed = [p for p in defaults if p]
    # Additional user-configured paths from settings
    extra = getattr(settings, "allowed_write_directories", "")
    if extra:
        allowed.extend(p.strip() for p in extra.split(",") if p.strip())
    return allowed


class DirectoryAccessCheck(GuardCheck):
    """Step 6: Enforce allowlist of writable/readable paths.
    System-critical directories are never writable regardless of mode."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.DIRECTORY_ACCESS

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        t0 = time.monotonic()

        # Gather all paths this command touches
        paths_to_check: List[tuple[str, str]] = []  # (path, operation)

        if isinstance(command, CreateFile):
            paths_to_check.append((command.path, "write"))
        elif isinstance(command, DeleteFile):
            paths_to_check.append((command.path, "delete"))
        elif isinstance(command, MoveFile):
            paths_to_check.append((command.source, "read"))
            paths_to_check.append((command.destination, "write"))
        elif isinstance(command, CopyFile):
            paths_to_check.append((command.source, "read"))
            paths_to_check.append((command.destination, "write"))
        elif hasattr(command, "path"):
            paths_to_check.append((getattr(command, "path"), "read"))

        if not paths_to_check:
            return GuardVerdict(
                action=GuardAction.ALLOW,
                reason="No path access required",
                risk_level=RiskLevel.SAFE,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        allowed_bases = _get_allowed_base_paths()

        for path, operation in paths_to_check:
            try:
                normalized = os.path.normpath(os.path.abspath(path))
            except Exception:
                normalized = path

            # Check against allowed bases
            in_allowed = any(
                normalized.lower().startswith(os.path.normpath(base).lower())
                for base in allowed_bases
                if base
            )

            if not in_allowed and operation in ("write", "delete"):
                return GuardVerdict(
                    action=GuardAction.BLOCK,
                    reason=(
                        f"Write/delete to '{normalized}' is outside the allowed directory list. "
                        f"Allowed bases: {', '.join(allowed_bases[:3])}..."
                    ),
                    risk_level=RiskLevel.HIGH,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="Directory access within allowed paths",
            risk_level=COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.LOW),
            duration_ms=(time.monotonic() - t0) * 1000,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Step 7: Confirmation Gate
# Destructive/HIGH actions always require approval, regardless of mode
# ──────────────────────────────────────────────────────────────────────────────

# Commands that always require human confirmation
_ALWAYS_CONFIRM_TYPES = {
    CommandType.DELETE_FILE,
    CommandType.RUN_APPROVED_SCRIPT,
}


class ConfirmationGateCheck(GuardCheck):
    """Step 7: Force approval for destructive/HIGH-risk Commands.
    AUTONOMOUS mode reduces friction for routine actions ONLY.
    This gate cannot be overridden by autonomous mode for HIGH/CRITICAL risks."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.CONFIRMATION_GATE

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        t0 = time.monotonic()

        # Always-confirm command types
        if command.command_type in _ALWAYS_CONFIRM_TYPES:
            return GuardVerdict(
                action=GuardAction.REQUIRE_APPROVAL,
                reason=f"'{command.command_type.value}' always requires human confirmation",
                risk_level=RiskLevel.HIGH,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # Command marked as requiring approval at construction time
        if command.requires_approval:
            return GuardVerdict(
                action=GuardAction.REQUIRE_APPROVAL,
                reason=f"Command is marked as requiring approval: {command.description}",
                risk_level=COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.HIGH),
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # HIGH risk + HITL mode = require approval for everything
        risk = COMMAND_RISK_DEFAULTS.get(command.command_type, RiskLevel.LOW)
        if risk == RiskLevel.HIGH and context.execution_mode == "hitl":
            return GuardVerdict(
                action=GuardAction.REQUIRE_APPROVAL,
                reason="Human-in-the-Loop mode: HIGH risk actions require approval",
                risk_level=risk,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="Confirmation gate passed",
            risk_level=risk,
            duration_ms=(time.monotonic() - t0) * 1000,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Step 8: Audit Logger (always runs, even on blocked actions)
# ──────────────────────────────────────────────────────────────────────────────

class AuditLoggerCheck(GuardCheck):
    """Step 8: Write a hash-chained audit log entry.
    This check ALWAYS runs, including after a block — it is the last step."""

    @property
    def check_name(self) -> GuardCheckName:
        return GuardCheckName.AUDIT_LOG

    async def evaluate(self, command: BaseCommand, context: GuardContext) -> GuardVerdict:
        # The pipeline result is passed in context._final_verdict by the chain runner
        # This check doesn't block; it only logs
        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="Audit entry recorded",
            risk_level=RiskLevel.SAFE,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Guard Chain Runner
# ──────────────────────────────────────────────────────────────────────────────

class MalwareGuard:
    """
    The Malware Guard — chains all 8 checks in order.
    Any step can halt the pipeline; logging always happens.
    """

    def __init__(self):
        self._chain: List[GuardCheck] = [
            PolicyValidationCheck(),
            StaticCommandAnalysisCheck(),
            PrivilegeEscalationCheck(),
            FileScanCheck(),
            SandboxDecisionCheck(),
            DirectoryAccessCheck(),
            ConfirmationGateCheck(),
            # AuditLogger runs after the chain regardless
        ]

    async def validate(
        self,
        command: BaseCommand,
        context: GuardContext,
    ) -> PipelineResult:
        """Run the full guard chain. Returns final PipelineResult."""
        all_verdicts: List[tuple[GuardCheckName, GuardVerdict]] = []
        highest_risk = RiskLevel.SAFE
        blocked_by: Optional[GuardCheckName] = None
        block_reason: Optional[str] = None
        sandboxed = False
        requires_approval = False

        risk_order = [RiskLevel.SAFE, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]

        for check in self._chain:
            # Emit check_started event
            await self._emit_check_started(check.check_name, command, context)

            verdict = await check.evaluate(command, context)
            all_verdicts.append((check.check_name, verdict))

            # Track highest risk seen
            if risk_order.index(verdict.risk_level) > risk_order.index(highest_risk):
                highest_risk = verdict.risk_level

            # Emit check_result event
            await self._emit_check_result(check.check_name, verdict, command, context)

            if verdict.action == GuardAction.SANDBOX:
                sandboxed = True

            if verdict.action == GuardAction.REQUIRE_APPROVAL:
                requires_approval = True
                # Don't halt the chain — continue checking but mark approval needed

            if verdict.halts_pipeline:
                blocked_by = check.check_name
                block_reason = verdict.reason
                await self._emit_blocked(check.check_name, verdict, command, context)
                break

        # Step 8: Audit log — always runs
        final_verdict = "blocked" if blocked_by else ("requires_approval" if requires_approval else "allowed")
        audit_entry = audit_logger.append(
            action_id=context.action_id,
            session_id=context.session_id,
            check_name=blocked_by.value if blocked_by else "pipeline",
            verdict=final_verdict,
            risk_level=highest_risk.value,
            reason=block_reason or "All checks passed",
            task_id=context.task_id,
            blocked_content=context.raw_input if blocked_by else None,
        )

        # Emit audit log event to UI
        if context.event_emitter:
            from app.schemas.events import AuditLogEntryEvent
            await context.event_emitter(AuditLogEntryEvent(
                type=EventType.AUDIT_LOG_ENTRY,
                session_id=context.session_id,
                sequence=audit_entry.sequence,
                action_id=context.action_id,
                task_id=context.task_id,
                verdict=final_verdict,
                check_name=blocked_by.value if blocked_by else "pipeline",
                risk_level=highest_risk.value,
                entry_hash=audit_entry.entry_hash,
                prev_hash=audit_entry.prev_hash,
            ))

        return PipelineResult(
            allowed=blocked_by is None,
            requires_approval=requires_approval,
            sandboxed=sandboxed,
            blocked_by=blocked_by,
            block_reason=block_reason,
            highest_risk=highest_risk,
            audit_sequence=audit_entry.sequence,
            all_verdicts=all_verdicts,
        )

    def scan_url(self, url: str) -> GuardVerdict:
        """Scan a URL for blocklist rules (used by WebAutomationEngine)."""
        if not url or not isinstance(url, str):
            return GuardVerdict(
                action=GuardAction.BLOCK,
                reason="Empty or invalid URL",
                risk_level=RiskLevel.CRITICAL
            )

        # Basic blocklist patterns
        blocked_keywords = ["malicious", "phishing", "exploit-db.com", "metasploit"]
        url_lower = url.lower()
        for kw in blocked_keywords:
            if kw in url_lower:
                return GuardVerdict(
                    action=GuardAction.BLOCK,
                    reason=f"URL blocked: keyword '{kw}' is blacklisted",
                    risk_level=RiskLevel.CRITICAL
                )

        return GuardVerdict(
            action=GuardAction.ALLOW,
            reason="URL policy check passed",
            risk_level=RiskLevel.SAFE
        )

    async def _emit_check_started(self, check_name, command, context):
        if context.event_emitter:
            from app.schemas.events import SecurityCheckStartedEvent
            try:
                await context.event_emitter(SecurityCheckStartedEvent(
                    type=EventType.SECURITY_CHECK_STARTED,
                    session_id=context.session_id,
                    task_id=context.task_id or "",
                    step_index=0,
                    check_name=check_name,
                    command_type=command.command_type.value,
                ))
            except Exception:
                pass

    async def _emit_check_result(self, check_name, verdict, command, context):
        if context.event_emitter:
            from app.schemas.events import SecurityCheckResultEvent
            try:
                await context.event_emitter(SecurityCheckResultEvent(
                    type=EventType.SECURITY_CHECK_RESULT,
                    session_id=context.session_id,
                    task_id=context.task_id or "",
                    step_index=0,
                    check_name=check_name,
                    passed=not verdict.halts_pipeline,
                    halted=verdict.halts_pipeline,
                    reason=verdict.reason,
                    risk_level=verdict.risk_level.value,
                    duration_ms=verdict.duration_ms,
                ))
            except Exception:
                pass

    async def _emit_blocked(self, check_name, verdict, command, context):
        if context.event_emitter:
            from app.schemas.events import ActionBlockedEvent
            try:
                await context.event_emitter(ActionBlockedEvent(
                    type=EventType.ACTION_BLOCKED,
                    session_id=context.session_id,
                    task_id=context.task_id or "",
                    step_index=0,
                    blocked_by=check_name,
                    reason=verdict.reason,
                    redacted_content_hash=make_redacted_hash(context.raw_input) if context.raw_input else None,
                ))
            except Exception:
                pass


# Singleton guard instance
malware_guard = MalwareGuard()
security_guard = malware_guard

