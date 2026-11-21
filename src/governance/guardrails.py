import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class RateLimitExceeded(Exception):
    """Raised when invocation rate exceeds the configured window limit."""


@dataclass
class RateLimiter:
    max_calls: int = 10
    window_seconds: float = 60.0
    _timestamps: dict[str, deque[float]] = field(default_factory=dict)

    def check_and_record(self, key: str = "default") -> None:
        now = time.monotonic()
        bucket = self._timestamps.setdefault(key, deque())
        while bucket and (now - bucket[0]) > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.max_calls:
            raise RateLimitExceeded(
                f"Rate limit exceeded ({self.max_calls} calls per {self.window_seconds:.0f}s)."
            )
        bucket.append(now)


@dataclass
class ResponseCache:
    ttl_seconds: float = 300.0
    _store: dict[str, tuple[float, Any]] = field(default_factory=dict)

    @staticmethod
    def make_key(payload: dict[str, Any]) -> str:
        raw = repr(sorted(payload.items())).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self.ttl_seconds, value)


def prepare_hitl_ticket(draft: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(draft or {})
    base.setdefault("title", "Remediate primary service fault and add regression guard")
    base.setdefault("owner_role", "SRE-OnCall")
    base.setdefault("priority", "P1")
    base.setdefault("due_days", 14)
    base["requires_human_confirmation"] = True
    base.setdefault("approved", False)
    base["status"] = "APPROVED_FOR_EXPORT" if base.get("approved") else "DRAFT"
    return base


def export_ticket_if_approved(ticket: dict[str, Any], human_approved: bool) -> dict[str, Any]:
    gated = prepare_hitl_ticket(ticket)
    if not human_approved:
        return {
            "exported": False,
            "reason": "Human-in-the-loop confirmation required before exporting operational ticket.",
            "ticket": gated,
        }
    gated["approved"] = True
    gated["status"] = "APPROVED_FOR_EXPORT"
    return {
        "exported": True,
        "reason": "Explicitly approved by human operator.",
        "ticket": gated,
    }
