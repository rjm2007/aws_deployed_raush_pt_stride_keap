"""The production image must contain the files the SMS agent reads at runtime.

The first demo deploy (2026-11-02) copied only src/ and supabase/, so every test-chat reply failed with
FileNotFoundError for config/sms_agent_prompt.md.
"""

from pathlib import Path

from rpt_agent.services import sms_agent

ROOT = Path(__file__).resolve().parents[1]


def test_production_image_copies_the_sms_agent_prompt_and_tools():
    dockerfile = (ROOT / "Dockerfile.prod").read_text()
    for path in (sms_agent.PROMPT_FILE, sms_agent.TOOLS_FILE):
        relative = path.relative_to(ROOT).as_posix()
        assert relative in dockerfile, f"Dockerfile.prod does not copy {relative}"
        assert path.exists()


def test_config_dir_is_the_repo_config_folder():
    # In the image the package runs from /app/src (PYTHONPATH), so CONFIG_DIR is /app/config.
    assert sms_agent.CONFIG_DIR == ROOT / "config"
