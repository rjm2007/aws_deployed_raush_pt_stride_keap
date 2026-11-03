"""Test chat tab for the SMS agent (docs/SMS_AGENT.md "Test chat tab", plan section 12).

A stateless sandbox: the browser sends the made-up lead, the chat so far and the new text; the agent
answers with SandboxTools. Nothing here writes to the database or sends a text: chats live only in the
viewer's browser, so the route stays a thin pass-through to services.sms_agent.reply_sandbox.
"""

from __future__ import annotations

import dataclasses
import importlib
import threading
import time
from collections import deque
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace, trace_id_var
from ..security import DashboardActor, require_dashboard_auth

router = APIRouter(prefix="/api/v1/dashboard/sms-agent-test", tags=["dashboard"])
Actor = Annotated[DashboardActor, Depends(require_dashboard_auth)]

# A text chat rarely passes 20 turns; 60 leaves room for long tests without letting one request
# carry a book. 1600 characters is Twilio's limit for one text (10 segments).
MAX_TURNS = 60
MAX_TEXT_CHARS = 1600
MAX_CHAT_CHARS = 40_000

# Models the tab may pick, for comparing them (plan section 10, step 1h). The owner's list
# (2026-11-02): the Luna / Terra / Sol models are the ones to weigh against gpt-5.5, and 5.4-mini as
# the cheap floor. SMS_AGENT_MODEL is always added first, so a new default never needs a code change.
TEST_MODELS: tuple[str, ...] = (
    "gpt-5.5", "gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol", "gpt-5.4-mini",
)

# leads.status values (001_initial.sql check) with the words staff see. "Needs review" is not a
# status: it is an open review card on a lead in any status.
LEAD_STATUSES: tuple[tuple[str, str], ...] = (
    ("new", "New"),
    ("in_progress", "In cadence"),
    ("callback_scheduled", "Callback scheduled"),
    ("booking_link_sent", "Booking link sent"),
    ("booked", "Booked"),
    ("declined", "Declined"),
    ("transferred_human", "Transferred to front desk"),
    ("needs_attention", "Needs attention"),
    ("invalid_phone", "Invalid phone"),
    ("closed_no_response", "Closed, no response"),
    ("do_not_contact", "Do not contact"),
)
LeadStatus = Literal[
    "new", "in_progress", "callback_scheduled", "booking_link_sent", "booked", "declined",
    "transferred_human", "needs_attention", "invalid_phone", "closed_no_response", "do_not_contact",
]


def allowed_models() -> list[str]:
    default = get_settings().sms_agent_model.strip()
    return list(dict.fromkeys([m for m in (default, *TEST_MODELS) if m]))


def _require_enabled() -> None:
    if not get_settings().sms_agent_test_enabled:
        raise HTTPException(
            status_code=404,
            detail="The SMS agent test chat is turned off (SMS_AGENT_TEST_ENABLED).",
        )


class _RateLimiter:
    """Per-user sliding window. The staff assistant counts rows in dashboard_audit_log, but this
    sandbox promises to write nothing to the database, so the window lives in memory. The API runs as
    one uvicorn process, so one counter is the real count; a restart only forgives a minute."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, user_id: str, limit: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(user_id, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = _RateLimiter()


def _plain_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("text must not be blank")
    return value


def _optional_text(value: str) -> str:
    return value.strip()


class ChatTurn(BaseModel):
    role: Literal["patient", "elliot"]
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)

    _text = field_validator("text")(_plain_text)


class BookedVisitIn(BaseModel):
    """A Booked lead's visit (2026-11-02 eval, bk-02: the sandbox only said "no visit details set")."""

    # Clinic wall time (Pacific), like `clock`.
    at: datetime
    therapist: str = Field(default="", max_length=60)
    # '' = the lead's clinic.
    clinic: str = Field(default="", max_length=120)

    _strip = field_validator("therapist", "clinic")(_optional_text)


# Which review is open on the lead (2026-11-02 eval, ls-01: status Needs attention always meant a full
# review that stops booking). 'auto' = as before, from the status; 'front_desk' = only Elliot's own
# front-desk card (booking by text still works); 'full' = a review that stops booking.
OpenReview = Literal["auto", "none", "front_desk", "full"]


class SandboxSetupIn(BaseModel):
    who: Literal["existing", "new"] = "existing"
    status: LeadStatus = "in_progress"
    clinic: str = Field(default="", max_length=120)
    service: str = Field(default="", max_length=120)
    referral_word: str = Field(default="", max_length=60)
    first_name: str = Field(default="", max_length=60)
    last_name: str = Field(default="", max_length=60)
    date_of_birth: date | None = None
    front_desk: Literal["auto", "open", "closed"] = "auto"
    # Clinic local time to pretend it is (nights, Sundays, holidays); null = now.
    clock: datetime | None = None
    model: str | None = Field(default=None, max_length=80)
    booked_visit: BookedVisitIn | None = None
    open_review: OpenReview = "auto"

    _strip = field_validator(
        "clinic", "service", "referral_word", "first_name", "last_name"
    )(_optional_text)

    @model_validator(mode="after")
    def extras_fit_the_lead(self):
        # A new person has no lead, so no visit and no review; a visit only on a Booked lead.
        if self.who == "new" and self.open_review not in {"auto", "none"}:
            raise ValueError("a new person has no open review")
        if self.booked_visit is not None and (self.who != "existing" or self.status != "booked"):
            raise ValueError("a booked visit needs an existing lead with status booked")
        return self

    def agent_fields(self, model: str) -> dict:
        """The SandboxSetup keyword arguments: the booked visit as the Context line live writes."""
        data = self.model_dump(exclude={"booked_visit"})
        visit = self.booked_visit
        data["booked_visit"] = "" if visit is None else _setup_extras().booked_visit_text(
            visit.at, visit.therapist, visit.clinic or self.clinic)
        data["model"] = model
        return data

    @field_validator("date_of_birth")
    @classmethod
    def sensible_birthday(cls, value: date | None) -> date | None:
        # At least a year back (owner, 2026-11-03): a test lead born "2026-11-01" could never be
        # booked, and Elliot looped asking for the year. The same rule as check_birthday.
        if value is not None and not (date(1900, 1, 1) <= value
                                      and _birthday().possible(value, _birthday().real_today())):
            raise ValueError("the birthday must be a real date of birth, at least 1 year ago "
                             "(and after 1900)")
        return value

    @field_validator("model")
    @classmethod
    def known_model(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value.strip() not in allowed_models():
            raise ValueError("model is not in the test chat's model list")
        return value.strip()


class PriorAction(BaseModel):
    kind: str = Field(default="", max_length=40)
    result_code: str = Field(default="", max_length=60)
    # A hand-off's reason (insurance, different_case, ...), sent back as the reply gave it. The codes
    # alone cannot tell a repeat of the same hand-off from a new one (x-02, 2026-11-02). '' = none.
    reason: str = Field(default="", max_length=40, pattern=r"^\s*[A-Za-z_]*\s*$")

    @field_validator("reason")
    @classmethod
    def plain_reason(cls, value: str) -> str:
        return value.strip().lower()

    def for_agent(self) -> dict:
        """{kind, result_code}, plus reason only when there is one (as older tabs sent it)."""
        data = {"kind": self.kind, "result_code": self.result_code}
        if self.reason:
            data["reason"] = self.reason
        return data


class ReplyRequest(BaseModel):
    setup: SandboxSetupIn
    history: list[ChatTurn] = Field(default_factory=list, max_length=MAX_TURNS - 1)
    message: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    # What Elliot did on the earlier replies (the side panel's codes, oldest first). The sandbox
    # writes nothing, so without these a case change on turn 1 was forgotten and "book me" on turn 2
    # booked where live refuses (2026-11-02 review). A reply can hold several actions.
    prior_actions: list[PriorAction] = Field(default_factory=list, max_length=MAX_TURNS * 10)

    _message = field_validator("message")(_plain_text)

    @model_validator(mode="after")
    def chat_not_too_large(self):
        if sum(len(turn.text) for turn in self.history) + len(self.message) > MAX_CHAT_CHARS:
            raise ValueError("chat is too long; start a new chat")
        return self


class ActionOut(BaseModel):
    kind: str
    summary: str
    result_code: str
    # The hand-off's reason when the agent gives one; the tab sends it back in prior_actions.
    reason: str | None = None


class ReplyResponse(BaseModel):
    texts: list[str]
    actions: list[ActionOut]
    no_reply: bool
    model: str
    ms: int


def _birthday():
    return importlib.import_module(
        f"{__package__.rsplit('.', 1)[0]}.services.sms_agent_birthday")


def _setup_extras():
    return importlib.import_module(
        f"{__package__.rsplit('.', 1)[0]}.services.sms_agent_sandbox_setup")


# Setup fields with these values mean "as before", so an agent without the field may skip them.
_EXTRA_DEFAULTS = {"booked_visit": "", "open_review": "auto"}


def _sandbox_setup(agent, fields: dict):
    """agent.SandboxSetup from the request. A field the agent's SandboxSetup does not know yet is
    dropped only when it holds its default; a chosen visit or review is never silently ignored."""
    known = {f.name for f in dataclasses.fields(agent.SandboxSetup)}
    missing = [name for name, value in fields.items()
               if name not in known and value != _EXTRA_DEFAULTS.get(name, value)]
    if missing:
        raise HTTPException(
            status_code=501,
            detail="This server's test chat cannot set "
                   + " / ".join("Booked visit" if m == "booked_visit" else "Open review"
                                for m in missing) + " yet.",
        )
    return agent.SandboxSetup(**{k: v for k, v in fields.items() if k in known})


def _agent():
    # Imported on first use so the API starts (and the rest of the dashboard works) even when the
    # agent module or its OpenAI dependency fails to import; tests replace this function.
    return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.services.sms_agent")


@router.get("/options")
def sms_agent_test_options(actor: Actor):
    _require_enabled()
    # Read-only: active clinics and services, the same rows the booking engine uses.
    with transaction() as conn:
        # Postgres refuses any write in this transaction, so the "writes nothing" promise holds.
        conn.execute("set transaction read only")
        clinics = conn.execute(
            "select name from booking_locations where practice_id=%s and is_active order by name",
            (actor.practice_id,),
        ).fetchall()
        services = conn.execute(
            "select name, is_default from case_types where practice_id=%s and is_active "
            "order by is_default desc, name",
            (actor.practice_id,),
        ).fetchall()
    models = allowed_models()
    return {
        "clinics": [row["name"] for row in clinics],
        "services": [row["name"] for row in services],
        "models": models,
        "default_model": models[0],
        "statuses": [{"value": value, "label": label} for value, label in LEAD_STATUSES],
        "max_turns": MAX_TURNS,
        "max_text_chars": MAX_TEXT_CHARS,
        # The tab warns before these and offers a fresh chat; a 422 at the limit cannot be retried.
        "max_chat_chars": MAX_CHAT_CHARS,
    }


@router.post("/reply", response_model=ReplyResponse)
def sms_agent_test_reply(payload: ReplyRequest, actor: Actor):
    _require_enabled()
    settings = get_settings()
    trace = WorkflowTrace("sms_agent_test", "api", trace_id_var.get())
    # Counts and choices only: never the chat text, names or birthday.
    trace.log(
        "request_parsed",
        actor_id=actor.user_id,
        who=payload.setup.who,
        open_review=payload.setup.open_review,
        booked_visit=payload.setup.booked_visit is not None,
        turn_count=len(payload.history) + 1,
    )
    if not rate_limiter.allow(actor.user_id, settings.sms_agent_test_requests_per_minute):
        trace.log("rate_limited")
        raise HTTPException(
            status_code=429,
            detail="Too many test messages. Wait a minute and try again.",
            headers={"Retry-After": "60"},
        )
    model = payload.setup.model or allowed_models()[0]
    started = time.monotonic()
    try:
        agent = _agent()
        setup = _sandbox_setup(agent, payload.setup.agent_fields(model))
        history = [agent.Turn(role=t.role, text=t.text, at=None) for t in payload.history]
        history.append(agent.Turn(role="patient", text=payload.message, at=None))
        prior = [action.for_agent() for action in payload.prior_actions]
        reply = agent.reply_sandbox(setup=setup, history=history, settings=settings,
                                    prior_actions=prior)
    except HTTPException:
        raise
    except ImportError as exc:
        trace.fail(exc)
        raise HTTPException(status_code=503, detail="The SMS agent is not available.") from None
    except Exception as exc:  # noqa: BLE001 - any agent failure is one plain 502; no detail leaks
        trace.fail(exc)
        raise HTTPException(
            status_code=502, detail="Elliot could not answer just now. Please try again."
        ) from None
    ms = round((time.monotonic() - started) * 1000)
    if reply.error:
        # e.g. the model timed out. The error string is ours (no patient text), but the browser only
        # needs to know to try again.
        trace.log("agent_error", error=str(reply.error)[:80], duration_ms=ms)
        raise HTTPException(
            status_code=502, detail="Elliot could not answer just now. Please try again."
        )
    actions = [
        ActionOut(kind=a.kind, summary=a.summary, result_code=a.result_code,
                  reason=getattr(a, "reason", None) or None)
        for a in reply.actions
    ]
    trace.complete(
        model=model,
        text_count=len(reply.texts),
        action_kinds=[a.kind for a in actions],
        no_reply=reply.no_reply,
    )
    return ReplyResponse(
        texts=list(reply.texts), actions=actions, no_reply=bool(reply.no_reply), model=model, ms=ms
    )
