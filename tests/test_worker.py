from datetime import date, datetime

from rpt_agent.config import Settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderError
from rpt_agent.worker import (
    Job,
    compute_send_time,
    dispatch_job,
    format_phone,
    materialize_cadence,
    render_sms_template,
)


def test_phone_normalization():
    assert format_phone("(949) 555-1212") == "+19495551212"
    assert format_phone("bad") is None


def test_existing_scheduler_stays_inside_business_day():
    settings = {
        "timezone": "America/Los_Angeles", "holidays": [],
        "business_hours": {str(day): {"open": "09:00", "close": "17:00"} for day in range(1, 6)},
    }
    result = compute_send_time(settings, "lead-1", date(2026, 8, 24), 0)
    local = result.astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    assert 9 <= local.hour <= 16


def test_sms_template_renders_name_and_booking_link():
    job = Job(
        event_id=1,
        lead_id="lead-1",
        channel="sms",
        phone="+19495551212",
        name="Synthetic Patient",
        body="Hi {name}, book here: {link}",
        booking_link_url="https://example.test/book",
        day_offset=1,
        vapi_assistant_id=None,
        vapi_phone_number_id=None,
    )
    assert render_sms_template(job) == "Hi Synthetic, book here: https://example.test/book"


class _Result:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _CadenceConnection:
    def __init__(self, is_test: bool):
        self.is_test = is_test
        self.inserted = []

    def execute(self, query, params):
        if "select ps.business_hours" in query:
            return _Result(one={
                "business_hours": {
                    str(day): {"open": "09:00", "close": "17:00"} for day in range(1, 6)
                },
                "holidays": [],
                "timezone": "America/Los_Angeles",
            })
        if "select is_test" in query:
            return _Result(one={"is_test": self.is_test})
        if "select id,step_order" in query:
            return _Result(many=[
                {"id": 1, "step_order": 1, "day_offset": 0, "channel": "call"},
                {"id": 2, "step_order": 2, "day_offset": 1, "channel": "sms"},
            ])
        if "insert into outreach_events" in query:
            self.inserted.append(params[4])
        return _Result()


def test_test_mode_compresses_only_synthetic_leads(monkeypatch):
    monkeypatch.setattr(
        "rpt_agent.worker.get_settings",
        lambda: Settings(test_mode=True, test_cadence_day_minutes=5),
    )
    synthetic = _CadenceConnection(is_test=True)
    materialize_cadence(synthetic, "test-lead", 1, date(2026, 8, 24))
    assert isinstance(synthetic.inserted[0], datetime)
    delta = synthetic.inserted[1] - synthetic.inserted[0]
    assert 300 <= delta.total_seconds() <= 302

    production = _CadenceConnection(is_test=False)
    materialize_cadence(production, "prod-lead", 1, date(2026, 8, 24))
    assert (production.inserted[1] - production.inserted[0]).total_seconds() > 5 * 60


def test_dispatch_classifies_safe_retry_and_ambiguous_exception():
    job = Job(1, "lead", "sms", "+15555550123", "Test", "hello", None, 0, None, None)

    class RetryableProvider:
        def send_sms(self, trace, phone, body):
            raise ProviderError("twilio", "429", "rate limited", retryable=True)

    class UnexpectedProvider:
        def send_sms(self, trace, phone, body):
            raise ValueError("malformed successful response")

    trace = WorkflowTrace("worker", "test")
    assert dispatch_job(trace, job, RetryableProvider()).state == "retry"
    assert dispatch_job(trace, job, UnexpectedProvider()).state == "unknown"
