from argparse import Namespace
from pathlib import Path

import pytest

from rpt_agent.cli import create_test_lead
from rpt_agent.config import Settings


def _args() -> Namespace:
    return Namespace(
        phone="+15555550123",
        first_name="Synthetic",
        last_name="Tester",
        dob="1990-01-01",
        consent_reference="unit-test-consent",
    )


def test_test_lead_requires_test_mode(monkeypatch):
    """A deployed box may create synthetic leads, but only with the compressed clock on."""
    monkeypatch.setattr(
        "rpt_agent.cli.get_settings",
        lambda: Settings(app_env="production", test_mode=False),
    )
    with pytest.raises(RuntimeError, match="set TEST_MODE=true"):
        create_test_lead(_args())


def test_reset_sql_can_only_target_marked_synthetic_leads():
    sql = Path("supabase/dev/reset_test_lead_by_name.sql").read_text(encoding="utf-8")
    assert "l.is_test is true" in sql
    assert "l.source_system = 'synthetic_test'" in sql
    assert "first_name" in sql and "last_name" in sql
    assert "suppressed_numbers" not in sql
