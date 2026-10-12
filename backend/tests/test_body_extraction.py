"""Tests for Gmail email body extraction."""

from __future__ import annotations

import base64

from app.gmail.sync import extract_email_fields


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _part(mime_type, text=None, parts=None):
    p = {"mimeType": mime_type}
    if parts is not None:
        p["parts"] = parts
    elif text is not None:
        p["body"] = {"data": _b64(text), "size": len(text)}
    return p


def _body(payload):
    return extract_email_fields({"id": "m1", "payload": payload}).body


def test_simple_text_plain():
    assert _body(_part("text/plain", "Hello, world!")) == "Hello, world!"


def test_multipart_alternative_prefers_plain():
    payload = _part(
        "multipart/alternative",
        parts=[
            _part("text/plain", "Plain text version"),
            _part("text/html", "<p>HTML version</p>"),
        ],
    )
    assert _body(payload) == "Plain text version"


def test_nested_mime_parts():
    payload = _part(
        "multipart/mixed",
        parts=[
            _part(
                "multipart/alternative",
                parts=[
                    _part("text/plain", "Nested plain text"),
                    _part("text/html", "<b>Nested html</b>"),
                ],
            ),
            # attachment-like part with no decodable text body
            {"mimeType": "image/png", "body": {"attachmentId": "abc"}},
        ],
    )
    assert _body(payload) == "Nested plain text"


def test_html_only_converts_to_text():
    payload = _part(
        "text/html",
        "<html><body><p>Hello <b>World</b></p><p>Second line</p></body></html>",
    )
    assert _body(payload) == "Hello World Second line"


def test_placeholder_plain_falls_back_to_html():
    payload = _part(
        "multipart/alternative",
        parts=[
            _part("text/plain", "Please Enable HTML"),
            _part("text/html", "<p>Actual message content</p>"),
        ],
    )
    assert _body(payload) == "Actual message content"


def test_missing_body_data_returns_none():
    assert _body(_part("text/plain", None)) is None


def test_empty_body_data_returns_none():
    payload = {"mimeType": "text/plain", "body": {"data": "", "size": 0}}
    assert _body(payload) is None


def test_unpadded_urlsafe_base64():
    text = "unpadded payload"
    unpadded = _b64(text).rstrip("=")
    payload = {"mimeType": "text/plain", "body": {"data": unpadded}}
    assert _body(payload) == text
