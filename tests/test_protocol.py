import json
import unittest
from pathlib import Path

from agent_runtime.protocol import SUPPORTED, validate_action


class ProtocolTests(unittest.TestCase):
    def test_minimal(self):
        validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "noop"}]})

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "think"}]})

    def test_powershell_requires_script(self):
        with self.assertRaisesRegex(ValueError, "script is required"):
            validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "powershell.exec"}]})

    def test_file_requires_workspace(self):
        with self.assertRaisesRegex(ValueError, "workspace is required"):
            validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "file.read", "path": "x.txt"}]})

    def test_git_exec_requires_args_and_workspace(self):
        with self.assertRaises(ValueError):
            validate_action({"protocol": "q-agent-v4", "id": "a-1", "workspace": "scratch", "steps": [{"type": "git.exec", "args": []}]})
        validate_action({"protocol": "q-agent-v4", "id": "a-1", "workspace": "scratch", "steps": [{"type": "git.exec", "args": ["status"]}]})

    def test_workspace_guard_validation(self):
        validate_action({
            "protocol": "q-agent-v4", "id": "a-1", "workspace": "example-project",
            "workspace_guard": {"allow_dirty": True, "allow_branch_mismatch": True},
            "steps": [{"type": "git.exec", "args": ["status"]}],
        })
        with self.assertRaisesRegex(ValueError, "allow_dirty must be boolean"):
            validate_action({
                "protocol": "q-agent-v4", "id": "a-1", "workspace": "example-project",
                "workspace_guard": {"allow_dirty": "yes"}, "steps": [{"type": "git.exec", "args": ["status"]}],
            })
        with self.assertRaisesRegex(ValueError, "allow_branch_mismatch must be boolean"):
            validate_action({
                "protocol": "q-agent-v4", "id": "a-1", "workspace": "example-project",
                "workspace_guard": {"allow_branch_mismatch": "yes"}, "steps": [{"type": "git.exec", "args": ["status"]}],
            })

    def test_browser_nested_validation(self):
        with self.assertRaisesRegex(ValueError, "url is required"):
            validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "browser.playwright", "actions": [{"op": "goto"}]}]})
        validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "browser.playwright", "actions": [{"op": "goto", "url": "https://example.com"}]}]})

    def test_browser_cdp_and_interactive_validation(self):
        validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "browser.playwright", "connection": "cdp", "actions": [{"op": "goto", "url": "https://example.com"}]}]})
        validate_action({"protocol": "q-agent-v4", "id": "a-2", "steps": [{"type": "browser.interactive", "op": "launch", "url": "https://accounts.google.com/"}]})
        validate_action({"protocol": "q-agent-v4", "id": "a-3", "steps": [{"type": "browser.interactive", "op": "status"}]})
        with self.assertRaisesRegex(ValueError, "connection must be"):
            validate_action({"protocol": "q-agent-v4", "id": "a-4", "steps": [{"type": "browser.playwright", "connection": "remote", "actions": [{"op": "url"}]}]})
        with self.assertRaisesRegex(ValueError, "browser.interactive op"):
            validate_action({"protocol": "q-agent-v4", "id": "a-5", "steps": [{"type": "browser.interactive", "op": "click"}]})

    def test_action_schema_step_types_match_runtime_protocol(self):
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "action.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema_types = set(schema["properties"]["steps"]["items"]["properties"]["type"]["enum"])
        self.assertEqual(schema_types, SUPPORTED)

    def test_windows_ui_requires_one_source(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate_action({"protocol": "q-agent-v4", "id": "a-1", "steps": [{"type": "windows.ui", "actions": [{"op": "wait_ready"}]}]})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate_action({
                "protocol": "q-agent-v4", "id": "a-2", "target": {"mode": "agent", "agent": "worker-pc"},
                "steps": [{"type": "windows.ui", "connect": {"title": "Existing"}, "start": "notepad.exe", "actions": [{"op": "wait_ready"}]}],
            })
        validate_action({"protocol": "q-agent-v4", "id": "a-3", "steps": [{"type": "windows.ui", "connect": {"title": "Existing"}, "actions": [{"op": "wait_ready"}]}]})

    def test_intrusive_windows_ui_requires_exact_target(self):
        with self.assertRaisesRegex(ValueError, "exact target"):
            validate_action({
                "protocol": "q-agent-v4", "id": "a-4",
                "steps": [{"type": "windows.ui", "start": "notepad.exe", "actions": [{"op": "type_text", "text": "hello"}]}],
            })
        validate_action({
            "protocol": "q-agent-v4", "id": "a-5", "target": {"mode": "agent", "agent": "worker-pc"},
            "steps": [{"type": "windows.ui", "start": "notepad.exe", "actions": [{"op": "focus"}, {"op": "type_text", "text": "こんにちは"}]}],
        })
        with self.assertRaisesRegex(ValueError, "exact target"):
            validate_action({
                "protocol": "q-agent-v4", "id": "a-6", "target": {"mode": "any"},
                "steps": [{"type": "windows.ui", "desktop": True, "actions": [{"op": "list_windows"}]}],
            })
        validate_action({
            "protocol": "q-agent-v4", "id": "a-7", "target": {"mode": "agent", "agent": "worker-pc"},
            "steps": [{"type": "windows.ui", "desktop": True, "actions": [{"op": "list_windows"}, {"op": "click_at", "coords": [100, 100]}]}],
        })

    def test_windows_ui_action_shapes(self):
        with self.assertRaisesRegex(ValueError, "type_keys requires"):
            validate_action({"protocol": "q-agent-v4", "id": "a-8", "target": {"mode": "agent", "agent": "worker-pc"}, "steps": [{"type": "windows.ui", "connect": {"title": "Existing"}, "actions": [{"op": "type_keys"}]}]})
        with self.assertRaisesRegex(ValueError, "requires coords"):
            validate_action({"protocol": "q-agent-v4", "id": "a-9", "target": {"mode": "agent", "agent": "worker-pc"}, "steps": [{"type": "windows.ui", "desktop": True, "actions": [{"op": "click_at"}]}]})
        with self.assertRaisesRegex(ValueError, "coordinates"):
            validate_action({"protocol": "q-agent-v4", "id": "a-10", "target": {"mode": "agent", "agent": "worker-pc"}, "steps": [{"type": "windows.ui", "desktop": True, "actions": [{"op": "drag", "start": [1], "end": [2, 3]}]}]})

    def test_ordered_target_requires_created_at(self):
        with self.assertRaises(ValueError):
            validate_action({"protocol": "q-agent-v4", "id": "a-1", "target": {"mode": "ordered", "order": ["desktop-pc", "worker-pc"]}, "steps": [{"type": "noop"}]})

    def test_ordered_target(self):
        validate_action({
            "protocol": "q-agent-v4", "id": "a-1", "created_at": "2026-08-30T00:00:00Z", "workspace": "example-project",
            "target": {"mode": "ordered", "order": ["desktop-pc", "worker-pc"], "fallback_after_seconds": 8},
            "requires": ["git"], "steps": [{"type": "workspace.git_sync"}],
        })


if __name__ == "__main__":
    unittest.main()
