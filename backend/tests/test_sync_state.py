"""Tests for sync-state cursor persistence."""

from __future__ import annotations

from sqlmodel import Session, select

from app.db.models import SyncState
from app.gmail.sync import _lock_cursor, ensure_cursor, get_cursor, set_cursor


def test_get_cursor_returns_none_when_absent(session: Session):
    assert get_cursor(session, "a@example.com") is None


def test_set_cursor_creates_then_updates_single_row(session: Session):
    set_cursor(session, "a@example.com", "100")
    session.commit()

    set_cursor(session, "a@example.com", "200")
    session.commit()

    rows = session.exec(
        select(SyncState).where(SyncState.email_address == "a@example.com")
    ).all()
    assert len(rows) == 1
    assert rows[0].history_id == "200"


def test_ensure_cursor_creates_if_missing(session: Session):
    ensure_cursor(session, "a@example.com", "100")
    session.commit()
    assert get_cursor(session, "a@example.com") == "100"


def test_ensure_cursor_does_not_overwrite_existing(session: Session):
    set_cursor(session, "a@example.com", "100")
    session.commit()

    ensure_cursor(session, "a@example.com", "999")
    session.commit()

    assert get_cursor(session, "a@example.com") == "100"


def test_cursors_are_per_mailbox(session: Session):
    set_cursor(session, "a@example.com", "100")
    set_cursor(session, "b@example.com", "200")
    session.commit()

    assert get_cursor(session, "a@example.com") == "100"
    assert get_cursor(session, "b@example.com") == "200"


def test_lock_cursor_creates_missing_row(session: Session):
    assert get_cursor(session, "a@example.com") is None

    result = _lock_cursor(session, "a@example.com")
    session.commit()

    assert result is None  # no real cursor yet, but the row now exists
    rows = session.exec(
        select(SyncState).where(SyncState.email_address == "a@example.com")
    ).all()
    assert len(rows) == 1
    assert rows[0].history_id == ""
