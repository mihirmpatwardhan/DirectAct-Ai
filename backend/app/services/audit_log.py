"""
Hash-Chained Audit Log — Phase 1.1
=====================================
Tamper-evident, append-only audit log for all security decisions.

Each entry includes:
  - SHA-256 hash of its own content
  - SHA-256 hash of the previous entry (chain link)
  - Tamper detection: any modification breaks the chain

Content redaction:
  - Blocked content (e.g. detected malware strings) is NEVER stored verbatim
  - Stored as SHA-256(content) for audit matching without re-creating the risk
  - The UI receives the hash only; "Reveal" action shows only the reason, not content

Storage: SQLite `audit_chain` table + human-readable log file rotation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Chain Entry
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    """A single entry in the hash-chained audit log."""
    sequence: int
    timestamp: str              # ISO-8601 UTC
    action_id: str
    task_id: Optional[str]
    session_id: str
    check_name: str             # Which guard step produced this entry
    verdict: str                # "allowed" | "blocked" | "requires_approval"
    risk_level: str
    reason: str                 # Human-readable (sanitized)
    redacted_content_hash: Optional[str]  # SHA-256 of blocked content; never the content itself
    prev_hash: str              # Hash of previous entry
    entry_hash: str = ""        # Computed after all fields set

    def compute_hash(self) -> str:
        """Compute SHA-256 of this entry's content (excluding entry_hash itself)."""
        content = json.dumps({
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "action_id": self.action_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "check_name": self.check_name,
            "verdict": self.verdict,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "redacted_content_hash": self.redacted_content_hash,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# Audit Logger
# ──────────────────────────────────────────────────────────────────────────────

class AuditLogger:
    """
    Hash-chained append-only audit logger.

    Thread/async safety: entries are appended sequentially; callers should not
    call append() concurrently for the same log. The orchestrator serializes
    audit log writes through the EventBus.
    """

    GENESIS_HASH = "0" * 64  # The "previous hash" for the very first entry

    def __init__(self, log_file_path: str = "./logs/audit_chain.jsonl"):
        self._log_file = log_file_path
        self._sequence = 0
        self._last_hash = self.GENESIS_HASH
        self._entries_buffer: list[AuditEntry] = []
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        self._load_last_state()

    def _load_last_state(self):
        """Resume sequence and last hash from existing log file."""
        if not os.path.exists(self._log_file):
            logger.info("AuditLog: starting new chain")
            return
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines:
                last = json.loads(lines[-1])
                self._sequence = last.get("sequence", 0) + 1
                self._last_hash = last.get("entry_hash", self.GENESIS_HASH)
                logger.info(f"AuditLog: resumed at sequence={self._sequence}, last_hash={self._last_hash[:16]}...")
        except Exception as e:
            logger.error(f"AuditLog: failed to load existing chain: {e} — starting fresh")

    def append(
        self,
        action_id: str,
        session_id: str,
        check_name: str,
        verdict: str,
        risk_level: str,
        reason: str,
        task_id: Optional[str] = None,
        blocked_content: Optional[str] = None,
    ) -> AuditEntry:
        """Append a new entry to the chain and write to disk."""
        redacted_hash = None
        if blocked_content:
            redacted_hash = hashlib.sha256(blocked_content.encode("utf-8")).hexdigest()

        entry = AuditEntry(
            sequence=self._sequence,
            timestamp=datetime.utcnow().isoformat() + "Z",
            action_id=action_id,
            task_id=task_id,
            session_id=session_id,
            check_name=check_name,
            verdict=verdict,
            risk_level=risk_level,
            reason=reason,
            redacted_content_hash=redacted_hash,
            prev_hash=self._last_hash,
        )
        entry.entry_hash = entry.compute_hash()

        self._sequence += 1
        self._last_hash = entry.entry_hash

        # Write to JSONL file
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except Exception as e:
            logger.error(f"AuditLog: failed to write entry: {e}")

        logger.debug(
            f"AuditLog: seq={entry.sequence} [{verdict}] {check_name} "
            f"action={action_id[:8]} hash={entry.entry_hash[:12]}..."
        )
        return entry

    def verify_chain(self) -> tuple[bool, str]:
        """Verify the integrity of the entire chain. Returns (valid, message)."""
        if not os.path.exists(self._log_file):
            return True, "Chain is empty — nothing to verify"

        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            prev_hash = self.GENESIS_HASH
            for i, line in enumerate(lines):
                entry_dict = json.loads(line)
                seq = entry_dict.get("sequence", -1)

                # Re-compute expected hash
                temp = AuditEntry(**{k: v for k, v in entry_dict.items() if k != "entry_hash"})
                expected_hash = temp.compute_hash()
                stored_hash = entry_dict.get("entry_hash", "")

                if stored_hash != expected_hash:
                    return False, f"Chain broken at sequence {seq}: hash mismatch"
                if entry_dict.get("prev_hash") != prev_hash:
                    return False, f"Chain broken at sequence {seq}: prev_hash link broken"

                prev_hash = stored_hash

            return True, f"Chain valid — {len(lines)} entries verified"
        except Exception as e:
            return False, f"Chain verification error: {e}"

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Return recent audit entries for the UI Security Status panel."""
        if not os.path.exists(self._log_file):
            return []
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            entries = []
            for line in lines[-limit:]:
                entry = json.loads(line)
                # Redact sensitive fields before UI display
                entry.pop("redacted_content_hash", None)  # Only shown on explicit reveal
                entries.append(entry)
            return list(reversed(entries))
        except Exception as e:
            logger.error(f"AuditLog: failed to read entries: {e}")
            return []


# Singleton
audit_logger = AuditLogger(log_file_path="./logs/audit_chain.jsonl")
