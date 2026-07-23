# DirectAct-AI — Privilege Escalation Policy
# ============================================
# DirectAct-AI NEVER silently elevates privileges.
# Escalation must go through the OS-native consent prompt (UAC/sudo/Polkit)
# initiated by the user — never triggered automatically by the AI.

package directact.guard.privilege

import future.keywords.if
import future.keywords.in

default privilege_escalation_detected = false

# Patterns that indicate privilege escalation attempts in raw input
escalation_patterns := {
    "runas",
    "sudo",
    "administrator",
    "run as admin",
    "invoke-command.*-credential",
    "start-process.*-verb runas",
    "net localgroup administrators",
    "schtasks.*highest"
}

privilege_escalation_detected if {
    lower_input := lower(input.raw_input)
    some pattern in escalation_patterns
    contains(lower_input, pattern)
}

# If escalation detected, deny
deny if {
    privilege_escalation_detected
}
