from __future__ import annotations

import json
from pathlib import Path

from app.domain.defaults import default_participants

ROOT = Path(__file__).resolve().parents[2]


def test_prompt_template_examples_cover_default_participant_templates() -> None:
    payload = json.loads((ROOT / "examples" / "prompt-templates.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == "dialecticore.prompt_templates.v1"
    template_by_id = {template["id"]: template for template in payload["templates"]}
    default_template_ids = {profile.system_prompt_template for profile in default_participants()}
    assert default_template_ids <= set(template_by_id)
    for template_id in default_template_ids:
        template = template_by_id[template_id]
        assert template["participant_type"] in {"host", "panelist"}
        assert template["version"]
        assert template["created_by"]
        assert template["created_at"]
        assert template["enabled"] is True
        assert template["change_summary"]
        assert "Return only JSON" in template["system"]
        assert "{public_transcript}" in template["user"]
        assert "{private_memory}" in template["user"]
        assert template["expected_behavior"]


def test_synthetic_test_env_uses_mock_local_and_no_paid_integrations() -> None:
    values = _env_values(ROOT / "examples" / "synthetic-test.env")

    assert values["DIALECTICORE_MODEL_PROVIDER"] == "mock"
    assert values["DIALECTICORE_OBJECT_STORAGE_BACKEND"] == "local"
    assert values["DIALECTICORE_AUTH_ENABLED"] == "false"
    assert values["DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED"] == "false"
    assert values["DIALECTICORE_RESEARCH_DISCOVERY_ENABLED"] == "false"
    assert values["DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_ENABLED"] == "false"
    assert values["DIALECTICORE_TEMPORAL_BACKEND_MODE"] == "local"
    assert not values["DIALECTICORE_RESEARCH_DISCOVERY_URL_TEMPLATE"]
    assert not values["DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_URL"]
    assert not values["DIALECTICORE_AUTH_API_KEY_REFERENCE"]


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator == "=", f"invalid env line: {raw_line}"
        values[key] = value
    return values
