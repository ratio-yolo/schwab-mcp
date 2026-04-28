from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Any, Sequence

import pytest

from schwab_mcp.db import DatabaseManager
from schwab_mcp.remote.token_storage import PostgresTokenStorage


_FAKE_TS = datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc)


class FakeDatabaseManager(DatabaseManager):
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.timestamps: dict[str, datetime.datetime] = {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def execute(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[tuple[Any, ...]]:
        self.executed.append((sql, tuple(params)))
        stripped = sql.strip()
        key = params[0] if params else "default"

        if stripped.startswith("SELECT token_data"):
            if key in self.rows:
                ts = self.timestamps.get(key, _FAKE_TS)
                return [(self.rows[key], ts)]
            return []
        if stripped.startswith("SELECT updated_at"):
            if key in self.rows:
                ts = self.timestamps.get(key, _FAKE_TS)
                return [(ts,)]
            return []
        if stripped.startswith("SELECT 1"):
            if key in self.rows:
                return [(1,)]
            return []
        if stripped.startswith("INSERT"):
            key = params[0]
            self.rows[key] = json.loads(params[1])
            self.timestamps[key] = datetime.datetime.now(datetime.timezone.utc)
        return []

    async def execute_many(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        pass


def run(coro: Any) -> Any:
    return asyncio.run(coro)


SAMPLE_TOKEN: dict[str, Any] = {
    "access_token": "test_access",
    "refresh_token": "test_refresh",
    "token_type": "Bearer",
    "expires_in": 1800,
}


class TestPostgresTokenStorage:
    def test_ensure_table_executes_ddl(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        run(storage.ensure_table())

        assert len(db.executed) == 1
        sql = db.executed[0][0]
        assert "CREATE TABLE" in sql
        assert "schwab_tokens" in sql

    def test_load_sync_returns_cached_token(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)
        storage._cached_token = SAMPLE_TOKEN

        result = storage.load()

        assert result == SAMPLE_TOKEN

    def test_load_sync_raises_when_no_cache(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        with pytest.raises(FileNotFoundError):
            storage.load()

    def test_load_async_returns_from_db(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = SAMPLE_TOKEN
        storage = PostgresTokenStorage(db)

        result = run(storage.load_async())

        assert result == SAMPLE_TOKEN
        assert storage._cached_token == SAMPLE_TOKEN

    def test_load_async_returns_cached_when_fresh(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = {"access_token": "from_db"}
        storage = PostgresTokenStorage(db)
        storage._cached_token = SAMPLE_TOKEN
        storage._cache_time = time.time()

        result = run(storage.load_async())

        assert result == SAMPLE_TOKEN
        assert len(db.executed) == 0

    def test_load_async_refreshes_stale_cache(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = {"access_token": "fresh_from_db"}
        storage = PostgresTokenStorage(db, cache_ttl=10)
        storage._cached_token = SAMPLE_TOKEN
        storage._cache_time = time.time() - 20

        result = run(storage.load_async())

        assert result == {"access_token": "fresh_from_db"}
        assert storage._cached_token == {"access_token": "fresh_from_db"}

    def test_load_async_raises_when_not_in_db(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        with pytest.raises(FileNotFoundError, match="not found"):
            run(storage.load_async())

    def test_load_async_parses_string_json(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = json.dumps(SAMPLE_TOKEN)  # type: ignore[assignment]
        storage = PostgresTokenStorage(db)

        result = run(storage.load_async())

        assert result == SAMPLE_TOKEN

    def test_load_async_raises_on_non_dict(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = json.dumps([1, 2, 3])  # type: ignore[assignment]
        storage = PostgresTokenStorage(db)

        with pytest.raises(ValueError, match="not a valid dictionary"):
            run(storage.load_async())

    def test_write_async_upserts_to_db(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        run(storage.write_async(SAMPLE_TOKEN))

        insert_calls = [(sql, params) for sql, params in db.executed if "INSERT" in sql]
        assert len(insert_calls) == 1
        assert "ON CONFLICT" in insert_calls[0][0]
        assert storage._cached_token == SAMPLE_TOKEN
        assert db.rows["default"] == SAMPLE_TOKEN

    def test_write_async_skips_empty_token(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        run(storage.write_async({}))

        assert len(db.executed) == 0
        assert storage._cached_token is None

    def test_write_sync_updates_cache(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        storage.write(SAMPLE_TOKEN)

        assert storage._cached_token == SAMPLE_TOKEN

    def test_write_sync_skips_empty_token(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        storage.write({})

        assert storage._cached_token is None

    def test_exists_sync_checks_cache(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        assert storage.exists() is False

        storage._cached_token = SAMPLE_TOKEN

        assert storage.exists() is True

    def test_exists_async_queries_db(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        assert run(storage.exists_async()) is False

        db.rows["default"] = SAMPLE_TOKEN

        assert run(storage.exists_async()) is True

    def test_invalidate_cache_clears_state(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = {"access_token": "from_db"}
        storage = PostgresTokenStorage(db)

        run(storage.load_async())
        assert storage._cached_token is not None

        storage.invalidate_cache()

        assert storage._cached_token is None
        assert storage._cache_time == 0.0

        result = run(storage.load_async())
        assert result == {"access_token": "from_db"}
        select_calls = [
            (sql, params) for sql, params in db.executed if "SELECT token_data" in sql
        ]
        assert len(select_calls) == 2

    def test_custom_key(self) -> None:
        db = FakeDatabaseManager()
        db.rows["user-42"] = SAMPLE_TOKEN
        storage = PostgresTokenStorage(db, key="user-42")

        result = run(storage.load_async())

        assert result == SAMPLE_TOKEN
        select_calls = [(sql, params) for sql, params in db.executed if "SELECT" in sql]
        assert select_calls[0][1] == ("user-42",)

    def test_load_from_db_captures_updated_at(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = SAMPLE_TOKEN
        ts = datetime.datetime(2025, 3, 15, tzinfo=datetime.timezone.utc)
        db.timestamps["default"] = ts
        storage = PostgresTokenStorage(db)

        run(storage.load_async())

        assert storage._db_updated_at == ts


class TestPollForUpdates:
    def test_returns_true_and_reloads_when_db_is_newer(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = SAMPLE_TOKEN
        newer_ts = datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc)
        db.timestamps["default"] = newer_ts
        storage = PostgresTokenStorage(db)
        # _db_updated_at starts at _EPOCH (year 2000), so newer_ts is definitely newer

        result = run(storage.poll_for_updates())

        assert result is True
        assert storage._cached_token == SAMPLE_TOKEN
        assert storage._db_updated_at == newer_ts

    def test_returns_false_when_db_timestamp_unchanged(self) -> None:
        db = FakeDatabaseManager()
        db.rows["default"] = SAMPLE_TOKEN
        ts = datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc)
        db.timestamps["default"] = ts
        storage = PostgresTokenStorage(db)
        storage._db_updated_at = ts  # same as DB → no update needed

        result = run(storage.poll_for_updates())

        assert result is False
        assert storage._cached_token is None  # no reload occurred

    def test_returns_false_when_no_token_in_db(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        result = run(storage.poll_for_updates())

        assert result is False

    def test_returns_false_and_logs_on_db_error(self) -> None:
        class BrokenDB(FakeDatabaseManager):
            async def execute(
                self, sql: str, params: Sequence[Any] = ()
            ) -> list[tuple[Any, ...]]:
                if "SELECT updated_at" in sql:
                    raise RuntimeError("connection lost")
                return await super().execute(sql, params)

        db = BrokenDB()
        storage = PostgresTokenStorage(db)

        result = run(storage.poll_for_updates())

        assert result is False

    def test_write_async_sets_db_updated_at(self) -> None:
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        before = datetime.datetime.now(datetime.timezone.utc)
        run(storage.write_async(SAMPLE_TOKEN))
        after = datetime.datetime.now(datetime.timezone.utc)

        assert before <= storage._db_updated_at <= after

    def test_poll_after_write_back_does_not_reload(self) -> None:
        """Normal access-token write-back should not trigger a poll reload."""
        db = FakeDatabaseManager()
        storage = PostgresTokenStorage(db)

        # Simulate schwab-py writing back a refreshed access token
        run(storage.write_async(SAMPLE_TOKEN))
        # The DB timestamp and _db_updated_at are now ~equal

        result = run(storage.poll_for_updates())

        # No reload: DB updated_at should not be newer than _db_updated_at
        assert result is False
