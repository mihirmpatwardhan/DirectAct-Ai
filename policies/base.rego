# DirectAct-AI — Base Security Policy
# =====================================
# Deny-by-default policy for the Malware Guard pipeline.
# Evaluated per-Command before any OS execution occurs.
#
# Policy structure:
#   - base.rego   — global deny-by-default + shared helpers
#   - filesystem.rego — directory and extension rules
#   - privilege.rego  — privilege escalation rules
#   - commands.rego   — per-CommandType risk classification
#
# All decisions are logged by the guard chain (Step 8: AuditLogger).
# Policies are versioned alongside application code (not config files).
# Run tests with: opa test policies/ -v

package directact.guard.base

import future.keywords.if
import future.keywords.in

# ──────────────────────────────────────────────────────────────────────────────
# Deny-by-default: everything is blocked unless explicitly allowed
# ──────────────────────────────────────────────────────────────────────────────

default allow = false
default deny = true
default risk_level = "medium"
default requires_approval = false

# ──────────────────────────────────────────────────────────────────────────────
# Top-level decision: allow if command_type is in the permitted set
# and no specific deny rule matches
# ──────────────────────────────────────────────────────────────────────────────

allow if {
    input.command_type in permitted_command_types
    not deny
}

# All permitted command types (closed vocabulary)
permitted_command_types := {
    "launch_app",
    "close_app",
    "focus_app",
    "create_file",
    "delete_file",
    "move_file",
    "copy_file",
    "rename_file",
    "read_file",
    "list_directory",
    "create_directory",
    "get_system_info",
    "take_screenshot",
    "get_clipboard",
    "set_clipboard",
    "run_approved_script",
    "open_url",
    "query_llm"
}

# ──────────────────────────────────────────────────────────────────────────────
# Hard denies — these override any allow rule
# ──────────────────────────────────────────────────────────────────────────────

# Unknown command types are always denied
deny if {
    not input.command_type in permitted_command_types
}

# Script execution requires the global feature flag
deny if {
    input.command_type == "run_approved_script"
    not input.script_execution_enabled
}

# Absolute block: write to protected system directories
deny if {
    input.command_type in {"create_file", "move_file", "copy_file", "delete_file"}
    protected_path(input.path)
}

deny if {
    input.command_type in {"move_file", "copy_file"}
    protected_path(input.destination)
}

# ──────────────────────────────────────────────────────────────────────────────
# Risk classification
# ──────────────────────────────────────────────────────────────────────────────

risk_level := "safe" if { input.command_type in safe_commands }
risk_level := "low" if { input.command_type in low_risk_commands }
risk_level := "medium" if { input.command_type in medium_risk_commands }
risk_level := "high" if { input.command_type in high_risk_commands }
risk_level := "critical" if { deny }

safe_commands := {
    "get_system_info", "take_screenshot", "get_clipboard",
    "list_directory", "read_file", "focus_app", "query_llm"
}

low_risk_commands := {
    "launch_app", "close_app", "set_clipboard",
    "create_file", "copy_file", "create_directory", "open_url"
}

medium_risk_commands := {
    "move_file", "rename_file"
}

high_risk_commands := {
    "delete_file", "run_approved_script"
}

# ──────────────────────────────────────────────────────────────────────────────
# Approval requirements
# ──────────────────────────────────────────────────────────────────────────────

requires_approval if { input.command_type == "delete_file" }
requires_approval if { input.command_type == "run_approved_script" }

# HIGH risk in HITL mode always requires approval
requires_approval if {
    input.execution_mode == "hitl"
    input.command_type in high_risk_commands
}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

protected_prefixes := {
    "c:\\windows",
    "c:\\windows\\system32",
    "c:\\windows\\syswow64",
    "c:\\program files",
    "/etc",
    "/sys",
    "/proc",
    "/boot",
    "/system",
    "/library/system"
}

protected_path(p) if {
    lower_p := lower(p)
    some prefix in protected_prefixes
    startswith(lower_p, prefix)
}
