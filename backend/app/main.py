"""FastAPI application entry point (minimal: DB init + Gmail OAuth)."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.db.database import create_db_and_tables
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Job Tracker", lifespan=lifespan)

auth_router = APIRouter(prefix="/auth", tags=["auth"])


def get_settings() -> GmailAuthSettings:
    try:
        return GmailAuthSettings.from_env()
    except GmailAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@auth_router.get("/google")
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


@auth_router.get("/google/callback")
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
        raise HTTPException(status_code=400, detail="Invalid or missing 'state' parameter.")

    code_verifier = request.cookies.get(CODE_VERIFIER_COOKIE)
    try:
        exchange_code(settings, code, code_verifier)
    except GmailAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = JSONResponse({"status": "authorized"})
    response.delete_cookie(STATE_COOKIE)
    response.delete_cookie(CODE_VERIFIER_COOKIE)
    return response


app.include_router(auth_router)
