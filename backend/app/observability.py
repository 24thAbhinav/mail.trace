"""LangSmith tracing configuration for the email-processing pipeline.

Tracing is toggled by environment variables read by the LangChain/LangGraph
stack (``LANGSMITH_TRACING`` + ``LANGSMITH_API_KEY``, or the legacy
``LANGCHAIN_TRACING_V2`` / ``LANGCHAIN_API_KEY`` names). Nothing here calls
LangSmith directly; this module only builds the run config that LangGraph
forwards when ``workflow.invoke(..., config=...)`` executes.

``_process_single_email_tx`` invokes the workflow exactly once per raw email, so
**one email processed == one LangSmith trace**, named after the email and tagged
``email-processor``.
"""

from __future__ import annotations

from typing import Any

from app.db.models import Email

_EMAIL_RUN_TAGS = ["email-processor", "job-tracker"]


def email_trace_config(email: Email) -> dict[str, Any]:
    """Build the LangGraph run config so each email maps to exactly one trace.

    ``run_name`` becomes the LangSmith trace name; ``metadata`` makes traces
    searchable by email id / sender / subject without opening each one.
    """
    label = (email.subject or email.message_id or "email").strip()
    return {
        "run_name": f"Email: {label[:80]}",
        "tags": list(_EMAIL_RUN_TAGS),
        "metadata": {
            "email_id": email.message_id,
            "thread_id": email.thread_id,
            "sender": email.sender_email,
            "subject": email.subject,
        },
    }
