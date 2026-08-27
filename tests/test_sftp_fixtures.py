from rpt_agent.observability import WorkflowTrace
from rpt_agent.sftp_fixtures import load_stride_fixtures


def test_stride_fixture_bundle_loads_all_delta_types():
    result = load_stride_fixtures(WorkflowTrace("fixtures", "test", "fixtures-trace"))
    assert set(result) == {"patients", "cases", "users", "locations"}
    assert all(rows[0]["Action"] == "upsert" for rows in result.values())

