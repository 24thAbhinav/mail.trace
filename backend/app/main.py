"""FastAPI application entry point (minimal: DB init + Gmail OAuth)."""

from __future__ import annotations

import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session

from app.db import database
from app.db.database import create_db_and_tables, get_session
from app.gmail.oauth import (
    CODE_VERIFIER_COOKIE,
    STATE_COOKIE,
    STATE_MAX_AGE,
    GmailAuthError,
    GmailAuthSettings,
    authorization_url,
    exchange_code,
    generate_state,
)
from app.gmail.service import build_service, register_watch
from app.gmail.sync import (
    GmailSyncError,
    HistoryExpiredError,
    full_sync,
    sync_history,
)
from app.services.email_processor import process_pending_emails


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Job Tracker", lifespan=lifespan)


def get_settings() -> GmailAuthSettings:
    try:
        return GmailAuthSettings.from_env()
    except GmailAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def get_gmail_service(settings: GmailAuthSettings = Depends(get_settings)):
    """FastAPI dependency that yields an authenticated Gmail API client."""
    return build_service(settings)


logger = logging.getLogger(__name__)

# Upper bound on emails drained per background run, so one run stays bounded.
PROCESS_BATCH_LIMIT = 25


def _drain_pending_emails(limit: int = PROCESS_BATCH_LIMIT) -> None:
    """Process pending raw emails in a fresh, self-closing session.

    Scheduled as a FastAPI background task so the Pub/Sub HTTP response is not
    blocked by (potentially slow) LLM processing. The request-scoped session is
    already closed by the time this runs, so it opens and closes its own
    ``Session`` on the project's engine.
    """
    try:
        with Session(database.engine) as session:
            process_pending_emails(session, limit=limit)
    except Exception:
        logger.exception("Background email processing failed")


@app.get("/auth/google", tags=["auth"])
def google_oauth_start(
    request: Request,
    settings: GmailAuthSettings = Depends(get_settings),
):
    """Redirect to Google's OAuth consent screen."""
    state = generate_state()
    url, code_verifier = authorization_url(settings, state)
    response = RedirectResponse(url)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CODE_VERIFIER_COOKIE,
        code_verifier,
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/auth/google/callback", tags=["auth"])
def google_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    settings: GmailAuthSettings = Depends(get_settings),
):
    """Validate OAuth state, exchange the code, and store the token."""
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code' parameter.")

    expected_state = request.cookies.get(STATE_COOKIE)
    if (
        not state
        or not expected_state
        or not secrets.compare_digest(state, expected_state)
    ):
        raise HTTPException(
            status_code=400, detail="Invalid or missing 'state' parameter."
        )

    code_verifier = request.cookies.get(CODE_VERIFIER_COOKIE)
    try:
        exchange_code(settings, code, code_verifier)
    except GmailAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = JSONResponse({"status": "authorized"})
    response.delete_cookie(STATE_COOKIE)
    response.delete_cookie(CODE_VERIFIER_COOKIE)
    return response


@app.post("/gmail/watch")
def start_gmail_watch(
    settings: GmailAuthSettings = Depends(get_settings),
    session: Session = Depends(get_session),
):
    try:
        return register_watch(settings, session)
    except GmailAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/pubsub/push")
async def pubsub_push(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    service=Depends(get_gmail_service),
):
    try:
        body = await request.json()
        data = body["message"]["data"]
        notification = json.loads(base64.b64decode(data).decode("utf-8"))
        email_address = notification["emailAddress"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid Pub/Sub payload"},
        )

    try:
        # The stored cursor is the authoritative startHistoryId; the
        # notification's historyId is only a fallback for a fresh mailbox.
        sync_history(
            service,
            session,
            email_address,
            fallback_history_id=notification.get("historyId"),
        )
    except HistoryExpiredError:
        # Cursor is too old for incremental sync: recover with a full resync.
        try:
            full_sync(service, session, email_address)
        except Exception:
            session.rollback()
            return JSONResponse(
                status_code=500,
                content={"detail": "Gmail resynchronization failed"},
            )
    except GmailSyncError as exc:
        session.rollback()
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    except Exception:
        session.rollback()
        return JSONResponse(status_code=500, content={"detail": "Gmail sync failed"})

    # Sync succeeded (incremental, or full-sync recovery above). Drain the newly
    # synced emails in the background so the HTTP response is not blocked by
    # LLM processing. The task opens its own session.
    background_tasks.add_task(_drain_pending_emails)

    return {"status": "ok"}


@app.post("/emails/process", tags=["emails"])
def trigger_email_processing(
    limit: int = 10,
    retry_failed: bool = False,
    message_id: str | None = None,
    session: Session = Depends(get_session),
):
    """Trigger processing of pending (or failed) raw emails through the LangGraph pipeline."""
    return process_pending_emails(
        session,
        limit=limit,
        retry_failed=retry_failed,
        message_id=message_id,
    )
