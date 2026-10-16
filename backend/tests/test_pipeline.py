"""Tests for the email processing pipeline (LangGraph + PostgreSQL)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.db.database as db
from app.db.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    Email,
    EmailStatus,
    ProcessedEmail,
)
from app.main import app
from app.services.email_processor import (
    compute_match_key,
    normalize_text,
    process_pending_emails,
    process_single_email,
)


class MockGraph:
    """Mock LangGraph workflow returning configurable responses or raising errors."""

    def __init__(self, output: dict | None = None, exc: Exception | None = None):
        self.output = output or {}
        self.exc = exc
        self.invoked_states: list[dict] = []

    def invoke(self, state: dict, config: dict | None = None) -> dict:
        self.invoked_states.append(state)
        if self.exc:
            raise self.exc
        return self.output


def _create_email(
    session: Session,
    message_id: str,
    subject: str = "Test Subject",
    body: str = "Test Body",
    thread_id: str | None = None,
    status: EmailStatus = EmailStatus.PENDING,
    sender_email: str = "recruiter@example.com",
) -> Email:
    email = Email(
        message_id=message_id,
        thread_id=thread_id,
        subject=subject,
        body=body,
        sender_email=sender_email,
        status=status,
    )
    session.add(email)
    session.commit()
    session.refresh(email)
    return email


def test_normalize_text_and_compute_match_key():
    assert normalize_text("  Acme   Corp  ") == "acme corp"
    assert normalize_text(None) == ""
    assert compute_match_key(" Acme Corp ", " Software Engineer ") == "acme corp|software engineer"


def test_successful_application_extraction(session: Session):
    """A relevant email creates Application, ApplicationEvent, ProcessedEmail, and marks COMPLETED."""
    email = _create_email(
        session,
        message_id="msg-101",
        subject="Thanks for applying to Stripe",
        body="We received your application for Backend Engineer.",
        sender_email="jobs@stripe.com",
    )

    mock_graph = MockGraph(
        output={
            "is_application_email": True,
            "company": "Stripe",
            "role": "Backend Engineer",
            "status": ApplicationStatus.APPLIED,
            "summary": "Application received for Backend Engineer",
            "action_url": "https://stripe.com/portal",
            "event_date": None,
        }
    )

    processed = process_single_email(session, email, graph=mock_graph)

    assert processed.verdict == "processed"
    assert processed.is_application_email is True
    assert processed.message_id == "msg-101"
    assert processed.application_id is not None
    assert processed.event_id is not None
    assert processed.error is None

    # Check Email model
    session.refresh(email)
    assert email.status == EmailStatus.COMPLETED
    assert email.error is None

    # Check Application
    app_record = session.get(Application, processed.application_id)
    assert app_record is not None
    assert app_record.company == "Stripe"
    assert app_record.role == "Backend Engineer"
    assert app_record.match_key == "stripe|backend engineer"
    assert app_record.status == ApplicationStatus.APPLIED
    assert app_record.latest_note == "Application received for Backend Engineer"
    assert app_record.latest_action_url == "https://stripe.com/portal"
    assert app_record.source_email_id == "msg-101"
    assert app_record.sender_email == "jobs@stripe.com"

    # Check ApplicationEvent
    event = session.get(ApplicationEvent, processed.event_id)
    assert event is not None
    assert event.application_id == app_record.id
    assert event.status == ApplicationStatus.APPLIED
    assert event.note == "Application received for Backend Engineer"
    assert event.action_url == "https://stripe.com/portal"
    assert event.source_email_id == "msg-101"


def test_irrelevant_email_is_skipped(session: Session):
    """An irrelevant email creates ProcessedEmail(verdict='skipped'), marks COMPLETED, no Application."""
    email = _create_email(
        session,
        message_id="msg-news-01",
        subject="Your weekly newsletter",
        body="Top 10 tech news this week.",
    )

    mock_graph = MockGraph(output={"is_application_email": False})

    processed = process_single_email(session, email, graph=mock_graph)

    assert processed.verdict == "skipped"
    assert processed.is_application_email is False
    assert processed.application_id is None
    assert processed.event_id is None

    session.refresh(email)
    assert email.status == EmailStatus.COMPLETED
    assert email.error is None

    # Assert no applications or events exist in DB
    apps = session.exec(select(Application)).all()
    events = session.exec(select(ApplicationEvent)).all()
    assert len(apps) == 0
    assert len(events) == 0


def test_existing_application_update_by_match_key(session: Session):
    """A follow-up email updates an existing Application rather than creating a duplicate."""
    # First email creates application
    email1 = _create_email(
        session,
        message_id="msg-app-01",
        subject="Application received at Figma",
        body="You applied for Frontend Engineer",
    )
    graph1 = MockGraph(
        output={
            "is_application_email": True,
            "company": "Figma",
            "role": "Frontend Engineer",
            "status": ApplicationStatus.APPLIED,
            "summary": "Application submitted",
        }
    )
    process_single_email(session, email1, graph=graph1)

    # Second email: Interview invitation for same company and role
    email2 = _create_email(
        session,
        message_id="msg-app-02",
        subject="Interview with Figma!",
        body="Schedule your technical interview.",
    )
    graph2 = MockGraph(
        output={
            "is_application_email": True,
            "company": "Figma",
            "role": "Frontend Engineer",
            "status": ApplicationStatus.INTERVIEW,
            "summary": "Invited to technical interview",
            "action_url": "https://calendly.com/figma/interview",
            "event_date": "Oct 25, 2026 at 2:00 PM PST",
        }
    )
    processed2 = process_single_email(session, email2, graph=graph2)

    # Must be linked to the same application
    apps = session.exec(select(Application)).all()
    assert len(apps) == 1
    app_record = apps[0]
    assert app_record.id == processed2.application_id
    assert app_record.status == ApplicationStatus.INTERVIEW
    assert app_record.previous_status == ApplicationStatus.APPLIED
    assert app_record.latest_note == "Invited to technical interview"
    assert app_record.latest_action_url == "https://calendly.com/figma/interview"
    assert app_record.latest_event_date == "Oct 25, 2026 at 2:00 PM PST"

    # There should now be 2 events for this application
    events = session.exec(
        select(ApplicationEvent).where(ApplicationEvent.application_id == app_record.id)
    ).all()
    assert len(events) == 2
    assert {e.source_email_id for e in events} == {"msg-app-01", "msg-app-02"}
    assert {e.status for e in events} == {ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW}


def test_existing_application_update_by_thread_id(session: Session):
    """Follow-up in the same Gmail thread links to the application even if role differs slightly."""
    # First email creates application
    email1 = _create_email(
        session,
        message_id="msg-th-01",
        thread_id="thread-12345",
        subject="Your Datadog Application",
        body="Thanks for applying to Datadog.",
    )
    graph1 = MockGraph(
        output={
            "is_application_email": True,
            "company": "Datadog",
            "role": "Systems Engineer",
            "status": ApplicationStatus.UNDER_REVIEW,
        }
    )
    process_single_email(session, email1, graph=graph1)

    # Second email has the same thread_id, but generic/slightly different role
    email2 = _create_email(
        session,
        message_id="msg-th-02",
        thread_id="thread-12345",
        subject="Re: Your Datadog Application",
        body="Online Assessment link inside.",
    )
    graph2 = MockGraph(
        output={
            "is_application_email": True,
            "company": "Datadog",
            "role": "Engineering Candidate",
            "status": ApplicationStatus.OA,
            "summary": "Coding assessment sent",
            "action_url": "https://hackerrank.com/test",
        }
    )
    processed2 = process_single_email(session, email2, graph=graph2)

    apps = session.exec(select(Application)).all()
    assert len(apps) == 1
    assert apps[0].id == processed2.application_id
    assert apps[0].status == ApplicationStatus.OA


def test_idempotent_duplicate_processing(session: Session):
    """Processing the same email multiple times does not duplicate applications, events, or audit records."""
    email = _create_email(
        session,
        message_id="msg-dup-01",
        subject="Your Notion Application",
        body="We got your application.",
    )
    mock_graph = MockGraph(
        output={
            "is_application_email": True,
            "company": "Notion",
            "role": "Product Engineer",
            "status": ApplicationStatus.APPLIED,
        }
    )

    # First run
    p1 = process_single_email(session, email, graph=mock_graph)
    assert p1.verdict == "processed"

    # Second run on the same email
    p2 = process_single_email(session, email, graph=mock_graph)
    assert p2.id == p1.id
    assert p2.verdict == "processed"

    # Assert exactly 1 row per table
    assert len(session.exec(select(Application)).all()) == 1
    assert len(session.exec(select(ApplicationEvent)).all()) == 1
    assert len(session.exec(select(ProcessedEmail)).all()) == 1


def test_processing_failure_records_error_and_allows_retry(session: Session):
    """A failed email records FAILED status, does not create partial applications, and can be retried."""
    email = _create_email(
        session,
        message_id="msg-fail-01",
        subject="Malformed email",
        body="Some corrupted body",
    )

    failing_graph = MockGraph(exc=RuntimeError("Rate limit exceeded"))

    processed = process_single_email(session, email, graph=failing_graph)

    assert processed.verdict == "error"
    assert "Rate limit exceeded" in (processed.error or "")

    session.refresh(email)
    assert email.status == EmailStatus.FAILED
    assert "Rate limit exceeded" in (email.error or "")

    # No application or event should have been committed
    assert len(session.exec(select(Application)).all()) == 0
    assert len(session.exec(select(ApplicationEvent)).all()) == 0

    # Retry the failed email with a working graph
    working_graph = MockGraph(
        output={
            "is_application_email": True,
            "company": "Airbnb",
            "role": "Software Engineer",
            "status": ApplicationStatus.APPLIED,
            "summary": "Recovered on retry",
        }
    )

    stats = process_pending_emails(session, retry_failed=True, graph=working_graph)
    assert stats["claimed"] == 1
    assert stats["processed"] == 1
    assert stats["failed"] == 0

    session.refresh(email)
    assert email.status == EmailStatus.COMPLETED
    assert email.error is None

    # ProcessedEmail should now be "processed"
    proc_records = session.exec(select(ProcessedEmail)).all()
    assert len(proc_records) == 1
    assert proc_records[0].verdict == "processed"
    assert proc_records[0].error is None

    # Application and event now exist
    assert len(session.exec(select(Application)).all()) == 1
    assert len(session.exec(select(ApplicationEvent)).all()) == 1


def test_batch_processing_fault_tolerance(session: Session):
    """One malformed email in a batch does not prevent others from succeeding."""
    e1 = _create_email(session, "msg-batch-01", subject="Email 1")
    e2 = _create_email(session, "msg-batch-02", subject="Email 2 (will fail)")
    e3 = _create_email(session, "msg-batch-03", subject="Email 3")

    class FlakyGraph:
        def invoke(self, state: dict, config: dict | None = None) -> dict:
            if state["source_email_id"] == "msg-batch-02":
                raise ValueError("Corrupt body formatting")
            return {
                "is_application_email": True,
                "company": "Company " + state["source_email_id"],
                "role": "Engineer",
                "status": ApplicationStatus.APPLIED,
            }

    stats = process_pending_emails(session, limit=10, graph=FlakyGraph())

    assert stats["claimed"] == 3
    assert stats["processed"] == 2
    assert stats["failed"] == 1

    session.refresh(e1)
    session.refresh(e2)
    session.refresh(e3)

    assert e1.status == EmailStatus.COMPLETED
    assert e2.status == EmailStatus.FAILED
    assert "Corrupt body formatting" in (e2.error or "")
    assert e3.status == EmailStatus.COMPLETED


def test_fastapi_manual_trigger_endpoint(engine, monkeypatch):
    """POST /emails/process drains pending emails and returns execution summary."""
    monkeypatch.setattr(db, "engine", engine)

    # Seed 2 pending emails in SQLite
    with Session(engine) as s:
        _create_email(s, "msg-api-01", subject="Sub 1")
        _create_email(s, "msg-api-02", subject="Sub 2")

    # Mock the default workflow in email_processor to avoid real LLM calls
    mock_graph = MockGraph(
        output={
            "is_application_email": True,
            "company": "Linear",
            "role": "Frontend",
            "status": ApplicationStatus.APPLIED,
        }
    )
    import app.services.email_processor as ep
    monkeypatch.setattr(ep, "workflow", mock_graph)

    with TestClient(app) as client:
        response = client.post("/emails/process?limit=5")
        assert response.status_code == 200
        data = response.json()
        assert data["claimed"] == 2
        assert data["processed"] == 2
        assert data["failed"] == 0

        # Calling again has 0 claimed
        response2 = client.post("/emails/process")
        assert response2.status_code == 200
        assert response2.json()["claimed"] == 0
