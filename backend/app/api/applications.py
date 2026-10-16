"""Read/write API for tracked job applications.

Response shapes match the frontend's TypeScript types
(``frontend/src/types/index.ts``): the denormalized ``latest_*`` columns on the
``Application`` model are exposed as ``notes`` / ``action_url`` / ``event_date``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.db.database import get_session
from app.db.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    utcnow,
)

router = APIRouter(prefix="/applications", tags=["applications"])

MAX_LIMIT = 200


class ApplicationRead(BaseModel):
    id: int
    company: str
    role: str
    status: ApplicationStatus
    applied_at: datetime | None
    updated_at: datetime
    source_email_id: str | None
    sender_email: str | None
    notes: str | None
    action_url: str | None
    event_date: str | None
    is_closed: bool
    closed_at: datetime | None


class ApplicationEventRead(BaseModel):
    id: int
    application_id: int
    status: ApplicationStatus
    note: str | None
    action_url: str | None
    event_date: str | None
    created_at: datetime


class ApplicationList(BaseModel):
    items: list[ApplicationRead]
    total: int
    limit: int
    offset: int


class ApplicationStats(BaseModel):
    total: int
    closed: int
    by_status: dict[str, int]


class ApplicationUpdate(BaseModel):
    status: ApplicationStatus | None = None
    is_closed: bool | None = None
    notes: str | None = None


def _to_read(app_record: Application) -> ApplicationRead:
    return ApplicationRead(
        id=app_record.id,  # type: ignore[arg-type]
        company=app_record.company,
        role=app_record.role,
        status=app_record.status,
        applied_at=app_record.applied_at,
        updated_at=app_record.updated_at,
        source_email_id=app_record.source_email_id,
        sender_email=app_record.sender_email,
        notes=app_record.latest_note,
        action_url=app_record.latest_action_url,
        event_date=app_record.latest_event_date,
        is_closed=app_record.is_closed,
        closed_at=app_record.closed_at,
    )


def _to_event_read(event: ApplicationEvent) -> ApplicationEventRead:
    return ApplicationEventRead(
        id=event.id,  # type: ignore[arg-type]
        application_id=event.application_id,
        status=event.status,
        note=event.note,
        action_url=event.action_url,
        event_date=event.event_date,
        created_at=event.created_at,
    )


@router.get("/stats", response_model=ApplicationStats)
def application_stats(session: Session = Depends(get_session)) -> ApplicationStats:
    """Counts for the dashboard cards and status tabs.

    ``total`` excludes closed applications; closed ones are bucketed under
    ``by_status["CLOSED"]`` so the tab count matches what the list shows.
    """
    rows = session.exec(
        select(Application.status, Application.is_closed)
    ).all()

    by_status: dict[str, int] = {}
    closed = 0
    for status, is_closed in rows:
        key = "CLOSED" if is_closed else getattr(status, "value", str(status))
        by_status[key] = by_status.get(key, 0) + 1
        if is_closed:
            closed += 1

    return ApplicationStats(total=len(rows) - closed, closed=closed, by_status=by_status)


@router.get("", response_model=ApplicationList)
def list_applications(
    status: str | None = None,
    company: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_closed: bool = True,
    session: Session = Depends(get_session),
) -> ApplicationList:
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    conditions = []
    if status == "CLOSED":
        conditions.append(Application.is_closed == True)  # noqa: E712
    elif status:
        try:
            status_enum = ApplicationStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status: {status}")
        conditions.append(Application.status == status_enum)
        conditions.append(Application.is_closed == False)  # noqa: E712
    elif not include_closed:
        conditions.append(Application.is_closed == False)  # noqa: E712

    if company:
        conditions.append(col(Application.company).ilike(f"%{company}%"))
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                col(Application.company).ilike(pattern),
                col(Application.role).ilike(pattern),
            )
        )

    total = session.exec(
        select(func.count()).select_from(Application).where(*conditions)
    ).one()
    rows = session.exec(
        select(Application)
        .where(*conditions)
        .order_by(col(Application.updated_at).desc())
        .offset(offset)
        .limit(limit)
    ).all()

    return ApplicationList(
        items=[_to_read(app_record) for app_record in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{application_id}", response_model=ApplicationRead)
def get_application(
    application_id: int, session: Session = Depends(get_session)
) -> ApplicationRead:
    app_record = session.get(Application, application_id)
    if app_record is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return _to_read(app_record)


@router.get("/{application_id}/events", response_model=list[ApplicationEventRead])
def get_application_events(
    application_id: int, session: Session = Depends(get_session)
) -> list[ApplicationEventRead]:
    if session.get(Application, application_id) is None:
        raise HTTPException(status_code=404, detail="Application not found")
    events = session.exec(
        select(ApplicationEvent)
        .where(ApplicationEvent.application_id == application_id)
        .order_by(col(ApplicationEvent.created_at))
    ).all()
    return [_to_event_read(event) for event in events]


@router.patch("/{application_id}", response_model=ApplicationRead)
def update_application(
    application_id: int,
    payload: ApplicationUpdate,
    session: Session = Depends(get_session),
) -> ApplicationRead:
    app_record = session.get(Application, application_id)
    if app_record is None:
        raise HTTPException(status_code=404, detail="Application not found")

    if payload.status is not None and payload.status != app_record.status:
        app_record.previous_status = app_record.status
        app_record.status = payload.status
    if payload.is_closed is not None and payload.is_closed != app_record.is_closed:
        app_record.is_closed = payload.is_closed
        app_record.closed_at = utcnow() if payload.is_closed else None
    if payload.notes is not None:
        app_record.latest_note = payload.notes

    app_record.updated_at = utcnow()
    session.add(app_record)
    session.commit()
    session.refresh(app_record)
    return _to_read(app_record)
