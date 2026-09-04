from __future__ import annotations

import json
import os
import socket
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .protocol import validate_action
from .util import atomic_write_json, load_json, parse_utc, run_process, utc_now


class GitBus:
    def __init__(self, config: Config):
        self.c = config
        self.repo = config.repo_path

    def _git(self, *args: str, timeout: int = 120, check: bool = True) -> dict[str, Any]:
        result = run_process([self.c.git, *args], self.repo, timeout, max_output_bytes=self.c.max_output_bytes)
        if check and result["exit_code"] != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result['stderr'] or result['stdout']}")
        return result

    def remote_head(self) -> str:
        result = self._git("ls-remote", "--heads", self.c.remote, self.c.branch)
        line = result["stdout"].strip().splitlines()
        if not line:
            raise RuntimeError(f"remote branch not found: {self.c.remote}/{self.c.branch}")
        parts = line[0].split()
        if not parts:
            raise RuntimeError(f"cannot parse remote head for {self.c.remote}/{self.c.branch}")
        return parts[0]

    def ensure_clean_control_repo(self) -> None:
        result = self._git("status", "--porcelain")
        if result["stdout"].strip():
            raise RuntimeError("control repo has local changes; refusing to mix runtime state with user changes")

    def sync(self) -> None:
        self.ensure_clean_control_repo()
        # Give runtime-authored ledger commits a deterministic local identity.
        self._git("config", "user.name", f"GPT Controller ({self.c.agent_id})")
        self._git("config", "user.email", f"qagentv4+{self.c.agent_id}@localhost")
        self._git("fetch", self.c.remote, self.c.branch)
        current = self._git("branch", "--show-current")["stdout"].strip()
        if current != self.c.branch:
            self._git("checkout", self.c.branch)

        counts = self._git("rev-list", "--left-right", "--count", f"HEAD...{self.c.remote}/{self.c.branch}")["stdout"].strip().split()
        ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
        if ahead and behind:
            self._git("rebase", f"{self.c.remote}/{self.c.branch}")
            ahead, behind = 1, 0
        elif behind:
            self._git("merge", "--ff-only", f"{self.c.remote}/{self.c.branch}")
        if ahead:
            pushed = self._git("push", self.c.remote, self.c.branch, check=False)
            if pushed["exit_code"] != 0:
                self._git("fetch", self.c.remote, self.c.branch)
                self._git("rebase", f"{self.c.remote}/{self.c.branch}")
                self._git("push", self.c.remote, self.c.branch)

    def pending(self) -> list[Path]:
        root = self.repo / "queue" / "pending"
        if not root.exists():
            return []
        return sorted(p for p in root.glob("*.json") if p.is_file())

    def reject(self, pending_path: Path, reason: str) -> None:
        safe = pending_path.name
        rejected = self.repo / "queue" / "rejected" / safe
        rejected.parent.mkdir(parents=True, exist_ok=True)
        pending_path.replace(rejected)
        key = hashlib.sha256(safe.encode("utf-8")).hexdigest()[:16]
        meta = self.repo / "rejections" / f"{key}.json"
        atomic_write_json(meta, {
            "protocol": "q-agent-v4-rejection",
            "source": safe,
            "agent_id": self.c.agent_id,
            "rejected_at": utc_now(),
            "reason": reason,
        })
        self._git("add", "queue", "rejections")
        self._git("commit", "-m", f"gpt-controller reject {safe}")
        pushed = self._git("push", self.c.remote, self.c.branch, check=False)
        if pushed["exit_code"] != 0:
            self._git("reset", "--hard", f"{self.c.remote}/{self.c.branch}")

    def recover_ambiguous(self) -> int:
        root = self.repo / "queue" / "running"
        if not root.exists():
            return 0
        recovered = 0
        for running in sorted(root.glob(f"*.{self.c.agent_id}.json")):
            try:
                action = load_json(running)
                action_id = str(action.get("id") or running.name.split(".")[0])
            except Exception:
                action_id = running.name.split(".")[0]
            if (self.repo / "results" / f"{action_id}.json").exists():
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
            recovered += 1
        if recovered:
            self._git("add", "queue", "results", "claims")
            self._git("commit", "-m", f"gpt-controller mark {recovered} action(s) ambiguous after restart")
            for attempt in range(10):
                pushed = self._git("push", self.c.remote, self.c.branch, check=False)
                if pushed["exit_code"] == 0:
                    break
                time.sleep(min(2 ** attempt, 30))
                rebased = self._git("pull", "--rebase", self.c.remote, self.c.branch, check=False)
                if rebased["exit_code"] != 0:
                    raise RuntimeError("cannot publish ambiguous recovery state")
            else:
                raise RuntimeError("cannot publish ambiguous recovery state")
        return recovered

    def _eligible(self, action: dict[str, Any]) -> bool:
        requires = set(str(x) for x in action.get("requires", []))
        if not requires.issubset(self.c.capabilities):
            return False

        target_agent = action.get("target_agent")
        if target_agent not in (None, "*", self.c.agent_id):
            return False

        target = action.get("target")
        if not target:
            return True
        mode = target.get("mode", "any")
        if mode == "any":
            return True
        if mode == "agent":
            return target.get("agent") == self.c.agent_id
        if mode == "ordered":
            order = target["order"]
            if self.c.agent_id not in order:
                return False
            rank = order.index(self.c.agent_id)
            delay = int(target.get("fallback_after_seconds", 8)) * rank
            created = parse_utc(action["created_at"])
            now = datetime.now(timezone.utc)
            return (now - created).total_seconds() >= delay
        return False

    def claim(self, pending_path: Path) -> tuple[dict[str, Any], Path] | None:
        action = load_json(pending_path)
        validate_action(action)
        if not self._eligible(action):
            return None
        action_id = action["id"]
        if (self.repo / "results" / f"{action_id}.json").exists():
            return None
        running = self.repo / "queue" / "running" / f"{action_id}.{self.c.agent_id}.json"
        running.parent.mkdir(parents=True, exist_ok=True)
        pending_path.replace(running)
        claim_meta = self.repo / "claims" / f"{action_id}.json"
        atomic_write_json(claim_meta, {
            "protocol": "q-agent-v4-claim",
            "action_id": action_id,
            "agent_id": self.c.agent_id,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "claimed_at": utc_now(),
        })
        self._git("add", "queue", "claims")
        self._git("commit", "-m", f"gpt-controller claim {action_id} by {self.c.agent_id}")
        pushed = self._git("push", self.c.remote, self.c.branch, check=False)
        if pushed["exit_code"] != 0:
            self._git("reset", "--hard", f"{self.c.remote}/{self.c.branch}")
            return None
        return action, running

    def finish(self, action: dict[str, Any], running_path: Path, result: dict[str, Any]) -> None:
        action_id = action["id"]
        result_path = self.repo / "results" / f"{action_id}.json"
        atomic_write_json(result_path, result)
        done_path = self.repo / "queue" / "done" / f"{action_id}.json"
        done_path.parent.mkdir(parents=True, exist_ok=True)
        if running_path.exists():
            running_path.replace(done_path)
        self._git("add", "queue", "results", "claims")
        self._git("commit", "-m", f"gpt-controller result {action_id} {result['status']}")
        # Result publication retries are safe: execution is already represented by the durable local claim.
        for attempt in range(10):
            pushed = self._git("push", self.c.remote, self.c.branch, check=False)
            if pushed["exit_code"] == 0:
                return
            time.sleep(min(2 ** attempt, 30))
            # Do NOT reset/re-execute here. Preserve local result commit and rebase it on remote.
            rebased = self._git("pull", "--rebase", self.c.remote, self.c.branch, check=False)
            if rebased["exit_code"] != 0:
                raise RuntimeError(f"cannot publish result without risking replay: {rebased['stderr'] or rebased['stdout']}")
        raise RuntimeError("result push failed after retries")
