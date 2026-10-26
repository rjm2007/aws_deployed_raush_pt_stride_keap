"""Review codes, plain reasons, choices and Sheet words (services/lead_review.py), no database."""

import pytest

from rpt_agent.services.lead_review import (
    CHOICE_LABELS,
    GROUP_CHOICES,
    SHEET_RESOLVED,
    describe,
    mask_phone,
    sheet_open_status,
    sms_failure,
    step_name,
)

LEAD = {"id": "lead-1", "full_name": "Asha Rao", "review_reason": None}


def _review(code, **details):
    return {"id": 1, "lead_id": "lead-1", "code": code, "message": "m", "details": details,
            "source": "text", "flagged_at": None}


def test_landline_errors_hide_try_again():
    """2026-10-21: Twilio 30006 on the Day 0 text. Texting again cannot work on a landline."""
    card = describe(_review("sms_failed", error_code="30006", day_offset=0), LEAD)
    assert card["group"] == "text" and card["landline"] is True
    assert "can't receive texts (likely a landline)" in card["reason"]
    assert "Day 0 text" in card["reason"]
    assert [o["choice"] for o in card["options"]] == ["calls_only", "restart_cadence", "front_desk"]
    assert {"label": "Error code", "value": "Twilio 30006"} in card["proof"]


def test_temporary_errors_keep_try_again():
    card = describe(_review("sms_failed", error_code="30003"), LEAD)
    assert [o["choice"] for o in card["options"]] == ["calls_only", "retry_texts", "restart_cadence",
                                                      "front_desk"]
    assert sms_failure("30003")["permanent"] is False
    assert "Twilio error 39999" in sms_failure("39999")["reason"]
    assert "no reason given" in sms_failure(None)["reason"]


@pytest.mark.parametrize(("code", "group", "choices"), [
    # Owner 2026-10-25: restart choices on the card (a Sheet Restart on a lead in review is refused).
    ("name_changed", "name", ["use_new_name", "keep_old_name", "use_new_name_restart",
                              "keep_old_name_restart", "different_person", "front_desk"]),
    ("wrong_person", "call", ["number_ok", "wrong_number", "restart_cadence", "front_desk"]),
    ("call_no_outcome", "call", ["number_ok", "wrong_number", "restart_cadence", "front_desk"]),
    ("dispatch_unconfirmed", "technical", ["continue", "restart_cadence", "front_desk"]),
    ("manual_sms_unconfirmed", "technical", ["continue", "restart_cadence", "front_desk"]),
    ("number_blocked", "blocked", ["front_desk"]),
    ("phone_changed", "phone", ["close_phone_changed", "front_desk"]),
    ("booking_unclear", "booking", ["front_desk"]),
    ("something_new", "other", ["continue", "restart_cadence", "front_desk"]),  # unknown/legacy code
])
def test_each_group_offers_its_choices(code, group, choices):
    card = describe(_review(code, old_name="Asha Rao", new_name="Asha Kumar"), LEAD, has_calls=True)
    assert card["group"] == group
    assert [o["choice"] for o in card["options"]] == choices
    if group == "call":
        assert card["transcript_url"] == "/leads/lead-1/conversations/calls"


def test_name_change_card_shows_old_and_new():
    card = describe(_review("name_changed", old_name="Dana Smith", new_name="Dana Smyth"), LEAD)
    assert "“Dana Smith” to “Dana Smyth”" in card["reason"]
    assert card["proof"][:2] == [{"label": "Old name", "value": "Dana Smith"},
                                 {"label": "New name", "value": "Dana Smyth"}]


def test_sheet_words():
    assert sheet_open_status("name_changed", {}) == "Needs review: name changed"
    assert sheet_open_status("sms_failed", {"error_code": "30006"}) == "Needs review: text failed (landline)"
    assert sheet_open_status("sms_failed", {"error_code": "30003"}) == "Needs review: text failed"
    assert sheet_open_status("wrong_person", {}) == "Needs review: wrong number"
    assert sheet_open_status("number_blocked", {}) == "Number blocked; needs review"
    # Every open text keeps the "needs review" words the Sheet worker reads as "no command".
    for code in ("name_changed", "sms_failed", "call_no_outcome", "dispatch_unconfirmed", "other"):
        assert "needs review" in sheet_open_status(code, {}).lower()
    assert SHEET_RESOLVED["calls_only"] == "Resolved: calls only"
    assert SHEET_RESOLVED["front_desk"] == "Closed: handled by front desk"
    assert SHEET_RESOLVED["use_new_name"] == "Resolved: new name used"
    assert set(SHEET_RESOLVED) == set(CHOICE_LABELS)
    assert {c for choices in GROUP_CHOICES.values() for c in choices} <= set(CHOICE_LABELS)


def test_small_helpers():
    assert mask_phone("+15555558847") == "…8847"
    assert step_name("call", 3) == "Day 3 call" and step_name("sms", None) == "Callback text"


def test_a_legacy_name_review_shows_the_name_on_file_and_cannot_use_an_unknown_name():
    """QA 2026-10-22: a pre-048 name review (the D. S. lead) read "from ? to ?" and offered "use new
    name", which then failed: the new name was never recorded."""
    card = describe(_review("name_changed"), {**LEAD, "full_name": "Dana Smith"})
    assert "?" not in card["reason"] and "“Dana Smith”" in card["reason"]
    assert {"label": "Old name", "value": "Dana Smith"} in card["proof"]
    assert [o["choice"] for o in card["options"]] == ["keep_old_name", "keep_old_name_restart",
                                                      "different_person", "front_desk"]


def test_a_legacy_wrong_person_review_keeps_the_agents_note():
    row = {**_review("wrong_person"), "message": "Said this is Bob's number", "source": "legacy"}
    assert "Agent's note: Said this is Bob's number" in describe(row, LEAD)["reason"]
    plain = {**_review("wrong_person"), "message": "wrong phone number"}
    assert "Agent's note" not in describe(plain, LEAD)["reason"]


def test_continue_wording_fits_the_group():
    from rpt_agent.services.lead_review import _effect

    assert _effect("continue", {}, LEAD, "technical") == "The unconfirmed step is not repeated"
    assert "not repeated" not in _effect("continue", {}, LEAD, "other")
    # A failed booking-link text is not resent; the choice must not promise it.
    assert "goes out again" not in _effect("retry_texts", {}, LEAD, "text", "booking_link_failed")


def test_demo_reasons_get_demo_groups_and_never_continue():
    """Demo (owner, 2026-10-22): a Stride cancel or an unconfirmed agent booking is never "Continue"."""
    from rpt_agent.services.lead_review import (
        GROUP_CHOICES,
        demo_code_for_reason,
        describe,
        group_of,
    )

    assert demo_code_for_reason("Cancelled in Stride") == "appointment_cancelled"
    assert demo_code_for_reason("No show in Stride") == "appointment_no_show"
    assert demo_code_for_reason(
        "Stride appointment result unclear during booking; confirm in Stride") == "booking_unclear"
    assert demo_code_for_reason("Unexpected error during booking; confirm in Stride") == "booking_unclear"
    assert demo_code_for_reason("Stride says this patient already exists; staff must book manually") == \
        "booking_patient_exists"
    assert demo_code_for_reason("Booked in Stride (appointment 12) but our records did not update; "
                                "reconcile") == "booking_not_recorded"
    assert demo_code_for_reason("Patient asked a question") is None
    assert "continue" not in GROUP_CHOICES["appointment"] and GROUP_CHOICES["booking"] == ["front_desk"]
    legacy = {"id": 1, "code": "other", "message": "Cancelled in Stride", "details": {}, "source": "legacy"}
    card = describe(legacy, {"id": "l1", "full_name": "A B", "sms_disabled": True})
    assert group_of(card["code"]) == "appointment"
    # Calls only: no booking-link text either.
    assert [o["choice"] for o in card["options"]] == ["front_desk", "restart_cadence"]


class _OneRowConn:
    """Answers closed_review's single query with `row` (or no row)."""

    def __init__(self, row):
        self.row = row

    def execute(self, sql, params):
        assert "from lead_reviews" in sql and params == ("lead-1",)
        return self

    def fetchone(self):
        return self.row


@pytest.mark.parametrize(("lead", "row", "reason", "message"), [
    # 2026-10-23: a newer Sheet row on the same number replaced the lead while its page was open.
    ({"status_reason": "replaced by a newer referral on this number"}, None, "replaced",
     "This lead was closed: a newer Sheet row uses the same phone number. Its history is kept."),
    ({}, {"resolution": "replaced", "resolved_by_name": "System"}, "replaced",
     "This lead was closed: a newer Sheet row uses the same phone number. Its history is kept."),
    ({}, {"resolution": "calls_only", "resolved_by_name": "Emma"}, "resolved",
     "This review was already resolved by Emma: Continue with calls only."),
    ({}, {"resolution": "booked", "resolved_by_name": "Google Sheet"}, "resolved",
     "This review was already resolved by Google Sheet: Marked Booked."),
    ({}, {"resolution": "mystery", "resolved_by_name": "Emma"}, "closed", "This review is already closed."),
    ({}, None, "closed", "This review is already closed."),
])
def test_closed_review_says_why(lead, row, reason, message):
    from rpt_agent.services.lead_review import closed_review, options_preview

    lead = {"id": "lead-1", "needs_review": False, **lead}
    closed = closed_review(_OneRowConn(row), lead)
    assert closed["reason"] == reason and closed["message"] == message
    # The confirm pop-up's GET keeps its old keys and adds `closed`.
    preview = options_preview(_OneRowConn(row), lead)
    assert preview["review"] is None and preview["options"] == [] and preview["closed"] == closed


def test_resolving_a_closed_review_returns_the_reason():
    from rpt_agent.services.lead_review import ReviewError, resolve_review

    lead = {"id": "lead-1", "needs_review": False,
            "status_reason": "replaced by a newer referral on this number"}

    class Conn:
        """The lead row first (locked), then no open and no resolved review rows."""

        def __init__(self):
            self.rows = [lead]

        def execute(self, sql, params):
            return self

        def fetchone(self):
            return self.rows.pop(0) if self.rows else None

        def fetchall(self):
            return []

    with pytest.raises(ReviewError) as error:
        resolve_review(Conn(), "lead-1", "calls_only", note=None, actor_id="u", actor_name="Emma")
    assert error.value.status_code == 409
    assert error.value.detail.startswith("This lead was closed: a newer Sheet row")


# --- owner, 2026-10-25: restart choices, location / case / date of birth reviews ----------------


def test_restart_choices_have_their_labels_and_sheet_words():
    from rpt_agent.services.lead_review import choice_label, sheet_resolved_text

    assert CHOICE_LABELS["use_new_name_restart"] == "Same person, use new name & restart from Day 0"
    assert CHOICE_LABELS["keep_old_name_restart"] == "Same person, keep old name & restart from Day 0"
    assert CHOICE_LABELS["restart_cadence"] == "Restart cadence from Day 0"
    assert sheet_resolved_text("use_new_name_restart") == "Resolved: new name used; cadence restarted"
    assert sheet_resolved_text("keep_old_name_restart") == "Resolved: old name kept; cadence restarted"
    assert sheet_resolved_text("restart_cadence") == "Cadence restarted"
    assert choice_label("use_new_value", "location_changed") == "Use new location & continue"
    assert choice_label("keep_old_value", "case_changed") == "Keep old case & continue"
    assert choice_label("use_new_value_restart", "dob_changed") == (
        "Use new date of birth & restart from Day 0")
    assert choice_label("keep_old_value_restart", "location_changed") == (
        "Keep old location & restart from Day 0")
    assert sheet_resolved_text("keep_old_value", "dob_changed") == "Resolved: old date of birth kept"
    assert sheet_resolved_text("use_new_value_restart", "case_changed") == (
        "Resolved: new case used; cadence restarted")


@pytest.mark.parametrize(("code", "field", "sheet"), [
    ("location_changed", "location", "Needs review: location changed"),
    ("case_changed", "case", "Needs review: case changed"),
    ("dob_changed", "date of birth", "Needs review: date of birth changed"),
])
def test_a_location_case_or_birthday_change_is_reviewed_like_a_name(code, field, sheet):
    card = describe(_review(code, field=field, old_value="Old", new_value="New"), LEAD)
    assert card["group"] == "details" and card["group_label"] == "Details changed"
    assert f"The {field} in the Google Sheet changed from “Old” to “New”" in card["reason"]
    assert card["proof"][:2] == [{"label": f"Old {field}", "value": "Old"},
                                 {"label": f"New {field}", "value": "New"}]
    assert [(o["choice"], o["label"]) for o in card["options"]] == [
        ("use_new_value", f"Use new {field} & continue"),
        ("keep_old_value", f"Keep old {field} & continue"),
        ("use_new_value_restart", f"Use new {field} & restart from Day 0"),
        ("keep_old_value_restart", f"Keep old {field} & restart from Day 0"),
        ("front_desk", "Front desk will handle"),
    ]
    assert sheet_open_status(code, {}) == sheet


def test_no_restart_is_offered_while_another_review_is_open():
    card = describe(_review("wrong_person"), LEAD, more=True)
    assert [o["choice"] for o in card["options"]] == ["number_ok", "wrong_number", "front_desk"]
    card = describe(_review("name_changed", old_name="A", new_name="B"), LEAD, more=True)
    assert "use_new_name_restart" not in [o["choice"] for o in card["options"]]


def test_restart_choices_say_what_they_do():
    from rpt_agent.services.lead_review import _effect

    effect = _effect("use_new_name_restart", {"new_name": "Asha Kumar"}, LEAD, "name", "name_changed")
    assert effect == ("Name becomes Asha Kumar. The old schedule is discarded and a new cadence "
                      "starts from Day 0")
    effect = _effect("keep_old_value", {"old_value": "Dana Point"}, LEAD, "details", "location_changed")
    assert effect == "The location stays Dana Point; the Sheet cell is set back to it"


def test_a_booking_that_failed_on_the_call_is_a_front_desk_card():
    # Owner, 2026-10-26: Elliot told the patient the front desk will take care of it; the card says
    # so, offers only "Front desk will handle" (the patient is not booked) and never mentions a text.
    card = describe(_review("booking_failed", on_call=True,
                            why="ran out of time before the case step"), LEAD)
    assert card["group"] == "booking" and card["title"] == "Booking did not go through"
    assert "could not book a visit on the call (ran out of time before the case step)" in card["reason"]
    assert "front desk team will take care of the appointment" in card["reason"]
    assert "by text" not in card["reason"]
    assert [o["choice"] for o in card["options"]] == ["front_desk"]
    assert sheet_open_status("booking_failed", {"on_call": True}) == \
        "Needs review: booking did not go through"
    # The unclear-booking check's own card (no on_call) keeps its Stride wording.
    assert "Stride still shows that time as open" in describe(_review("booking_failed"), LEAD)["reason"]


def test_yes_then_hang_up_card_asks_the_front_desk_to_book_by_hand():
    # Owner, 2026-10-27: the patient said yes to the confirmation and hung up; the backend could
    # not book it, so the front desk does.
    card = describe(_review("booking_said_yes", confirmed="Tuesday, June 16 at 9:00 AM with Thao",
                            why="that time was no longer free"), LEAD)
    assert card["group"] == "booking" and card["title"] == "Patient said yes, then hung up"
    assert "said yes to Tuesday, June 16 at 9:00 AM with Thao and hung up" in card["reason"]
    assert "(that time was no longer free)" in card["reason"] and "book by hand" in card["reason"]
    assert [o["choice"] for o in card["options"]] == ["front_desk"]
    assert sheet_open_status("booking_said_yes", {}) == \
        "Needs review: patient said yes to a time, book by hand"


def test_booked_then_hung_up_card_is_plain_and_general():
    # Owner, 2026-10-27: summary said booked, nothing was booked; stays a Call card.
    card = describe(_review("booking_hung_up"), LEAD, has_calls=True)
    assert card["group"] == "call" and card["title"] == "Patient asked to book, then hung up"
    assert card["reason"] == ("The patient was asking about times or choosing one and hung up before "
                              "confirming. Nothing was booked. Read the transcript, then continue the "
                              "cadence or have the front desk call them.")
    assert [o["choice"] for o in card["options"]] == \
        [o["choice"] for o in describe(_review("call_outcome_not_applied"), LEAD,
                                       has_calls=True)["options"]]
    assert card["transcript_url"] == "/leads/lead-1/conversations/calls"
    assert sheet_open_status("booking_hung_up", {}) == \
        "Needs review: patient asked to book, then hung up"
    assert sheet_open_status("call_outcome_not_applied", {}) == "Needs review: call result unclear"


def test_booking_cards_and_sheet_words_name_what_happened():
    assert sheet_open_status("booking_patient_exists", {}) == \
        "Needs review: patient already in Stride, book by hand"
    assert "front desk team will take care" in describe(_review("booking_unclear"), LEAD)["reason"]
