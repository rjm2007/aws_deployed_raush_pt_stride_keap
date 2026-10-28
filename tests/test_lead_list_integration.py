"""Paged Leads list and board on a real Postgres schema (2026-10-28).

Needs TEST_DATABASE_URL (a disposable local database). Reuses the Stride import fixture, which builds
its own practice and removes it afterwards. All names and numbers are synthetic (555).
"""

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from test_stride_import_integration import (  # noqa: F401 (env is a fixture)
    APPOINTMENT_HEADER,
    PATIENT_HEADER,
    appointment,
    csv_bytes,
    env,
    future_day,
    patient,
)

from rpt_agent.routes import dashboard
from rpt_agent.security import DashboardActor
from rpt_agent.services.lead_stage import CLOSED_STATUSES, STAGE_SQL, stage_of

pytestmark = pytest.mark.integration

STATUSES = ["new", "in_progress", "callback_scheduled", "needs_attention", "booked", "transferred_human",
            "booking_link_sent", *sorted(CLOSED_STATUSES)]
CADENCE_STATES = ["pending", "active", "paused", "completed", "terminated"]
# none: no events; planned: a versioned step still to run; spent: every versioned step ran;
# unversioned: old events with no cadence version (cadence_total 0); in_flight: awaiting a provider.
EVENTS = ["none", "planned", "spent", "unversioned", "in_flight"]


def _actor(ctx):
    return DashboardActor("staff-1", "staff@example.test", practice_id=ctx.practice_id)


def _version(ctx):
    return ctx.db.execute(
        "insert into cadence_versions(practice_id,version_number,name,status) values(%s,1,'Standard','active') "
        "returning id", (ctx.practice_id,)).fetchone()["id"]


def _lead(ctx, name, *, status="in_progress", cadence="active", review=False, events="planned", version=None,
          source="google_sheets", owner=None, location=None, phone="+15552019999", email=None, created=None):
    lead_id = ctx.db.execute(
        "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,email,status,"
        "cadence_state,needs_review,owner,location,is_test,created_at) "
        "values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,coalesce(%s,now())) returning id",
        (ctx.practice_id, source, name, name.split()[0], name.split()[-1], phone, email, status, cadence,
         review, owner, location, created),
    ).fetchone()["id"]
    plan = {"none": [], "planned": [("delivered", version), ("planned", version)],
            "spent": [("delivered", version), ("failed", version)], "unversioned": [("delivered", None)],
            "in_flight": [("in_flight", version)]}[events]
    for attempt, (state, cv) in enumerate(plan, 1):
        ctx.db.execute(
            "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for,cadence_version_id) "
            "values(%s,%s,'call',%s,now(),%s)", (lead_id, attempt, state, cv))
    return str(lead_id)


def test_sql_stage_matches_stage_of_for_every_combination(env):  # noqa: F811
    ctx = env
    version = _version(ctx)
    for n, (status, cadence, review, events) in enumerate(product(STATUSES, CADENCE_STATES, [False, True], EVENTS)):
        _lead(ctx, f"Grid Lead{n}", status=status, cadence=cadence, review=review, events=events, version=version)
    rows = ctx.db.execute(dashboard._LEAD_SELECT + "where l.practice_id=%s", (ctx.practice_id,)).fetchall()
    sql = {str(r["id"]): r["stage"] for r in ctx.db.execute(
        "select l.id," + STAGE_SQL + " as stage from leads l where l.practice_id=%s", (ctx.practice_id,))}
    assert len(rows) == len(STATUSES) * len(CADENCE_STATES) * 2 * len(EVENTS)
    seen = set()
    for row in rows:
        expected = stage_of(row)
        seen.add(expected)
        assert sql[str(row["id"])] == expected, {k: row[k] for k in ("status", "cadence_state", "needs_review",
                                                                     "cadence_total", "next_event_id")}
    assert seen == {"new", "cadence", "attention", "booked", "closed"}


def _stride_setup(ctx):
    # Sheet lead "Sheet Booked" matches Stride patient 101 (linked: never a Stride row).
    # 102 front desk patient with an upcoming visit, 103 past visit only, 104 no visits.
    ctx.lead("Sheet Booked", dob="1990-01-01", phone="+15552010001")
    past = (datetime.now(UTC).date() - timedelta(days=20)).isoformat()
    ctx.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", "Sheet", "Booked", "1990-01-01", mobile="(555) 201-0001"),
        patient("102", "Walk", "Upcoming", "1985-05-05", mobile="(555) 201-0102", **{"Personal Email": "walk@example.test"}),
        patient("103", "Past", "Visitor", "1980-03-03", mobile="(555) 201-0103"),
        patient("104", "Never", "Seen", "1970-07-07", mobile="(555) 201-0104"),
    ]))
    ctx.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", future_day(3)),
        appointment("9002", "102", "7002", future_day(4), **{"Location Name": "Rausch PT - Dana Point"}),
        appointment("9003", "103", "7003", past, status="Checked In"),
    ]))
    ctx.tick()


def test_list_mixes_sheet_leads_and_unlinked_stride_patients(env):  # noqa: F811
    ctx = env
    _stride_setup(ctx)
    _lead(ctx, "Manual Person", source="manual", events="none", status="new", cadence="pending")
    actor = _actor(ctx)

    page = dashboard.dashboard_lead_list(actor)
    assert page["sources"] == {"all": 5, "sheet": 1, "stride": 3}
    assert page["total"] == 5
    by_name = {r["full_name"]: r for r in page["rows"]}
    assert set(by_name) == {"Sheet Booked", "Manual Person", "Walk Upcoming", "Past Visitor", "Never Seen"}
    # Source is where the row started: the booked Sheet lead stays a lead, never a Stride row.
    assert by_name["Sheet Booked"]["kind"] == "lead" and by_name["Sheet Booked"]["stage"] == "booked"
    walk = by_name["Walk Upcoming"]
    assert walk["kind"] == "stride" and walk["stage"] == "booked" and walk["next_step"] == "View only"
    assert walk["phone"] == "+15552010102"
    assert by_name["Past Visitor"]["stage"] is None and by_name["Past Visitor"]["stage_label"] == "No upcoming visit"
    assert by_name["Never Seen"]["stage_label"] == "No visits"

    sheet = dashboard.dashboard_lead_list(actor, source="sheet")
    assert [r["full_name"] for r in sheet["rows"]] == ["Sheet Booked"] and sheet["total"] == 1
    stride = dashboard.dashboard_lead_list(actor, source="stride")
    assert stride["total"] == 3 and all(r["kind"] == "stride" for r in stride["rows"])
    # Stage Booked keeps the booked Stride patient; any other stage drops every Stride row.
    booked = dashboard.dashboard_lead_list(actor, stage=["booked"])
    assert {r["full_name"] for r in booked["rows"]} == {"Sheet Booked", "Walk Upcoming"}
    assert dashboard.dashboard_lead_list(actor, stage=["new"])["sources"] == {"all": 1, "sheet": 0, "stride": 0}
    # Owner: Stride patients have none.
    assert dashboard.dashboard_lead_list(actor, owner="Unassigned")["sources"]["stride"] == 0
    # Search reaches Stride names, emails and phone digits.
    assert [r["full_name"] for r in dashboard.dashboard_lead_list(actor, q="walk@example")["rows"]] == ["Walk Upcoming"]
    assert [r["full_name"] for r in dashboard.dashboard_lead_list(actor, q="(555) 201-0103")["rows"]] == ["Past Visitor"]
    assert [r["full_name"] for r in dashboard.dashboard_lead_list(actor, location="Dana Point")["rows"]] == ["Walk Upcoming"]

    view = dashboard.dashboard_stride_patient(102, actor)
    assert view["patient"]["clinic"] == "Rausch PT - Dana Point" and view["patient"]["date_of_birth"]
    assert [v["id"] for v in view["visits"]] == [9002] and view["visits"][0]["status"] == "unmarked"
    # A linked patient is the lead's, not a Stride row.
    with pytest.raises(Exception) as missing:
        dashboard.dashboard_stride_patient(101, actor)
    assert missing.value.status_code == 404


def test_list_pages_search_and_owner_filter_across_every_lead(env):  # noqa: F811
    ctx = env
    version = _version(ctx)
    start = datetime.now(UTC) - timedelta(days=30)
    for n in range(620):
        _lead(ctx, f"Paged Lead{n:04d}", version=version, created=start + timedelta(minutes=n),
              owner="Ana" if n % 2 else None, location="Dana Point" if n % 3 == 0 else "Laguna Niguel",
              phone=f"+1555300{n:04d}", email=f"paged{n}@example.test")
    actor = _actor(ctx)

    first = dashboard.dashboard_lead_list(actor)
    assert first["total"] == 620 and len(first["rows"]) == 500
    assert first["rows"][0]["full_name"] == "Paged Lead0619"  # newest first
    second = dashboard.dashboard_lead_list(actor, offset=500)
    assert len(second["rows"]) == 120 and second["rows"][-1]["full_name"] == "Paged Lead0000"
    assert not {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]}
    small = dashboard.dashboard_lead_list(actor, limit=25, offset=25)
    assert [r["full_name"] for r in small["rows"]] == [r["full_name"] for r in first["rows"][25:50]]

    # The oldest lead (far past the old 250 cap) is found by name, phone and email.
    assert [r["full_name"] for r in dashboard.dashboard_lead_list(actor, q="lead0000")["rows"]] == ["Paged Lead0000"]
    assert [r["full_name"] for r in dashboard.dashboard_lead_list(actor, q="555-300-0007")["rows"]] == ["Paged Lead0007"]
    assert dashboard.dashboard_lead_list(actor, q="paged12@")["total"] == 1
    # A literal % is text, not a wildcard.
    assert dashboard.dashboard_lead_list(actor, q="%")["total"] == 0

    assert dashboard.dashboard_lead_list(actor, owner="Ana")["total"] == 310
    assert dashboard.dashboard_lead_list(actor, owner="Unassigned")["total"] == 310
    assert dashboard.dashboard_lead_list(actor, location="Dana Point")["total"] == 207
    assert dashboard.dashboard_lead_list(actor, stage=["cadence"])["total"] == 620
    assert dashboard.dashboard_lead_list(actor, stage=["new", "closed"])["total"] == 0


def test_board_columns_have_real_totals_and_page_by_25(env):  # noqa: F811
    ctx = env
    version = _version(ctx)
    start = datetime.now(UTC) - timedelta(days=10)
    for n in range(60):
        _lead(ctx, f"Board Cadence{n:03d}", version=version, created=start + timedelta(minutes=n),
              owner="Ana" if n < 10 else None)
    for n in range(30):
        _lead(ctx, f"Board New{n:03d}", status="new", cadence="pending", events="none",
              created=start + timedelta(minutes=n))
    _lead(ctx, "Board Flagged", review=True, version=version)
    _lead(ctx, "Board Manual", source="manual", status="declined", cadence="terminated", events="none")
    actor = _actor(ctx)

    board = dashboard.dashboard_lead_board(actor)
    totals = {stage: column["total"] for stage, column in board["columns"].items()}
    assert totals == {"new": 30, "cadence": 60, "attention": 1, "booked": 0, "closed": 1}
    assert len(board["columns"]["cadence"]["rows"]) == 25
    assert board["columns"]["cadence"]["rows"][0]["full_name"] == "Board Cadence059"
    # Cards are the snapshot's shape (review card, cadence progress).
    flagged = board["columns"]["attention"]["rows"][0]
    assert flagged["stage"] == "attention" and flagged["cadence_total"] == 2

    more = dashboard.dashboard_lead_board(actor, stage=["cadence"], offset=50)
    assert list(more["columns"]) == ["cadence"]
    assert [r["full_name"] for r in more["columns"]["cadence"]["rows"]] == [
        f"Board Cadence{n:03d}" for n in range(9, -1, -1)]
    assert dashboard.dashboard_lead_board(actor, owner="Ana")["columns"]["cadence"]["total"] == 10
    searched = dashboard.dashboard_lead_board(actor, q="Board New00")
    assert searched["columns"]["new"]["total"] == 10 and searched["columns"]["cadence"]["total"] == 0


def test_snapshot_counts_every_lead_not_the_loaded_250(env):  # noqa: F811
    ctx = env
    actor = _actor(ctx)
    before = dashboard.dashboard_snapshot(actor)
    version = _version(ctx)
    old = datetime.now(UTC) - timedelta(days=400)
    flagged = _lead(ctx, "Old Flagged", review=True, version=version, created=old, location="Mission Viejo")
    for n in range(260):
        _lead(ctx, f"Count Lead{n:03d}", status="new", cadence="pending", events="none", location="Dana Point")
    after = dashboard.dashboard_snapshot(actor)
    assert after["counts"]["new"] - before["counts"]["new"] == 260
    assert after["counts"]["attention"] - before["counts"]["attention"] == 1
    assert after["metrics"]["total_leads"] - before["metrics"]["total_leads"] == 261
    assert after["counts_by_location"]["Dana Point"]["new"] - (
        before["counts_by_location"].get("Dana Point", {}).get("new", 0)) == 260
    # An old lead waiting for review is still in the snapshot (the Review queue reads it).
    assert flagged in {lead["id"] for lead in after["leads"]}
