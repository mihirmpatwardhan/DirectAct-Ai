"""
LLM Provider Abstraction — Phase 1.3
======================================
Provider-agnostic LLM interface with:
  - Abstract LLMProvider base class (Strategy pattern)
  - GeminiProvider + OpenAIProvider concrete implementations
  - KeyRotationManager with Circuit Breaker per key
  - PromptConstraintLayer: constrains LLM output to typed ActionPlan JSON
  - Keys loaded from OS keychain (keyring) or .env — NEVER hardcoded

Security requirements:
  - No API keys in source code
  - Keys loaded at startup from OS keychain via `keyring` library
  - Circuit breaker prevents hammering failed/quota-exhausted keys
  - LLM output always re-validated by Malware Guard regardless of prompt content
    (prompt injection can at worst cause a denied action, never a bypass)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, List, Dict, Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# System Prompt — defines the DirectAct-AI persona
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are DirectAct-AI, an intelligent AI Copilot specialized in local OS automation and security-first task execution.

Your capabilities:
- 🖥️ **OS Automation**: Launch apps, manage files, control system settings via a secure, typed action vocabulary
- 🔒 **Security-First**: Every action passes through a deterministic, non-LLM security gate before execution
- 🔍 **Information**: Answer questions, explain plans, summarize results
- 🌐 **Web** (Phase 2): Navigate websites and interact with web content

Communication style:
- Be concise and action-oriented — acknowledge what you're doing, not just what you plan
- Use markdown for clarity (bold, code blocks, numbered lists)
- When planning multi-step tasks, enumerate each step clearly
- If an action requires user approval, say so explicitly and explain why
- If an action is blocked, explain non-judgmentally what was blocked and what the user can do instead

IMPORTANT — Structured Action Output:
When the user requests an automation task, respond with BOTH:
1. A natural-language explanation (for the chat panel)
2. A JSON ```action_plan``` code block (parsed by the system)

Action plan format:
```action_plan
{
  "task_id": "<uuid>",
  "original_intent": "<user's request>",
  "steps": [
    {"command_type": "launch_app", "app_id": "notepad", "description": "Open Notepad"},
    {"command_type": "create_file", "path": "~/notes.txt", "content": "Hello", "description": "Create notes.txt"}
  ]
}
```

Available command types: launch_app, close_app, focus_app, create_file, delete_file, move_file, copy_file, rename_file, read_file, list_directory, create_directory, get_system_info, take_screenshot, get_clipboard, set_clipboard, open_url, query_llm.

For pure information queries, use command_type "query_llm" with no action plan block."""


# ──────────────────────────────────────────────────────────────────────────────
# Circuit Breaker
# ──────────────────────────────────────────────────────────────────────────────

class CircuitState(str, Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Tripped — not routing to this key
    HALF_OPEN = "half_open" # Testing if key has recovered


@dataclass
class KeyCircuitBreaker:
    """Per-API-key circuit breaker. Trips on repeated failures or quota errors."""
    key_hint: str                      # Last 8 chars of key for logging (never the full key)
    failure_threshold: int = 3         # Failures before tripping
    cooldown_seconds: int = 60         # Time in OPEN state before testing
    failure_count: int = 0
    state: CircuitState = CircuitState.CLOSED
    last_failure_time: float = 0.0
    quota_exhausted: bool = False

    def record_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.quota_exhausted = False

    def record_failure(self, is_quota_error: bool = False):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if is_quota_error:
            self.quota_exhausted = True
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"Circuit breaker OPEN for key ...{self.key_hint}")

    @property
    def is_available(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= self.cooldown_seconds:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN — allow one test request through
        return True


def _is_retryable_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return any(term in err_str for term in [
        "503", "unavailable", "overloaded", "resource exhausted",
        "429", "rate limit", "deadline exceeded", "try again later",
        "service unavailable", "temporary failure"
    ])


def _is_quota_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return any(term in err_str for term in ["quota", "per_minute", "per_day", "rate limit", "exceeded your current quota"])


def _extract_json(text: str) -> dict:
    if not text:
        return {}
    cleaned = text.strip()
    import re
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                pass
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Abstract LLM Provider (Strategy pattern)
# ──────────────────────────────────────────────────────────────────────────────

class LLMProvider(ABC):
    """Abstract base for all LLM provider implementations.
    Adding a new provider = implementing this interface and registering in the factory.
    Zero changes required in the Router, Orchestrator, or UI."""

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @property
    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    async def stream(
        self,
        history: List[Dict[str, str]],
        user_message: str,
    ) -> AsyncIterator[str]: ...

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str: ...

    @abstractmethod
    async def complete_json(self, prompt: str) -> dict: ...


# ──────────────────────────────────────────────────────────────────────────────
# Gemini Provider
# ──────────────────────────────────────────────────────────────────────────────

class GeminiProvider(LLMProvider):
    """Google Gemini implementation using the google-generativeai SDK."""

    CANDIDATE_MODELS = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-flash-latest",
    ]

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._key_hint = api_key[-8:] if len(api_key) >= 8 else "***"
        self._client = None
        self._model_name = None
        self._initialization_error: Optional[Exception] = None

    def _ensure_client(self) -> None:
        """Import and configure the heavy SDK only when this key is selected."""
        if self._client is not None:
            return
        if self._initialization_error is not None:
            raise RuntimeError("Gemini client initialization previously failed") from self._initialization_error

        try:
            import google.generativeai as genai
            genai.configure(api_key=self._api_key)
            for model_name in self.CANDIDATE_MODELS:
                try:
                    client = genai.GenerativeModel(
                        model_name=model_name,
                        system_instruction=SYSTEM_PROMPT,
                        generation_config={
                            "temperature": 0.7,
                            "max_output_tokens": 2048,
                            "top_p": 0.95,
                        },
                    )
                    self._client = client
                    self._model_name = model_name
                    logger.info(f"GeminiProvider: initialized ({model_name}, key=...{self._key_hint})")
                    return
                except Exception as error:
                    logger.debug(f"GeminiProvider: model {model_name} unavailable: {error}")
            raise RuntimeError("No Gemini model could be initialized")
        except Exception as error:
            self._initialization_error = error
            raise

    @property
    def provider_name(self) -> str:
        return f"gemini/{self._model_name or 'unknown'}"

    @property
    def is_available(self) -> bool:
        return self._initialization_error is None

    async def stream(
        self,
        history: List[Dict[str, str]],
        user_message: str,
    ) -> AsyncIterator[str]:
        if not self._client:
            self._ensure_client()
        try:
            gemini_history = [
                {"role": "user" if m["role"] == "user" else "model",
                 "parts": [m["content"]]}
                for m in history
            ]
            chat = self._client.start_chat(history=gemini_history)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: chat.send_message(user_message, stream=True)
            )
            for chunk in response:
                if chunk.text:
                    yield chunk.text
                    await asyncio.sleep(0)
        except Exception as e:
            logger.error(f"GeminiProvider stream error (key=...{self._key_hint}): {e}")
            raise

    async def complete(self, prompt: str, max_tokens: int = 512, temperature: float = 0.2) -> str:
        if not self._client:
            self._ensure_client()
        loop = asyncio.get_event_loop()
        retries = 3
        backoff = 1.0
        for attempt in range(retries):
            try:
                response = await loop.run_in_executor(
                    None, lambda: self._client.generate_content(prompt)
                )
                return response.text.strip()
            except Exception as e:
                if attempt < retries - 1 and _is_retryable_error(e):
                    logger.warning(
                        f"GeminiProvider complete retry {attempt + 1}/{retries} "
                        f"after transient error: {e}. Backing off {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                else:
                    raise

    async def complete_json(self, prompt: str) -> dict:
        text = await self.complete(prompt, max_tokens=1024, temperature=0.1)
        return _extract_json(text)


# ──────────────────────────────────────────────────────────────────────────────
# OpenAI Provider
# ──────────────────────────────────────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    """OpenAI GPT implementation using the openai async SDK."""

    def __init__(self, api_key: str, model: Optional[str] = None):
        self._key_hint = api_key[-8:] if len(api_key) >= 8 else "***"
        self._api_key = api_key
        self._client = None
        self._initialization_error: Optional[Exception] = None
        self._model = model or getattr(settings, "openai_model", None) or os.getenv("OPENAI_MODEL") or "gpt-4o"

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if self._initialization_error is not None:
            raise RuntimeError("OpenAI client initialization previously failed") from self._initialization_error
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._api_key)
            logger.info(f"OpenAIProvider: initialized (model={self._model}, key=...{self._key_hint})")
        except Exception as error:
            self._initialization_error = error
            raise

    @property
    def provider_name(self) -> str:
        return f"openai/{self._model}"

    @property
    def is_available(self) -> bool:
        return self._initialization_error is None

    async def stream(
        self,
        history: List[Dict[str, str]],
        user_message: str,
    ) -> AsyncIterator[str]:
        self._ensure_client()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend({"role": m["role"], "content": m["content"]} for m in history)
        messages.append({"role": "user", "content": user_message})

        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
            max_tokens=2048,
            temperature=0.7,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def complete(self, prompt: str, max_tokens: int = 512, temperature: float = 0.2) -> str:
        self._ensure_client()
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    async def complete_json(self, prompt: str) -> dict:
        text = await self.complete(prompt, max_tokens=1024, temperature=0.1)
        return _extract_json(text)


# ──────────────────────────────────────────────────────────────────────────────
# Groq Provider (Ultra-fast LPU inference via OpenAI-compatible API)
# ──────────────────────────────────────────────────────────────────────────────

class GroqProvider(LLMProvider):
    """Groq Cloud LPU implementation using AsyncOpenAI with Groq base URL."""

    CANDIDATE_MODELS = [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "qwen/qwen3.8-27b",
        "mixtral-8x7b-32768",
    ]

    def __init__(self, api_key: str, model: Optional[str] = None):
        self._key_hint = api_key[-8:] if len(api_key) >= 8 else "***"
        self._api_key = api_key
        self._client = None
        self._initialization_error: Optional[Exception] = None
        self._model = model or getattr(settings, "groq_model", None) or self.CANDIDATE_MODELS[0]

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if self._initialization_error is not None:
            raise RuntimeError("Groq client initialization previously failed") from self._initialization_error
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._api_key, base_url="https://api.groq.com/openai/v1")
            logger.info(f"GroqProvider: initialized (model={self._model}, key=...{self._key_hint})")
        except Exception as error:
            self._initialization_error = error
            raise

    @property
    def provider_name(self) -> str:
        return f"groq/{self._model}"

    @property
    def is_available(self) -> bool:
        return self._initialization_error is None

    async def stream(
        self,
        history: List[Dict[str, str]],
        user_message: str,
    ) -> AsyncIterator[str]:
        self._ensure_client()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend({"role": m["role"], "content": m["content"]} for m in history)
        messages.append({"role": "user", "content": user_message})

        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
            max_tokens=2048,
            temperature=0.7,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def complete(self, prompt: str, max_tokens: int = 512, temperature: float = 0.2) -> str:
        self._ensure_client()
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    async def complete_json(self, prompt: str) -> dict:
        text = await self.complete(prompt, max_tokens=1024, temperature=0.1)
        return _extract_json(text)


# ──────────────────────────────────────────────────────────────────────────────
# NVIDIA NIM Provider (Fast inference via OpenAI-compatible API)
# ──────────────────────────────────────────────────────────────────────────────

class NvidiaProvider(LLMProvider):
    """NVIDIA NIM implementation using AsyncOpenAI with NVIDIA base URL."""

    CANDIDATE_MODELS = [
        "meta/llama-3.2-11b-vision-instruct",
        "mistralai/mistral-7b-instruct-v0.3",
        "deepseek-ai/deepseek-v4.1-flash",
        "ibm/granite-3.0-8b-instruct",
    ]

    def __init__(self, api_key: str, model: Optional[str] = None):
        self._key_hint = api_key[-8:] if len(api_key) >= 8 else "***"
        self._api_key = api_key
        self._client = None
        self._initialization_error: Optional[Exception] = None
        self._model = model or getattr(settings, "nvidia_model", None) or self.CANDIDATE_MODELS[0]

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if self._initialization_error is not None:
            raise RuntimeError("NVIDIA client initialization previously failed") from self._initialization_error
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._api_key, base_url="https://integrate.api.nvidia.com/v1")
            logger.info(f"NvidiaProvider: initialized (model={self._model}, key=...{self._key_hint})")
        except Exception as error:
            self._initialization_error = error
            raise

    @property
    def provider_name(self) -> str:
        return f"nvidia/{self._model}"

    @property
    def is_available(self) -> bool:
        return self._initialization_error is None

    async def stream(
        self,
        history: List[Dict[str, str]],
        user_message: str,
    ) -> AsyncIterator[str]:
        self._ensure_client()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend({"role": m["role"], "content": m["content"]} for m in history)
        messages.append({"role": "user", "content": user_message})

        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
            max_tokens=2048,
            temperature=0.7,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def complete(self, prompt: str, max_tokens: int = 512, temperature: float = 0.2) -> str:
        self._ensure_client()
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    async def complete_json(self, prompt: str) -> dict:
        text = await self.complete(prompt, max_tokens=1024, temperature=0.1)
        return _extract_json(text)


# ──────────────────────────────────────────────────────────────────────────────
# Stub Provider (no API key configured)
# ──────────────────────────────────────────────────────────────────────────────

class StubProvider(LLMProvider):
    """Fallback stub for development — simulates streaming with helpful messages."""

    @property
    def provider_name(self) -> str:
        return "stub"

    @property
    def is_available(self) -> bool:
        return True

    async def stream(self, history: List[Dict[str, str]], user_message: str) -> AsyncIterator[str]:
        lower = user_message.lower()
        if any(kw in lower for kw in ["open", "launch", "start", "run"]):
            response = (
                "🖥️ **Desktop Action Detected**\n\n"
                f"I'll help you with: *{user_message}*\n\n"
                "**Steps I'll take:**\n"
                "1. Parse the request into a typed Action Plan\n"
                "2. Pass each step through the 8-stage Malware Guard\n"
                "3. Execute only after all checks pass\n\n"
                "> ⚠️ **Note**: Set `GEMINI_API_KEY` in your OS keychain or `backend/.env` to enable real AI responses."
            )
        elif any(kw in lower for kw in ["search", "browse", "navigate", "google", "find"]):
            response = (
                "🌐 **Web Navigation** (Phase 2)\n\n"
                f"I'll handle: *{user_message}*\n\n"
                "Web automation is planned for Phase 2. The architecture is ready — "
                "the typed `open_url` command will dispatch to the WebAutomationEngine.\n\n"
                "> ⚠️ **Note**: Set `GEMINI_API_KEY` in `backend/.env` to enable real AI."
            )
        else:
            response = (
                "👋 **DirectAct-AI is Online** *(Stub Mode)*\n\n"
                f"You asked: *{user_message}*\n\n"
                "I can help you:\n"
                "- 🖥️ **Control your desktop** — 'Open Notepad', 'Create a file called notes.txt'\n"
                "- 📁 **Manage files** — 'Move report.pdf to the Desktop', 'List my Documents folder'\n"
                "- 🔍 **Answer questions** — Ask anything\n\n"
                "> 💡 **To enable real AI**: Add `GEMINI_API_KEY` to `backend/.env`"
            )
        words = response.split(" ")
        for i in range(0, len(words), 3):
            yield " ".join(words[i:i + 3]) + " "
            await asyncio.sleep(0.03)

    async def complete(self, prompt: str, max_tokens: int = 512, temperature: float = 0.2) -> str:
        return '{"task_type": "query", "confidence": 0.5, "intent": "stub", "parameters": {}, "requires_approval": false}'

    async def complete_json(self, prompt: str) -> dict:
        # A development fallback must never claim that a browser task finished.
        # Returning `done` here was the direct cause of false green task cards
        # whenever no real provider/key was usable.
        return {
            "action": "fail",
            "target": "No usable LLM provider is configured; automation was not started.",
            "status": "unavailable",
        }


# ──────────────────────────────────────────────────────────────────────────────
# Key Rotation Manager
# ──────────────────────────────────────────────────────────────────────────────

class KeyRotationManager:
    """Manages a pool of API keys with circuit breaker per key.
    Transparent failover — no user-visible interruption on key/quota failure."""

    def __init__(self):
        self._providers: List[tuple[LLMProvider, KeyCircuitBreaker]] = []
        # Advance the preferred entry after every attempt.  This balances a
        # healthy key pool and, more importantly, makes a failed key's
        # replacement immediate instead of repeatedly selecting index zero.
        self._next_index = 0

    def register(self, provider: LLMProvider, key: str):
        hint = key[-8:] if len(key) >= 8 else "***"
        cb = KeyCircuitBreaker(key_hint=hint)
        self._providers.append((provider, cb))
        logger.info(f"KeyRotationManager: registered {provider.provider_name} key=...{hint}")

    def get_available(
        self, excluded: Optional[set[int]] = None
    ) -> Optional[tuple[LLMProvider, KeyCircuitBreaker]]:
        """Return the next healthy provider, skipping entries already tried."""
        if not self._providers:
            return None

        excluded = excluded or set()
        total = len(self._providers)
        for offset in range(total):
            index = (self._next_index + offset) % total
            provider, cb = self._providers[index]
            if index not in excluded and cb.is_available:
                return provider, cb
        return None

    def index_of(self, breaker: KeyCircuitBreaker) -> Optional[int]:
        for index, (_, cb) in enumerate(self._providers):
            if cb is breaker:
                return index
        return None

    def available_attempts(self):
        """Yield each currently healthy key at most once, in rotation order."""
        attempted: set[int] = set()
        while pair := self.get_available(attempted):
            index = self.index_of(pair[1])
            if index is None:
                return
            attempted.add(index)
            yield pair

    def mark_success(self, cb: KeyCircuitBreaker):
        cb.record_success()
        index = self.index_of(cb)
        if index is not None and self._providers:
            self._next_index = (index + 1) % len(self._providers)

    def mark_failure(self, cb: KeyCircuitBreaker, is_quota: bool = False):
        cb.record_failure(is_quota_error=is_quota)
        if cb.state.value == "open":
            logger.warning(f"KeyRotationManager: key ...{cb.key_hint} tripped (quota={is_quota})")

    @property
    def has_any_available(self) -> bool:
        return any(cb.is_available for _, cb in self._providers)


# ──────────────────────────────────────────────────────────────────────────────
# LLM Service — unified entry point
# ──────────────────────────────────────────────────────────────────────────────

def _load_keys_from_env() -> tuple[List[str], List[str], List[str], List[str]]:
    """Load API keys from .env settings.
    In production these would come from OS keychain via the `keyring` library.
    The keyring integration stub is below — enable when keys are in keychain."""
    gemini_keys = []
    openai_keys = []
    groq_keys = []
    nvidia_keys = []

    def add_configured(target: List[str], primary: str, pool: str, placeholders: set[str]):
        """Add a primary key plus every valid comma-separated fallback key."""
        for value in (primary, *(pool or "").split(",")):
            key = (value or "").strip()
            if key and key not in placeholders:
                target.append(key)

    # Primary key plus every configured fallback.  Gemini/OpenAI pools were
    # previously declared in Settings but never loaded, so their fallbacks
    # silently did nothing.
    add_configured(
        gemini_keys, settings.gemini_api_key, settings.gemini_api_keys,
        {"your_gemini_api_key_here", "YOUR_KEY_HERE"},
    )
    add_configured(
        openai_keys, settings.openai_api_key, settings.openai_api_keys,
        {"your_openai_api_key_here", "YOUR_KEY_HERE"},
    )
    add_configured(
        groq_keys, settings.groq_api_key, settings.groq_api_keys,
        {"your_groq_api_key_here", "YOUR_KEY_HERE"},
    )
    add_configured(
        nvidia_keys, settings.nvidia_api_key, settings.nvidia_api_keys,
        {"your_nvidia_api_key_here", "YOUR_KEY_HERE"},
    )

    # Credential Manager can block for many seconds on some Windows setups.
    # Environment-configured keys are the normal fast path, so only query the
    # keychain when the user explicitly opts in.
    use_keyring = bool(getattr(settings, "llm_use_keyring", False))
    if use_keyring:
        try:
            import keyring
            for i in range(1, 6):  # Support up to 5 pooled keys per provider
                gkey = keyring.get_password("directact-ai", f"gemini_api_key_{i}")
                if gkey:
                    gemini_keys.append(gkey)
                okey = keyring.get_password("directact-ai", f"openai_api_key_{i}")
                if okey:
                    openai_keys.append(okey)
                groq_key = keyring.get_password("directact-ai", f"groq_api_key_{i}")
                if groq_key:
                    groq_keys.append(groq_key)
                nvidia_key = keyring.get_password("directact-ai", f"nvidia_api_key_{i}")
                if nvidia_key:
                    nvidia_keys.append(nvidia_key)
        except ImportError:
            logger.debug("keyring not installed — OS keychain integration disabled. pip install keyring to enable.")
        except Exception as e:
            logger.warning(f"OS keychain lookup failed: {e}")

    # Deduplicate
    return (
        list(dict.fromkeys(gemini_keys)),
        list(dict.fromkeys(openai_keys)),
        list(dict.fromkeys(groq_keys)),
        list(dict.fromkeys(nvidia_keys)),
    )


class LLMService:
    """Unified LLM service: provider abstraction + key rotation + circuit breaker.

    Usage:
        async for chunk in llm_service.stream_response(history, user_message):
            ...
    """

    def __init__(self):
        self._rotation_manager = KeyRotationManager()
        self._stub = StubProvider()
        self._init_providers()

    def _init_providers(self):
        gemini_keys, openai_keys, groq_keys, nvidia_keys = _load_keys_from_env()

        def _reg_nvidia():
            for key in nvidia_keys:
                try:
                    provider = NvidiaProvider(key)
                    if provider.is_available:
                        self._rotation_manager.register(provider, key)
                except Exception as e:
                    logger.warning(f"Failed to initialize Nvidia provider: {e}")

        def _reg_groq():
            for key in groq_keys:
                try:
                    provider = GroqProvider(key)
                    if provider.is_available:
                        self._rotation_manager.register(provider, key)
                except Exception as e:
                    logger.warning(f"Failed to initialize Groq provider: {e}")

        def _reg_gemini():
            for key in gemini_keys:
                try:
                    provider = GeminiProvider(key)
                    if provider.is_available:
                        self._rotation_manager.register(provider, key)
                except Exception as e:
                    logger.warning(f"Failed to initialize Gemini provider: {e}")

        def _reg_openai():
            for key in openai_keys:
                try:
                    provider = OpenAIProvider(key)
                    if provider.is_available:
                        self._rotation_manager.register(provider, key)
                except Exception as e:
                    logger.warning(f"Failed to initialize OpenAI provider: {e}")

        pref = (settings.llm_provider or "").lower()
        if pref == "nvidia" or (nvidia_keys and not groq_keys and not gemini_keys and not openai_keys):
            _reg_nvidia()
            _reg_groq()
            _reg_gemini()
            _reg_openai()
        elif pref == "groq" or (groq_keys and not gemini_keys and not openai_keys):
            _reg_groq()
            _reg_nvidia()
            _reg_gemini()
            _reg_openai()
        elif pref == "openai":
            _reg_openai()
            _reg_nvidia()
            _reg_groq()
            _reg_gemini()
        else:
            _reg_gemini()
            _reg_nvidia()
            _reg_groq()
            _reg_openai()

        if not self._rotation_manager.has_any_available:
            logger.warning(
                "⚠️ No LLM API keys configured — using stub responses. "
                "Set NVIDIA_API_KEY, GROQ_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY in backend/.env"
            )
        else:
            logger.info(f"✅ LLM Service ready with {len(self._rotation_manager._providers)} key(s)")

    @property
    def is_configured(self) -> bool:
        return self._rotation_manager.has_any_available

    @property
    def active_provider(self) -> str:
        pair = self._rotation_manager.get_available()
        if pair:
            return pair[0].provider_name
        return "stub"

    async def warm_up(self):
        """Warm up LLM client in background thread so first request isn't slow."""
        try:
            pair = self._rotation_manager.get_available()
            if pair:
                logger.info(f"LLMService: warmed up provider {pair[0].provider_name}")
        except Exception as e:
            logger.debug(f"LLMService warm_up error: {e}")

    async def stream_response(
        self,
        history: List[Dict[str, str]],
        user_message: str,
    ) -> AsyncIterator[str]:
        """Stream LLM response with automatic key rotation on failure."""
        attempts = list(self._rotation_manager.available_attempts())
        if not attempts:
            async for chunk in self._stub.stream(history, user_message):
                yield chunk
            return

        failures: list[str] = []
        for provider, cb in attempts:
            try:
                async for chunk in provider.stream(history, user_message):
                    yield chunk
                self._rotation_manager.mark_success(cb)
                return
            except Exception as error:
                is_quota = _is_quota_error(error)
                self._rotation_manager.mark_failure(cb, is_quota=is_quota)
                failures.append(str(error))
                logger.warning("Provider %s failed while streaming; rotating immediately: %s", provider.provider_name, error)

        yield f"\n\n⚠️ *Every configured LLM key failed: {failures[-1][:200] if failures else 'unknown error'}.*"

    async def classify_intent(self, user_input: str) -> dict:
        """Use LLM to classify user input when rule-based router is ambiguous."""
        prompt = (
            "Analyze the following user input for an AI automation copilot.\n"
            "Respond ONLY with a valid JSON object:\n"
            '{"task_type": "web"|"desktop"|"query", "confidence": 0.0-1.0, '
            '"intent": "short summary", "parameters": {"url": "", "app_name": "", "query": ""}, '
            '"requires_approval": false}\n\n'
            f'User Input: "{user_input}"'
        )
        attempts = list(self._rotation_manager.available_attempts())
        if not attempts:
            return {"task_type": "query", "confidence": 0.5, "intent": user_input[:80],
                    "parameters": {}, "requires_approval": False}

        for provider, cb in attempts:
            try:
                text = await asyncio.wait_for(
                    provider.complete(prompt, max_tokens=300, temperature=0.1),
                    timeout=float(settings.llm_attempt_timeout_seconds),
                )
                result = _extract_json(text)
                if not result:
                    raise ValueError("Provider returned no valid JSON object")
                self._rotation_manager.mark_success(cb)
                return result
            except Exception as error:
                self._rotation_manager.mark_failure(cb, is_quota=_is_quota_error(error))
                logger.warning("Provider %s failed to classify intent; rotating immediately: %s", provider.provider_name, error)

        return {"task_type": "query", "confidence": 0.5, "intent": user_input[:80],
                "parameters": {}, "requires_approval": False}

    async def complete_json(self, prompt: str, timeout: float = 60.0) -> dict:
        """Complete JSON, trying every healthy key/provider within one deadline."""
        if not self._rotation_manager.has_any_available:
            return await self._stub.complete_json(prompt)

        started = time.monotonic()
        attempted = 0
        attempts = list(self._rotation_manager.available_attempts())
        for position, (provider, cb) in enumerate(attempts):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            # Divide the remaining deadline fairly across every healthy
            # fallback rather than letting the first slow key consume the
            # whole browser step. The final fallback receives whatever is
            # left, while the per-key cap keeps a dead endpoint from stalling
            # the agent.
            remaining_attempts = len(attempts) - position
            fair_share = remaining / remaining_attempts
            per_attempt_timeout = min(
                float(settings.llm_attempt_timeout_seconds), remaining, fair_share
            )
            attempted += 1
            try:
                result = await asyncio.wait_for(
                    provider.complete_json(prompt), timeout=per_attempt_timeout
                )
                if not isinstance(result, dict) or not result:
                    raise ValueError("Provider returned no valid JSON object")
                self._rotation_manager.mark_success(cb)
                return result
            except Exception as error:
                is_quota = _is_quota_error(error)
                self._rotation_manager.mark_failure(cb, is_quota=is_quota)
                logger.warning(
                    "Provider %s failed on JSON attempt %s; rotating immediately: %s",
                    provider.provider_name, attempted, error,
                )

        return {}

    async def parse_action_plan_from_response(self, llm_response: str) -> Optional[dict]:
        """Extract and parse a ```action_plan``` JSON block from LLM response."""
        import re
        pattern = r"```action_plan\s*\n(.*?)\n```"
        match = re.search(pattern, llm_response, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse action_plan block: {e}")
            return None


# Singleton
llm_service = LLMService()


def strip_planner_artifacts(text: str) -> str:
    """Remove ```action_plan ... ``` and raw JSON blocks from LLM natural response."""
    import re
    if not text:
        return ""
    cleaned = re.sub(r"```(?:action_plan|json)?\s*\{.*?\n```", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"```action_plan.*?```", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()

