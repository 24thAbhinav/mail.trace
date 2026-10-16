"""Tests for the Pub/Sub push endpoint."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.db.database as db
import app.main as main
from app.db.models import Email
from app.gmail.sync import get_cursor, set_cursor
from app.main import app, get_gmail_service

from .conftest import make_404, make_message


def _payload(email_address="a@example.com", history_id="123"):
    notification = {"emailAddress": email_address, "historyId": history_id}
    data = base64.b64encode(json.dumps(notification).encode()).decode()
    return {"message": {"data": data}}


@pytest.fixture()
def client(engine, fake_gmail, monkeypatch):
    monkeypatch.setattr(db, "engine", engine)

    def _service():
        return fake_gmail

    app.dependency_overrides[get_gmail_service] = _service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def no_llm_processing(monkeypatch):
    """Stub the processing entrypoint so tests never invoke the real LLM.

    Records each call's ``(session, kwargs)`` so tests can assert whether the
    Pub/Sub background drain ran, and that it opened its own session.
    """
    calls: list[tuple[Session, dict]] = []

    def spy(session, **kwargs):
        calls.append((session, kwargs))
        return {"claimed": 0, "processed": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(main, "process_pending_emails", spy)
    return calls


def test_pubsub_push_ok(client):
    response = client.post("/pubsub/push", json=_payload())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_pubsub_push_invalid_payload_returns_400(client):
    response = client.post("/pubsub/push", json={"message": {"data": "!!bad!!"}})
    assert response.status_code == 400


def test_pubsub_push_missing_message_returns_400(client):
    response = client.post("/pubsub/push", json={"foo": "bar"})
    assert response.status_code == 400


def test_pubsub_push_sync_failure_returns_500(client, fake_gmail):
    fake_gmail.history_error = Exception("transient failure")
    response = client.post("/pubsub/push", json=_payload())
    assert response.status_code == 500


def test_pubsub_push_duplicate_notification_no_duplicate_emails(client, fake_gmail, engine):
    fake_gmail.history_pages = [
        {"historyId": "100", "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}
    ]
    fake_gmail.messages = {"m1": make_message("m1", subject="one")}

    payload = _payload()
    assert client.post("/pubsub/push", json=payload).status_code == 200
    assert client.post("/pubsub/push", json=payload).status_code == 200

    with Session(engine) as s:
        emails = s.exec(select(Email)).all()
        assert len(emails) == 1
        assert emails[0].message_id == "m1"


def test_pubsub_push_delayed_notification_uses_stored_cursor(client, fake_gmail, engine):
    with Session(engine) as s:
        set_cursor(s, "a@example.com", "999")
        s.commit()

    # A stale/delayed notification carries an older historyId; the stored
    # cursor (999) must win so we do not re-fetch already-seen history.
    fake_gmail.history_pages = [{"historyId": "1000", "history": []}]

    response = client.post(
        "/pubsub/push", json=_payload(history_id="100")
    )
    assert response.status_code == 200
    assert fake_gmail.history_list_kwargs[0]["startHistoryId"] == "999"


def test_pubsub_push_expired_history_triggers_full_sync(client, fake_gmail, engine):
    fake_gmail.history_error = make_404()
    fake_gmail.list_pages = [{"messages": [{"id": "m1"}]}]
    fake_gmail.messages = {"m1": make_message("m1", subject="one")}
    fake_gmail.profile_response = {
        "emailAddress": "a@example.com",
        "historyId": "500",
    }

    response = client.post("/pubsub/push", json=_payload())
    assert response.status_code == 200

    with Session(engine) as s:
        emails = s.exec(select(Email)).all()
        assert {e.message_id for e in emails} == {"m1"}
        assert get_cursor(s, "a@example.com") == "500"


def test_pubsub_push_schedules_processing(client, no_llm_processing):
    response = client.post("/pubsub/push", json=_payload())
    assert response.status_code == 200
    assert len(no_llm_processing) == 1


def test_pubsub_push_full_sync_recovery_schedules_processing(
    client, fake_gmail, no_llm_processing
):
    fake_gmail.history_error = make_404()
    fake_gmail.list_pages = [{"messages": []}]

    response = client.post("/pubsub/push", json=_payload())
    assert response.status_code == 200
    assert len(no_llm_processing) == 1


def test_pubsub_push_sync_failure_does_not_schedule_processing(
    client, fake_gmail, no_llm_processing
):
    fake_gmail.history_error = Exception("transient failure")

    response = client.post("/pubsub/push", json=_payload())
    assert response.status_code == 500
    assert no_llm_processing == []


def test_pubsub_push_invalid_payload_does_not_schedule_processing(
    client, no_llm_processing
):
    response = client.post("/pubsub/push", json={"message": {"data": "!!bad!!"}})
    assert response.status_code == 400
    assert no_llm_processing == []


def test_drain_pending_emails_uses_own_session(
    engine, monkeypatch, no_llm_processing
):
    monkeypatch.setattr(db, "engine", engine)

    main._drain_pending_emails(limit=7)

    assert len(no_llm_processing) == 1
    session, kwargs = no_llm_processing[0]
    assert kwargs["limit"] == 7
    assert isinstance(session, Session)
    assert session.get_bind() is engine


def test_gmail_sync_returns_counts_and_schedules_processing(
    client, fake_gmail, no_llm_processing
):
    fake_gmail.list_pages = [{"messages": [{"id": "m1"}, {"id": "m2"}]}]
    fake_gmail.messages = {
        "m1": make_message("m1", subject="one"),
        "m2": make_message("m2", subject="two"),
    }

    response = client.post("/gmail/sync")
    assert response.status_code == 200
    data = response.json()
    assert data["fetched"] == 2
    assert set(data["processed"]) == {"m1", "m2"}
    assert len(no_llm_processing) == 1


def test_gmail_sync_second_run_reports_no_new_emails(
    client, fake_gmail, no_llm_processing
):
    fake_gmail.list_pages = [{"messages": [{"id": "m1"}]}]
    fake_gmail.messages = {"m1": make_message("m1", subject="one")}

    assert client.post("/gmail/sync").json()["processed"] == ["m1"]

    # Re-listing the same message must not report it as newly persisted.
    fake_gmail.list_pages = [{"messages": [{"id": "m1"}]}]
    second = client.post("/gmail/sync").json()
    assert second["fetched"] == 1
    assert second["processed"] == []
