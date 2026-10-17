"""Google Gmail OAuth 2.0 (read-only) for a single-user local setup."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

STATE_COOKIE = "oauth_state"
CODE_VERIFIER_COOKIE = "oauth_code_verifier"
STATE_MAX_AGE = 600  # seconds


class GmailAuthError(RuntimeError):
    """Raised for missing config or OAuth failures the caller should handle."""


@dataclass(frozen=True)
class GmailAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    token_path: str

    @classmethod
    def from_env(cls) -> "GmailAuthSettings":
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")

        missing = [
            name
            for name, value in (
                ("GOOGLE_CLIENT_ID", client_id),
                ("GOOGLE_CLIENT_SECRET", client_secret),
                ("GOOGLE_REDIRECT_URI", redirect_uri),
            )
            if not value
        ]
        if missing:
            raise GmailAuthError(f"Missing required env var(s): {', '.join(missing)}")

        root = Path(__file__).resolve().parents[2]
        token_path = Path(os.getenv("GMAIL_TOKEN_PATH", "token.json"))
        if not token_path.is_absolute():
            token_path = root / token_path

        return cls(
            client_id=client_id,  # type: ignore[arg-type]
            client_secret=client_secret,  # type: ignore[arg-type]
            redirect_uri=redirect_uri,  # type: ignore[arg-type]
            token_path=str(token_path),
        )


def generate_state() -> str:
    """Random, unguessable value used to prevent CSRF on the callback."""
    return secrets.token_urlsafe(32)


def build_flow(settings: GmailAuthSettings, code_verifier: str | None = None) -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=settings.redirect_uri,
        code_verifier=code_verifier,
    )


def authorization_url(settings: GmailAuthSettings, state: str) -> tuple[str, str]:
    """Build the Google consent URL requesting read-only + offline access.

    Returns ``(url, code_verifier)``; the caller must persist the verifier
    between the start of the flow and the token exchange (PKCE).
    """
    flow = build_flow(settings)
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return url, flow.code_verifier


def save_credentials(settings: GmailAuthSettings, credentials: Credentials) -> None:
    """Persist tokens to disk with owner-only permissions (0600)."""
    path = Path(settings.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json())
    os.chmod(path, 0o600)


def load_credentials(settings: GmailAuthSettings) -> Credentials | None:
    """Read the stored token, or return None if absent/corrupt."""
    path = Path(settings.token_path)
    if not path.exists():
        return None
    try:
        return Credentials.from_authorized_user_info(
            json.loads(path.read_text()), scopes=SCOPES
        )
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def exchange_code(
    settings: GmailAuthSettings, code: str, code_verifier: str | None = None
) -> Credentials:
    """Exchange the OAuth callback code, store the token, return credentials."""
    flow = build_flow(settings, code_verifier=code_verifier)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise GmailAuthError(f"Token exchange failed: {exc}") from exc
    credentials = flow.credentials
    save_credentials(settings, credentials)
    return credentials


def get_credentials(settings: GmailAuthSettings) -> Credentials:
    """Load the saved token, refreshing it via the refresh token if expired."""
    credentials = load_credentials(settings)
    if credentials is None:
        raise GmailAuthError("No stored Gmail token. Complete the OAuth flow first.")
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_credentials(settings, credentials)
    if not credentials.valid:
        raise GmailAuthError("Stored Gmail credentials are invalid. Re-run OAuth.")
    return credentials
