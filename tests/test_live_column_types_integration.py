"""The Sheet paths work on the LIVE column types of lead_action_requests, not only the migrated ones.

Needs TEST_DATABASE_URL (a disposable local database with the migrations applied).

2026-10-22 (caught before deploy, by the demo chat): the live databases were filled by a copy on
2026-10-10 and store lead_action_requests.request_id / lead_id / practice_id as TEXT, with no foreign
keys. The migrations create them as uuid / uuid / bigint, so every test database had the other types
and a `text = uuid` comparison only failed live ("operator does not exist: text = uuid"). This file
turns the table into the live shape for its tests and puts it back afterwards.
"""

import pytest
from test_lead_review_integration import _get, _post, _started, review_env  # noqa: F401
from test_sheet_direct_integration import env  # noqa: F401 - the shared Sheet fixture

pytestmark = pytest.mark.integration

LIVE_SHAPE = (
    "alter table lead_action_requests "
    "drop constraint if exists lead_action_requests_lead_id_fkey, "
    "drop constraint if exists lead_action_requests_practice_id_fkey; "
    "alter table lead_action_requests alter column request_id type text, "
    "alter column lead_id type text, alter column practice_id type text"
)
MIGRATED_SHAPE = (
    "alter table lead_action_requests alter column request_id type uuid using request_id::uuid, "
    "alter column lead_id type uuid using lead_id::uuid, "
    "alter column practice_id type bigint using practice_id::bigint; "
    "alter table lead_action_requests "
    "add constraint lead_action_requests_lead_id_fkey foreign key (lead_id) "
    "references leads(id) on delete set null not valid, "
    "add constraint lead_action_requests_practice_id_fkey foreign key (practice_id) "
    "references practices(id) on delete restrict not valid"
)


@pytest.fixture
def live(review_env):  # noqa: F811 - pytest fixture injection
    review_env.db.execute(LIVE_SHAPE)
    try:
        yield review_env
    finally:
        # Rows other tests may still reference must survive the type change back.
        review_env.db.execute(
            "delete from lead_action_requests where lead_id is not null "
            "and lead_id not in (select id::text from leads)")
        review_env.db.execute(MIGRATED_SHAPE)


def test_the_live_shape_is_what_this_file_tests(live):
    types = {row["column_name"]: row["data_type"] for row in live.db.execute(
        "select column_name,data_type from information_schema.columns "
        "where table_name='lead_action_requests' and column_name in ('request_id','lead_id','practice_id')")}
    assert types == {"request_id": "text", "lead_id": "text", "practice_id": "text"}


def test_a_resolved_review_still_updates_the_sheet_on_live_types(live):
    """sheet_sync._review_status compared lead_action_requests.lead_id (text) with a uuid."""
    number, lead_id = _started(live)
    live.sheet.set(number, "name", "Asha Kumar")
    live.tick()
    live.tick()
    assert live.cell(number, "outcome_status") == "Needs review: name changed"
    response = _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"})
    assert response.status_code == 200, response.text
    live.tick()
    assert live.cell(number, "outcome_status") == "Resolved: new name used"
    assert live.cell(number, "lead_status") == "In cadence"


def test_a_start_row_rerun_links_the_lead_on_live_types(live):
    """sheet_direct._start_row_already_done joined leads.id (uuid) to lead_action_requests.lead_id (text)."""
    number = live.start_row()
    live.tick()
    request_id, lead_id = live.cell(number, "action_request_id"), live.cell(number, "lead_id")
    live.sheet.set(number, "lead_id", "")        # the result never reached the Sheet,
    live.sheet.set(number, "outcome_status", "")  # and staff cleared the status to try again
    live.tick()
    assert live.cell(number, "lead_id") == lead_id and len(live.leads()) == 1
    assert live.cell(number, "action_request_id") == request_id


def test_an_old_command_is_recorded_and_found_on_live_types(live):
    """sheet_direct._record_handled / _ran_actions (2026-10-23) on the live text columns."""
    number, lead_id = _started(live)
    live.sheet.set(number, "action", "Booked")
    live.sheet.set(number, "outcome_status", "")
    live.tick()
    assert live.cell(number, "outcome_status") == "Booked"
    live.sheet.set(number, "action", "Restart cadence")  # turned down next to "Booked": recorded
    live.tick()
    request_id = live.cell(number, "action_request_id")
    stored = live.db.execute("select action,lead_id,status from lead_action_requests where request_id=%s",
                             (request_id,)).fetchone()
    assert (stored["action"], stored["lead_id"], stored["status"]) == ("restart_cadence", lead_id, "failed")
    live.tick()
    assert live.cell(number, "action_request_id") == request_id  # found again, not recorded twice
