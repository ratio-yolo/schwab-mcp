"""Lightweight in-memory rate limiting middleware for Starlette.

Uses a sliding-window counter per client IP. Suitable for single-instance
deployments (Cloud Run max-instances=1). No external dependencies required.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


@dataclass
class _RateLimitRule:
    """Rate limit for a path prefix."""

    path_prefix: str
    max_requests: int
    window_seconds: float


@dataclass
class _ClientWindow:
    """Sliding window request timestamps for a single client+path."""

    timestamps: list[float] = field(default_factory=list)


# Default rules: path prefix → (max requests, window in seconds)
DEFAULT_RULES: list[_RateLimitRule] = [
    # OAuth registration: 10 per minute
    _RateLimitRule("/register", max_requests=10, window_seconds=60),
    # Token exchange: 20 per minute
    _RateLimitRule("/token", max_requests=20, window_seconds=60),
    # Consent/authorization: 20 per minute
    _RateLimitRule("/authorize", max_requests=20, window_seconds=60),
    _RateLimitRule("/consent", max_requests=20, window_seconds=60),
    # MCP endpoint: 120 per minute
    _RateLimitRule("/mcp", max_requests=120, window_seconds=60),
]

# Maximum number of tracked clients before evicting oldest
_MAX_TRACKED_CLIENTS = 1000


class RateLimitMiddleware:
    """ASGI middleware that enforces per-IP rate limits on configured paths."""

    def __init__(
        self,
        app: ASGIApp,
        rules: list[_RateLimitRule] | None = None,
    ) -> None:
        self.app = app
        self.rules = rules or DEFAULT_RULES
        # key: (client_ip, path_prefix) → sliding window
        self._windows: dict[tuple[str, str], _ClientWindow] = defaultdict(
            _ClientWindow
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"

        for rule in self.rules:
            if path.startswith(rule.path_prefix):
                if self._is_rate_limited(client_ip, rule):
                    logger.warning(
                        "Rate limited: client=%s, path=%s, limit=%d/%ds",
                        client_ip,
                        path,
                        rule.max_requests,
                        int(rule.window_seconds),
                    )
                    response = JSONResponse(
                        {"error": "Rate limit exceeded. Try again later."},
                        status_code=429,
                    )
                    await response(scope, receive, send)
                    return
                break

        await self.app(scope, receive, send)

    def _is_rate_limited(self, client_ip: str, rule: _RateLimitRule) -> bool:
        """Check and record a request. Returns True if rate limited."""
        now = time.monotonic()
        key = (client_ip, rule.path_prefix)
        window = self._windows[key]

        # Evict old timestamps outside the window
        cutoff = now - rule.window_seconds
        window.timestamps = [t for t in window.timestamps if t > cutoff]

        if len(window.timestamps) >= rule.max_requests:
            return True

        window.timestamps.append(now)

        # Periodic cleanup of stale clients
        if len(self._windows) > _MAX_TRACKED_CLIENTS:
            self._evict_stale_clients(now)

        return False

    def _evict_stale_clients(self, now: float) -> None:
        """Remove clients with no recent activity."""
        max_window = max(r.window_seconds for r in self.rules)
        cutoff = now - max_window * 2
        stale = [
            k
            for k, v in self._windows.items()
            if not v.timestamps or v.timestamps[-1] < cutoff
        ]
        for k in stale:
            del self._windows[k]
