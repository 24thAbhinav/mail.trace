"""Migration tests against PostgreSQL (skipped without ``TEST_DATABASE_URL``).

``apply_migrations`` is a no-op on SQLite (where ``create_all`` builds fresh
tables), so these tests target a throwaway Postgres schema to exercise the
real ``ALTER TABLE`` / ``CREATE INDEX`` paths against a legacy ``emails`` table
that predates the processing-status columns.

The throwaway schema is addressed explicitly (via ``apply_migrations``'s
``schema`` argument) rather than by mutating ``search_path``, because Neon's
pooled connections leak session-level ``SET search_path`` across clients.

Run with::

    TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest tests/test_migrations.py
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app.db.migrations import apply_migrations

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run Postgres tests"
)


@pytest.fixture()
def pg_engine():
    """Engine against the default schema; tests create/drop their own schema."""
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def schema(pg_engine) -> str:
    """A throwaway, uniquely-named schema holding a legacy ``emails`` table."""
    name = f"mig_{uuid4().hex}"
    with pg_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))
    yield name
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))


def _create_legacy_emails(engine, schema: str) -> None:
    """Recreate the ``emails`` table as it existed before the status columns."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE "{schema}".emails (
                    id SERIAL PRIMARY KEY,
                    message_id VARCHAR NOT NULL,
                    thread_id VARCHAR,
                    sender_email VARCHAR,
                    recipient_email VARCHAR,
                    subject VARCHAR,
                    date VARCHAR,
                    body TEXT,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )


def _columns(engine, schema: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("emails", schema=schema)}


def _indexes(engine, schema: str) -> set[str]:
    return {i["name"] for i in inspect(engine).get_indexes("emails", schema=schema)}


def test_adds_missing_columns_and_indexes(pg_engine, schema):
    _create_legacy_emails(pg_engine, schema)

    apply_migrations(pg_engine, schema=schema)

    assert {"status", "error", "updated_at"} <= _columns(pg_engine, schema)
    assert "ix_emails_message_id" in _indexes(pg_engine, schema)
    assert "ix_emails_status" in _indexes(pg_engine, schema)


def test_idempotent(pg_engine, schema):
    _create_legacy_emails(pg_engine, schema)

    apply_migrations(pg_engine, schema=schema)
    apply_migrations(pg_engine, schema=schema)  # must not raise or create duplicates


def test_skips_unique_index_when_duplicate_message_ids(pg_engine, schema):
    _create_legacy_emails(pg_engine, schema)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".emails (message_id, received_at) '
                "VALUES ('dup', now()), ('dup', now())"
            )
        )

    apply_migrations(pg_engine, schema=schema)

    assert "ix_emails_message_id" not in _indexes(pg_engine, schema)
    assert "ix_emails_status" in _indexes(pg_engine, schema)
    # status column still backfilled
    assert "status" in _columns(pg_engine, schema)
