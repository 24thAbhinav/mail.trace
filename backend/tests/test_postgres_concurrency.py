"""PostgreSQL-specific tests for the unique constraint and concurrency.

Skipped unless ``TEST_DATABASE_URL`` points at a throwaway Postgres database
(e.g. a Neon branch). They exercise ``FOR UPDATE`` and the unique index that
SQLite cannot.

Run with::

    TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest tests/test_postgres_concurrency.py
"""

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import models  # noqa: F401  (register tables on metadata)
from app.db.migrations import apply_migrations
from app.db.models import Email, SyncState
from app.gmail.sync import full_sync

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run Postgres tests"
)


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeService:
    """Minimal Gmail resource double that emits one message, once."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self.profile = {"emailAddress": "me@x.com", "historyId": "999"}
        self._listed = False

    def users(self):
        return self

    def getProfile(self, **kw):
        return _Exec(lambda: self.profile)

    def messages(self):
        return self

    def list(self, **kw):
        return _Exec(self._list_page)

    def get(self, **kw):
        return _Exec(self._message)

    def _list_page(self):
        if self._listed:
            return {"messages": []}
        self._listed = True
        return {"messages": [{"id": self.message_id}]}

    def _message(self):
        return {"id": self.message_id, "threadId": "t1", "payload": {}}


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    SQLModel.metadata.create_all(engine)
    # Mirror production startup (create_db_and_tables): create_all is additive
    # only, so reconcile a pre-existing legacy ``emails`` table that predates
    # the processing-status columns.
    apply_migrations(engine)
    yield engine
    engine.dispose()


def test_message_id_unique_constraint_enforced(pg_engine):
    message_id = f"uniq-{uuid4().hex}"

    with Session(pg_engine) as session:
        session.add(Email(message_id=message_id, subject="first"))
        session.commit()

    with Session(pg_engine) as session:
        session.add(Email(message_id=message_id, subject="second"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_concurrent_full_sync_single_cursor_no_duplicates(pg_engine):
    account = f"c-{uuid4().hex}"
    message_id = f"m-{uuid4().hex}"
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker():
        try:
            with Session(pg_engine) as session:
                barrier.wait()
                full_sync(FakeService(message_id), session, account)
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []

    with Session(pg_engine) as session:
        cursors = session.exec(
            select(SyncState).where(SyncState.email_address == account)
        ).all()
        assert len(cursors) == 1
        assert cursors[0].history_id == "999"

        stored = session.exec(
            select(Email).where(Email.message_id == message_id)
        ).all()
        assert len(stored) == 1
