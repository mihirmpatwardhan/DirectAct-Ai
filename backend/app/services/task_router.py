"""
Hybrid Task Intent Router — Phase 2
====================================
Classifies user input into task categories using:
  1. Fast Rule-Based Path  — regex + keyword matching for high-confidence, routine tasks
  2. LLM Reasoning Fallback — sends ambiguous tasks to Gemini for structured classification

Output drives the Execution Orchestrator to the correct engine:
  - "web"     → Playwright Web Automation Engine
  - "desktop" → PowerShell / WinAPI Local OS Engine
  - "query"   → Pure LLM response (no automation)
"""
import re
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)


class TaskType(str, Enum):
    WEB = "web"
    DESKTOP = "desktop"
    QUERY = "query"          # Pure information / no automation
    AMBIGUOUS = "ambiguous"  # Needs LLM reasoning


class RoutingMethod(str, Enum):
    RULE_BASED = "rule_based"    # Fast path — no LLM call needed
    LLM_CLASSIFIED = "llm"       # LLM reasoning was used


@dataclass
class RouterDecision:
    task_type: TaskType
    routing_method: RoutingMethod
    confidence: float              # 0.0 – 1.0
    requires_approval: bool        # True for sensitive / destructive actions
    extracted_intent: str          # Human-readable intent summary
    parameters: dict = field(default_factory=dict)   # Extracted params (url, app name, etc.)
    raw_input: str = ""


# ─────────────────────────────────────────────
# Rule Dictionaries
# ─────────────────────────────────────────────

# Web task patterns (generalized for ANY website/domain)
_WEB_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(open|go to|navigate to|browse|visit)\b.*(?:https?://|www\.|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,})", re.I),
    re.compile(r"\b(play|watch|listen to)\b.*\b(video|song|music|audio|playlist|track|episode|stream|channel)\b", re.I),
    re.compile(r"\b(search|find|look up)\b.*\b(web|internet|online|browser|page|site)\b", re.I),
    re.compile(r"\b(download|fetch)\b.*(from|at|on)\b.*(?:https?://|www\.|\.[a-zA-Z]{2,}|website|web)", re.I),
    re.compile(r"\b(fill|submit|enter|type)\b.*(form|field|input)\b.*(website|web|online|page)", re.I),
    re.compile(r"\b(screenshot|screengrab|capture)\b.*(webpage|website|browser)", re.I),
    re.compile(r"\bweb\b.*(automat|scrape|crawl|test)", re.I),
    # ── Booking / Ticketing ──────────────────────────────────────────────────
    re.compile(r"\b(book|reserve|purchase|buy|order|get)\b.*(ticket|seat|pass|entry|slot|appointment|table|room|hotel|flight|bus|train|cab|ride|trip|tour|cruise)", re.I),
    re.compile(r"\b(book|reserve|schedule)\b.*(movie|film|show|concert|event|match|game)", re.I),
    re.compile(r"\b(cancel|reschedule|change)\b.*(booking|reservation|ticket|order|appointment)", re.I),
    # ── Trip / Travel Planning ───────────────────────────────────────────────
    re.compile(r"\b(plan|arrange|organise|organize)\b.*(trip|travel|vacation|holiday|journey|tour|itinerary)", re.I),
    re.compile(r"\b(find|search|compare|check)\b.*(flight|bus|train|hotel|stay|hostel|resort|cab|taxi)", re.I),
    re.compile(r"\b(cheapest|best|lowest fare|price)\b.*(flight|bus|train|hotel|ticket)", re.I),
    # ── Form Filling / Registration ──────────────────────────────────────────
    re.compile(r"\b(fill|complete|submit|send)\b.*(form|application|registration|signup|checkout|details|information)", re.I),
    re.compile(r"\b(register|sign up|enroll|apply|login|log in)\b.*(website|app|portal|site|online|platform)", re.I),
    re.compile(r"\b(checkout|place order|add to cart|buy now)\b", re.I),
    # ── Generic commerce / delivery actions ──────────────────────────────────
    re.compile(r"\b(order|get|buy)\b.*(food|pizza|burger|biryani|meal|dinner|lunch|breakfast|coffee|delivery)", re.I),
    # ── Generic online actions ───────────────────────────────────────────────
    re.compile(r"\b(track|check status|view status)\b.*(order|shipment|delivery|parcel|package|booking|pnr)", re.I),
    re.compile(r"\b(recharge|top.?up|pay bill|pay the)\b.*(mobile|phone|dth|electricity|gas|water|broadband)", re.I),
]

_WEB_KEYWORDS = [
    "website", "webpage", "browser", "url", "http", "https", "www",
    "online", "internet", "scrape", "crawl", "download from", "web form", "search engine",
    # booking/commerce
    "book", "reserve", "checkout", "cart", "wishlist", "coupon", "promo code",
    "ticket", "reservation", "itinerary", "travel", "hotel", "flight",
]

# Desktop task patterns (generalized for ANY desktop application)
_DESKTOP_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(open|launch|start|run)\b(?!.*\b(browser|website|webpage|url|http|https)\b).+", re.I),
    re.compile(r"\b(create|make|new)\b.*(file|folder|directory|document|spreadsheet|workspace)", re.I),
    re.compile(r"\b(delete|remove|move|copy|rename)\b.*(file|folder|directory)", re.I),
    re.compile(r"\b(install|uninstall|setup)\b.*(software|app|program|application|package)", re.I),
    re.compile(r"\b(type|write|enter)\b.*(in|into|on)\b.+", re.I),
    re.compile(r"\b(take|capture)\b.*(screenshot|screen ?shot)\b(?!.*(web|browser))", re.I),
    re.compile(r"\bpowershell\b|\bcmd\b|\bbatch\b|\bscript\b|\bterminal\b", re.I),
    re.compile(r"\b(press|click|type)\b.*(key|button|shortcut)", re.I),
]

_DESKTOP_KEYWORDS = [
    "desktop", "taskbar", "start menu", "file explorer", "powershell",
    "command prompt", "cmd", "registry", "control panel", "task manager",
    "application", "program", "software", "install", "uninstall", "folder", "directory",
    "local file", "my computer", "c drive", "windows", "clipboard", "right click",
]

# Purely destructive / high-risk patterns — always require approval
_DESTRUCTIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(delete|remove|rm|format|wipe|erase|destroy)\b.*(all|everything|system|windows|c:)", re.I),
    re.compile(r"\bformat.*(drive|disk|volume|c:)", re.I),
    re.compile(r"\b(shutdown|restart|reboot)\b", re.I),
    re.compile(r"\b(uninstall|remove)\b.*(windows|system|driver)", re.I),
    re.compile(r"rm\s+-rf|del\s+/s|rd\s+/s", re.I),
]

# Pure query patterns (no automation needed) — includes casual / conversational phrases
_QUERY_KEYWORDS = [
    # Informational
    "what is", "what are", "explain", "tell me", "describe", "how does",
    "why does", "when did", "who is", "define", "difference between",
    "pros and cons", "compare", "summarize", "write a", "generate",
    "calculate", "convert", "translate", "help me understand",
    "give me", "can you", "could you", "how do i", "how to",
    # Casual / social
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "good night", "thanks", "thank you", "cheers", "bye", "goodbye",
    "how are you", "how's it going", "what's up", "sup", "yo",
    "nice", "cool", "great", "awesome", "okay", "ok", "sure",
    "who are you", "what can you do", "your name", "tell me about yourself",
    "introduce yourself", "help", "i need help", "i want to know",
    "interesting", "really", "sounds good", "got it", "understood",
]


class HybridTaskRouter:
    """
    Routes user input to the correct execution pipeline.

    Fast-path: Regex + keyword matching for common tasks (no LLM call, <5ms).
    LLM-path:  Sends ambiguous inputs to the LLM for structured classification.
    """

    def classify(self, user_input: str, target_engine: Optional[str] = None) -> RouterDecision:
        """
        Synchronously classify user input. Fast-path only (<5ms).
        Supports target_engine override ('web', 'desktop', 'auto').
        """
        text = user_input.strip()
        lower = text.lower()
        requires_approval = self._is_destructive(text)

        if target_engine == "web":
            params = self._extract_parameters(text, TaskType.WEB)
            return RouterDecision(
                task_type=TaskType.WEB,
                routing_method=RoutingMethod.RULE_BASED,
                confidence=1.0,
                requires_approval=requires_approval,
                extracted_intent=f"Web Automation: {text}",
                parameters=params,
                raw_input=text,
            )
        elif target_engine == "desktop":
            params = self._extract_parameters(text, TaskType.DESKTOP)
            return RouterDecision(
                task_type=TaskType.DESKTOP,
                routing_method=RoutingMethod.RULE_BASED,
                confidence=1.0,
                requires_approval=requires_approval,
                extracted_intent=f"Desktop Automation: {text}",
                parameters=params,
                raw_input=text,
            )

        web_score = self._score_web(lower)
        desktop_score = self._score_desktop(lower)
        query_score = self._score_query(lower)

        # Web / browser / online cues prioritize web engine
        if self._has_web_search_intent(lower) or any(k in lower for k in ["youtube", "google", "browser", "chrome", "web", "site", "online", "url", "http", ".com", ".in"]):
            web_score = max(web_score, 0.90)

        # Polite request wrappers must not turn an automation request into a chat query
        if re.search(
            r"\b(can you|could you|please|i want you to|help me)\b.*\b(open|launch|start|run|click|type|fill|send|upload|download|create|edit|close)\b",
            lower,
        ):
            query_score = 0.0

        logger.debug(
            f"Router scores — web={web_score:.2f}, desktop={desktop_score:.2f}, query={query_score:.2f}"
        )

        # Determine winner
        scores = {"web": web_score, "desktop": desktop_score, "query": query_score}
        best = max(scores, key=scores.get)
        best_score = scores[best]

        if best_score < 0.35:
            # Low confidence — flag for LLM
            return RouterDecision(
                task_type=TaskType.AMBIGUOUS,
                routing_method=RoutingMethod.RULE_BASED,
                confidence=best_score,
                requires_approval=requires_approval,
                extracted_intent=f"Ambiguous input — LLM classification needed",
                raw_input=text,
            )

        task_type_map = {
            "web": TaskType.WEB,
            "desktop": TaskType.DESKTOP,
            "query": TaskType.QUERY,
        }
        task_type = task_type_map[best]
        parameters = self._extract_parameters(text, task_type)

        intent = self._build_intent_summary(text, task_type, parameters)

        return RouterDecision(
            task_type=task_type,
            routing_method=RoutingMethod.RULE_BASED,
            confidence=best_score,
            requires_approval=requires_approval,
            extracted_intent=intent,
            parameters=parameters,
            raw_input=text,
        )

    def _has_web_search_intent(self, lower: str) -> bool:
        return bool(re.search(r"\b(search|seach|find|play|playlist|browse|look)\b", lower))

    def _score_web(self, lower: str) -> float:
        score = 0.0
        # Instant fast-path check for search, booking, travel, and domain terms
        if any(lower.startswith(prefix) for prefix in [
            "search ", "go to ", "find ", "look up ", "check ", "book ", "plan ", "reserve ", "order ", "fill ", "buy ", "schedule "
        ]):
            score = max(score, 0.85)
        if re.search(r"\b(open|go to|navigate)\b.*\b(search|seach|find|play|watch|browse|look up)\b", lower):
            score = max(score, 0.90)
        if re.search(r"\b[a-z0-9-]+\.(?:com|org|net|io|in|co|ai|uk)\b|https?://|\bwww\.", lower):
            score = max(score, 0.90)

        for pattern in _WEB_PATTERNS:
            if pattern.search(lower):
                score = max(score, 0.85)
        for kw in _WEB_KEYWORDS:
            if kw in lower:
                score = max(score, 0.60)
        return min(score, 1.0)

    def _score_desktop(self, lower: str) -> float:
        score = 0.0
        if re.search(r"\b(desktop automation|desktop mode|via desktop|select desktop|use desktop|using desktop|desktop agent)\b", lower):
            return 1.0
        for pattern in _DESKTOP_PATTERNS:
            if pattern.search(lower):
                score = max(score, 0.85)
        for kw in _DESKTOP_KEYWORDS:
            if kw in lower:
                score = max(score, 0.60)
        return min(score, 1.0)

    def _score_query(self, lower: str) -> float:
        score = 0.0
        stripped = lower.strip().rstrip('!?.,')
        for kw in _QUERY_KEYWORDS:
            # Match at start, as whole token in sentence, or if the entire stripped input IS the keyword
            if stripped == kw or lower.startswith(kw) or f" {kw} " in lower or lower.endswith(f" {kw}"):
                score = max(score, 0.85)
        # Short inputs with no automation cues are almost always casual chat
        if len(stripped.split()) <= 4 and score == 0.0:
            score = 0.70  # treat short ambiguous inputs as query rather than automation
        return min(score, 1.0)

    def _is_destructive(self, text: str) -> bool:
        return any(p.search(text) for p in _DESTRUCTIVE_PATTERNS)

    def _extract_parameters(self, text: str, task_type: TaskType) -> dict:
        params: dict = {"raw_task": text}
        if task_type == TaskType.WEB:
            url_match = re.search(r"https?://\S+|www\.\S+|\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?\b", text, re.I)
            if url_match:
                url = url_match.group()
                if not url.startswith("http"):
                    url = "https://" + url
                params["url"] = url

            search_match = re.search(
                r"(?:search|seach|find|look up|play|watch|listen to)\s+(?:for\s+)?['\"]?(.+?)['\"]?(?:\s+on\b|\s+in\b|$)",
                text, re.I
            )
            if search_match:
                params["query"] = search_match.group(1).strip()

            # Keep only an explicit host supplied by the user. Choosing a
            # destination from a product catalog made the router brittle and
            # silently sent unrelated tasks to a guessed website. The web
            # agent can decide the next navigation from the live page state.
            if params.get("url"):
                params["target_site"] = re.sub(
                    r"^https?://(?:www\.)?", "", params["url"], flags=re.I
                ).split("/", 1)[0]

        elif task_type == TaskType.DESKTOP:
            app_match = re.search(
                r"(?:open|launch|start|run)\s+([a-zA-Z0-9 _\-]+?)(?:\s+and|\s+then|$)",
                text, re.I
            )
            if app_match:
                params["app_name"] = app_match.group(1).strip().removesuffix(" app").strip()

        return params

    def _build_intent_summary(self, text: str, task_type: TaskType, params: dict) -> str:
        if task_type == TaskType.WEB:
            if "url" in params:
                return f"Navigate browser to {params['url']}"
            if "query" in params and "target_site" in params:
                return f"Search '{params['query']}' on {params['target_site']}"
            if "query" in params:
                return f"Web search: {params['query']}"
            return f"Web automation task: {text[:80]}"
        elif task_type == TaskType.DESKTOP:
            if "app_name" in params:
                return f"Launch desktop application: {params['app_name']}"
            return f"Local OS task: {text[:80]}"
        else:
            return f"Information query: {text[:80]}"

    async def classify_with_llm(self, user_input: str) -> RouterDecision:
        """
        Asynchronously classify user input using Gemini/LLM fallback.
        Called when rule-based classification yields AMBIGUOUS confidence (< 0.35).
        """
        from app.services.llm_service import llm_service

        # First attempt fast rule-based path
        decision = self.classify(user_input)
        if decision.task_type != TaskType.AMBIGUOUS:
            return decision

        logger.info(f"Router: Fast-path ambiguous for '{user_input[:50]}...' -> invoking LLM fallback")
        result = await llm_service.classify_intent(user_input)

        raw_type = result.get("task_type", "query").lower()
        task_type_map = {
            "web": TaskType.WEB,
            "desktop": TaskType.DESKTOP,
            "query": TaskType.QUERY,
        }
        task_type = task_type_map.get(raw_type, TaskType.QUERY)
        confidence = float(result.get("confidence", 0.8))
        requires_approval = bool(result.get("requires_approval", False)) or self._is_destructive(user_input)
        intent = result.get("intent", f"LLM Classified: {user_input[:60]}")
        # LLM classification supplies semantics; deterministic extraction only
        # fills missing transport data (URL/app/query) and never selects a
        # website or application from a hardcoded catalog.
        extracted = self._extract_parameters(user_input, task_type)
        params = {**extracted, **(result.get("parameters") or {})}
        params = {key: value for key, value in params.items() if value not in (None, "")}

        return RouterDecision(
            task_type=task_type,
            routing_method=RoutingMethod.LLM_CLASSIFIED,
            confidence=confidence,
            requires_approval=requires_approval,
            extracted_intent=intent,
            parameters=params,
            raw_input=user_input,
        )


# Singleton
task_router = HybridTaskRouter()
