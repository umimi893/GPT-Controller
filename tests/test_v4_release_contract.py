from __future__ import annotations

import json
from pathlib import Path
import tomllib

from agent_runtime import __version__
from agent_runtime.protocol import validate_action

ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_and_public_product_metadata_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "4.0.0"
    assert __version__ == "4.0.0"
    assert project["name"] == "gpt-controller-runtime"
    assert "gpt-controller" in project["scripts"]
    assert "gpt-controller-interactive" in project["scripts"]
    assert "q-agent-v4" in project["scripts"]
    assert "q-agent-v4-interactive" in project["scripts"]
    assert "agent1" not in project["scripts"]


def test_canonical_v4_wire_protocol_is_retained_for_compatibility():
    validate_action({"protocol": "q-agent-v4", "id": "v4-smoke", "steps": [{"type": "noop"}]})


def test_prototype_protocol_is_not_accepted():
    try:
        validate_action({"protocol": "agent-1.0", "id": "prototype", "steps": [{"type": "noop"}]})
    except ValueError:
        pass
    else:
        raise AssertionError("Agent 1.0 protocol must not be accepted")


def test_example_config_uses_private_gpt_controller_bus_and_public_paths():
    cfg = json.loads((ROOT / "examples" / "agent.config.example.json").read_text(encoding="utf-8"))
    assert cfg["branch"] == "gpt-controller-control"
    assert "GPT-Controller" in cfg["control_repo"]
    assert "GPT-Controller" in cfg["interactive_host"]["spool"]
    assert cfg["managed_git_workspaces"] == []
    assert cfg["deploy_profiles"] == {}


def test_public_export_does_not_ship_private_migration_cleanup():
    assert not (ROOT / "scripts" / "remove-legacy-q-agent.ps1").exists()


def test_chatgpt_entrypoint_separates_public_source_from_private_control_repo():
    text = (ROOT / "CHATGPT_CONTROLLER.md").read_text(encoding="utf-8")
    assert "umimi893/GPT-Controller" in text
    assert "public source repository" in text
    assert "gpt-controller-control" in text
    assert '"protocol": "q-agent-v4"' in text


def test_schema_ids_remain_v4_compatible():
    action = json.loads((ROOT / "schemas" / "action.schema.json").read_text(encoding="utf-8"))
    result = json.loads((ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8"))
    assert action["$id"].endswith("/action-v4.json")
    assert result["$id"].endswith("/result-v4.json")
