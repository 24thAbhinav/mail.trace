"""Shared test fixtures: in-memory SQLite engine and a fake Gmail client."""

from __future__ import annotations

import base64

import pytest
from googleapiclient.errors import HttpError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import models  # noqa: F401  (registers tables on SQLModel.metadata)
from app.db.models import Email, SyncState


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _History:
    def __init__(self, g):
        self.g = g

    def list(self, **kwargs):
        self.g.history_list_kwargs.append(kwargs)
        return _Exec(self.g._history_page)


class _Messages:
    def __init__(self, g):
        self.g = g

    def list(self, **kwargs):
        self.g.messages_list_kwargs.append(kwargs)
        return _Exec(self.g._list_page)

    def get(self, **kwargs):
        self.g.messages_get_ids.append(kwargs["id"])
        return _Exec(lambda: self.g._message_page(self.g.messages_get_ids[-1]))


class _Users:
    def __init__(self, g):
        self.g = g

    def history(self):
        return _History(self.g)

    def messages(self):
        return _Messages(self.g)

    def getProfile(self, **kwargs):
        return _Exec(lambda: self.g.profile_response)


class FakeGmail:
    """Minimal stand-in for the googleapiclient Gmail resource.

    Drive behavior via the public attributes; inspect calls via the ``*_kwargs``
    / ``*_ids`` lists.
    """

    def __init__(self):
        self.history_pages: list[dict] = []
        self.history_error: Exception | None = None
        self.history_list_kwargs: list[dict] = []

        self.messages: dict[str, dict] = {}
        self.message_errors: dict[str, Exception] = {}
        self.messages_get_ids: list[str] = []

        self.list_pages: list[dict] = []
        self.messages_list_kwargs: list[dict] = []

        self.profile_response = {"emailAddress": "user@example.com", "historyId": "999"}

    def users(self):
        return _Users(self)

    def _history_page(self):
        if self.history_error:
            raise self.history_error
        return self.history_pages.pop(0) if self.history_pages else {"history": []}

    def _list_page(self):
        return self.list_pages.pop(0) if self.list_pages else {"messages": []}

    def _message_page(self, message_id):
        if message_id in self.message_errors:
            raise self.message_errors[message_id]
        return self.messages[message_id]


def make_message(
    message_id,
    thread_id=None,
    sender=None,
    recipient=None,
    subject=None,
    date=None,
    body=None,
):
    headers = []
    for name, value in (
        ("From", sender),
        ("To", recipient),
        ("Subject", subject),
        ("Date", date),
    ):
        if value is not None:
            headers.append({"name": name, "value": value})

    payload = {"headers": headers}
    if body is not None:
        payload["body"] = {
            "data": base64.urlsafe_b64encode(body.encode()).decode(),
            "size": len(body),
        }
    return {"id": message_id, "threadId": thread_id, "payload": payload}


def history_record(*message_ids):
    return {"messagesAdded": [{"message": {"id": mid}} for mid in message_ids]}


def make_404():
    class _Resp:
        status = 404
        reason = "Not Found"

    return HttpError(_Resp(), b"History id not found")


@pytest.fixture()
def fake_gmail():
    return FakeGmail()
