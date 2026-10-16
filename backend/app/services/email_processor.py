"""Email processing pipeline for mail.trace.

Drains pending raw emails, executes the LangGraph classification/extraction
workflow, persists job applications, timeline events, and audit logs to PostgreSQL,
and idempotently manages email processing statuses.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlmodel import Session, col, select

from app.db.email_processing import claim_email_by_message_id, claim_pending_emails
from app.db.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    Email,
    EmailStatus,
    ProcessedEmail,
    utcnow,
)
from app.graph.graph import ApplicationState, workflow

logger = logging.getLogger(__name__)


def normalize_text(text: str | None) -> str:
    """Lowercase and collapse whitespace for resilient matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


def compute_match_key(company: str, role: str) -> str:
    """Key used to match follow-up emails to an existing application."""
    return f"{normalize_text(company)}|{normalize_text(role)}"


def email_to_state(email: Email) -> ApplicationState:
    """Map a raw Email model to the LangGraph ApplicationState input schema."""
    return {
        "email_subject": email.subject or "",
        "email_body": email.body or "",
        "source_email_id": email.message_id,
        "sender_email": email.sender_email,
        "received_at": email.received_at,
    }


def parse_application_status(val: Any) -> ApplicationStatus:
    """Safely convert any enum or string status to the DB ApplicationStatus enum."""
    if isinstance(val, ApplicationStatus):
        return val
    if hasattr(val, "value"):
        val = val.value
    if isinstance(val, str):
        normalized = val.strip().upper()
        if normalized in ApplicationStatus.__members__:
            return ApplicationStatus[normalized]
    return ApplicationStatus.APPLIED


def find_matching_application(
    session: Session,
    email: Email,
    company: str,
    role: str,
) -> Application | None:
    """Match an incoming email to an existing Application.

    Matching precedence:
    1. Thread match: if an earlier email in the same Gmail thread is already linked
       to an application, use that application.
    2. Exact match key: normalized company + role. Prefer open (non-closed) applications.
    3. Generic role fallback: if the extracted role is generic (e.g. '', 'applicant')
       and an application exists for the same company.
    """
    # 1. Thread match
    if email.thread_id:
        matched_processed = session.exec(
            select(ProcessedEmail)
            .where(
                ProcessedEmail.thread_id == email.thread_id,
                col(ProcessedEmail.application_id).is_not(None),
            )
            .order_by(col(ProcessedEmail.processed_at).desc())
        ).first()
        if matched_processed and matched_processed.application_id:
            app_record = session.get(Application, matched_processed.application_id)
            if app_record:
                return app_record

    norm_company = normalize_text(company)
    norm_role = normalize_text(role)
    if not norm_company:
        return None

    # 2. Match key: prioritize non-closed applications
    match_key = f"{norm_company}|{norm_role}"
    app_record = session.exec(
        select(Application)
        .where(Application.match_key == match_key)
        .order_by(col(Application.is_closed).asc(), col(Application.updated_at).desc())
    ).first()
    if app_record:
        return app_record

    # 3. Generic role fallback
    generic_roles = {"", "applicant", "candidate", "n/a", "unknown", "application", "job application"}
    if norm_role in generic_roles:
        app_record = session.exec(
            select(Application)
            .where(col(Application.match_key).startswith(f"{norm_company}|"))
            .order_by(col(Application.is_closed).asc(), col(Application.updated_at).desc())
        ).first()
        if app_record:
            return app_record

    return None


def _record_processing_failure(
    session: Session,
    email: Email,
    error_message: str,
) -> ProcessedEmail:
    """Record processing failure on the Email and ProcessedEmail audit log."""
    try:
        db_email = session.exec(
            select(Email).where(Email.id == email.id)
        ).first() or email

        db_email.status = EmailStatus.FAILED
        db_email.error = error_message
        db_email.updated_at = utcnow()
        session.add(db_email)

        processed = session.exec(
            select(ProcessedEmail).where(ProcessedEmail.message_id == email.message_id)
        ).first()

        if processed is None:
            processed = ProcessedEmail(
                message_id=email.message_id,
                thread_id=email.thread_id,
                is_application_email=False,
                application_id=None,
                event_id=None,
                verdict="error",
                error=error_message,
                processed_at=utcnow(),
            )
        else:
            processed.verdict = "error"
            processed.error = error_message
            processed.processed_at = utcnow()

        session.add(processed)
        session.commit()
        session.refresh(processed)
        return processed
    except Exception as exc:
        session.rollback()
        logger.exception(
            "Failed to record failure status for email %s: %s", email.message_id, exc
        )
        try:
            email.status = EmailStatus.FAILED
            email.error = error_message
            email.updated_at = utcnow()
            session.add(email)
            session.commit()
        except Exception:
            session.rollback()
        return ProcessedEmail(
            message_id=email.message_id,
            thread_id=email.thread_id,
            is_application_email=False,
            verdict="error",
            error=error_message,
        )


def _process_single_email_tx(
    session: Session,
    email: Email,
    *,
    graph=workflow,
) -> ProcessedEmail:
    """Execute processing transaction for a single email."""
    # 1. Idempotency: Check if already cleanly processed
    existing_processed = session.exec(
        select(ProcessedEmail).where(ProcessedEmail.message_id == email.message_id)
    ).first()

    if existing_processed and existing_processed.verdict in ("processed", "skipped"):
        email.status = EmailStatus.COMPLETED
        email.error = None
        email.updated_at = utcnow()
        session.add(email)
        session.commit()
        session.refresh(existing_processed)
        return existing_processed

    # 2. Idempotency: Check if an ApplicationEvent already exists for this email
    existing_event = session.exec(
        select(ApplicationEvent).where(ApplicationEvent.source_email_id == email.message_id)
    ).first()

    if existing_event:
        app_id = existing_event.application_id
        if existing_processed is None:
            existing_processed = ProcessedEmail(
                message_id=email.message_id,
                thread_id=email.thread_id,
                is_application_email=True,
                application_id=app_id,
                event_id=existing_event.id,
                verdict="processed",
                error=None,
                processed_at=utcnow(),
            )
        else:
            existing_processed.application_id = app_id
            existing_processed.event_id = existing_event.id
            existing_processed.is_application_email = True
            existing_processed.verdict = "processed"
            existing_processed.error = None
            existing_processed.processed_at = utcnow()

        session.add(existing_processed)
        email.status = EmailStatus.COMPLETED
        email.error = None
        email.updated_at = utcnow()
        session.add(email)
        session.commit()
        session.refresh(existing_processed)
        return existing_processed

    # 3. Invoke LangGraph workflow
    input_state = email_to_state(email)
    run_name = f"Email: {email.subject[:50]}" if email.subject else f"Email: {email.message_id}"
    config = {
        "run_name": run_name,
        "tags": ["email-processor", "job-tracker"],
        "metadata": {
            "email_id": email.message_id,
            "sender": email.sender_email,
            "subject": email.subject,
        },
    }
    graph_output = graph.invoke(input_state, config=config)

    is_app_email = bool(graph_output.get("is_application_email"))

    # 4. Handle irrelevant email
    if not is_app_email:
        if existing_processed is None:
            existing_processed = ProcessedEmail(
                message_id=email.message_id,
                thread_id=email.thread_id,
                is_application_email=False,
                application_id=None,
                event_id=None,
                verdict="skipped",
                error=None,
                processed_at=utcnow(),
            )
        else:
            existing_processed.thread_id = email.thread_id
            existing_processed.is_application_email = False
            existing_processed.application_id = None
            existing_processed.event_id = None
            existing_processed.verdict = "skipped"
            existing_processed.error = None
            existing_processed.processed_at = utcnow()

        session.add(existing_processed)
        email.status = EmailStatus.COMPLETED
        email.error = None
        email.updated_at = utcnow()
        session.add(email)
        session.commit()
        session.refresh(existing_processed)
        return existing_processed

    # 5. Handle relevant email
    raw_company = graph_output.get("company") or ""
    raw_role = graph_output.get("role") or ""
    company = raw_company.strip() or "Unknown Company"
    role = raw_role.strip() or "Unknown Role"
    status = parse_application_status(graph_output.get("status"))
    summary = graph_output.get("summary")
    action_url = graph_output.get("action_url")
    event_date = graph_output.get("event_date")

    match_key = compute_match_key(company, role)

    # Check for existing application by thread or match_key
    target_app = find_matching_application(session, email, company, role)

    if target_app is not None:
        target_app.previous_status = target_app.status
        target_app.status = status
        target_app.updated_at = utcnow()
        if summary:
            target_app.latest_note = summary
        if action_url:
            target_app.latest_action_url = action_url
        if event_date:
            target_app.latest_event_date = event_date
        session.add(target_app)
        session.flush()
    else:
        target_app = Application(
            company=company,
            role=role,
            match_key=match_key,
            status=status,
            applied_at=email.received_at or utcnow(),
            updated_at=utcnow(),
            latest_note=summary,
            latest_action_url=action_url,
            latest_event_date=event_date,
            source_email_id=email.message_id,
            sender_email=email.sender_email,
        )
        session.add(target_app)
        session.flush()

    # Create the timeline event
    event = ApplicationEvent(
        application_id=target_app.id,
        status=status,
        note=summary,
        action_url=action_url,
        event_date=event_date,
        source_email_id=email.message_id,
        sender_email=email.sender_email,
        received_at=email.received_at or utcnow(),
        created_at=utcnow(),
    )
    session.add(event)
    session.flush()

    # Create or update ProcessedEmail record
    if existing_processed is None:
        existing_processed = ProcessedEmail(
            message_id=email.message_id,
            thread_id=email.thread_id,
            is_application_email=True,
            application_id=target_app.id,
            event_id=event.id,
            verdict="processed",
            error=None,
            processed_at=utcnow(),
        )
    else:
        existing_processed.thread_id = email.thread_id
        existing_processed.is_application_email = True
        existing_processed.application_id = target_app.id
        existing_processed.event_id = event.id
        existing_processed.verdict = "processed"
        existing_processed.error = None
        existing_processed.processed_at = utcnow()

    session.add(existing_processed)

    # Mark email as completed
    email.status = EmailStatus.COMPLETED
    email.error = None
    email.updated_at = utcnow()
    session.add(email)

    # Commit all writes atomically
    session.commit()
    session.refresh(existing_processed)
    return existing_processed


def process_single_email(
    session: Session,
    email: Email,
    *,
    graph: Any = None,
) -> ProcessedEmail:
    """Process one raw email through the LangGraph workflow and persist results.

    Ensures atomicity: if processing or database insertion fails, changes are
    rolled back and the failure is recorded with EmailStatus.FAILED.
    """
    if graph is None:
        graph = workflow
    try:
        return _process_single_email_tx(session, email, graph=graph)
    except Exception as exc:
        session.rollback()
        logger.exception("Failed processing email %s: %s", email.message_id, exc)
        return _record_processing_failure(session, email, str(exc))


def process_pending_emails(
    session: Session,
    *,
    limit: int = 10,
    retry_failed: bool = False,
    message_id: str | None = None,
    graph: Any = None,
) -> dict[str, int]:
    """Drain pending (or failed) emails and run them through the processing pipeline.

    Returns stats dict: {"claimed": N, "processed": N, "skipped": N, "failed": N}
    """
    if graph is None:
        graph = workflow
    if message_id:
        email = claim_email_by_message_id(session, message_id)
        emails = [email] if email else []
    else:
        emails = claim_pending_emails(session, limit=limit, include_failed=retry_failed)

    stats = {"claimed": len(emails), "processed": 0, "skipped": 0, "failed": 0}

    for email in emails:
        try:
            result = process_single_email(session, email, graph=graph)
            if result.verdict == "processed":
                stats["processed"] += 1
            elif result.verdict == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1

    return stats
