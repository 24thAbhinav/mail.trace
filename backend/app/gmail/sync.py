"""Incremental Gmail synchronization over the History API.

The cursor is a Gmail ``historyId`` stored per mailbox in ``sync_state``.
``sync_history`` pages through ``history.list``, fetches every message that was
added, persists it (deduped by message id), and only then advances the cursor --
all inside a single transaction so a mid-sync failure leaves the cursor
untouched. Concurrent notifications for the same mailbox are serialized with a
``SELECT ... FOR UPDATE`` on the cursor row (Postgres only).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from html.parser import HTMLParser

from googleapiclient.errors import HttpError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import Email, SyncState, utcnow


class HistoryExpiredError(RuntimeError):
    """Raised when Gmail rejects ``startHistoryId`` (older than its retention)."""


class GmailSyncError(RuntimeError):
    """Raised when a sync cannot proceed (e.g. no starting cursor)."""


@dataclass(frozen=True)
class EmailFields:
    message_id: str
    thread_id: str | None
    sender_email: str | None
    recipient_email: str | None
    subject: str | None
    date: str | None
    body: str | None


# --- cursor persistence -------------------------------------------------------


def get_cursor(session: Session, email_address: str) -> str | None:
    """Return the stored history cursor for a mailbox, or ``None``."""
    row = session.exec(
        select(SyncState).where(SyncState.email_address == email_address)
    ).first()
    return row.history_id if row else None


def set_cursor(session: Session, email_address: str, history_id: str) -> None:
    """Insert or update the cursor for a mailbox. Caller is responsible for commit."""
    row = session.exec(
        select(SyncState).where(SyncState.email_address == email_address)
    ).first()
    if row is None:
        session.add(SyncState(email_address=email_address, history_id=history_id))
    else:
        row.history_id = history_id
        row.updated_at = utcnow()


def ensure_cursor(session: Session, email_address: str, history_id: str) -> None:
    """Create the cursor only if one does not already exist (watch renewal)."""
    if get_cursor(session, email_address) is None:
        session.add(SyncState(email_address=email_address, history_id=history_id))


def _lock_cursor(session: Session, email_address: str) -> str | None:
    """Read the cursor within a transaction, locking the row for the duration.

    ``with_for_update()`` serializes concurrent syncs for the same mailbox on
    PostgreSQL; on SQLite it is a no-op (tests rely on single-threaded flows).

    Handles the first-sync case where no row exists yet: a placeholder row is
    inserted (racing the unique constraint on ``email_address``) so concurrent
    first syncs still serialize. Returns ``None`` when no real cursor exists.
    """
    row = session.exec(
        select(SyncState)
        .where(SyncState.email_address == email_address)
        .with_for_update()
    ).first()
    if row is None:
        row = SyncState(email_address=email_address, history_id="")
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            row = session.exec(
                select(SyncState)
                .where(SyncState.email_address == email_address)
                .with_for_update()
            ).first()
            if row is None:
                raise
    return row.history_id or None


# --- email extraction ---------------------------------------------------------


def extract_email_fields(message: dict) -> EmailFields:
    """Extract the useful fields from a Gmail ``messages.get`` resource."""
    payload = message.get("payload") or {}
    headers = {h.get("name"): h.get("value") for h in payload.get("headers", [])}
    return EmailFields(
        message_id=message["id"],
        thread_id=message.get("threadId"),
        sender_email=headers.get("From"),
        recipient_email=headers.get("To"),
        subject=headers.get("Subject"),
        date=headers.get("Date"),
        body=_extract_body(payload),
    )


def _extract_body(payload: dict) -> str | None:
    """Return the best available text body from a (possibly nested) MIME payload.

    Preference order:

    1. a meaningful ``text/plain`` part (non-blank and not a placeholder);
    2. a ``text/html`` part, converted to plain text;
    3. any other non-blank ``text/*`` part.

    Returns ``None`` when there is no usable body.
    """
    parts = _collect_text_parts(payload)

    plain = next(
        (
            text
            for mime, text in parts
            if mime == "text/plain" and _is_meaningful_plain(text)
        ),
        None,
    )
    if plain is not None:
        return plain

    html = next((text for mime, text in parts if mime == "text/html"), None)
    if html is not None:
        text = _html_to_text(html)
        if text:
            return text

    return next((text for _, text in parts if text.strip()), None)


def _collect_text_parts(payload: dict) -> list[tuple[str, str]]:
    """Flatten nested MIME parts into ``(mime_type, decoded_text)`` leaf pairs.

    Only leaf parts with decodable body data are returned; attachments and
    empty parts are skipped.
    """
    collected: list[tuple[str, str]] = []
    parts = payload.get("parts") or []
    if parts:
        for part in parts:
            collected.extend(_collect_text_parts(part))
    else:
        data = (payload.get("body") or {}).get("data")
        if data:
            text = _b64url_decode(data)
            if text:
                collected.append((payload.get("mimeType", ""), text))
    return collected


_PLACEHOLDER_MARKER = "please enable html"


def _is_meaningful_plain(text: str) -> bool:
    """True when the plain-text part holds real content rather than a stub."""
    stripped = text.strip()
    if not stripped:
        return False
    return _PLACEHOLDER_MARKER not in stripped.lower()


_BLOCK_TAGS = frozenset(
    {
        "article",
        "blockquote",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


class _HTMLTextExtractor(HTMLParser):
    """Collect visible text from HTML, dropping script/style and treating block
    boundaries as whitespace."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "br":
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return " ".join("".join(parser._chunks).split())


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error):
        return ""


# --- synchronization ----------------------------------------------------------


def sync_history(
    service,
    session: Session,
    email_address: str,
    fallback_history_id: str | None = None,
) -> None:
    """Fetch everything since ``startHistoryId`` and persist it, then advance the cursor.

    The starting point is the stored cursor; ``fallback_history_id`` (e.g. from a
    Pub/Sub notification) is used only when no cursor exists yet. Nothing is
    committed until every page and every message has been processed.
    """
    start_id = _lock_cursor(session, email_address) or fallback_history_id
    if start_id is None:
        raise GmailSyncError(
            f"No stored cursor for {email_address!r} and no fallback history id"
        )

    message_ids: set[str] = set()
    latest_history_id: str | None = None
    page_token: str | None = None

    while True:
        params: dict = {"userId": "me", "startHistoryId": start_id}
        if page_token:
            params["pageToken"] = page_token

        try:
            response = service.users().history().list(**params).execute()
        except HttpError as exc:
            if exc.resp is not None and exc.resp.status == 404:
                raise HistoryExpiredError(
                    f"History id {start_id!r} expired for {email_address!r}"
                ) from exc
            raise

        latest_history_id = response.get("historyId") or latest_history_id
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                message_id = (added.get("message") or {}).get("id")
                if message_id:
                    message_ids.add(message_id)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    for message_id in message_ids:
        _fetch_and_persist(service, session, message_id)

    # Advance the cursor only after all changes have been processed durably.
    if latest_history_id is not None:
        set_cursor(session, email_address, str(latest_history_id))

    session.commit()


def full_sync(service, session: Session, email_address: str) -> None:
    """Safe resynchronization after a history cursor has expired.

    Re-reads the entire mailbox (deduped by message id, so no email is lost) and
    re-anchors the cursor to the mailbox's current history id. The cursor is
    read *before* listing so anything arriving mid-scan is recovered by the
    next incremental sync (never missed, only possibly re-fetched).
    """
    _lock_cursor(session, email_address)  # serialize with concurrent sync_history

    # Anchor the cursor to a point at or before every message we list.
    profile = service.users().getProfile(userId="me").execute()
    history_id = profile.get("historyId")

    message_ids: list[str] = []
    page_token: str | None = None

    while True:
        params: dict = {"userId": "me"}
        if page_token:
            params["pageToken"] = page_token
        response = service.users().messages().list(**params).execute()
        for message in response.get("messages", []):
            if message.get("id"):
                message_ids.append(message["id"])
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    for message_id in message_ids:
        _fetch_and_persist(service, session, message_id)

    if history_id is not None:
        set_cursor(session, email_address, str(history_id))

    session.commit()


def _fetch_and_persist(service, session: Session, message_id: str) -> None:
    if _email_exists(session, message_id):
        return
    message = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()
    fields = extract_email_fields(message)
    session.add(
        Email(
            message_id=fields.message_id,
            thread_id=fields.thread_id,
            sender_email=fields.sender_email,
            recipient_email=fields.recipient_email,
            subject=fields.subject,
            date=fields.date,
            body=fields.body,
        )
    )


def _email_exists(session: Session, message_id: str) -> bool:
    return (
        session.exec(select(Email).where(Email.message_id == message_id)).first()
        is not None
    )
