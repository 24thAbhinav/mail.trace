# email_processing.py - minimal status-transition helpers for raw emails.
#
# These are the building blocks a future async worker will use to drain the
# ``emails`` table (no queue service is wired up yet). Gmail sync writes rows as
# ``PENDING``; these functions move them through ``PROCESSING`` -> ``COMPLETED``
# / ``FAILED``.

from __future__ import annotations

from sqlmodel import Session, select

from app.db.models import Email, EmailStatus, utcnow


def claim_pending_emails(session: Session, limit: int = 10) -> list[Email]:
    """Atomically claim up to ``limit`` pending emails, marking them processing.

    On PostgreSQL ``FOR UPDATE SKIP LOCKED`` ensures concurrent workers claim
    disjoint batches; on SQLite the lock is a no-op (single-threaded tests).
    """
    statement = (
        select(Email)
        .where(Email.status == EmailStatus.PENDING)
        .order_by(Email.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    emails = list(session.exec(statement).all())
    for email in emails:
        email.status = EmailStatus.PROCESSING
        email.updated_at = utcnow()
    if emails:
        session.commit()
    return emails


def mark_email_status(
    session: Session,
    email: Email,
    status: EmailStatus,
    *,
    error: str | None = None,
) -> Email:
    """Transition an email to a terminal (or failed) status."""
    email.status = status
    email.error = error
    email.updated_at = utcnow()
    session.add(email)
    session.commit()
    session.refresh(email)
    return email
