import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.config import Config


class InteractionPolicyTests(unittest.TestCase):
    def _load(self, interaction_policy=None, browser=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = {
                "agent_id": "test-agent",
                "control_repo": str(root / "control"),
                "interaction_policy": interaction_policy or {},
                "browser": browser or {},
            }
            config_path = root / "agent.config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            return Config.load(config_path)

    def test_defaults_are_non_interfering(self):
        config = self._load()
        self.assertTrue(config.non_interference)
        self.assertFalse(config.allow_physical_input)
        self.assertFalse(config.allow_foreground_activation)
        self.assertTrue(config.browser_headless_only)

    def test_non_interference_rejects_intrusive_controls(self):
        with self.assertRaisesRegex(ValueError, "non_interference=true"):
            self._load({
                "non_interference": True,
                "allow_physical_input": True,
            })
        with self.assertRaisesRegex(ValueError, "non_interference=true"):
            self._load({
                "non_interference": True,
                "allow_foreground_activation": True,
            })

    def test_non_interference_rejects_headed_managed_browser(self):
        with self.assertRaisesRegex(ValueError, "headless_only=false"):
            self._load(
                {"non_interference": True},
                {"headless_only": False},
            )

    def test_dedicated_agent_can_opt_in_to_full_ui_control(self):
        config = self._load(
            {
                "non_interference": False,
                "allow_physical_input": True,
                "allow_foreground_activation": True,
                "allow_visible_gui_launch": True,
            },
            {"headless_only": False},
        )
        self.assertFalse(config.non_interference)
        self.assertTrue(config.allow_physical_input)
        self.assertTrue(config.allow_foreground_activation)
        self.assertTrue(config.allow_visible_gui_launch)
        self.assertFalse(config.browser_headless_only)


if __name__ == "__main__":
    unittest.main()
