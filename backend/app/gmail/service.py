import os

from googleapiclient.discovery import build
from sqlmodel import Session

from app.gmail.oauth import GmailAuthSettings, get_credentials
from app.gmail.sync import ensure_cursor


def build_service(settings: GmailAuthSettings, session: Session | None = None):
    """Build an authenticated Gmail API client."""
    credentials = get_credentials(settings, session=session)
    return build("gmail", "v1", credentials=credentials)


def register_watch(settings: GmailAuthSettings, session: Session) -> dict:
    """Register (or renew) the Gmail push watch and persist the initial cursor.

    The initial ``historyId`` is stored only when no cursor exists for the
    mailbox yet; renewals never overwrite an existing cursor.
    """
    service = build_service(settings, session=session)

    watch_resp = (
        service.users()
        .watch(userId="me", body={"topicName": os.environ["GMAIL_PUBSUB_TOPIC"]})
        .execute()
    )

    profile = service.users().getProfile(userId="me").execute()
    email_address = profile["emailAddress"]

    ensure_cursor(session, email_address, str(watch_resp["historyId"]))
    session.commit()

    return watch_resp
