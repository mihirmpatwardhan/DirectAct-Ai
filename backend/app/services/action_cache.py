"""
Action Sequence Cache — Phase 4
=================================
Caches successful action sequences for routine/repeat tasks.
Bypasses the LLM for cached tasks, reducing latency and API costs.

Research value:
  - Demonstrates measurable latency reduction (target: <50ms cached vs ~1-3s LLM)
  - Reduces API costs to $0 for cached routine tasks
  - Provides deterministic, auditable execution for compliance

Cache key = normalized intent fingerprint (not raw string — handles minor variations)
"""
import json
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# Cache TTL: 24 hours (routine tasks — weather, music, etc.)
CACHE_TTL_HOURS = 24
MAX_CACHE_SIZE = 500  # Max number of cached sequences


@dataclass
class CachedActionStep:
    """A single step in a cached action sequence."""
    action_type: str        # "navigate", "click", "type", "powershell", etc.
    target: str             # URL, element selector, or command
    value: Optional[str]    # For type actions
    description: str        # Human-readable description


@dataclass
class CachedSequence:
    """A complete cached action sequence."""
    cache_key: str
    original_intent: str
    steps: List[CachedActionStep] = field(default_factory=list)
    task_type: str = "web"
    hit_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_used_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    avg_execution_ms: float = 0.0


class ActionSequenceCache:
    """
    In-memory LRU-ish cache for action sequences.
    In production this would be backed by Redis.
    """

    def __init__(self):
        self._cache: dict[str, CachedSequence] = {}
        # Cache only plans produced and verified by the live agent. Never ship
        # site/app-specific canned sequences: they become stale and can claim
        # success without observing the current UI.

    def _normalize_intent(self, intent: str) -> str:
        """
        Normalize an intent string to a stable cache key.
        Handles minor variations of an otherwise identical requested action.
        """
        lower = intent.strip().lower()

        # Normalize synonyms
        synonyms = {
            r"\b(open|launch|start|go to|navigate to|visit)\b": "open",
            r"\b(search|find|look up|google)\b": "search",
            r"\b(play|listen to|put on)\b": "play",
        }
        import re
        for pattern, replacement in synonyms.items():
            lower = re.sub(pattern, replacement, lower)

        # Strip filler words
        filler = ["please", "can you", "could you", "i want to", "i need to", "for me"]
        for f in filler:
            lower = lower.replace(f, "").strip()

        # Remove extra spaces
        lower = " ".join(lower.split())

        # Hash for fixed-length key
        return hashlib.md5(lower.encode()).hexdigest()

    def get(self, intent: str) -> Optional[CachedSequence]:
        """Look up a cached sequence by intent. Returns None on cache miss."""
        key = self._normalize_intent(intent)
        seq = self._cache.get(key)

        if seq is None:
            logger.debug(f"Cache MISS: '{intent[:60]}'")
            return None

        # Check TTL
        created = datetime.fromisoformat(seq.created_at)
        if datetime.utcnow() - created > timedelta(hours=CACHE_TTL_HOURS):
            logger.info(f"Cache EXPIRED: '{intent[:60]}'")
            del self._cache[key]
            return None

        # Update hit stats
        seq.hit_count += 1
        seq.last_used_at = datetime.utcnow().isoformat()
        logger.info(
            f"Cache HIT: '{intent[:60]}' (hits={seq.hit_count}, "
            f"steps={len(seq.steps)})"
        )
        return seq

    def store(self, intent: str, steps: List[CachedActionStep], task_type: str = "web", execution_ms: float = 0) -> str:
        """Store a new sequence in the cache. Returns the cache key."""
        if len(self._cache) >= MAX_CACHE_SIZE:
            self._evict_oldest()

        key = self._normalize_intent(intent)
        seq = CachedSequence(
            cache_key=key,
            original_intent=intent,
            steps=steps,
            task_type=task_type,
            avg_execution_ms=execution_ms,
        )
        self._cache[key] = seq
        logger.info(f"Cache STORED: '{intent[:60]}' ({len(steps)} steps)")
        return key

    def update_execution_time(self, intent: str, execution_ms: float):
        """Update the rolling average execution time for analytics."""
        key = self._normalize_intent(intent)
        if key in self._cache:
            seq = self._cache[key]
            # Exponential moving average
            alpha = 0.3
            seq.avg_execution_ms = alpha * execution_ms + (1 - alpha) * seq.avg_execution_ms

    def invalidate(self, intent: str):
        """Remove a specific sequence from cache."""
        key = self._normalize_intent(intent)
        if key in self._cache:
            del self._cache[key]
            logger.info(f"Cache INVALIDATED: '{intent[:60]}'")

    def stats(self) -> dict:
        """Return cache performance statistics."""
        total_hits = sum(s.hit_count for s in self._cache.values())
        return {
            "total_sequences": len(self._cache),
            "total_hits": total_hits,
            "most_used": sorted(
                [{"intent": s.original_intent, "hits": s.hit_count}
                 for s in self._cache.values()],
                key=lambda x: x["hits"],
                reverse=True,
            )[:5],
        }

    def _evict_oldest(self):
        """Remove the least recently used entry."""
        if not self._cache:
            return
        oldest_key = min(self._cache, key=lambda k: self._cache[k].last_used_at)
        del self._cache[oldest_key]

# Singleton
action_cache = ActionSequenceCache()
