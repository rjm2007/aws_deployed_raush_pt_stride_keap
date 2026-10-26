from rpt_agent.services.delivery import apply_twilio_message_status


class _Result:
    rowcount = 1

    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many if many is not None else ([] if one is None else [one])

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params):
        self.queries.append((query, params))
        if query.startswith("update sms_messages"):
            return _Result({
                "lead_id": "lead-1",
                "outreach_event_id": 7,
                "delivery_status": "delivered",
                "failure_reason": None,
            })
        if "returning oe.id,oe.lead_id" in query:
            return _Result({"id": 7, "lead_id": "lead-1"})
        return _Result()


def test_twilio_delivery_updates_are_durable_and_forward_only():
    conn = _Connection()
    matched = apply_twilio_message_status(
        conn,
        {"MessageSid": "SM-test", "MessageStatus": "delivered"},
    )
    assert matched == 3
    assert len(conn.queries) == 5
    assert all("status='delivered'" in query for query, _params in conn.queries[:3])
    assert all(params[-1] == "SM-test" for _query, params in conn.queries[:3])
    assert "update outreach_events" in conn.queries[3][0]
    assert "destination" in conn.queries[4][0]


def test_older_failed_callback_cannot_regress_a_delivered_outreach_event():
    conn = _Connection()
    apply_twilio_message_status(
        conn,
        {"MessageSid": "SM-test", "MessageStatus": "failed", "ErrorCode": "30001"},
    )
    event_params = conn.queries[3][1]
    # The fake SMS update returns its already-forward delivery state. The event
    # must follow that current database truth, not the older callback payload.
    assert event_params[0] == "delivered"
    assert event_params[1] == "delivered"


def test_undelivered_cadence_sms_pauses_for_review():
    class Connection(_Connection):
        def execute(self, query, params):
            self.queries.append((query, params))
            if query.startswith("update sms_messages"):
                return _Result({
                    "lead_id": "lead-1",
                    "outreach_event_id": 7,
                    "delivery_status": "undelivered",
                    "failure_reason": "30003",
                })
            if "returning oe.id,oe.lead_id" in query:
                return _Result({"id": 7, "lead_id": "lead-1"})
            return _Result()

    conn = Connection()
    apply_twilio_message_status(
        conn,
        {"MessageSid": "SM-test", "MessageStatus": "undelivered", "ErrorCode": "30003"},
    )
    review_queries = [query for query, _ in conn.queries if query.startswith("update leads set")]
    assert len(review_queries) == 1
    assert "needs_review=true" in review_queries[0]
    assert "'paused'" in review_queries[0]
    skip = [(query, params) for query, params in conn.queries if "status='skipped'" in query]
    assert len(skip) == 1
    assert skip[0][1][0] == "paused_for_review"
    assert skip[0][1][1] == "lead-1"


def _settle_with(monkeypatch, structured, turns=None):
    """Run the post-call fallback on one extractor result; return the status
    it applied, or "review" when the call went to staff instead."""
    from contextlib import contextmanager

    from rpt_agent.observability import WorkflowTrace
    from rpt_agent.services import delivery, lead_status

    applied = {}

    class Conn:
        def execute(self, sql, params=None):
            if "needs_review=true" in sql or "outcome='manual'" in sql:
                applied.setdefault("status", "review")
            return self

        def fetchone(self):
            return {"outcome": applied.get("status"), "id": 1}

        def fetchall(self):
            return []

    @contextmanager
    def fake_transaction():
        yield Conn()

    def fake_report(trace, **kwargs):
        applied["status"], applied["notes"] = kwargs["status"], kwargs["notes"]

    monkeypatch.setattr(delivery, "transaction", fake_transaction)
    monkeypatch.setattr(lead_status, "report_lead_status", fake_report)
    message = {"artifact": {
        "structuredOutputs": {"x": {"result": structured}},
        "messages": [{"role": r, "message": m} for r, m in (turns or [])],
    }}
    delivery._settle_from_structured_output(
        WorkflowTrace("t", "test"), message, lead_id="lead-1", event_id=1, call_id="c"
    )
    return applied


def test_hang_up_with_no_decision_keeps_outreach_going(monkeypatch):
    """Rohan hung up during the introduction. The extractor is told that is not
    a refusal and leaves status out; that must count as a missed contact."""
    applied = _settle_with(monkeypatch, {"summary": "Call ended during the introduction."})
    assert applied["status"] == "no_answer"


def test_extractor_refusal_and_opt_out_are_applied_with_their_note(monkeypatch):
    answered = [("bot", "Am I speaking with Emma?"), ("user", "Yes."),
                ("bot", "This is Sarah calling from Rausch. Is now a good time?"),
                ("user", "No, I'm all set.")]
    applied = _settle_with(monkeypatch, {"status": "declined", "summary": "Said she is all set."}, answered)
    assert applied == {"status": "declined", "notes": "Said she is all set."}
    assert _settle_with(monkeypatch, {"status": "do_not_contact"}, answered)["status"] == "do_not_contact"


DEVYANSH = [  # 26 Sep, 10:47 PM: "Yes", then silence until the call timed out
    ("user", "Hello?"),
    ("bot", "Hi. Am I speaking with Devianch Chaudhary?"),
    ("user", "Yes."),
    ("bot", ("Great. This is Sarah calling from Rausch Physical Therapy and Wellness. "
             "Is this a convenient time to speak?")),
    ("bot", "Are you still there?"),
    ("bot", "Are you still there?"),
]


def test_silence_after_the_introduction_is_never_a_refusal(monkeypatch):
    """Gemini read this call as "declined" despite the extractor's rules. The
    patient never answered anything after Sarah said why she was calling."""
    applied = _settle_with(
        monkeypatch,
        {"status": "declined", "summary": "The patient did not respond."},
        DEVYANSH,
    )
    assert applied["status"] == "no_answer"
    assert "The patient did not respond." in applied["notes"]  # evidence kept


def test_sure_then_a_hang_up_is_never_a_refusal(monkeypatch):
    """21 Oct (demo): "Yes", "Sure", then the patient hung up at the location
    question. The summary said "declined" and the lead was closed."""
    sure = [("user", "Hello?"), ("bot", "Am I speaking with Test Drive?"), ("user", "Yes."),
            ("bot", "This is Sarah calling from Rausch. Is now a good time?"), ("user", "Sure."),
            ("bot", "Great. This is for your neck visit at our Dana Point office. Is that location good for you?")]
    applied = _settle_with(
        monkeypatch, {"status": "declined", "summary": "The patient hung up."}, sure
    )
    assert applied["status"] == "no_answer"
    no = sure[:-2] + [("user", "No.")]
    assert _settle_with(monkeypatch, {"status": "declined"}, no)["status"] == "declined"


def test_opt_out_said_before_the_introduction_is_still_honoured(monkeypatch):
    early = [("bot", "Hi. Am I speaking with Devyansh?"), ("user", "Stop calling me.")]
    assert _settle_with(monkeypatch, {"status": "do_not_contact"}, early)["status"] == "do_not_contact"


def test_failed_extraction_goes_to_staff(monkeypatch):
    """An empty object is what an extraction that ran out of tokens returns."""
    assert _settle_with(monkeypatch, {})["status"] == "review"


def test_sent_without_delivery_receipt_settles_the_step():
    """Olivia's carrier never returned a delivery receipt: Twilio stayed at
    "sent", the step stayed attempted, and the cadence froze behind it."""
    from rpt_agent.services.delivery import apply_twilio_message_status

    class Result:
        def __init__(self, one=None):
            self.one, self.rowcount = one, 1

        def fetchone(self):
            return self.one

    class Conn:
        def __init__(self):
            self.queries = []

        def execute(self, sql, params=None):
            self.queries.append((" ".join(sql.split()), params))
            if sql.startswith("update sms_messages"):
                return Result({"lead_id": "lead-1", "outreach_event_id": 7,
                               "delivery_status": "sent", "failure_reason": None})
            if "returning oe.id,oe.lead_id" in sql:
                return Result({"id": 7, "lead_id": "lead-1"})
            return Result()

    conn = Conn()
    apply_twilio_message_status(conn, {"MessageSid": "SM-1", "MessageStatus": "sent"})
    settle = [(q, p) for q, p in conn.queries if q.startswith("update outreach_events oe")]
    assert settle, "a sent text must settle its cadence step"
    assert "when %s in ('sent','delivered') then 'delivered'" in settle[0][0]
    assert settle[0][1][0] == "sent"
    assert not any("needs_review=true" in q for q, _ in conn.queries)


DEMO_01A1795F = [  # 27 Oct (demo): hung up while Elliot was checking times
    ("bot", "Hi, am I speaking with Test Patient?"),
    ("user", "Yep."),
    ("bot", "Hi Test, it's Elliot from Rausch Physical Therapy. We got your referral. Do you have a moment?"),
    ("user", "Yeah."),
    ("bot", "So this is for your back visit, at our Dana Point office. Is that location good for you?"),
    ("user", "Yes."),
    ("bot", "And do mornings or afternoons usually work better for you?"),
    ("user", "Bone works for me."),
    ("bot", "One moment, please, while I check our available"),
]


def test_declined_needs_the_patients_own_no(monkeypatch):
    """Owner, 2026-10-27: "Bone works for me" passed the old "more than a yes" check and the
    summary's invented "said they are all set" closed the lead. Only a real no declines."""
    applied = _settle_with(monkeypatch, {
        "status": "declined",
        "summary": "The patient said they are all set and do not need to schedule an appointment "
                   "at this time.",
    }, DEMO_01A1795F)
    assert applied["status"] == "no_answer"
    assert applied["notes"].startswith("No answer from the patient; outreach continues.")
    real_no = DEMO_01A1795F[:7] + [("user", "No thanks, I'm all set.")]
    assert _settle_with(monkeypatch, {"status": "declined"}, real_no)["status"] == "declined"


def test_refusal_phrases_are_whole_words_and_ignore_case_and_apostrophes():
    from rpt_agent.services.delivery import _patient_refused

    def refused(reply):
        return _patient_refused({"artifact": {"messages": [
            {"role": "bot", "message": "It's Elliot from Rausch. Do you have a moment?"},
            {"role": "user", "message": reply}]}})

    for reply in ("NOT interested.", "No, thanks.", "I don't need it anymore", "Nope.", "no",
                  "Please stop calling me", "I already found a therapist", "I'm all set"):
        assert refused(reply), reply
    for reply in ("Bone works for me.", "Yes.", "Mornings", "Nothing in the morning, afternoons",
                  "I know", "Notice me", "Is that set up?"):
        assert not refused(reply), reply


def test_yes_to_the_confirmation_and_the_offered_slot_it_names():
    from datetime import date, time

    from rpt_agent.services import hang_up_booking as h

    assert h.plain_yes(["Yes, please."]) and h.plain_yes(["Yeah", "go ahead"])
    assert h.plain_yes(["Sounds good."]) and h.plain_yes(["Okay, book it."])
    for reply in (["No."], ["Yes, which office?"], ["Wait"], ["Hello?"], []):
        assert not h.plain_yes(reply), reply
    slot = {"local_date": date(2026, 6, 16), "local_time": time(9, 0), "display_name": "Thao Nguyen"}
    names = {"thao", "nguyen", "alan", "rome"}
    line = ("So that's Tuesday the sixteenth at nine in the morning with Thao at our Laguna Niguel "
            "office. Shall I book it?")
    what = h.find_confirmation([("bot", line)])[1]
    assert h._matches(slot, what, names)
    assert not h._matches({**slot, "display_name": "Alan Rome"}, what, names)
    assert not h._matches({**slot, "local_time": time(9, 30)}, what, names)
    assert not h._matches({**slot, "local_date": date(2026, 6, 23)}, what, names)
    half = h._plain("tuesday the sixteenth at nine thirty with alan")
    assert h._matches({**slot, "local_time": time(9, 30), "display_name": "Alan Rome"}, half, names)
    assert not h._matches(slot, half, names)  # "at nine" inside "at nine thirty"
    assert h._matches({**slot, "local_date": date(2026, 6, 22), "local_time": time(13, 0)},
                      h._plain("monday the twenty-second at one in the afternoon"), names)


def test_the_report_shows_a_booking_tool_call():
    from rpt_agent.services.hang_up_booking import booking_tool_called

    vapi = {"artifact": {"messages": [{"role": "tool_calls", "toolCalls": [
        {"function": {"name": "book_appointment", "arguments": "{}"}}]}]}}
    openai = {"artifact": {"messagesOpenAIFormatted": [{"role": "assistant", "tool_calls": [
        {"function": {"name": "book_appointment"}}]}]}}
    result = {"artifact": {"messages": [{"role": "tool_call_result", "name": "book_appointment"}]}}
    assert booking_tool_called(vapi) and booking_tool_called(openai) and booking_tool_called(result)
    other = {"artifact": {"messages": [{"role": "tool_calls", "toolCalls": [
        {"function": {"name": "find_slots"}}]}]}}
    assert not booking_tool_called(other) and not booking_tool_called({})


def test_last_name_from_the_call_only_when_clear():
    from rpt_agent.services.hang_up_booking import _last_name_from_call

    def last(*replies, after="And just so I pull up the right file, your birthday's ...?"):
        turns = [("bot", "And what's your last name?"), *[("user", r) for r in replies],
                 ("bot", after), ("user", "Yes."), ("bot", "So that's ... Shall I book it?")]
        return _last_name_from_call(turns, len(turns) - 1)

    assert last("It's makhija.") == "Makhija"
    assert last("M A K H I J A") == "Makhija"
    assert last("De La Cruz") is None
    assert last("Makhija", after="Sorry, could you spell that?") is None


YES_REPLIES = [
    "Yes.", "Yes, please.", "Yeah.", "Sure.", "Okay.", "Go ahead.", "Sounds good.", "Yep, that's fine",
    "Perfect, thank you.", "Uh huh.", "Mhm.", "Mm-hmm.", "Let's do it.", "Book it.", "That works.",
    "Yes, that works for me.", "Sure thing.", "Go for it.", "Yes ma'am.", "Yes, sir.", "Do it.",
    "Yeah, book it for me.", "Of course.", "Yes please, thanks Elliot.", "Absolutely.", "Definitely.",
    "Please.", "Yes. Bye.", "Yes, okay, bye.", "Yeah. Thank you. Bye.", "Yes, I have to go, bye.",
]
NOT_YES_REPLIES = [
    "No.", "No, wait.", "Wait, what about Thursday?", "Yes but can we do 11 instead?",
    "Yes, which office?", "Actually no", "Hmm, let me think", "yes no", "Hello?", "Bye.",
    "Can we do another day?",
]


def test_the_first_reply_after_the_confirmation_decides():
    """Tester, 2026-10-27: "Yes. Bye." was not a yes (every word had to be a yes word)."""
    from rpt_agent.services import hang_up_booking as h

    for reply in YES_REPLIES:
        assert h.plain_yes([reply]), reply
    for reply in NOT_YES_REPLIES:
        assert not h.plain_yes([reply]), reply
    assert h.reply_kind("Yes, Tuesday at nine is fine.") == "maybe"  # review, not nothing
    assert h.reply_kind("Yes but can we do 11 instead?") is None  # "instead": a change, nothing
    assert h.plain_yes(["Hello", "Yes."])  # filler first, then the answer
    assert h._first_reply(["Yes.", "Hold on"]) == "maybe"  # a later "hold on": review, not a booking


def test_confirmation_wording_variants_and_cut_lines():
    from rpt_agent.services.hang_up_booking import find_confirmation

    base = "So that's Tuesday the sixteenth at nine with Thao at our Laguna Niguel office."
    for question in ("Shall I book it?", "Shall I book that?", "Shall I book it for you?",
                     "Shall I go ahead and book it?", "Should I book it?", "Can I book that for you?",
                     "Want me to book it?", "Would you like me to book it?"):
        found = find_confirmation([("bot", "Perfect. " + base + " " + question), ("user", "Yes.")])
        assert found and found[2] and found[1].startswith("tuesday the sixteenth"), question
    for summary in (base.replace("that's", "that is"), base.replace("So that's", "That's"),
                    base.replace("'", "\u2019")):
        assert find_confirmation([("bot", summary + " Shall I book it?")])[2], summary
    # Stored cut short (the patient spoke over it): reviewed, never booked.
    assert find_confirmation([("bot", base), ("user", "Yes")])[2] is False
    # The booking tool's own waiting line after a logged tool call is not Elliot's last word.
    turns = [("bot", base + " Shall I book it?"), ("user", "Yes."),
             ("bot", "Thank you. One moment, please, while I confirm your appointment.")]
    assert find_confirmation(turns)[0] == 0
    assert find_confirmation([("bot", "And do mornings or afternoons usually work better?")]) is None


def _refused_after(question, reply):
    from rpt_agent.services.delivery import _patient_refused

    turns = [("user", "Hello?"), ("bot", "Hi, am I speaking with Rpt Tester?"), ("user", "Yes."),
             ("bot", "Hi Rpt, it's Elliot from Rausch Physical Therapy. Do you have a moment?")]
    if question:
        turns += [("user", "Sure."), ("bot", question)]
    turns += [("user", reply)]
    return _patient_refused({"artifact": {"messages": [{"role": r, "message": m} for r, m in turns]}})


REFUSALS = [
    "No, I'm good.", "I'm good, thanks.", "I'll pass.", "Not right now.", "Not at this time.",
    "Maybe later.", "I don't want it.", "I changed my mind.", "We're fine, thank you.",
    "I'm fine, thanks.", "No need.", "No thanks, I'm all set", "not interested", "No, I'm okay.",
]
NOT_REFUSALS = [
    "no problem, mornings work", "I'm not sure", "that's fine, 10:30 works", "Is Thursday all set up?",
    "No, I mean yes, mornings", "Bone works for me.", "Not sure, maybe afternoons?",
]


def test_more_refusals_and_tricky_non_refusals():
    for reply in REFUSALS:
        assert _refused_after(None, reply), reply
        assert _refused_after("Is that location good for you?", reply), reply
    for reply in NOT_REFUSALS:
        assert not _refused_after(None, reply), reply


def test_a_bare_no_declines_only_as_the_answer_to_the_introduction():
    """Owner rule: "no" to the clinic, a time or the confirmation means "not that one"."""
    for reply in ("No.", "Nope", "Nah.", "No, no."):
        assert _refused_after(None, reply), reply
    for question in ("So this is for your knee visit at our Dana Point office. Is that location good?",
                     "Tuesday the sixteenth I've got nine or ten. Would either of those work?",
                     ("So that's Tuesday the sixteenth at nine with Thao at our Dana Point office. "
                      "Shall I book it?")):
        assert not _refused_after(question, "No."), question


def test_last_name_kept_as_spoken():
    from rpt_agent.services.hang_up_booking import _last_name_from_call

    def last(reply):
        turns = [("bot", "And what's your last name?"), ("user", reply),
                 ("bot", "And just so I pull up the right file, your birthday's ...?")]
        return _last_name_from_call(turns, 3)

    assert last("O'Brien.") == "O'Brien" and last("It\u2019s O\u2019Brien") == "O'Brien"
    assert last("Smith-Jones") == "Smith-Jones" and last("My last name is Patel") == "Patel"
    assert last("mcDonald") == "McDonald"


def test_round_two_reply_reading():
    """Tester round 2 (2026-10-27): later lines, "no problem", "why not", "let me check"."""
    from rpt_agent.services import hang_up_booking as h

    for reply in ("Sure, why not.", "No problem, book it.", "Hello? Yes.", "Yes please, I have to run",
                  "Sounds great.", "I'll take it.", "Yep, see you then."):
        assert h.reply_kind(reply) == "yes", reply
    for reply in ("Okay, let me check with my wife.", "Okay, I'll call you back.", "let me think",
                  "Yes, wait.", "Ok but not with Thao"):
        assert h.reply_kind(reply) is None, reply
    for reply in ("Okay so", "Okay, um", "Sure, but can my wife come too?",
                  "No, that's fine, go ahead", "Yes, sorry, what time?"):
        assert h.reply_kind(reply) == "maybe", reply
    # Vapi splits speech at pauses: a correction in a later line is never booked.
    assert h._first_reply(["Yes.", "Wait, actually make it eleven."]) == "maybe"
    assert h._first_reply(["Yes.", "No, wait."]) == "maybe"
    assert h._first_reply(["Yeah.", "Actually, can we do Thursday?"]) == "maybe"
    assert h._first_reply(["Yes.", "Bye."]) == "yes" and h._first_reply(["Hello?", "Yes."]) == "yes"


def test_other_questions_are_not_cut_short_confirmations():
    from rpt_agent.services.hang_up_booking import find_confirmation

    for line in (("Perfect, that's Tuesday at nine with Thao. And just so I pull up the right file, "
                  "your birthday's April twenty-sixth?"),
                 "That's gone, but I've got Tuesday at ten with Thao open. Would that work?",
                 "I've got Tuesday the sixteenth at nine or ten with Thao. Shall I book one?",
                 "That's a great question for our front desk team. Would you like me to transfer you?"):
        assert find_confirmation([("bot", line), ("user", "Yes.")]) is None, line
    assert find_confirmation([("bot", "So that's Tuesday the sixteenth at nine with Thao at our")])[2] \
        is False


ROUND_TWO_REFUSALS = [
    "Nah, I'm good", "No, I'm all good.", "I'm all good.", "Not really.", "Remove my number.",
    "I'm going somewhere else", "We're going elsewhere.", "I found someone else.", "Cancel it.",
    "No. Bye.", "No, sorry.", "I don't want physical therapy.",
]
ROUND_TWO_NOT_REFUSALS = [
    "I don't want Thursday.", "I don't want to miss work, so mornings.", "Yes I'm all set for nine.",
    "No need to call my wife, mornings work.", "stop", "I'm busy.", "Can I cancel it later?",
]


def test_round_two_refusals_and_false_positives():
    from rpt_agent.services.delivery import REFUSAL_PHRASES

    assert len(REFUSAL_PHRASES) == len(set(REFUSAL_PHRASES))
    for reply in ROUND_TWO_REFUSALS:
        assert _refused_after(None, reply), reply
    for reply in ROUND_TWO_NOT_REFUSALS:
        assert not _refused_after(None, reply), reply
        assert not _refused_after("And do mornings or afternoons work better?", reply), reply
