"""Tests for raw-email processing-status transitions."""

from app.db.email_processing import claim_pending_emails, mark_email_status
from app.db.models import Email, EmailStatus


def _add(session, message_id, status=EmailStatus.PENDING):
    email = Email(message_id=message_id, subject=f"subject {message_id}", status=status)
    session.add(email)
    session.commit()
    session.refresh(email)
    return email


def test_claim_pending_marks_processing(session):
    _add(session, "m1")
    _add(session, "m2")

    claimed = claim_pending_emails(session, limit=10)

    assert {e.message_id for e in claimed} == {"m1", "m2"}
    assert all(e.status == EmailStatus.PROCESSING for e in claimed)


def test_claim_pending_respects_limit_and_skips_non_pending(session):
    _add(session, "m1")
    _add(session, "m2")
    _add(session, "m3", status=EmailStatus.COMPLETED)

    claimed = claim_pending_emails(session, limit=1)

    assert len(claimed) == 1
    assert claimed[0].status == EmailStatus.PROCESSING


def test_mark_completed(session):
    email = _add(session, "m1")

    updated = mark_email_status(session, email, EmailStatus.COMPLETED)

    assert updated.status == EmailStatus.COMPLETED
    assert updated.error is None


def test_mark_failed_records_error(session):
    email = _add(session, "m1")

    updated = mark_email_status(session, email, EmailStatus.FAILED, error="boom")

    assert updated.status == EmailStatus.FAILED
    assert updated.error == "boom"
