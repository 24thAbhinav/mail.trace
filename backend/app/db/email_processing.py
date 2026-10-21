# email_processing.py - status-transition helpers for the raw ``emails`` table.
#
# Gmail sync writes rows as ``PENDING``; app/services/email_processor.py claims
# them (``PENDING`` -> ``PROCESSING``) and marks them ``COMPLETED`` / ``FAILED``
# as it drains them.

from __future__ import annotations

from sqlmodel import Session, col, select

from app.db.models import Email, EmailStatus, utcnow


def claim_pending_emails(
    session: Session,
    limit: int = 10,
    *,
    include_failed: bool = False,
) -> list[Email]:
    """Atomically claim up to ``limit`` pending emails, marking them processing.

    On PostgreSQL ``FOR UPDATE SKIP LOCKED`` ensures concurrent workers claim
    disjoint batches; on SQLite the lock is a no-op (single-threaded tests).
    """
    if include_failed:
        condition = col(Email.status).in_([EmailStatus.PENDING, EmailStatus.FAILED])
    else:
        condition = Email.status == EmailStatus.PENDING

    statement = (
        select(Email)
        .where(condition)
        .order_by(col(Email.id))
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


def claim_email_by_message_id(
    session: Session,
    message_id: str,
) -> Email | None:
    """Atomically claim a specific email by its message_id, marking it processing."""
    statement = (
        select(Email)
        .where(Email.message_id == message_id)
        .with_for_update(skip_locked=True)
    )
    email = session.exec(statement).first()
    if email:
        email.status = EmailStatus.PROCESSING
        email.updated_at = utcnow()
        session.commit()
    return email
