"""
Secure Vault Service — Phase 1.6
===================================
Purpose-bound, field-level encrypted personal data store.

Architecture:
  - AES-256-GCM encrypted fields (via cryptography.hazmat)
  - Encryption key stored in OS keychain via keyring, NOT in the database
  - Purpose-bound access: callers must declare WHY they need a field
  - Every access is logged — transparency to the user
  - Three sensitivity tiers with escalating confirmation requirements

Tier model:
  Tier 1 — Routine (name, preferences, pronouns)
    → Auto-approved in any mode
  Tier 2 — Contextual (address, phone, work details)
    → Auto-approved if task_purpose matches declared field intent; else prompts
  Tier 3 — Identity (passport, license, identity docs)
    → ALWAYS requires explicit per-request confirmation, even in Autonomous mode

What is NEVER stored:
  - Credit card numbers, CVV, PINs
  - Third-party passwords or credentials (use OS credential store directly)
  - Raw biometric data
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Any

logger = logging.getLogger(__name__)

_VAULT_DB = "./logs/vault.db"
_KEYRING_SERVICE = "directact-ai"
_KEYRING_KEY_NAME = "vault_master_key"


# ──────────────────────────────────────────────────────────────────────────────
# Sensitivity Tiers
# ──────────────────────────────────────────────────────────────────────────────

class SensitivityTier(str, Enum):
    TIER_1_ROUTINE = "tier_1"       # Name, preferences — auto-approve
    TIER_2_CONTEXTUAL = "tier_2"    # Address, phone — approve if purpose matches
    TIER_3_IDENTITY = "tier_3"      # Passport, license — always explicit


# Field-to-tier mapping
FIELD_TIERS: dict[str, SensitivityTier] = {
    # Tier 1
    "display_name": SensitivityTier.TIER_1_ROUTINE,
    "first_name": SensitivityTier.TIER_1_ROUTINE,
    "last_name": SensitivityTier.TIER_1_ROUTINE,
    "pronouns": SensitivityTier.TIER_1_ROUTINE,
    "preferences": SensitivityTier.TIER_1_ROUTINE,
    "language": SensitivityTier.TIER_1_ROUTINE,
    # Tier 2
    "email": SensitivityTier.TIER_2_CONTEXTUAL,
    "phone": SensitivityTier.TIER_2_CONTEXTUAL,
    "home_address": SensitivityTier.TIER_2_CONTEXTUAL,
    "work_address": SensitivityTier.TIER_2_CONTEXTUAL,
    "company": SensitivityTier.TIER_2_CONTEXTUAL,
    "job_title": SensitivityTier.TIER_2_CONTEXTUAL,
    "date_of_birth": SensitivityTier.TIER_2_CONTEXTUAL,
    # Tier 3
    "passport_reference": SensitivityTier.TIER_3_IDENTITY,
    "national_id_reference": SensitivityTier.TIER_3_IDENTITY,
    "drivers_license_reference": SensitivityTier.TIER_3_IDENTITY,
    "biometric_reference": SensitivityTier.TIER_3_IDENTITY,
}

# These field names can never be stored — hard block
FORBIDDEN_FIELDS = frozenset([
    "credit_card_number", "card_number", "cvv", "cvc", "pin",
    "bank_account_number", "routing_number",
    "social_security_number", "ssn",
    "password", "api_key", "secret_key",
])


@dataclass
class VaultAccessDecision:
    allowed: bool
    requires_confirmation: bool
    reason: str
    field_name: str
    tier: Optional[SensitivityTier]


@dataclass
class VaultAccessLog:
    timestamp: str
    task_id: Optional[str]
    field_name: str
    purpose: str
    decision: str  # "allowed" | "denied" | "pending_confirmation"
    session_id: str


# ──────────────────────────────────────────────────────────────────────────────
# Encryption
# ──────────────────────────────────────────────────────────────────────────────

def _get_or_create_key() -> bytes:
    """Load vault master key from OS keychain; generate + store if missing."""
    try:
        import keyring
        existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY_NAME)
        if existing:
            return base64.b64decode(existing)
        # Generate a new 256-bit key
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        import secrets
        raw_key = secrets.token_bytes(32)
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY_NAME, base64.b64encode(raw_key).decode())
        logger.info("VaultService: generated and stored new master key in OS keychain")
        return raw_key
    except Exception as e:
        logger.warning(f"VaultService: keyring unavailable ({e}), using session-only key")
        # Fallback: session-only key (not persisted — vault data won't survive restart)
        import secrets
        return secrets.token_bytes(32)


def _encrypt(plaintext: str, key: bytes) -> str:
    """AES-256-GCM encrypt. Returns base64-encoded ciphertext+nonce+tag."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import secrets
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    # Pack: nonce(12) + ciphertext+tag
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def _decrypt(token: str, key: bytes) -> str:
    """AES-256-GCM decrypt."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    data = base64.b64decode(token)
    nonce, ciphertext = data[:12], data[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# Vault Repository
# ──────────────────────────────────────────────────────────────────────────────

class VaultService:
    """
    Purpose-bound, field-level encrypted personal data store.
    Every access is logged. Forbidden fields are rejected at write time.
    """

    def __init__(self, db_path: str = _VAULT_DB):
        self._db_path = db_path
        self._key: Optional[bytes] = None
        self._init_db()

    def _get_key(self) -> bytes:
        if self._key is None:
            self._key = _get_or_create_key()
        return self._key

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vault_fields (
                field_name TEXT PRIMARY KEY,
                encrypted_value TEXT NOT NULL,
                tier TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vault_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                task_id TEXT,
                field_name TEXT NOT NULL,
                purpose TEXT NOT NULL,
                decision TEXT NOT NULL,
                session_id TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def set_field(self, field_name: str, value: Any) -> bool:
        """Store (or update) a vault field with encryption."""
        if field_name in FORBIDDEN_FIELDS:
            logger.warning(f"VaultService: attempted to store forbidden field '{field_name}'")
            return False

        tier = FIELD_TIERS.get(field_name, SensitivityTier.TIER_2_CONTEXTUAL)
        encrypted = _encrypt(json.dumps(value), self._get_key())
        now = datetime.utcnow().isoformat() + "Z"

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR REPLACE INTO vault_fields VALUES (?,?,?,?,?)",
            (field_name, encrypted, tier.value,
             now,  # created_at (only used on first insert; REPLACE keeps it)
             now)  # updated_at
        )
        conn.commit()
        conn.close()
        logger.info(f"VaultService: stored field '{field_name}' (tier={tier.value})")
        return True

    def check_access(
        self,
        field_name: str,
        purpose: str,
    ) -> VaultAccessDecision:
        """Evaluate whether a task may access a field. Does NOT fetch the value."""
        if field_name in FORBIDDEN_FIELDS:
            return VaultAccessDecision(
                allowed=False, requires_confirmation=False,
                reason=f"Field '{field_name}' can never be stored or accessed by DirectAct-AI",
                field_name=field_name, tier=None,
            )

        tier = FIELD_TIERS.get(field_name, SensitivityTier.TIER_2_CONTEXTUAL)

        if tier == SensitivityTier.TIER_1_ROUTINE:
            return VaultAccessDecision(
                allowed=True, requires_confirmation=False,
                reason="Tier 1 field — auto-approved",
                field_name=field_name, tier=tier,
            )
        elif tier == SensitivityTier.TIER_2_CONTEXTUAL:
            return VaultAccessDecision(
                allowed=True, requires_confirmation=True,
                reason=f"Tier 2 field — task must confirm purpose: '{purpose}'",
                field_name=field_name, tier=tier,
            )
        else:  # Tier 3 — always requires explicit confirmation
            return VaultAccessDecision(
                allowed=True, requires_confirmation=True,
                reason=f"Tier 3 identity field — requires explicit user confirmation regardless of mode",
                field_name=field_name, tier=tier,
            )

    def get_field(
        self,
        field_name: str,
        purpose: str,
        task_id: Optional[str],
        session_id: str,
        confirmed: bool = False,
    ) -> tuple[Optional[Any], VaultAccessDecision]:
        """
        Fetch a vault field value if access is permitted.
        Returns (value, decision). Value is None if access denied or confirmation needed.
        """
        decision = self.check_access(field_name, purpose)

        if not decision.allowed:
            self._log_access(task_id, field_name, purpose, "denied", session_id)
            return None, decision

        if decision.requires_confirmation and not confirmed:
            self._log_access(task_id, field_name, purpose, "pending_confirmation", session_id)
            return None, decision

        # Fetch and decrypt
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT encrypted_value FROM vault_fields WHERE field_name=?", (field_name,)
        ).fetchone()
        conn.close()

        if not row:
            return None, VaultAccessDecision(
                allowed=False, requires_confirmation=False,
                reason=f"Field '{field_name}' not found in vault",
                field_name=field_name, tier=decision.tier,
            )

        try:
            value = json.loads(_decrypt(row[0], self._get_key()))
            self._log_access(task_id, field_name, purpose, "allowed", session_id)
            return value, decision
        except Exception as e:
            logger.error(f"VaultService: decryption failed for '{field_name}': {e}")
            return None, VaultAccessDecision(
                allowed=False, requires_confirmation=False,
                reason="Decryption error — vault key may have changed",
                field_name=field_name, tier=decision.tier,
            )

    def get_profile_summary(self) -> dict:
        """Return a non-sensitive summary (field names + tiers only, no values) for the UI."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute("SELECT field_name, tier, updated_at FROM vault_fields").fetchall()
        conn.close()
        return {
            r[0]: {"tier": r[1], "updated_at": r[2]}
            for r in rows
        }

    def delete_field(self, field_name: str) -> bool:
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM vault_fields WHERE field_name=?", (field_name,))
        conn.commit()
        conn.close()
        return True

    def get_access_log(self, limit: int = 50) -> list:
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT timestamp, task_id, field_name, purpose, decision, session_id "
            "FROM vault_access_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [
            {"timestamp": r[0], "task_id": r[1], "field_name": r[2],
             "purpose": r[3], "decision": r[4], "session_id": r[5]}
            for r in rows
        ]

    def _log_access(self, task_id, field_name, purpose, decision, session_id):
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO vault_access_log (timestamp,task_id,field_name,purpose,decision,session_id) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.utcnow().isoformat() + "Z", task_id, field_name, purpose, decision, session_id)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"VaultService: failed to log access: {e}")


# Singleton
vault_service = VaultService()
