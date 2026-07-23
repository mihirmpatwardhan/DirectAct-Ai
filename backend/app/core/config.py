from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import List, Any
import os


class Settings(BaseSettings):
    # App
    app_name: str = Field(default="DirectAct-AI")
    app_version: str = Field(default="0.1.0")
    debug: bool = Field(default=True)
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
    openai_api_key: str = Field(default="")

    # Security
    secret_key: str = Field(default="dev-secret-key-change-in-production")
    max_session_duration_minutes: int = Field(default=480)

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

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }


settings = Settings()
