"""SQLModel ORM models for the mail.trace schema.

Three tables:

* ``applications``       -- one row per job opportunity (the "first entry")
* ``application_events`` -- the "updates" table (one row per follow-up email)
* ``processed_emails``   -- ingestion dedup / audit log
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime
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
