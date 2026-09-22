"""
Typed Action Vocabulary — Phase 1.0
=====================================
The closed, typed action vocabulary that constrains LLM output to a finite,
deterministic set of Command objects. This is what makes the Malware Guard's
deterministic validation possible — the guard validates typed Commands, not
free-form shell strings.

Design principles:
  - Every OS action is a Command object (execute + optional undo/compensate)
  - The LLM emits structured ActionPlan (list of Commands), never raw shell
  - Each Command has exactly the parameters it needs, no more (least privilege)
  - Script execution is a separate, extra-scrutinized CommandType, off by default

Usage:
    plan = ActionPlan(steps=[
        LaunchApp(app_id="notepad"),
        CreateFile(path="~/notes.txt", content="Hello"),
    ])
"""
from __future__ import annotations

import enum
from typing import Optional, List, Union, Literal
from pydantic import BaseModel, Field, field_validator
import os


# ──────────────────────────────────────────────────────────────────────────────
# Command Type Enumeration
# ──────────────────────────────────────────────────────────────────────────────

class CommandType(str, enum.Enum):
    """Exhaustive list of permitted action types. Adding a new type here
    requires a corresponding Rego policy entry — enforced in CI."""

    # Application lifecycle
    LAUNCH_APP = "launch_app"
    CLOSE_APP = "close_app"
    FOCUS_APP = "focus_app"

    # File system operations
    CREATE_FILE = "create_file"
    DELETE_FILE = "delete_file"
    MOVE_FILE = "move_file"
    COPY_FILE = "copy_file"
    RENAME_FILE = "rename_file"
    READ_FILE = "read_file"
    LIST_DIRECTORY = "list_directory"
    CREATE_DIRECTORY = "create_directory"

    # System information (read-only, always safe)
    GET_SYSTEM_INFO = "get_system_info"
    TAKE_SCREENSHOT = "take_screenshot"
    GET_CLIPBOARD = "get_clipboard"
    SET_CLIPBOARD = "set_clipboard"

    # Script execution (restricted, off by default, extra scrutiny)
    RUN_APPROVED_SCRIPT = "run_approved_script"

    # Web navigation
    OPEN_URL = "open_url"

    # Pure information (no OS side-effect)
    QUERY_LLM = "query_llm"


# ──────────────────────────────────────────────────────────────────────────────
# Risk Classification
# ──────────────────────────────────────────────────────────────────────────────

class RiskLevel(str, enum.Enum):
    """Risk level attached to each Command type by the guard's policy engine."""
    SAFE = "safe"           # Read-only, reversible, no OS state change
    LOW = "low"             # Creates state but easily reversible
    MEDIUM = "medium"       # Modifies existing state; partially reversible
    HIGH = "high"           # Destructive or privileged; requires approval
    CRITICAL = "critical"   # Always blocked; never executable via automation


# Default risk levels per CommandType (OPA policy may override per-invocation)
COMMAND_RISK_DEFAULTS: dict[CommandType, RiskLevel] = {
    CommandType.LAUNCH_APP: RiskLevel.LOW,
    CommandType.CLOSE_APP: RiskLevel.LOW,
    CommandType.FOCUS_APP: RiskLevel.SAFE,
    CommandType.CREATE_FILE: RiskLevel.LOW,
    CommandType.DELETE_FILE: RiskLevel.HIGH,        # Requires approval
    CommandType.MOVE_FILE: RiskLevel.MEDIUM,
    CommandType.COPY_FILE: RiskLevel.LOW,
    CommandType.RENAME_FILE: RiskLevel.MEDIUM,
    CommandType.READ_FILE: RiskLevel.SAFE,
    CommandType.LIST_DIRECTORY: RiskLevel.SAFE,
    CommandType.CREATE_DIRECTORY: RiskLevel.LOW,
    CommandType.GET_SYSTEM_INFO: RiskLevel.SAFE,
    CommandType.TAKE_SCREENSHOT: RiskLevel.SAFE,
    CommandType.GET_CLIPBOARD: RiskLevel.LOW,
    CommandType.SET_CLIPBOARD: RiskLevel.LOW,
    CommandType.RUN_APPROVED_SCRIPT: RiskLevel.HIGH,  # Always requires approval
    CommandType.OPEN_URL: RiskLevel.LOW,
    CommandType.QUERY_LLM: RiskLevel.SAFE,
}


# ──────────────────────────────────────────────────────────────────────────────
# Base Command Model
# ──────────────────────────────────────────────────────────────────────────────

class BaseCommand(BaseModel):
    """All Commands inherit from this. Guards validate this interface."""
    command_type: CommandType
    description: str = Field(
        default="",
        description="Human-readable description of what this step does (shown in Live Preview)"
    )
    requires_approval: bool = Field(
        default=False,
        description="Set True for HIGH/CRITICAL risk — overrides autonomous mode"
    )
    compensate_description: Optional[str] = Field(
        default=None,
        description="Human-readable description of what the undo/compensate action does"
    )

    model_config = {"frozen": False}

    @property
    def risk_level(self) -> RiskLevel:
        return COMMAND_RISK_DEFAULTS.get(self.command_type, RiskLevel.MEDIUM)


# ──────────────────────────────────────────────────────────────────────────────
# Application Commands
# ──────────────────────────────────────────────────────────────────────────────

class LaunchApp(BaseCommand):
    """Launch a desktop application by canonical ID or display name."""
    command_type: Literal[CommandType.LAUNCH_APP] = CommandType.LAUNCH_APP
    app_id: str = Field(
        description="Canonical app ID (e.g. 'notepad', 'vscode', 'chrome') or display name"
    )
    arguments: List[str] = Field(
        default_factory=list,
        description="Optional command-line arguments to pass to the application"
    )
    working_directory: Optional[str] = None

    @field_validator("app_id")
    @classmethod
    def validate_app_id(cls, v: str) -> str:
        v = v.strip().strip("\"'")
        if not v:
            raise ValueError("app_id cannot be empty")
        return v


class CloseApp(BaseCommand):
    """Close a running application gracefully, then forcefully if needed."""
    command_type: Literal[CommandType.CLOSE_APP] = CommandType.CLOSE_APP
    app_id: str = Field(description="App name or process name to close")
    force: bool = Field(default=False, description="Force-kill if graceful close fails")
    requires_approval: bool = False  # Closing apps is low-risk by default


class FocusApp(BaseCommand):
    """Bring an application window to the foreground."""
    command_type: Literal[CommandType.FOCUS_APP] = CommandType.FOCUS_APP
    app_id: str


# ──────────────────────────────────────────────────────────────────────────────
# File System Commands
# ──────────────────────────────────────────────────────────────────────────────

def _validate_path(path: str) -> str:
    """Normalize and basic-validate a file path. Guard will enforce allowlists."""
    path = path.strip().strip("\"'")
    # Expand common shortcuts
    path = os.path.expanduser(path)
    # Prevent obvious traversal attempts — the guard's DirectoryAccessCheck
    # does comprehensive allowlist enforcement; this is a lightweight pre-check
    normalized = os.path.normpath(path)
    dangerous_prefixes = [
        "C:\\Windows\\System32", "C:\\Windows\\SysWOW64",
        "/etc", "/sys", "/proc", "/System", "/Library/System",
    ]
    for prefix in dangerous_prefixes:
        if normalized.lower().startswith(prefix.lower()):
            raise ValueError(
                f"Path '{normalized}' targets a protected system directory. "
                "The Malware Guard will enforce this, but the vocabulary "
                "rejects it at construction time for immediate feedback."
            )
    return path


class CreateFile(BaseCommand):
    """Create a new file with optional content."""
    command_type: Literal[CommandType.CREATE_FILE] = CommandType.CREATE_FILE
    path: str
    content: str = Field(default="", description="File content (text only)")
    overwrite: bool = Field(default=False, description="Overwrite if file exists")
    compensate_description: str = "Delete the created file to undo"

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


class DeleteFile(BaseCommand):
    """Delete a file or empty directory. Always requires approval."""
    command_type: Literal[CommandType.DELETE_FILE] = CommandType.DELETE_FILE
    path: str
    requires_approval: bool = True  # Delete is always HIGH risk
    compensate_description: Optional[str] = None  # Deletion is not reversible

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


class MoveFile(BaseCommand):
    """Move a file or directory from source to destination."""
    command_type: Literal[CommandType.MOVE_FILE] = CommandType.MOVE_FILE
    source: str
    destination: str
    compensate_description: str = "Move the file back to its original location"

    @field_validator("source", "destination")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


class CopyFile(BaseCommand):
    """Copy a file or directory."""
    command_type: Literal[CommandType.COPY_FILE] = CommandType.COPY_FILE
    source: str
    destination: str
    compensate_description: str = "Delete the copied file at destination"

    @field_validator("source", "destination")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


class RenameFile(BaseCommand):
    """Rename a file in-place."""
    command_type: Literal[CommandType.RENAME_FILE] = CommandType.RENAME_FILE
    path: str
    new_name: str
    compensate_description: str = "Rename back to original name"

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


class ReadFile(BaseCommand):
    """Read and return the content of a text file (read-only)."""
    command_type: Literal[CommandType.READ_FILE] = CommandType.READ_FILE
    path: str
    max_bytes: int = Field(default=65536, description="Maximum bytes to read")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


class ListDirectory(BaseCommand):
    """List contents of a directory (read-only)."""
    command_type: Literal[CommandType.LIST_DIRECTORY] = CommandType.LIST_DIRECTORY
    path: str = Field(default="~", description="Directory to list; defaults to home")
    include_hidden: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


class CreateDirectory(BaseCommand):
    """Create a directory, including any missing parent directories."""
    command_type: Literal[CommandType.CREATE_DIRECTORY] = CommandType.CREATE_DIRECTORY
    path: str
    compensate_description: str = "Remove the created directory"

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_path(v)


# ──────────────────────────────────────────────────────────────────────────────
# System Information Commands (read-only, always safe)
# ──────────────────────────────────────────────────────────────────────────────

class GetSystemInfo(BaseCommand):
    """Retrieve OS, hardware, and user identity information (read-only)."""
    command_type: Literal[CommandType.GET_SYSTEM_INFO] = CommandType.GET_SYSTEM_INFO


class TakeScreenshot(BaseCommand):
    """Capture a screenshot of the primary display."""
    command_type: Literal[CommandType.TAKE_SCREENSHOT] = CommandType.TAKE_SCREENSHOT
    save_to: Optional[str] = Field(
        default=None,
        description="Optional path to save the screenshot; defaults to app screenshots dir"
    )


class GetClipboard(BaseCommand):
    """Read the current clipboard content."""
    command_type: Literal[CommandType.GET_CLIPBOARD] = CommandType.GET_CLIPBOARD


class SetClipboard(BaseCommand):
    """Set clipboard to the provided text."""
    command_type: Literal[CommandType.SET_CLIPBOARD] = CommandType.SET_CLIPBOARD
    content: str


# ──────────────────────────────────────────────────────────────────────────────
# Script Execution (restricted, off by default)
# ──────────────────────────────────────────────────────────────────────────────

class RunApprovedScript(BaseCommand):
    """Execute a pre-approved script. ALWAYS requires human approval.
    The guard subjects scripts to static analysis before execution.
    This command type is disabled by default — enable via settings."""
    command_type: Literal[CommandType.RUN_APPROVED_SCRIPT] = CommandType.RUN_APPROVED_SCRIPT
    script_content: str = Field(description="The script source to execute")
    interpreter: str = Field(
        default="powershell",
        description="Interpreter: 'powershell', 'bash', 'python'"
    )
    timeout_seconds: int = Field(default=30, le=300)
    requires_approval: bool = True  # ALWAYS — cannot be overridden


# ──────────────────────────────────────────────────────────────────────────────
# Web Stub (Phase 2 engine — orchestrator accepts now, engine dispatches later)
# ──────────────────────────────────────────────────────────────────────────────

class OpenURL(BaseCommand):
    """Navigate a browser to a URL. Dispatched to WebAutomationEngine in Phase 2."""
    command_type: Literal[CommandType.OPEN_URL] = CommandType.OPEN_URL
    url: str
    browser_id: Optional[str] = Field(default=None, description="Specific browser to use")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if v == "about:blank":
            return v
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v


# ──────────────────────────────────────────────────────────────────────────────
# Pure Query (no OS side-effect)
# ──────────────────────────────────────────────────────────────────────────────

class QueryLLM(BaseCommand):
    """Pure information query — no automation, no OS interaction."""
    command_type: Literal[CommandType.QUERY_LLM] = CommandType.QUERY_LLM
    query: str


# ──────────────────────────────────────────────────────────────────────────────
# Union type for type-safe dispatch
# ──────────────────────────────────────────────────────────────────────────────

Command = Union[
    LaunchApp, CloseApp, FocusApp,
    CreateFile, DeleteFile, MoveFile, CopyFile, RenameFile, ReadFile,
    ListDirectory, CreateDirectory,
    GetSystemInfo, TakeScreenshot, GetClipboard, SetClipboard,
    RunApprovedScript,
    OpenURL,
    QueryLLM,
]


# ──────────────────────────────────────────────────────────────────────────────
# Action Plan — the structured output the LLM emits
# ──────────────────────────────────────────────────────────────────────────────

class ActionPlan(BaseModel):
    """Ordered sequence of Commands comprising a multi-step task.
    The LLM emits this structure; the Orchestrator executes it step-by-step
    through the Malware Guard chain before each step."""
    task_id: str = Field(description="Unique identifier for this plan")
    original_intent: str = Field(description="The original user request")
    steps: List[Command] = Field(
        description="Ordered sequence of typed Commands to execute",
        min_length=1,
    )
    requires_sequential: bool = Field(
        default=True,
        description="If True, abort on first step failure; if False, continue on non-critical failures"
    )

    model_config = {"arbitrary_types_allowed": True}

    @property
    def highest_risk(self) -> RiskLevel:
        """Return the highest risk level across all steps."""
        levels = [s.risk_level for s in self.steps]
        order = [RiskLevel.SAFE, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        return max(levels, key=lambda l: order.index(l))

    @property
    def requires_any_approval(self) -> bool:
        return any(s.requires_approval for s in self.steps)


# ──────────────────────────────────────────────────────────────────────────────
# Command Result — returned after each step executes
# ──────────────────────────────────────────────────────────────────────────────

class CommandResult(BaseModel):
    """Result of executing a single Command."""
    command_type: CommandType
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    metadata: dict = Field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return not self.success or bool(self.error)
