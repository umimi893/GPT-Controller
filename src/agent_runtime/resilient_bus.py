from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .git_bus import GitBus
from .util import atomic_write_json, load_json, utc_now


class ResilientGitBus(GitBus):
    """GitBus with crash-only repair for the dedicated control checkout.

    The control checkout is runtime-owned transport, not a user workspace. It is safe to
    discard *uncommitted* mutations inside queue/claims/results/rejections after a crash;
    durable commits are preserved and pushed by the normal GitBus sync path.
    """

    RUNTIME_OWNED_PREFIXES = ("queue/", "claims/", "results/", "rejections/")

    def _abort_interrupted_git_operation(self) -> None:
        git_dir = self.repo / ".git"
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            self._git("rebase", "--abort", check=False)
        if (git_dir / "MERGE_HEAD").exists():
            self._git("merge", "--abort", check=False)
        if (git_dir / "CHERRY_PICK_HEAD").exists():
            self._git("cherry-pick", "--abort", check=False)

    @staticmethod
    def _porcelain_paths(text: str) -> list[str]:
        paths: list[str] = []
        for raw in text.splitlines():
            if len(raw) < 4:
                continue
            path = raw[3:].strip().strip('"')
            if " -> " in path:
                left, right = path.split(" -> ", 1)
                paths.extend([left.strip('"'), right.strip('"')])
            else:
                paths.append(path)
        return paths

    def repair_control_repo(self) -> bool:
        self._abort_interrupted_git_operation()
        status = self._git("status", "--porcelain", check=False)
        if status["exit_code"] != 0:
            raise RuntimeError(f"cannot inspect Agent control repo: {status['stderr'] or status['stdout']}")
        dirty = status["stdout"].strip()
        if not dirty:
            return False

        paths = self._porcelain_paths(dirty)
        unexpected = [
            path for path in paths
            if not any(path.replace("\\", "/").startswith(prefix) for prefix in self.RUNTIME_OWNED_PREFIXES)
        ]
        if unexpected:
            raise RuntimeError(
                "control repo has unexpected local changes outside runtime-owned transport paths: "
                + ", ".join(unexpected)
            )

        # A crash before commit/push left only non-durable local transport mutations.
        # Roll those back to HEAD. Local commits (including a durable local Result awaiting
        # push) are deliberately untouched and handled by the normal sync path.
        self._git("reset", "--hard", "HEAD")
        self._git("clean", "-fd", "--", "queue", "claims", "results", "rejections")
        return True

    def sync(self) -> None:
        self.repair_control_repo()
        super().sync()

    def _publish_recovery_commit(self, message: str) -> None:
        self._git("add", "queue", "results", "claims")
        self._git("commit", "-m", message)
        for attempt in range(10):
            pushed = self._git("push", self.c.remote, self.c.branch, check=False)
            if pushed["exit_code"] == 0:
                return
            time.sleep(min(2 ** attempt, 30))
            rebased = self._git("pull", "--rebase", self.c.remote, self.c.branch, check=False)
            if rebased["exit_code"] != 0:
                raise RuntimeError("cannot publish Agent recovery state")
        raise RuntimeError("cannot publish Agent recovery state after retries")

    def recover_ambiguous(self) -> int:
        root = self.repo / "queue" / "running"
        if not root.exists():
            return 0

        reconciled = 0
        ambiguous_count = 0
        resumable_count = 0

        # A Result is authoritative. Any matching running ledger entry is stale even if
        # it belongs to another currently-offline Agent, so every runtime may reconcile it.
        for running in sorted(root.glob("*.json")):
            try:
                action = load_json(running)
                action_id = str(action.get("id") or running.name.split(".")[0])
            except Exception:
                action_id = running.name.split(".")[0]
            result_path = self.repo / "results" / f"{action_id}.json"
            if not result_path.exists():
                continue
            done_path = self.repo / "queue" / "done" / f"{action_id}.json"
            done_path.parent.mkdir(parents=True, exist_ok=True)
            if done_path.exists():
                running.unlink()
            else:
                running.replace(done_path)
            reconciled += 1

        # Only the owning Agent may resolve a durable claim with no Result as ambiguous.
        # This preserves the no-replay guarantee if another machine is legitimately busy.
        for running in sorted(root.glob(f"*.{self.c.agent_id}.json")):
            try:
                action = load_json(running)
                action_id = str(action.get("id") or running.name.split(".")[0])
            except Exception:
                action = None
                action_id = running.name.split(".")[0]
            if (self.repo / "results" / f"{action_id}.json").exists():
                continue
            resumable = bool(isinstance(action, dict) and action.get("resume_from_checkpoint") is True)
            resumable = resumable and bool(action.get("steps")) and all(
                isinstance(step, dict) and step.get("type") == "desktop.loop"
                for step in action.get("steps", [])
            )
            if resumable:
                pending = self.repo / "queue" / "pending" / f"{action_id}.json"
                pending.parent.mkdir(parents=True, exist_ok=True)
                if pending.exists():
                    running.unlink()
                else:
                    running.replace(pending)
                resumable_count += 1
                continue

            ambiguous = self.repo / "queue" / "ambiguous" / running.name
            ambiguous.parent.mkdir(parents=True, exist_ok=True)
            running.replace(ambiguous)
            atomic_write_json(self.repo / "results" / f"{action_id}.json", {
                "protocol": "q-agent-v4-result",
                "action_id": action_id,
                "agent_id": self.c.agent_id,
                "started_at": None,
                "finished_at": utc_now(),
                "status": "ambiguous",
                "error": "Runtime restarted after durable claim but before a durable result. Action was NOT replayed.",
                "steps": [],
            })
            ambiguous_count += 1

        total = reconciled + ambiguous_count + resumable_count
        if total:
            self._publish_recovery_commit(
                f"gpt-controller recover running ledger: {reconciled} reconciled, {resumable_count} resumable, {ambiguous_count} ambiguous"
            )
        return total
