import logging
import secrets
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import List, Any
import os
from pathlib import Path

logger = logging.getLogger(__name__)

BACKEND_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# A weak sentinel so we can warn at startup without breaking dev
_WEAK_JWT_SECRET = "directact-jwt-super-secret-key-change-in-production-2026"
_WEAK_SECRET_KEY = "dev-secret-key-change-in-production"


class Settings(BaseSettings):
    # App
    app_name: str = Field(default="DirectAct-AI")
    app_version: str = Field(default="0.1.0")
    # FIX: debug defaults to False — never expose debug mode unless explicitly set
    debug: bool = Field(default=False)
    log_level: str = Field(default="info")

    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)

    # CORS — stored as str in .env, parsed via validator
    cors_origins_raw: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        alias="CORS_ORIGINS",
        validation_alias="CORS_ORIGINS",
    )

    @property
    def cors_origins(self) -> List[str]:
        raw = self.cors_origins_raw or ""
        return [o.strip() for o in raw.split(",") if o.strip()]

    # Database
    database_url: str = Field(default="sqlite+aiosqlite:///./directact.db")

    # LLM
    llm_provider: str = Field(default="gemini")
    gemini_api_key: str = Field(default="")
    gemini_api_keys: str = Field(default="")
    openai_api_key: str = Field(default="")
    openai_api_keys: str = Field(default="")
    openai_model: str = Field(default="gpt-4o")
    groq_api_key: str = Field(default="")
    groq_api_keys: str = Field(default="")
    groq_model: str = Field(default="openai/gpt-oss-120b")
    nvidia_api_key: str = Field(default="")
    nvidia_api_keys: str = Field(default="")
    nvidia_model: str = Field(default="meta/llama-3.2-11b-vision-instruct")
    llm_use_keyring: bool = Field(
        default=False,
        description="Read fallback API keys from the OS keychain; disabled by default to keep startup fast.",
    )
    llm_attempt_timeout_seconds: float = Field(
        default=8.0,
        ge=1.0,
        le=60.0,
        description="Maximum time to wait for one LLM key before rotating to the next healthy key.",
    )
    agent_llm_timeout_seconds: float = Field(
        default=35.0,
        ge=5.0,
        le=120.0,
        description="Total time budget for one browser-agent decision across all LLM fallback keys.",
    )

    # Security
    secret_key: str = Field(default=_WEAK_SECRET_KEY)
    max_session_duration_minutes: int = Field(default=480)

    # JWT Authentication
    jwt_secret_key: str = Field(default=_WEAK_JWT_SECRET)
    jwt_algorithm: str = Field(default="HS256")
    jwt_expire_minutes: int = Field(default=1440)  # 24 hours

    # Audit
    audit_log_path: str = Field(default="./logs/audit.log")
    screenshot_path: str = Field(default="./logs/screenshots")

    # VirusTotal — opt-in secondary only, never the primary scanner
    # Never set without explicit user consent (see amsi_scanner.py)
    virustotal_api_key: str = Field(default="")
    virustotal_user_consented: bool = Field(default=False)

    # Security — Malware Guard settings
    # Script execution is OFF by default — must be explicitly enabled
    enable_script_execution: bool = Field(default=False)
    # Comma-separated list of additional allowed write directories beyond home
    allowed_write_directories: str = Field(default="")

    # Execution mode — default is Human-in-the-Loop for safety
    # Can be overridden per-session in the UI
    default_execution_mode: str = Field(default="hitl")
    max_agent_steps: int = Field(
        default=100,
        description="Max steps for autonomous agents (0 for practically unlimited until task completes or token expires)",
    )

    # MCP Browser Automation (Phase 5)
    mcp_browser_profile_dir: str = Field(
        default="~/.directact/browser-profile",
        description="Dedicated browser profile directory for MCP automation (separate from personal Chrome)",
    )
    mcp_use_system_chrome: bool = Field(
        default=False,
        description="Use system Chrome profile (with saved passwords/cookies) for browser automation. Default off to avoid profile-lock when Chrome is already open.",
    )
    chrome_user_data_dir: str = Field(
        default="",
        description="Override path to Chrome User Data directory. Auto-detected if empty.",
    )
    chrome_profile_name: str = Field(
        default="Default",
        description="Chrome profile folder name (e.g. 'Default', 'Profile 1')",
    )
    mcp_default_headless: bool = Field(
        default=False,
        description="Run MCP browser in headless mode by default (screenshots still streamed to UI)",
    )
    mcp_screenshot_quality: int = Field(
        default=55,
        description="JPEG quality for MCP screenshots (lower = faster streaming)",
    )
    mcp_screenshot_max_width: int = Field(
        default=1280,
        description="Max width for MCP screenshots in pixels",
    )
    mcp_dom_wait_strategy: str = Field(
        default="domcontentloaded",
        description="Page wait strategy: 'domcontentloaded' (fast) or 'networkidle' (complete)",
    )

    model_config = {
        # Resolve relative to the backend package so startup works from either
        # the repository root or the backend directory.
        "env_file": str(BACKEND_ENV_FILE),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }

    def warn_weak_secrets(self) -> None:
        """Emit startup warnings for any insecure defaults still in use."""
        if self.jwt_secret_key == _WEAK_JWT_SECRET:
            logger.warning(
                "⚠️  JWT_SECRET_KEY is using the default insecure value. "
                "Set JWT_SECRET_KEY in your .env to a random 64-char secret before deploying."
            )
        if self.secret_key == _WEAK_SECRET_KEY:
            logger.warning(
                "⚠️  SECRET_KEY is using the default insecure value. "
                "Set SECRET_KEY in your .env to a random secret before deploying."
            )


settings = Settings()
