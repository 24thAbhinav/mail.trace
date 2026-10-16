"""Tests for the applications read/write API (frontend contract)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import app.db.database as db
from app.db.models import Application, ApplicationEvent, ApplicationStatus
from app.main import app


@pytest.fixture()
def client(engine, monkeypatch):
    monkeypatch.setattr(db, "engine", engine)
    with TestClient(app) as c:
        yield c


def _seed(session, company, role, status, *, is_closed=False):
    record = Application(
        company=company,
        role=role,
        match_key=f"{company.lower()}|{role.lower()}",
        status=status,
        is_closed=is_closed,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def test_list_applications_and_stats(client, engine):
    with Session(engine) as s:
        _seed(s, "Stripe", "Backend Engineer", ApplicationStatus.APPLIED)
        _seed(s, "Figma", "Frontend Engineer", ApplicationStatus.INTERVIEW)
        _seed(s, "Old Co", "Intern", ApplicationStatus.REJECTED, is_closed=True)

    resp = client.get("/applications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert {item["company"] for item in data["items"]} == {"Stripe", "Figma", "Old Co"}

    stats = client.get("/applications/stats").json()
    assert stats["total"] == 2  # excludes closed
    assert stats["closed"] == 1
    assert stats["by_status"]["APPLIED"] == 1
    assert stats["by_status"]["INTERVIEW"] == 1
    assert stats["by_status"]["CLOSED"] == 1


def test_list_filters_by_closed_status_and_search(client, engine):
    with Session(engine) as s:
        _seed(s, "Stripe", "Backend Engineer", ApplicationStatus.APPLIED)
        _seed(s, "Closed Co", "Engineer", ApplicationStatus.APPLIED, is_closed=True)

    # include_closed=false (the "All" tab) excludes closed applications.
    data = client.get("/applications", params={"include_closed": "false"}).json()
    assert data["total"] == 1
    assert data["items"][0]["company"] == "Stripe"

    # A concrete status filter also excludes closed applications.
    data = client.get("/applications", params={"status": "APPLIED"}).json()
    assert data["total"] == 1

    # The CLOSED tab returns only closed applications.
    data = client.get("/applications", params={"status": "CLOSED"}).json()
    assert data["total"] == 1
    assert data["items"][0]["company"] == "Closed Co"

    # Search matches company or role, case-insensitively.
    data = client.get("/applications", params={"search": "stripe"}).json()
    assert data["total"] == 1
    assert data["items"][0]["company"] == "Stripe"

    assert client.get("/applications", params={"status": "NOPE"}).status_code == 422


def test_get_application_and_events(client, engine):
    with Session(engine) as s:
        record = _seed(s, "Datadog", "Systems Engineer", ApplicationStatus.UNDER_REVIEW)
        s.add(
            ApplicationEvent(
                application_id=record.id,
                status=ApplicationStatus.UNDER_REVIEW,
                note="Under review",
                source_email_id="msg-1",
            )
        )
        s.commit()
        app_id = record.id

    detail = client.get(f"/applications/{app_id}")
    assert detail.status_code == 200
    assert detail.json()["company"] == "Datadog"

    events = client.get(f"/applications/{app_id}/events")
    assert events.status_code == 200
    body = events.json()
    assert len(body) == 1
    assert body[0]["note"] == "Under review"
    assert body[0]["status"] == "UNDER_REVIEW"
    assert body[0]["application_id"] == app_id

    assert client.get("/applications/999999").status_code == 404
    assert client.get("/applications/999999/events").status_code == 404


def test_patch_updates_status_notes_and_closed(client, engine):
    with Session(engine) as s:
        record = _seed(s, "Notion", "Product Engineer", ApplicationStatus.APPLIED)
        app_id = record.id

    resp = client.patch(f"/applications/{app_id}", json={"status": "INTERVIEW"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "INTERVIEW"

    with Session(engine) as s:
        reloaded = s.get(Application, app_id)
        assert reloaded.previous_status == ApplicationStatus.APPLIED
        assert reloaded.status == ApplicationStatus.INTERVIEW

    resp = client.patch(f"/applications/{app_id}", json={"notes": "moved to onsite"})
    assert resp.json()["notes"] == "moved to onsite"

    resp = client.patch(f"/applications/{app_id}", json={"is_closed": True})
    body = resp.json()
    assert body["is_closed"] is True
    assert body["closed_at"] is not None

    assert client.patch("/applications/999999", json={"is_closed": True}).status_code == 404
