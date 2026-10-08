"""Regression coverage for fast remote-database startup."""
from pathlib import Path
import sys
from contextlib import contextmanager


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sourcing import store


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Raw:
    autocommit = True


class _FakeConnection:
    def __init__(self, ready):
        self.ready = ready
        self.queries = []
        self.raw = _Raw()

    def execute(self, query, args=()):
        self.queries.append((query, args))
        if query.startswith("SELECT "):
            return _Cursor({"ready": self.ready})
        return _Cursor()


def test_prepare_postgres_skips_schema_replay_when_database_is_current(monkeypatch):
    connection = _FakeConnection(True)
    monkeypatch.setattr(store, "_POSTGRES_SCHEMA_READY", False)
    monkeypatch.setattr(store.config, "DATABASE_SCHEMA", "medhunt")

    store._prepare_postgres(connection)

    assert store._POSTGRES_SCHEMA_READY is True
    assert len(connection.queries) == 1
    query = connection.queries[0][0]
    assert "to_regclass('medhunt.candidates')" in query
    assert "table_schema = 'medhunt'" in query
    assert "table_name = 'enrichment_events'" in query
    assert "column_name = 'candidate_id'" in query
    assert "column_name = 'contact_expires_at'" in query
    assert "to_regclass('medhunt.idx_nexus_deliveries_ready')" in query
    assert "to_regclass('medhunt.watcher_email_deliveries')" in query


def test_postgres_namespace_is_created_and_selected(monkeypatch):
    connection = _FakeConnection(True)
    monkeypatch.setattr(store.config, "DATABASE_SCHEMA", "medhunt")

    store._configure_postgres_namespace(connection)
    store._activate_postgres_namespace(connection)

    assert connection.queries == [
        ('CREATE SCHEMA IF NOT EXISTS "medhunt"', ()),
        ('SET LOCAL search_path TO "medhunt"', ()),
    ]


def test_prepare_postgres_runs_migrations_when_schema_is_incomplete(monkeypatch):
    connection = _FakeConnection(False)
    monkeypatch.setattr(store, "_POSTGRES_SCHEMA_READY", False)

    store._prepare_postgres(connection)

    assert store._POSTGRES_SCHEMA_READY is True
    assert len(connection.queries) == 1 + len(store._POSTGRES_SCHEMA)
    assert connection.queries[1][0] == store._POSTGRES_SCHEMA[0]


def test_legacy_candidate_columns_are_repaired_before_indexes():
    indexed_tables = {
        "enrichment_events": "idx_enrichment_events_candidate",
        "talent_pool_members": "idx_pool_members_candidate",
        "campaign_members": "idx_campaign_members_candidate",
        "resumes": "idx_resumes_candidate",
        "resume_extractions": "idx_resume_extractions_candidate",
    }
    candidate_tables = {
        "enrichment_events", "outreach", "talent_pool_members",
        "campaign_members", "resumes", "resume_extractions",
        "provider_lookups", "lookup_run_items", "nexus_candidate_links",
        "resume_capture_locks", "nexus_deliveries",
    }

    for table in candidate_tables:
        migration = (
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS candidate_id BIGINT"
        )
        migration_index = store._POSTGRES_SCHEMA.index(migration)
        assert "candidate_id" in store._POSTGRES_REQUIRED_COLUMNS[table]
        if table in indexed_tables:
            index_creation_index = next(
                index for index, statement in enumerate(store._POSTGRES_SCHEMA)
                if indexed_tables[table] in statement
            )
            assert migration_index < index_creation_index


def test_nexus_startup_migration_selects_only_legacy_identity_keys(monkeypatch):
    class _RowsConnection:
        def __init__(self):
            self.query = ""

        def execute(self, query, args=()):
            self.query = query
            return type("Rows", (), {"fetchall": lambda _self: []})()

    connection = _RowsConnection()
    rekeyed = []
    monkeypatch.setattr(
        store, "_rekey_nexus_identity",
        lambda _connection, candidate_id: rekeyed.append(candidate_id),
    )

    store._migrate_all_nexus_identity_keys(connection)

    assert rekeyed == []
    assert "d.identity_key <>" in connection.query
    assert "l.identity_key <>" in connection.query
    assert "COALESCE(c.master_candidate_id,c.id)" in connection.query


def test_postgres_connection_falls_back_when_pool_package_is_missing(monkeypatch):
    import psycopg

    class _DirectRaw:
        def __init__(self):
            self.closed = False

        @contextmanager
        def transaction(self):
            yield

        def close(self):
            self.closed = True

    raw = _DirectRaw()
    connect_calls = []

    monkeypatch.setattr(store.config, "DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(store.config, "DATABASE_CONNECT_TIMEOUT", 7)
    monkeypatch.setitem(sys.modules, "psycopg_pool", None)
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *args, **kwargs: connect_calls.append((args, kwargs)) or raw,
    )
    monkeypatch.setattr(store, "_configure_postgres_namespace", lambda connection: None)
    monkeypatch.setattr(store, "_activate_postgres_namespace", lambda connection: None)
    monkeypatch.setattr(store, "_prepare_postgres", lambda connection: None)

    with store._conn() as connection:
        assert connection.raw is raw
        assert raw.closed is False

    assert raw.closed is True
    assert len(connect_calls) == 1
    assert connect_calls[0][1]["connect_timeout"] == 7
