"""SQLModel ORM models for the mail.trace schema.

Six tables:

* ``applications``       -- one row per job opportunity (the "first entry")
* ``application_events`` -- the "updates" table (one row per follow-up email)
* ``processed_emails``   -- ingestion dedup / audit log
* ``sync_state``         -- per-mailbox Gmail history cursor for incremental sync
* ``emails``             -- raw synced emails (deduped by Gmail message id)
* ``gmail_tokens``       -- persisted OAuth credentials (survives restarts)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


class ApplicationStatus(str, Enum):
    RECOMMENDED = "RECOMMENDED"  # suggested by a platform (not yet applied)
    APPLIED = "APPLIED"
    UNDER_REVIEW = "UNDER_REVIEW"
    OA = "OA"
    INTERVIEW = "INTERVIEW"
    INTERVIEW_PASSED = "INTERVIEW_PASSED"
    INTERVIEW_REJECTED = "INTERVIEW_REJECTED"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED = "CLOSED"  # set by the user only, never the LLM


class Application(SQLModel, table=True):
    """One row per job opportunity. Holds a denormalized latest snapshot."""

    __tablename__ = "applications"

    id: int | None = Field(default=None, primary_key=True)

    company: str = Field(index=True)
    role: str = Field(index=True)
    # normalize(company) + "|" + normalize(role): the key used to match
    # follow-up emails to this application instead of creating duplicates.
    match_key: str = Field(index=True)

    status: ApplicationStatus = Field(index=True)

    applied_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )

    is_closed: bool = Field(default=False)
    closed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    previous_status: ApplicationStatus | None = Field(default=None)

    # Denormalized latest snapshot so the list view doesn't need to join events.
    latest_note: str | None = None
    latest_action_url: str | None = None
    latest_event_date: str | None = None

    source_email_id: str | None = None  # the first email that created this row
    sender_email: str | None = None


class ApplicationEvent(SQLModel, table=True):
    """One follow-up email update for an application (timeline entry)."""

    __tablename__ = "application_events"

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(
        foreign_key="applications.id", index=True, ondelete="CASCADE"
    )

    status: ApplicationStatus

    note: str | None = None
    action_url: str | None = None
    event_date: str | None = None

    # Unique so a given email produces at most one event (idempotent re-sync).
    source_email_id: str = Field(unique=True, index=True)
    sender_email: str | None = None

    received_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )


class ProcessedEmail(SQLModel, table=True):
    """Every email that has been through the pipeline, for dedup + audit."""

    __tablename__ = "processed_emails"

    id: int | None = Field(default=None, primary_key=True)

    message_id: str = Field(unique=True, index=True)  # Gmail message id
    thread_id: str | None = None

    is_application_email: bool  # classifier verdict

    application_id: int | None = Field(
        default=None, foreign_key="applications.id", ondelete="SET NULL"
    )
    event_id: int | None = Field(
        default=None, foreign_key="application_events.id", ondelete="SET NULL"
    )

    verdict: str  # "processed" | "skipped" | "error"
    error: str | None = None

    processed_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )


class SyncState(SQLModel, table=True):
    """Persistent Gmail history cursor, one row per watched mailbox."""

    __tablename__ = "sync_state"

    id: int | None = Field(default=None, primary_key=True)
    email_address: str = Field(unique=True, index=True)
    # Gmail history IDs are opaque, monotonic integers; keep as string to avoid
    # precision loss across API calls and comparisons.
    history_id: str

    updated_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )


class EmailStatus(str, Enum):
    """Processing state for a persisted raw email.

    Sync only writes ``PENDING``; a future async worker transitions through
    ``PROCESSING`` -> ``COMPLETED`` / ``FAILED``.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Email(SQLModel, table=True):
    """A raw email fetched during Gmail sync, deduped by message id.

    Kept separate from ``Application`` (structured job records) so sync can
    ingest emails without running the LangGraph workflow.
    """

    __tablename__ = "emails"

    id: int | None = Field(default=None, primary_key=True)

    message_id: str = Field(unique=True, index=True)  # Gmail message id
    thread_id: str | None = None

    sender_email: str | None = None
    recipient_email: str | None = None
    subject: str | None = None
    date: str | None = None  # raw "Date" header value
    body: str | None = None

    received_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )

    status: EmailStatus = Field(default=EmailStatus.PENDING, index=True)
    error: str | None = None
    updated_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )


class GmailToken(SQLModel, table=True):
    """Persisted Google OAuth credentials, one row per Gmail account.

    Storing the token JSON in Postgres means it survives server restarts
    (important for ephemeral platforms like Render's free tier). The
    ``credentials_json`` column holds the raw JSON string returned by
    ``Credentials.to_json()`` so the full token — including the refresh
    token — is preserved intact.
    """

    __tablename__ = "gmail_tokens"

    id: int | None = Field(default=None, primary_key=True)

    # The Gmail address this token belongs to (e.g. "user@gmail.com").
    # Unique so each account has exactly one active row.
    email_address: str = Field(unique=True, index=True)

    # Full JSON blob from google.oauth2.credentials.Credentials.to_json().
    credentials_json: str = Field(sa_type=Text)

    updated_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )
