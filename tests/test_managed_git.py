import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_runtime.config import Config
from agent_runtime.executor import Executor


class ManagedGitTests(unittest.TestCase):
    def _run(self, *args, cwd=None):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)

    def _fixture(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        remote = root / "remote.git"
        seed = root / "seed"
        work = root / "work"
        control = root / "control"
        control.mkdir()

        self._run("git", "init", "--bare", str(remote))
        self._run("git", "init", str(seed))
        self._run("git", "config", "user.name", "Agent Test", cwd=seed)
        self._run("git", "config", "user.email", "agent-test@example.invalid", cwd=seed)
        (seed / "README.md").write_text("initial\n", encoding="utf-8")
        self._run("git", "add", "README.md", cwd=seed)
        self._run("git", "commit", "-m", "initial", cwd=seed)
        self._run("git", "branch", "-M", "main", cwd=seed)
        self._run("git", "remote", "add", "origin", str(remote), cwd=seed)
        self._run("git", "push", "-u", "origin", "main", cwd=seed)
        self._run("git", "clone", "--branch", "main", str(remote), str(work))

        cfg_path = root / "config.json"
        cfg_path.write_text(json.dumps({
            "agent_id": "test-agent",
            "control_repo": str(control),
            "workspaces": {"example-project": str(work)},
            "capabilities": ["general", "git"],
            "interaction_policy": {
                "non_interference": True,
                "allow_physical_input": False,
                "allow_foreground_activation": False,
                "allow_visible_gui_launch": False
            },
            "browser": {"headless_only": True}
        }), encoding="utf-8")
        return td, work, Config.load(cfg_path)

    def test_existing_local_gen_defaults_to_managed(self):
        td, _, config = self._fixture()
        try:
            self.assertEqual(config.managed_git_workspaces, frozenset({"example-project"}))
            self.assertEqual(config.managed_git_expected_branches, {"example-project": "main"})
        finally:
            td.cleanup()

    def test_wrong_branch_refuses_without_checkout(self):
        td, work, config = self._fixture()
        try:
            self._run("git", "checkout", "-b", "feature", cwd=work)
            result = Executor(config).execute({
                "protocol": "q-agent-v4",
                "id": "branch-guard",
                "workspace": "example-project",
                "steps": [{"type": "git.exec", "args": ["status", "--short", "--branch"]}],
            })
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["steps"], [])
            self.assertIn("expected branch 'main'", result["error"])
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=work, text=True).strip()
            self.assertEqual(branch, "feature")
        finally:
            td.cleanup()

    def test_explicit_branch_mismatch_override_syncs_feature_branch(self):
        td, work, config = self._fixture()
        try:
            self._run("git", "checkout", "-b", "feature", cwd=work)
            self._run("git", "push", "-u", "origin", "feature", cwd=work)
            result = Executor(config).execute({
                "protocol": "q-agent-v4",
                "id": "branch-override",
                "workspace": "example-project",
                "workspace_guard": {"allow_branch_mismatch": True},
                "steps": [{"type": "git.exec", "args": ["status", "--short", "--branch"]}],
            })
            self.assertEqual(result["status"], "succeeded")
            pre = result["workspace_guards"]["example-project"]["pre"]
            self.assertTrue(pre["branch_mismatch"])
            self.assertEqual(pre["branch"], "feature")
            self.assertEqual(pre["head"], pre["remote_head"])
        finally:
            td.cleanup()

    def test_local_unpushed_commit_refuses_before_execution(self):
        td, work, config = self._fixture()
        try:
            self._run("git", "config", "user.name", "Agent Test", cwd=work)
            self._run("git", "config", "user.email", "agent-test@example.invalid", cwd=work)
            (work / "local.txt").write_text("local only\n", encoding="utf-8")
            self._run("git", "add", "local.txt", cwd=work)
            self._run("git", "commit", "-m", "local only", cwd=work)
            result = Executor(config).execute({
                "protocol": "q-agent-v4",
                "id": "ahead-guard",
                "workspace": "example-project",
                "steps": [{"type": "git.exec", "args": ["status"]}],
            })
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["steps"], [])
            self.assertIn("not identical to origin/main", result["error"])
        finally:
            td.cleanup()

    def test_dirty_workspace_refuses_before_execution(self):
        td, work, config = self._fixture()
        try:
            (work / "dirty.txt").write_text("do not overwrite\n", encoding="utf-8")
            result = Executor(config).execute({
                "protocol": "q-agent-v4",
                "id": "dirty-guard",
                "workspace": "example-project",
                "steps": [{"type": "process.exec", "program": sys.executable, "args": ["-c", "print('should not run')"]}],
            })
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["steps"], [])
            self.assertIn("local changes", result["error"])
            self.assertTrue((work / "dirty.txt").exists())
        finally:
            td.cleanup()

    def test_allow_dirty_preserves_changes_and_skips_sync(self):
        td, work, config = self._fixture()
        try:
            (work / "dirty.txt").write_text("preserve me\n", encoding="utf-8")
            result = Executor(config).execute({
                "protocol": "q-agent-v4",
                "id": "dirty-recovery",
                "workspace": "example-project",
                "workspace_guard": {"allow_dirty": True},
                "steps": [{"type": "git.exec", "args": ["status", "--short"]}],
            })
            self.assertEqual(result["status"], "succeeded")
            pre = result["workspace_guards"]["example-project"]["pre"]
            self.assertTrue(pre["dirty"])
            self.assertTrue(pre["sync_skipped"])
            self.assertTrue((work / "dirty.txt").exists())
            self.assertIn("dirty.txt", result["steps"][0]["result"]["stdout"])
        finally:
            td.cleanup()

    def test_clean_workspace_runs_and_reports_post_dirty_without_committing(self):
        td, work, config = self._fixture()
        try:
            before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
            result = Executor(config).execute({
                "protocol": "q-agent-v4",
                "id": "write-guard",
                "workspace": "example-project",
                "steps": [{"type": "file.write", "path": "generated.txt", "text": "hello\n"}],
            })
            after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(before, after)
            self.assertTrue(result["workspace_guards"]["example-project"]["post"]["dirty"])
            self.assertIn("generated.txt", result["workspace_guards"]["example-project"]["post"]["git_status_short_branch"])
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
