"""Tests for incremental Gmail synchronization."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.db.models import Email, EmailStatus
from app.gmail.sync import (
    GmailSyncError,
    HistoryExpiredError,
    extract_email_fields,
    full_sync,
    get_cursor,
    set_cursor,
    sync_history,
)

from .conftest import history_record, make_404, make_message


def test_extract_email_fields():
    message = make_message(
        "m1",
        thread_id="t1",
        sender="Recruiter <recruiter@acme.com>",
        recipient="me@example.com",
        subject="Interview invite",
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body="hello world",
    )
    fields = extract_email_fields(message)
    assert fields.message_id == "m1"
    assert fields.thread_id == "t1"
    assert fields.sender_email == "Recruiter <recruiter@acme.com>"
    assert fields.recipient_email == "me@example.com"
    assert fields.subject == "Interview invite"
    assert fields.date == "Mon, 01 Jan 2026 10:00:00 +0000"
    assert fields.body == "hello world"


def test_sync_history_paginates_persists_and_advances(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.commit()

    fake_gmail.history_pages = [
        {
            "historyId": "100",
            "nextPageToken": "tok1",
            "history": [history_record("m1")],
        },
        {"historyId": "200", "history": [history_record("m2")]},
    ]
    fake_gmail.messages = {
        "m1": make_message("m1", thread_id="t1", subject="one"),
        "m2": make_message("m2", thread_id="t2", subject="two"),
    }

    with Session(engine) as s:
        sync_history(fake_gmail, s, "a@example.com")

    # pagination: first call uses stored cursor, second uses nextPageToken
    assert fake_gmail.history_list_kwargs[0]["startHistoryId"] == "50"
    assert "pageToken" not in fake_gmail.history_list_kwargs[0]
    assert fake_gmail.history_list_kwargs[1]["pageToken"] == "tok1"

    with Session(engine) as s:
        emails = s.exec(select(Email)).all()
        assert {e.message_id for e in emails} == {"m1", "m2"}
        assert get_cursor(s, "a@example.com") == "200"


def test_sync_history_uses_fallback_when_no_cursor(engine, fake_gmail):
    fake_gmail.history_pages = [{"historyId": "300", "history": []}]

    with Session(engine) as s:
        sync_history(fake_gmail, s, "a@example.com", fallback_history_id="250")

    assert fake_gmail.history_list_kwargs[0]["startHistoryId"] == "250"


def test_sync_history_requires_starting_point(engine, fake_gmail):
    with Session(engine) as s:
        with pytest.raises(GmailSyncError):
            sync_history(fake_gmail, s, "a@example.com")


def test_sync_history_deduplicates_repeated_message_ids(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.commit()

    fake_gmail.history_pages = [
        {
            "historyId": "100",
            "history": [history_record("m1", "m1")],  # same id twice
        }
    ]
    fake_gmail.messages = {"m1": make_message("m1")}

    with Session(engine) as s:
        sync_history(fake_gmail, s, "a@example.com")

    with Session(engine) as s:
        assert len(s.exec(select(Email)).all()) == 1


def test_sync_history_skips_already_persisted_emails(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.add(Email(message_id="m1", subject="existing"))
        s.commit()

    fake_gmail.history_pages = [
        {"historyId": "100", "history": [history_record("m1")]}
    ]
    fake_gmail.messages = {"m1": make_message("m1", subject="existing")}

    with Session(engine) as s:
        sync_history(fake_gmail, s, "a@example.com")

    assert fake_gmail.messages_get_ids == []  # never re-fetched
    with Session(engine) as s:
        assert len(s.exec(select(Email)).all()) == 1


def test_sync_history_does_not_advance_cursor_on_failure(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.commit()

    fake_gmail.history_pages = [
        {"historyId": "100", "history": [history_record("m1")]}
    ]
    fake_gmail.message_errors["m1"] = Exception("transient fetch failure")

    with Session(engine) as s:
        with pytest.raises(Exception):
            sync_history(fake_gmail, s, "a@example.com")
        s.rollback()

    with Session(engine) as s:
        assert get_cursor(s, "a@example.com") == "50"
        assert s.exec(select(Email)).all() == []


def test_sync_history_raises_on_expired_history(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.commit()

    fake_gmail.history_error = make_404()

    with Session(engine) as s:
        with pytest.raises(HistoryExpiredError):
            sync_history(fake_gmail, s, "a@example.com")
        s.rollback()


def test_full_sync_lists_fetches_and_reanchors(engine, fake_gmail):
    fake_gmail.list_pages = [
        {"messages": [{"id": "m1"}], "nextPageToken": "tok1"},
        {"messages": [{"id": "m2"}]},
    ]
    fake_gmail.messages = {
        "m1": make_message("m1", subject="one"),
        "m2": make_message("m2", subject="two"),
    }
    fake_gmail.profile_response = {
        "emailAddress": "a@example.com",
        "historyId": "500",
    }

    with Session(engine) as s:
        full_sync(fake_gmail, s, "a@example.com")

    assert fake_gmail.messages_list_kwargs[1]["pageToken"] == "tok1"
    with Session(engine) as s:
        emails = s.exec(select(Email)).all()
        assert {e.message_id for e in emails} == {"m1", "m2"}
        assert get_cursor(s, "a@example.com") == "500"


def test_sync_persists_emails_as_pending(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.commit()

    fake_gmail.history_pages = [
        {"historyId": "100", "history": [history_record("m1", "m2")]}
    ]
    fake_gmail.messages = {
        "m1": make_message("m1", subject="one"),
        "m2": make_message("m2", subject="two"),
    }

    with Session(engine) as s:
        sync_history(fake_gmail, s, "a@example.com")

    with Session(engine) as s:
        emails = s.exec(select(Email)).all()
        assert {e.message_id for e in emails} == {"m1", "m2"}
        assert all(e.status == EmailStatus.PENDING for e in emails)


def test_sync_persists_full_email_fields(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.commit()

    fake_gmail.history_pages = [
        {"historyId": "100", "history": [history_record("m1")]}
    ]
    fake_gmail.messages = {
        "m1": make_message(
            "m1",
            thread_id="t1",
            sender="Recruiter <recruiter@acme.com>",
            recipient="me@example.com",
            subject="Offer letter",
            date="Mon, 01 Jan 2026 10:00:00 +0000",
            body="Congratulations, here is your offer.",
        )
    }

    with Session(engine) as s:
        sync_history(fake_gmail, s, "a@example.com")

    with Session(engine) as s:
        email = s.exec(select(Email)).one()
        assert email.message_id == "m1"
        assert email.thread_id == "t1"
        assert email.sender_email == "Recruiter <recruiter@acme.com>"
        assert email.recipient_email == "me@example.com"
        assert email.subject == "Offer letter"
        assert email.date == "Mon, 01 Jan 2026 10:00:00 +0000"
        assert email.body == "Congratulations, here is your offer."
        assert email.status == EmailStatus.PENDING


def test_sync_commit_failure_does_not_advance_cursor(engine, fake_gmail, monkeypatch):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.commit()

    fake_gmail.history_pages = [
        {"historyId": "100", "history": [history_record("m1")]}
    ]
    fake_gmail.messages = {"m1": make_message("m1", subject="one")}

    # Simulate a commit failure: the cursor and the email are written in the
    # same transaction, so neither may become durable independently.
    sess = Session(engine)

    def fail_commit():
        raise RuntimeError("simulated database commit failure")

    monkeypatch.setattr(sess, "commit", fail_commit)

    with pytest.raises(RuntimeError):
        sync_history(fake_gmail, sess, "a@example.com")
    sess.rollback()

    with Session(engine) as s:
        assert get_cursor(s, "a@example.com") == "50"
        assert s.exec(select(Email)).all() == []


def test_reprocessing_does_not_reset_status(engine, fake_gmail):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "50")
        s.add(Email(message_id="m1", subject="done", status=EmailStatus.COMPLETED))
        s.commit()

    fake_gmail.history_pages = [
        {"historyId": "100", "history": [history_record("m1")]}
    ]
    fake_gmail.messages = {"m1": make_message("m1", subject="done")}

    with Session(engine) as s:
        sync_history(fake_gmail, s, "a@example.com")

    with Session(engine) as s:
        email = s.exec(select(Email)).one()
        assert email.status == EmailStatus.COMPLETED  # not reset to pending


def test_full_sync_reads_history_id_before_listing(engine):
    order = []

    class _Profile:
        def execute(self):
            order.append("profile")
            return {"emailAddress": "a@example.com", "historyId": "500"}

    class _List:
        def execute(self):
            order.append("list")
            return {"messages": []}

    class _Messages:
        def list(self, **kw):
            return _List()

        def get(self, **kw):
            class _Get:
                def execute(self):
                    return {"id": "x", "payload": {}}

            return _Get()

    class _Users:
        def getProfile(self, **kw):
            return _Profile()

        def messages(self):
            return _Messages()

    class _Svc:
        def users(self):
            return _Users()

    with Session(engine) as s:
        full_sync(_Svc(), s, "a@example.com")

    assert order == ["profile", "list"]
