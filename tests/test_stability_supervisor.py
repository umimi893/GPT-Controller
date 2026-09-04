import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.config import Config
from agent_runtime.process_supervisor import ActionSupervisor
from agent_runtime.resilient_bus import ResilientGitBus
from agent_runtime.util import run_process


class HangingWorkerSupervisor(ActionSupervisor):
    def _worker_argv(self, action_path, result_path, progress_path, gate_path):
        code = (
            "import json,sys,time,datetime,pathlib;"
            "progress=pathlib.Path(sys.argv[1]);gate=pathlib.Path(sys.argv[2]);"
            "deadline=time.time()+5;"
            "\nwhile not gate.exists() and time.time()<deadline: time.sleep(0.01)"
            "\nnow=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z');"
            "payload={'protocol':'q-agent-v4-worker-progress','action_id':'hang-test','agent_id':'test-agent',"
            "'worker_pid':0,'updated_at':now,'phase':'step','event':'started','phase_started_at':now,"
            "'step_index':0,'step_type':'browser.playwright','timeout_seconds':1};"
            "progress.write_text(json.dumps(payload),encoding='utf-8');time.sleep(30)"
        )
        return [sys.executable, "-c", code, str(progress_path), str(gate_path)]


class BackgroundChildSupervisor(ActionSupervisor):
    def __init__(self, config, config_path, marker: Path):
        super().__init__(config, config_path)
        self.marker = marker

    def _worker_argv(self, action_path, result_path, progress_path, gate_path):
        code = (
            "import json,sys,time,pathlib,subprocess,datetime;"
            "result=pathlib.Path(sys.argv[1]);gate=pathlib.Path(sys.argv[2]);marker=pathlib.Path(sys.argv[3]);"
            "deadline=time.time()+5;"
            "\nwhile not gate.exists() and time.time()<deadline: time.sleep(0.01)"
            "\nchild=\"import pathlib,sys,time;time.sleep(2);pathlib.Path(sys.argv[1]).write_text('LEAKED',encoding='utf-8')\";"
            "subprocess.Popen([sys.executable,'-c',child,str(marker)]);"
            "now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z');"
            "payload={'protocol':'q-agent-v4-result','action_id':'background-child-test','agent_id':'test-agent',"
            "'started_at':now,'finished_at':now,'status':'succeeded','error':None,'steps':[]};"
            "result.write_text(json.dumps(payload),encoding='utf-8')"
        )
        return [sys.executable, "-c", code, str(result_path), str(gate_path), str(self.marker)]


class StabilitySupervisorTests(unittest.TestCase):
    def make_config(self, root: Path) -> tuple[Config, Path]:
        control = root / "control"
        control.mkdir(parents=True)
        config_path = root / "agent.config.json"
        raw = {
            "control_repo": str(control),
            "remote": "origin",
            "branch": "gpt-controller-control",
            "poll_seconds": 1,
            "agent_id": "test-agent",
            "capabilities": ["general"],
            "workspaces": {},
            "managed_git_workspaces": [],
            "allow_absolute_paths": False,
            "powershell": "pwsh",
            "git": "git",
            "default_timeout_seconds": 5,
            "max_output_bytes": 100000,
            "browser": {"headless_only": True},
            "interaction_policy": {
                "non_interference": True,
                "allow_physical_input": False,
                "allow_foreground_activation": False,
                "allow_visible_gui_launch": False,
            },
            "interactive_host": {"spool": str(root / "interactive"), "timeout_seconds": 5},
            "deploy_profiles": {},
        }
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return Config.load(config_path), config_path

    def test_run_process_timeout_returns_failure_instead_of_leaking_exception(self):
        result = run_process(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=1,
            max_output_bytes=10000,
        )
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["timed_out"])
        self.assertIn("terminated process tree", result["stderr"])

    def test_isolated_worker_is_killed_when_live_step_exceeds_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, config_path = self.make_config(root)
            supervisor = HangingWorkerSupervisor(config, config_path)
            supervisor.POLL_SECONDS = 0.05
            supervisor.STEP_TIMEOUT_GRACE_SECONDS = 0.2
            supervisor.WORKER_BOOT_TIMEOUT_SECONDS = 5
            supervisor.ACTION_OVERHEAD_SECONDS = 5
            action = {
                "protocol": "q-agent-v4",
                "id": "hang-test",
                "timeout_seconds": 2,
                "steps": [{"type": "browser.playwright", "timeout_seconds": 1, "actions": [{"op": "url"}]}],
            }
            result = supervisor.execute(action)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["supervisor"]["timed_out"])
            self.assertIn("exceeded", result["error"])
            self.assertEqual(result["steps"][0]["type"], "browser.playwright")
            self.assertTrue(result["steps"][0]["timed_out"])

    @unittest.skipUnless(os.name == "nt", "Windows Job Object behavior")
    def test_job_object_kills_background_descendant_after_worker_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, config_path = self.make_config(root)
            marker = root / "background-child-survived.txt"
            supervisor = BackgroundChildSupervisor(config, config_path, marker)
            supervisor.POLL_SECONDS = 0.05
            supervisor.WORKER_BOOT_TIMEOUT_SECONDS = 5
            action = {
                "protocol": "q-agent-v4",
                "id": "background-child-test",
                "timeout_seconds": 5,
                "steps": [],
            }
            result = supervisor.execute(action)
            self.assertEqual(result["status"], "succeeded")
            time.sleep(3)
            self.assertFalse(marker.exists(), "background descendant escaped Action Job Object")

    def test_existing_result_reconciles_stale_running_even_for_other_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in ("queue/running", "queue/done", "queue/ambiguous", "results", "claims"):
                (root / folder).mkdir(parents=True, exist_ok=True)
            running = root / "queue/running/action-a.other-agent.json"
            running.write_text(json.dumps({"id": "action-a"}), encoding="utf-8")
            (root / "results/action-a.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")

            bus = ResilientGitBus.__new__(ResilientGitBus)
            bus.repo = root
            bus.c = SimpleNamespace(agent_id="test-agent")
            published = []
            bus._publish_recovery_commit = lambda message: published.append(message)

            count = bus.recover_ambiguous()
            self.assertEqual(count, 1)
            self.assertFalse(running.exists())
            self.assertTrue((root / "queue/done/action-a.json").exists())
            self.assertEqual(len(published), 1)

    def test_own_claim_without_result_becomes_ambiguous_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in ("queue/running", "queue/done", "queue/ambiguous", "results", "claims"):
                (root / folder).mkdir(parents=True, exist_ok=True)
            running = root / "queue/running/action-b.test-agent.json"
            running.write_text(json.dumps({"id": "action-b"}), encoding="utf-8")

            bus = ResilientGitBus.__new__(ResilientGitBus)
            bus.repo = root
            bus.c = SimpleNamespace(agent_id="test-agent")
            published = []
            bus._publish_recovery_commit = lambda message: published.append(message)

            count = bus.recover_ambiguous()
            self.assertEqual(count, 1)
            self.assertFalse(running.exists())
            self.assertTrue((root / "queue/ambiguous/action-b.test-agent.json").exists())
            result = json.loads((root / "results/action-b.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(len(published), 1)


if __name__ == "__main__":
    unittest.main()
