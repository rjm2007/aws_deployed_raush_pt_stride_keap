"""Test chat tab backend (routes/sms_agent_test.py): auth, switch, validation, rate limit, the
sandbox call, error mapping and the read-only options. The agent module is faked; nothing here
touches a database, OpenAI or Twilio."""

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import rpt_agent.routes.sms_agent_test as route
from rpt_agent.api import app
from rpt_agent.config import get_settings

REPLY = "/api/v1/dashboard/sms-agent-test/reply"
OPTIONS = "/api/v1/dashboard/sms-agent-test/options"


def _headers(user="staff-1", role="employee"):
    return {
        "X-Dashboard-Token": "x" * 32,
        "X-Dashboard-User-ID": user,
        "X-Dashboard-User-Name": "Test%20Person",
        "X-Dashboard-User-Role": role,
    }


@dataclass
class FakeSetup:
    who: str
    status: str
    clinic: str
    service: str
    referral_word: str
    first_name: str
    last_name: str
    date_of_birth: date | None
    front_desk: str
    clock: datetime | None
    model: str


@dataclass
class FakeTurn:
    role: str
    text: str
    at: datetime | None


@dataclass
class FakeAction:
    kind: str
    summary: str
    result_code: str


@dataclass
class FakeReply:
    texts: list[str]
    actions: list[FakeAction] = field(default_factory=list)
    no_reply: bool = False
    error: str | None = None


class FakeAgent:
    SandboxSetup = FakeSetup
    Turn = FakeTurn

    def __init__(self, reply=None, raises=None):
        self.calls = []
        self.reply = reply or FakeReply(
            texts=["Here's what I have open:\nTue Nov 3, 9:00 AM with Alan"],
            actions=[FakeAction("find_times", "Looked for mornings next week", "OPENINGS")],
        )
        self.raises = raises

    def reply_sandbox(self, *, setup, history, settings=None, client=None, prior_actions=None):
        self.calls.append({"setup": setup, "history": history, "settings": settings,
                           "prior_actions": prior_actions})
        if self.raises:
            raise self.raises
        return self.reply


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setenv("SMS_AGENT_TEST_ENABLED", "true")
    monkeypatch.setenv("SMS_AGENT_MODEL", "gpt-5.5")
    monkeypatch.setenv("SMS_AGENT_TEST_REQUESTS_PER_MINUTE", "30")
    get_settings.cache_clear()
    route.rate_limiter.reset()

    # Any database use from this route is a bug, except the options read below, which installs
    # its own fake.
    @contextmanager
    def no_database():
        raise AssertionError("the test chat must not open a database transaction")
        yield

    monkeypatch.setattr(route, "transaction", no_database)
    yield
    get_settings.cache_clear()
    route.rate_limiter.reset()


@pytest.fixture
def agent(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(route, "_agent", lambda: fake)
    return fake


def _body(**overrides):
    body = {
        "setup": {
            "who": "existing",
            "status": "in_progress",
            "clinic": "Laguna Niguel",
            "service": "Physical Therapy",
            "referral_word": "knee",
            "first_name": "Sam",
            "last_name": "Test",
            "date_of_birth": "1980-03-14",
            "front_desk": "auto",
            "clock": None,
        },
        "history": [
            {"role": "elliot", "text": "Hi Sam, this is Elliot from Rausch Physical Therapy!"},
        ],
        "message": "Mornings next week?",
    }
    body.update(overrides)
    return body


def _setup(**changes):
    body = _body()
    body["setup"].update(changes)
    return body


client = TestClient(app)


def test_reply_needs_dashboard_auth(agent):
    assert client.post(REPLY, json=_body()).status_code == 401
    assert client.get(OPTIONS).status_code == 401
    assert agent.calls == []


def test_off_switch_gives_a_clear_404(monkeypatch, agent):
    monkeypatch.setenv("SMS_AGENT_TEST_ENABLED", "false")
    get_settings.cache_clear()
    for response in (client.post(REPLY, headers=_headers(), json=_body()),
                     client.get(OPTIONS, headers=_headers())):
        assert response.status_code == 404
        assert "SMS_AGENT_TEST_ENABLED" in response.json()["detail"]
    assert agent.calls == []


def test_any_dashboard_user_gets_a_reply_and_the_new_text_is_the_last_patient_turn(agent):
    response = client.post(REPLY, headers=_headers(role="employee"), json=_body())
    assert response.status_code == 200
    data = response.json()
    assert data["texts"] == ["Here's what I have open:\nTue Nov 3, 9:00 AM with Alan"]
    assert data["actions"] == [
        {"kind": "find_times", "summary": "Looked for mornings next week", "result_code": "OPENINGS",
         "reason": None}
    ]
    assert data["model"] == "gpt-5.5"
    assert data["no_reply"] is False
    assert isinstance(data["ms"], int) and data["ms"] >= 0

    call = agent.calls[0]
    assert [(t.role, t.text, t.at) for t in call["history"]] == [
        ("elliot", "Hi Sam, this is Elliot from Rausch Physical Therapy!", None),
        ("patient", "Mornings next week?", None),
    ]
    setup = call["setup"]
    assert setup.who == "existing" and setup.status == "in_progress"
    assert setup.date_of_birth == date(1980, 3, 14)
    assert setup.front_desk == "auto" and setup.clock is None
    assert setup.model == "gpt-5.5"
    assert call["settings"] is get_settings()


def test_setup_passes_clock_model_and_new_person(agent):
    body = _setup(who="new", first_name="", last_name="", date_of_birth=None,
                  front_desk="closed", clock="2026-11-08T21:30:00", model="gpt-6-luna")
    body["history"] = []
    response = client.post(REPLY, headers=_headers(), json=body)
    assert response.status_code == 200
    assert response.json()["model"] == "gpt-6-luna"
    setup = agent.calls[0]["setup"]
    assert setup.who == "new" and setup.first_name == "" and setup.date_of_birth is None
    assert setup.front_desk == "closed"
    # Naive on purpose: the clock is clinic local time, as picked in the tab.
    assert setup.clock == datetime(2026, 11, 8, 21, 30)  # noqa: DTZ001
    assert setup.model == "gpt-6-luna"
    assert [t.role for t in agent.calls[0]["history"]] == ["patient"]


@pytest.mark.parametrize(
    "body",
    [
        _setup(who="someone"),
        _setup(status="needs_review"),
        _setup(front_desk="maybe"),
        _setup(clock="not a time"),
        _setup(model="gpt-unknown"),
        # Dropped from the owner's list (2026-11-02): no longer pickable.
        _setup(model="gpt-4.1"),
        _setup(date_of_birth="2999-01-01"),
        _setup(first_name="x" * 61),
        _body(message=""),
        _body(message="   "),
        _body(message="x" * 1601),
        _body(history=[{"role": "staff", "text": "hi"}]),
        _body(history=[{"role": "cadence", "text": "hi"}]),
        _body(history=[{"role": "patient", "text": "x" * 1601}]),
        _body(history=[{"role": "patient", "text": "  "}]),
        _body(history=[{"role": "patient", "text": "hi"}] * 60),
        _body(history=[{"role": "patient", "text": "x" * 1600}] * 25),
        _setup(open_review="maybe"),
        # A visit only on a Booked existing lead; a new person has no review to open.
        _setup(booked_visit={"at": "2026-11-03T09:00", "therapist": "Alan"}),
        _setup(status="booked", booked_visit={"at": "not a time"}),
        _setup(status="booked", booked_visit={"at": "2026-11-03T09:00", "therapist": "x" * 61}),
        _setup(who="new", status="booked", booked_visit={"at": "2026-11-03T09:00"}),
        _setup(who="new", open_review="front_desk"),
        _setup(who="new", open_review="full"),
    ],
)
def test_bad_input_is_a_422_and_never_reaches_the_agent(agent, body):
    response = client.post(REPLY, headers=_headers(), json=body)
    assert response.status_code == 422
    assert agent.calls == []


def test_prior_actions_reach_the_agent_oldest_first(agent):
    # 2026-11-02 review: the route dropped them, so a case change on turn 1 was forgotten and
    # "book me" on turn 2 booked in the test chat where live refuses.
    prior = [{"kind": "find_times", "result_code": "OPENINGS"},
             {"kind": "report", "result_code": "REPORTED_CASE_CHANGE"}]
    response = client.post(REPLY, headers=_headers(), json=_body(prior_actions=prior))
    assert response.status_code == 200
    assert agent.calls[0]["prior_actions"] == prior
    # An older tab that sends none still works: nothing happened earlier.
    client.post(REPLY, headers=_headers(), json=_body())
    assert agent.calls[1]["prior_actions"] == []


@dataclass
class FakeActionWithReason(FakeAction):
    reason: str = ""


def test_a_hand_off_reason_goes_out_and_comes_back(agent):
    # x-02, 2026-11-02: {kind, result_code} alone cannot tell a repeat of the same hand-off from a
    # new one, so the reply gives the reason and the tab sends it back with prior_actions.
    agent.reply = FakeReply(texts=["The front desk will reach out."], actions=[
        FakeActionWithReason("hand_off", "Would open a Needs Review card", "HANDED_OFF",
                             "insurance"),
        FakeActionWithReason("find_times", "Looked", "OPENINGS", ""),
        FakeAction("report", "Would report", "REPORTED_DECLINED")])
    prior = [{"kind": "hand_off", "result_code": "HANDED_OFF", "reason": " Insurance "},
             {"kind": "report", "result_code": "REPORTED_CASE_CHANGE", "reason": ""}]
    response = client.post(REPLY, headers=_headers(), json=_body(prior_actions=prior))
    assert response.status_code == 200
    assert [a["reason"] for a in response.json()["actions"]] == ["insurance", None, None]
    # Sent on as given (trimmed, lower case); an empty reason is left out, as older tabs sent it.
    assert agent.calls[0]["prior_actions"] == [
        {"kind": "hand_off", "result_code": "HANDED_OFF", "reason": "insurance"},
        {"kind": "report", "result_code": "REPORTED_CASE_CHANGE"}]


@pytest.mark.parametrize("prior", [[{"kind": "report", "result_code": "x" * 61}],
                                   [{"kind": "x" * 41, "result_code": "OK"}], "BOOKED",
                                   [{"kind": "hand_off", "result_code": "HANDED_OFF",
                                     "reason": "x" * 41}],
                                   [{"kind": "hand_off", "result_code": "HANDED_OFF",
                                     "reason": "insurance; drop table"}]])
def test_bad_prior_actions_are_a_422(agent, prior):
    assert client.post(REPLY, headers=_headers(), json=_body(prior_actions=prior)).status_code == 422
    assert agent.calls == []


def test_59_turns_of_history_are_allowed(agent):
    history = [{"role": "patient" if i % 2 else "elliot", "text": "ok"} for i in range(59)]
    assert client.post(REPLY, headers=_headers(), json=_body(history=history)).status_code == 200
    assert len(agent.calls[0]["history"]) == 60


def test_rate_limit_is_per_user(monkeypatch, agent):
    monkeypatch.setenv("SMS_AGENT_TEST_REQUESTS_PER_MINUTE", "2")
    get_settings.cache_clear()
    for _ in range(2):
        assert client.post(REPLY, headers=_headers(), json=_body()).status_code == 200
    limited = client.post(REPLY, headers=_headers(), json=_body())
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert len(agent.calls) == 2
    # Another staff member is not held up by the first one's tests.
    assert client.post(REPLY, headers=_headers(user="staff-2"), json=_body()).status_code == 200


def test_rate_limiter_window_slides():
    limiter = route._RateLimiter()
    assert limiter.allow("a", 1, now=100.0)
    assert not limiter.allow("a", 1, now=159.9)
    assert limiter.allow("a", 1, now=160.0)


def test_agent_exception_is_a_plain_502_without_text_in_logs(monkeypatch, caplog):
    fake = FakeAgent(raises=RuntimeError("model said: Sam Test 1980-03-14 Mornings next week?"))
    monkeypatch.setattr(route, "_agent", lambda: fake)
    with caplog.at_level(logging.DEBUG):
        response = client.post(REPLY, headers=_headers(), json=_body())
    assert response.status_code == 502
    assert response.json() == {"detail": "Elliot could not answer just now. Please try again."}
    logged = " ".join(
        f"{r.getMessage()} {getattr(r, 'details', '')} {r.exc_text or ''}" for r in caplog.records
    )
    for secret in ("Sam", "1980-03-14", "Mornings next week", "model said"):
        assert secret not in logged
    assert "RuntimeError" in logged


def test_agent_reply_with_error_is_a_502(monkeypatch):
    fake = FakeAgent(reply=FakeReply(texts=[], error="timeout"))
    monkeypatch.setattr(route, "_agent", lambda: fake)
    response = client.post(REPLY, headers=_headers(), json=_body())
    assert response.status_code == 502
    assert "try again" in response.json()["detail"]


def test_missing_agent_module_is_a_503(monkeypatch):
    def missing():
        raise ImportError("no module")

    monkeypatch.setattr(route, "_agent", missing)
    response = client.post(REPLY, headers=_headers(), json=_body())
    assert response.status_code == 503
    assert response.json()["detail"] == "The SMS agent is not available."


def test_no_reply_is_passed_through(monkeypatch):
    fake = FakeAgent(reply=FakeReply(texts=[], no_reply=True))
    monkeypatch.setattr(route, "_agent", lambda: fake)
    response = client.post(REPLY, headers=_headers(), json=_body(message="ok thanks"))
    assert response.status_code == 200
    assert response.json()["texts"] == [] and response.json()["no_reply"] is True


def test_real_agent_module_is_imported_lazily():
    # The real import path; skipped until services/sms_agent.py lands so this file passes alone.
    pytest.importorskip("rpt_agent.services.sms_agent")
    module = route._agent()
    for name in ("SandboxSetup", "Turn", "reply_sandbox"):
        assert hasattr(module, name)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class OptionsConnection:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if sql == "set transaction read only":
            return None
        if "from booking_locations" in sql:
            return _Rows([{"name": "Laguna Niguel"}, {"name": "Mission Viejo"}])
        if "from case_types" in sql:
            return _Rows([
                {"name": "Physical Therapy", "is_default": True},
                {"name": "Physical Therapy - Pelvic", "is_default": False},
            ])
        raise AssertionError(f"unexpected SQL: {sql}")


def test_options_lists_clinics_services_models_and_statuses_read_only(monkeypatch):
    conn = OptionsConnection()

    @contextmanager
    def fake_transaction():
        yield conn

    monkeypatch.setattr(route, "transaction", fake_transaction)
    response = client.get(OPTIONS, headers=_headers())
    assert response.status_code == 200
    data = response.json()
    assert data["clinics"] == ["Laguna Niguel", "Mission Viejo"]
    assert data["services"] == ["Physical Therapy", "Physical Therapy - Pelvic"]
    assert data["default_model"] == "gpt-5.5"
    assert data["models"][0] == "gpt-5.5" and len(data["models"]) == len(set(data["models"]))
    values = [s["value"] for s in data["statuses"]]
    assert {"in_progress", "booked", "closed_no_response", "do_not_contact"} <= set(values)
    assert data["max_turns"] == 60 and data["max_text_chars"] == 1600
    assert data["max_chat_chars"] == 40_000
    assert conn.sql[0] == ("set transaction read only", None)
    assert len(conn.sql) == 3
    for sql, params in conn.sql[1:]:
        assert sql.lstrip().lower().startswith("select")
        assert params == (1,)


def test_options_put_a_custom_default_model_first(monkeypatch):
    monkeypatch.setenv("SMS_AGENT_MODEL", "gpt-5.4-mini")
    get_settings.cache_clear()
    models = route.allowed_models()
    assert models[0] == "gpt-5.4-mini"
    assert models.count("gpt-5.4-mini") == 1
    assert "gpt-5.5" in models


def test_model_list_is_the_owners_list_with_the_default_first(monkeypatch):
    # Owner (2026-11-02): compare Luna / Terra / Sol against gpt-5.5 and 5.4-mini; nothing else.
    monkeypatch.setenv("SMS_AGENT_MODEL", "gpt-5.5")
    get_settings.cache_clear()
    assert route.allowed_models() == [
        "gpt-5.5", "gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol", "gpt-5.4-mini",
    ]


@pytest.mark.parametrize("model", ["gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol", "gpt-5.4-mini"])
def test_each_listed_model_can_be_picked(agent, model):
    response = client.post(REPLY, headers=_headers(), json=_setup(model=model))
    assert response.status_code == 200
    assert agent.calls[0]["setup"].model == model


def test_status_literal_matches_the_listed_statuses():
    assert set(route.LeadStatus.__args__) == {value for value, _ in route.LEAD_STATUSES}


def test_route_sends_no_texts(monkeypatch, agent):
    # A Twilio send from the sandbox would cost money and reach a real phone.
    from rpt_agent.services import twilio_service

    def boom(*args, **kwargs):
        raise AssertionError("the test chat must never send a text")

    monkeypatch.setattr(twilio_service.TwilioService, "send_sms", boom, raising=False)
    assert client.post(REPLY, headers=_headers(), json=_body()).status_code == 200


def test_options_and_reply_are_registered_on_the_app():
    assert app.url_path_for("sms_agent_test_reply") == REPLY
    assert app.url_path_for("sms_agent_test_options") == OPTIONS



# --- booked visit and open review (2026-11-02 eval: bk-02, ls-01) ---------------------------------


@dataclass
class FakeSetupWithExtras(FakeSetup):
    booked_visit: str = ""
    open_review: str = "auto"


@pytest.fixture
def agent_with_extras(monkeypatch):
    fake = FakeAgent()
    fake.SandboxSetup = FakeSetupWithExtras
    monkeypatch.setattr(route, "_agent", lambda: fake)
    return fake


def test_booked_visit_reaches_the_agent_as_the_context_line(agent_with_extras):
    body = _setup(status="booked", open_review="front_desk",
                  booked_visit={"at": "2026-11-03T09:00", "therapist": " Alan "})
    response = client.post(REPLY, headers=_headers(), json=body)
    assert response.status_code == 200
    setup = agent_with_extras.calls[0]["setup"]
    # No clinic on the visit = the lead's clinic; the shape live's booked_visit() writes.
    assert setup.booked_visit == "Tue Nov 3 at 9:00 AM with Alan at Laguna Niguel"
    assert setup.open_review == "front_desk"


def test_booked_visit_with_its_own_clinic_and_no_therapist(agent_with_extras):
    body = _setup(status="booked", booked_visit={"at": "2026-11-12T14:30", "clinic": "Dana Point"})
    assert client.post(REPLY, headers=_headers(), json=body).status_code == 200
    assert agent_with_extras.calls[0]["setup"].booked_visit == "Thu Nov 12 at 2:30 PM at Dana Point"


@pytest.mark.parametrize("choice", ["auto", "none", "front_desk", "full"])
def test_each_open_review_choice_reaches_the_agent(agent_with_extras, choice):
    assert client.post(REPLY, headers=_headers(), json=_setup(open_review=choice)).status_code == 200
    setup = agent_with_extras.calls[0]["setup"]
    assert setup.open_review == choice and setup.booked_visit == ""


def test_new_person_may_say_no_review(agent_with_extras):
    body = _setup(who="new", first_name="", open_review="none")
    body["history"] = []
    assert client.post(REPLY, headers=_headers(), json=body).status_code == 200


def test_agent_without_the_new_fields_gets_the_old_setup(agent):
    # Defaults are dropped, so an agent whose SandboxSetup predates these fields still answers.
    assert client.post(REPLY, headers=_headers(), json=_setup(open_review="auto")).status_code == 200
    assert not hasattr(agent.calls[0]["setup"], "open_review")


@pytest.mark.parametrize("change", [
    {"open_review": "front_desk"},
    {"status": "booked", "booked_visit": {"at": "2026-11-03T09:00"}},
])
def test_a_chosen_extra_the_agent_cannot_take_is_refused_not_ignored(agent, change):
    response = client.post(REPLY, headers=_headers(), json=_setup(**change))
    assert response.status_code == 501
    assert "yet" in response.json()["detail"]
    assert agent.calls == []
