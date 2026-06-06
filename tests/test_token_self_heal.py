"""Tests for self-healing Schwab client reload after token re-auth.

When the admin service writes a fresh token to Postgres (e.g. after
``schwab-auth.sh``), the running MCP server must rebuild its Schwab client so
it uses the new token without a restart. These tests cover the two moving
pieces: the context client-swap and the poll loop that triggers a rebuild.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from schwab.client import AsyncClient

from schwab_mcp.context import SchwabServerContext
from schwab_mcp.db import NoOpDatabaseManager
from schwab_mcp.remote import app as remote_app


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_context(client: Any) -> SchwabServerContext:
    return SchwabServerContext(
        client=cast(AsyncClient, client),
        approval_manager=cast(Any, object()),
        db=NoOpDatabaseManager(),
    )


class TestSetClient:
    def test_set_client_swaps_client_and_facades(self) -> None:
        first = object()
        ctx = _make_context(first)

        assert ctx.client is first
        assert ctx.tools is first
        assert ctx.options is first

        second = object()
        ctx.set_client(cast(AsyncClient, second))

        assert ctx.client is second
        # All typed facades must point at the new client, not the old one.
        assert ctx.tools is second
        assert ctx.accounts is second
        assert ctx.price_history is second
        assert ctx.options is second
        assert ctx.orders is second
        assert ctx.quotes is second
        assert ctx.transactions is second


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def close_async_session(self) -> None:
        self.closed = True


class TestRebuildClient:
    def test_rebuild_swaps_in_new_client_and_closes_old(self, monkeypatch) -> None:
        old_client = _FakeClient()
        new_client = _FakeClient()
        ctx = _make_context(old_client)

        monkeypatch.setattr(
            remote_app, "_create_schwab_client", lambda config, storage: new_client
        )

        run(remote_app._rebuild_client(cast(Any, object()), cast(Any, object()), ctx))

        assert ctx.client is new_client
        assert ctx.tools is new_client
        assert old_client.closed is True

    def test_rebuild_tolerates_close_failure(self, monkeypatch) -> None:
        class _BadCloseClient:
            async def close_async_session(self) -> None:
                raise RuntimeError("boom")

        old_client = _BadCloseClient()
        new_client = _FakeClient()
        ctx = _make_context(old_client)

        monkeypatch.setattr(
            remote_app, "_create_schwab_client", lambda config, storage: new_client
        )

        # Should not raise despite the old client's close failing.
        run(remote_app._rebuild_client(cast(Any, object()), cast(Any, object()), ctx))

        assert ctx.client is new_client


class _FakeStorage:
    """Minimal poll-able storage: reports `updates` pending poll results."""

    def __init__(self, updates: list[bool]) -> None:
        self._updates = updates
        self.poll_calls = 0

    async def poll_for_updates(self) -> bool:
        self.poll_calls += 1
        if self._updates:
            return self._updates.pop(0)
        return False


class TestTokenPollLoop:
    def test_rebuild_invoked_only_when_update_detected(self, monkeypatch) -> None:
        # First poll: no change. Second poll: a newer token -> rebuild.
        storage = _FakeStorage(updates=[False, True])
        refreshed: list[int] = []

        async def on_refreshed() -> None:
            refreshed.append(1)

        # Make sleep a no-op and stop the loop after the second poll.
        sleep_calls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 2 and not storage._updates:
                # Let the second iteration run, then cancel on the next sleep.
                raise asyncio.CancelledError

        monkeypatch.setattr(remote_app.asyncio, "sleep", fake_sleep)

        try:
            run(remote_app._token_poll_loop(cast(Any, storage), on_refreshed))
        except asyncio.CancelledError:
            pass

        assert storage.poll_calls == 2
        assert refreshed == [1]  # rebuild fired exactly once, on the update

    def test_poll_errors_do_not_break_loop(self, monkeypatch) -> None:
        class _BrokenStorage:
            def __init__(self) -> None:
                self.calls = 0

            async def poll_for_updates(self) -> bool:
                self.calls += 1
                raise RuntimeError("db down")

        storage = _BrokenStorage()
        refreshed: list[int] = []

        async def on_refreshed() -> None:
            refreshed.append(1)

        sleep_calls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(remote_app.asyncio, "sleep", fake_sleep)

        try:
            run(remote_app._token_poll_loop(cast(Any, storage), on_refreshed))
        except asyncio.CancelledError:
            pass

        # The loop kept running despite the poll raising, and never rebuilt.
        assert storage.calls >= 1
        assert refreshed == []
