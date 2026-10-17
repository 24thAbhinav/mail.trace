import os
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TypedDict, cast

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, SecretStr

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

api_key = os.getenv("OPENCODE_API_KEY")
llm = ChatOpenAI(
    model="mimo-v2.5",
    api_key=SecretStr(api_key) if api_key else None,
    base_url="https://opencode.ai/zen/go/v1",
    default_headers={"x-opencode-session": str(uuid.uuid4())},
    max_retries=3,
)


# Status Enum
class ApplicationStatus(str, Enum):
    RECOMMENDED = "RECOMMENDED"  # Suggested job (not yet applied)
    APPLIED = "APPLIED"
    UNDER_REVIEW = "UNDER_REVIEW"
    OA = "OA"
    INTERVIEW = "INTERVIEW"
    INTERVIEW_PASSED = "INTERVIEW_PASSED"
    INTERVIEW_REJECTED = "INTERVIEW_REJECTED"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED = "CLOSED"


# State
class ApplicationState(TypedDict, total=False):
    email_subject: str
    email_body: str
    source_email_id: str | None
    sender_email: str | None
    received_at: datetime | None
    is_application_email: bool
    company: str | None
    role: str | None
    status: ApplicationStatus | None
    summary: str | None
    action_url: str | None
    event_date: str | None


# Structured output schemas
class ApplicationExtraction(BaseModel):
    company: str
    role: str
    status: ApplicationStatus
    summary: str | None = Field(
        default=None,
        description="Concise 1-2 sentence summary of this email's status update or required action.",
    )
    action_url: str | None = Field(
        default=None,
        description="Direct URL link for interview (Zoom/Meet/Teams), online assessment (HackerRank/CodeSignal/portal), or scheduling calendar if present in the email.",
    )
    event_date: str | None = Field(
        default=None,
        description="Date and/or time mentioned for the interview, test deadline, or next step if specified in the email (e.g. 'Sept 25, 2026, 3:00 PM IST', 'Within 48 hours').",
    )


class Classifier(BaseModel):
    is_application_email: bool


# LLM variants
structured_llm = llm.with_structured_output(ApplicationExtraction)
classifier_llm = llm.with_structured_output(Classifier)


# Node result types
class ClassificationResult(TypedDict):
    is_application_email: bool


class ExtractionResult(TypedDict):
    company: str
    role: str
    status: ApplicationStatus
    summary: str | None
    action_url: str | None
    event_date: str | None


# Nodes
def classify_email(state: ApplicationState) -> ClassificationResult:
    try:
        result = cast(
            Classifier,
            classifier_llm.invoke(
                f"""Analyze whether this email is related to a job application
(e.g. confirmation, status update, rejection, offer, OA, interview invite).

Subject: {state.get("email_subject", "")}

Body:
{state.get("email_body", "")}
"""
            ),
        )
        return {"is_application_email": result.is_application_email}
    except Exception as exc:
        # Proxy returned a malformed response (e.g. choices=null). Log and skip
        # this email rather than crashing the graph.
        print(f"[classify_email] LLM error, skipping email: {exc!r}")
        return {"is_application_email": False}


def extract(state: ApplicationState) -> ExtractionResult:
    result = cast(
        ApplicationExtraction,
        structured_llm.invoke(
            f"""You are a job application tracker.
Extract the following details from the email:
- company: Name of the company
- role: Job title / role applied for
- status: One of {[s.value for s in ApplicationStatus if s is not ApplicationStatus.CLOSED]}
  RECOMMENDED = email suggesting a job to apply for (from LinkedIn, Indeed, Naukri, etc.) — not yet applied
  UNDER_REVIEW = application received / under review
  OA = online assessment / coding test invitation
  INTERVIEW / INTERVIEW_PASSED / INTERVIEW_REJECTED = interview stages
  OFFER = job offer extended
  REJECTED = application rejected / not selected
  Never use CLOSED; that status is set by the user only.
- summary: A concise 1-2 sentence summary describing the email update or instructions (e.g. 'Received OA link for technical round', 'Invited to 45-min technical interview').
- action_url: The primary link for the candidate if available in the text (e.g. interview link, test link, scheduling calendar link, portal login).
- event_date: Any specific scheduled date/time or deadline mentioned for the action (e.g. 'Sept 25, 2026 at 3:00 PM', 'Complete by Sept 22').

Subject: {state.get("email_subject", "")}

Body:
{state.get("email_body", "")}
"""
        ),
    )
    return {
        "company": result.company,
        "role": result.role,
        "status": result.status,
        "summary": result.summary,
        "action_url": result.action_url,
        "event_date": result.event_date,
    }


# Conditional routing
def should_process(state: ApplicationState) -> str:
    return "process" if state.get("is_application_email") else "ignore"


# Graph assembly
graph = StateGraph(ApplicationState)  # type: ignore[bad-specialization]

graph.add_node("classify_email", classify_email)
graph.add_node("extract", extract)

graph.add_edge(START, "classify_email")
graph.add_conditional_edges(
    "classify_email",
    should_process,
    {
        "process": "extract",
        "ignore": END,
    },
)
graph.add_edge("extract", END)

workflow = graph.compile()


# Test invocation
if __name__ == "__main__":
    test_state: ApplicationState = {
        "email_subject": "Application Update - Software Engineer Intern",
        "email_body": """
            Thank you for applying to Acme Corp for the Software Engineer Intern role.
            Your application is currently under review.
        """,
        "source_email_id": "test-email-1",
        "sender_email": "recruiter@acme.com",
    }
    result = workflow.invoke(test_state)
    print("Graph execution result:")
    print(result)
