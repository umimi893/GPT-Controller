from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Config
from .desktop_stability import clear_checkpoint, condition_matches, load_checkpoint, wait_for_download, write_checkpoint
from .interactive_ipc import InteractiveClient
from .paths import resolve_path, workspace_for
from .util import run_process, utc_now


class Executor:
    def __init__(self, config: Config):
        self.config = config
        self.interactive = InteractiveClient(config)

    def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        started = utc_now()
        results: list[dict[str, Any]] = []
        status = "succeeded"
        error = None
        workspace_guards: dict[str, Any] = {}
        managed_workspaces = self._managed_workspaces_for_action(action)
        guard_timeout = int(action.get("timeout_seconds", self.config.default_timeout_seconds))
        allow_dirty = bool(action.get("workspace_guard", {}).get("allow_dirty", False))
        allow_branch_mismatch = bool(action.get("workspace_guard", {}).get("allow_branch_mismatch", False))

        for workspace in managed_workspaces:
            try:
                workspace_guards[workspace] = {
                    "pre": self._managed_git_preflight(
                        workspace,
                        guard_timeout,
                        allow_dirty=allow_dirty,
                        allow_branch_mismatch=allow_branch_mismatch,
                    )
                }
            except Exception as exc:
                status = "failed"
                error = str(exc)
                workspace_guards[workspace] = {
                    "pre": {
                        "status": "failed",
                        "error": str(exc),
                    }
                }
                return {
                    "protocol": "q-agent-v4-result",
                    "action_id": action["id"],
                    "agent_id": self.config.agent_id,
                    "started_at": started,
                    "finished_at": utc_now(),
                    "status": status,
                    "error": error,
                    "steps": results,
                    "workspace_guards": workspace_guards,
                }

        for index, step in enumerate(action["steps"]):
            try:
                result = self._step(action, step)
                item = {"index": index, "type": step["type"], "status": "succeeded", "result": result}
                if isinstance(result, dict) and result.get("exit_code", 0) != 0:
                    item["status"] = "failed"
                    status = "failed"
                    results.append(item)
                    if not step.get("continue_on_error", False):
                        break
                else:
                    results.append(item)
            except Exception as exc:
                status = "failed"
                results.append({
                    "index": index,
                    "type": step.get("type"),
                    "status": "failed",
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                })
                error = str(exc)
                if not step.get("continue_on_error", False):
                    break

        for workspace in managed_workspaces:
            try:
                workspace_guards.setdefault(workspace, {})["post"] = self._managed_git_postflight(workspace, guard_timeout)
            except Exception as exc:
                workspace_guards.setdefault(workspace, {})["post"] = {
                    "status": "failed",
                    "error": str(exc),
                }
                if status == "succeeded":
                    status = "failed"
                    error = f"managed_git postflight failed for {workspace}: {exc}"

        return {
            "protocol": "q-agent-v4-result",
            "action_id": action["id"],
            "agent_id": self.config.agent_id,
            "started_at": started,
            "finished_at": utc_now(),
            "status": status,
            "error": error,
            "steps": results,
            "workspace_guards": workspace_guards,
        }

    def _managed_workspaces_for_action(self, action: dict[str, Any]) -> list[str]:
        touching_types = {
            "powershell.exec",
            "process.exec",
            "file.read",
            "file.write",
            "file.append",
            "file.mkdir",
            "file.delete",
            "file.copy",
            "file.move",
            "git.exec",
            "workspace.git_sync",
            "browser.playwright",
            "deploy.exec",
        }
        names: set[str] = set()

        def visit(steps: list[Any], inherited_workspace: Any) -> None:
            for raw in steps:
                if not isinstance(raw, dict):
                    continue
                kind = raw.get("type")
                workspace = raw.get("workspace", inherited_workspace)
                if kind == "desktop.loop":
                    nested = raw.get("steps", [])
                    if isinstance(nested, list):
                        visit(nested, workspace)
                    continue
                if kind not in touching_types:
                    continue
                if kind == "deploy.exec":
                    profile = self.config.deploy_profiles.get(str(raw.get("profile")), {})
                    workspace = profile.get("workspace", workspace)
                if isinstance(workspace, str) and workspace in self.config.managed_git_workspaces:
                    names.add(workspace)

        visit(action.get("steps", []), action.get("workspace"))
        return sorted(names)

    def _git_checked(self, argv: list[str], cwd: Path, timeout: int, operation: str) -> dict[str, Any]:
        result = run_process(argv, cwd, timeout, max_output_bytes=self.config.max_output_bytes)
        if result["exit_code"] != 0:
            detail = (result["stderr"] or result["stdout"]).strip()
            raise RuntimeError(f"{operation} failed: {detail}")
        return result

    def _managed_git_preflight(
        self,
        workspace: str,
        timeout: int,
        *,
        allow_dirty: bool = False,
        allow_branch_mismatch: bool = False,
    ) -> dict[str, Any]:
        cwd = workspace_for(self.config, workspace)
        status = self._git_checked(
            [self.config.git, "status", "--porcelain"], cwd, timeout, f"managed_git status for {workspace}"
        )
        dirty = bool(status["stdout"].strip())
        branch = self._git_checked(
            [self.config.git, "branch", "--show-current"], cwd, timeout, f"managed_git branch for {workspace}"
        )["stdout"].strip()
        if not branch:
            raise RuntimeError(f"managed_git preflight refused for {workspace}: detached HEAD")

        expected_branch = self.config.managed_git_expected_branches.get(workspace)
        branch_mismatch = bool(expected_branch and branch != expected_branch)
        if branch_mismatch and not allow_branch_mismatch:
            raise RuntimeError(
                f"managed_git preflight refused for {workspace}: expected branch {expected_branch!r} "
                f"but found {branch!r}; Agent will not checkout branches automatically"
            )

        if dirty:
            if not allow_dirty:
                raise RuntimeError(
                    f"managed_git preflight refused for {workspace}: workspace has local changes; Agent will not reset, stash, merge, or overwrite them"
                )
            head = self._git_checked(
                [self.config.git, "rev-parse", "HEAD"], cwd, timeout, f"managed_git HEAD for {workspace}"
            )["stdout"].strip()
            return {
                "status": "succeeded",
                "workspace": workspace,
                "branch": branch,
                "expected_branch": expected_branch,
                "branch_mismatch": branch_mismatch,
                "head": head,
                "dirty": True,
                "sync_skipped": True,
                "reason": "workspace_guard.allow_dirty=true; preserved existing local changes and skipped fetch/pull",
            }

        remote = "origin"
        self._git_checked([self.config.git, "fetch", remote], cwd, timeout, f"managed_git fetch for {workspace}")
        pull = self._git_checked(
            [self.config.git, "pull", "--ff-only", remote, branch],
            cwd,
            timeout,
            f"managed_git ff-only pull for {workspace}",
        )
        head = self._git_checked(
            [self.config.git, "rev-parse", "HEAD"], cwd, timeout, f"managed_git HEAD for {workspace}"
        )["stdout"].strip()
        remote_head = self._git_checked(
            [self.config.git, "rev-parse", f"refs/remotes/{remote}/{branch}"],
            cwd,
            timeout,
            f"managed_git remote HEAD for {workspace}",
        )["stdout"].strip()
        if head != remote_head:
            raise RuntimeError(
                f"managed_git preflight refused for {workspace}: local {branch} is not identical to "
                f"{remote}/{branch} after ff-only sync; local unpushed commits or divergence must be resolved explicitly"
            )
        return {
            "status": "succeeded",
            "workspace": workspace,
            "remote": remote,
            "branch": branch,
            "expected_branch": expected_branch,
            "branch_mismatch": branch_mismatch,
            "head": head,
            "remote_head": remote_head,
            "dirty": False,
            "sync_skipped": False,
            "pull_stdout": pull["stdout"],
        }

    def _managed_git_postflight(self, workspace: str, timeout: int) -> dict[str, Any]:
        cwd = workspace_for(self.config, workspace)
        short_branch = self._git_checked(
            [self.config.git, "status", "--short", "--branch"],
            cwd,
            timeout,
            f"managed_git postflight status for {workspace}",
        )["stdout"]
        porcelain = self._git_checked(
            [self.config.git, "status", "--porcelain"],
            cwd,
            timeout,
            f"managed_git postflight porcelain for {workspace}",
        )["stdout"]
        head = self._git_checked(
            [self.config.git, "rev-parse", "HEAD"], cwd, timeout, f"managed_git postflight HEAD for {workspace}"
        )["stdout"].strip()
        return {
            "status": "succeeded",
            "workspace": workspace,
            "head": head,
            "dirty": bool(porcelain.strip()),
            "git_status_short_branch": short_branch,
        }

    def _step(self, action: dict[str, Any], step: dict[str, Any]) -> Any:
        kind = step["type"]
        workspace = step.get("workspace", action.get("workspace"))
        timeout = int(step.get("timeout_seconds", action.get("timeout_seconds", self.config.default_timeout_seconds)))

        if kind == "noop":
            return {"ok": True}

        if kind == "agent.info":
            return {
                "agent_id": self.config.agent_id,
                "capabilities": sorted(self.config.capabilities),
                "workspaces": sorted(self.config.workspaces),
                "managed_git_workspaces": sorted(self.config.managed_git_workspaces),
                "managed_git_expected_branches": dict(sorted(self.config.managed_git_expected_branches.items())),
                "non_interference": self.config.non_interference,
                "browser_headless_only": self.config.browser_headless_only,
                "browser_cdp_configured": bool(self.config.browser_cdp_endpoint),
                "browser_cdp_status": self._cdp_status(),
                "visible_gui_launch_allowed": self.config.allow_visible_gui_launch,
                "physical_input_allowed": self.config.allow_physical_input,
                "foreground_activation_allowed": self.config.allow_foreground_activation,
            }

        if kind == "powershell.exec":
            cwd = workspace_for(self.config, workspace) if workspace else None
            argv = [self.config.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", step["script"]]
            return run_process(argv, cwd, timeout, max_output_bytes=self.config.max_output_bytes)

        if kind == "process.exec":
            cwd = resolve_path(self.config, workspace, step["cwd"]) if step.get("cwd") else (workspace_for(self.config, workspace) if workspace else None)
            argv = [step["program"], *[str(x) for x in step.get("args", [])]]
            env = os.environ.copy()
            env.update({str(k): str(v) for k, v in step.get("env", {}).items()})
            return run_process(argv, cwd, timeout, env=env, max_output_bytes=self.config.max_output_bytes)

        if kind == "file.read":
            p = resolve_path(self.config, workspace, step["path"])
            data = p.read_bytes()
            limit = int(step.get("max_bytes", self.config.max_output_bytes))
            return {"path": str(p), "text": data[:limit].decode(step.get("encoding", "utf-8"), errors="replace"), "truncated": len(data) > limit}

        if kind in {"file.write", "file.append"}:
            p = resolve_path(self.config, workspace, step["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if kind == "file.append" else "w"
            with p.open(mode, encoding=step.get("encoding", "utf-8"), newline="") as f:
                f.write(step.get("text", ""))
            return {"path": str(p), "bytes": p.stat().st_size}

        if kind == "file.mkdir":
            p = resolve_path(self.config, workspace, step["path"])
            p.mkdir(parents=True, exist_ok=True)
            return {"path": str(p)}

        if kind == "file.delete":
            p = resolve_path(self.config, workspace, step["path"])
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
            return {"path": str(p), "deleted": True}

        if kind in {"file.copy", "file.move"}:
            src = resolve_path(self.config, workspace, step["source"])
            dst = resolve_path(self.config, workspace, step["destination"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if kind == "file.copy":
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=bool(step.get("dirs_exist_ok", False)))
                else:
                    shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
            return {"source": str(src), "destination": str(dst)}

        if kind == "git.exec":
            cwd = workspace_for(self.config, workspace)
            argv = [self.config.git, *[str(x) for x in step["args"]]]
            return run_process(argv, cwd, timeout, max_output_bytes=self.config.max_output_bytes)

        if kind == "workspace.git_sync":
            return self._git_sync(workspace, step, timeout)

        if kind == "deploy.exec":
            profile_name = step["profile"]
            if profile_name not in self.config.deploy_profiles:
                raise ValueError(f"unknown deploy profile: {profile_name}")
            profile = self.config.deploy_profiles[profile_name]
            cwd_name = profile.get("workspace", workspace)
            cwd = workspace_for(self.config, cwd_name) if cwd_name else None
            program = profile["program"]
            args = [str(x) for x in profile.get("args", [])]
            args.extend(str(x) for x in step.get("args", []))
            env = os.environ.copy()
            env.update({str(k): str(v) for k, v in profile.get("env", {}).items()})
            return run_process([program, *args], cwd, timeout, env=env, max_output_bytes=self.config.max_output_bytes)

        if kind == "download.wait":
            return wait_for_download(
                step["directory"],
                patterns=step.get("patterns"),
                timeout_seconds=float(step.get("timeout_seconds", timeout)),
                stable_seconds=float(step.get("stable_seconds", 1.5)),
                poll_seconds=float(step.get("poll_seconds", 0.25)),
                min_bytes=int(step.get("min_bytes", 1)),
                partial_suffixes=step.get("partial_suffixes"),
                allow_existing=bool(step.get("allow_existing", False)),
            )

        if kind == "desktop.checkpoint":
            return self._desktop_checkpoint(step)

        if kind == "desktop.loop":
            return self._desktop_loop(action, step, timeout)

        if kind == "browser.interactive":
            return self._browser_interactive(step)

        if kind == "browser.playwright":
            return self._browser(step, workspace)

        if kind == "windows.ui":
            if "windows-ui" not in self.config.capabilities:
                raise RuntimeError("this agent does not advertise windows-ui capability")
            return self.interactive.request(step, min(timeout, self.config.interactive_timeout_seconds))

        raise ValueError(f"unsupported step: {kind}")

    def _desktop_checkpoint(self, step: dict[str, Any]) -> dict[str, Any]:
        session = str(step["session"])
        op = str(step.get("op", "read"))
        if op == "read":
            return {"session": session, "checkpoint": load_checkpoint(self.config, session)}
        if op == "write":
            record = write_checkpoint(self.config, session, {
                "status": str(step.get("status", "manual")),
                "data": dict(step.get("data", {})),
            })
            return {"session": session, "checkpoint": record}
        if op == "clear":
            return {"session": session, "cleared": clear_checkpoint(self.config, session)}
        raise ValueError(f"unsupported desktop.checkpoint op: {op}")

    def _desktop_loop(self, action: dict[str, Any], step: dict[str, Any], timeout: int) -> dict[str, Any]:
        session = str(step["session"])
        nested_steps = list(step["steps"])
        max_cycles = max(1, min(int(step.get("max_cycles", 1)), 1000))
        retry_attempts_default = max(1, min(int(step.get("retry_attempts", 3)), 20))
        retry_delay_default = max(0.0, min(float(step.get("retry_delay_seconds", 0.5)), 30.0))
        retry_backoff_default = max(1.0, min(float(step.get("retry_backoff", 1.7)), 10.0))
        deadline = time.monotonic() + max(1, timeout)
        resume = bool(step.get("resume", True))
        checkpoint = load_checkpoint(self.config, session) if resume else None

        if checkpoint and checkpoint.get("status") == "completed" and bool(step.get("reuse_completed", True)):
            return {
                "session": session,
                "resumed": True,
                "completed": True,
                "reason": checkpoint.get("reason", "checkpoint_completed"),
                "cycle": checkpoint.get("cycle"),
                "last_outputs": checkpoint.get("last_outputs", []),
                "checkpoint": checkpoint,
            }

        cycle = 0
        next_step = 0
        cycle_outputs: list[Any] = []
        resumed = False
        if checkpoint and checkpoint.get("status") in {"running", "retrying", "failed"}:
            try:
                cycle = max(0, int(checkpoint.get("cycle", 0)))
                next_step = max(0, int(checkpoint.get("next_step", 0)))
                saved_outputs = checkpoint.get("cycle_outputs", [])
                cycle_outputs = list(saved_outputs) if isinstance(saved_outputs, list) else []
                resumed = True
            except Exception:
                cycle = 0
                next_step = 0
                cycle_outputs = []

        executed_steps = 0
        write_checkpoint(self.config, session, {
            "status": "running",
            "action_id": action["id"],
            "cycle": cycle,
            "next_step": next_step,
            "cycle_outputs": cycle_outputs,
            "in_progress": None,
        })

        while cycle < max_cycles:
            if next_step >= len(nested_steps):
                matched = condition_matches(cycle_outputs, step.get("until")) if step.get("until") else False
                if matched:
                    final = write_checkpoint(self.config, session, {
                        "status": "completed",
                        "action_id": action["id"],
                        "reason": "until_matched",
                        "cycle": cycle,
                        "next_step": len(nested_steps),
                        "last_outputs": cycle_outputs,
                        "in_progress": None,
                    })
                    return {
                        "session": session,
                        "resumed": resumed,
                        "completed": True,
                        "reason": "until_matched",
                        "cycles_completed": cycle + 1,
                        "executed_steps": executed_steps,
                        "last_outputs": cycle_outputs,
                        "checkpoint": final,
                    }
                cycle += 1
                next_step = 0
                if cycle >= max_cycles:
                    break
                cycle_outputs = []
                write_checkpoint(self.config, session, {
                    "status": "running",
                    "action_id": action["id"],
                    "cycle": cycle,
                    "next_step": 0,
                    "cycle_outputs": [],
                    "in_progress": None,
                })
                continue

            if time.monotonic() >= deadline:
                raise TimeoutError(f"desktop.loop session {session} exceeded its {timeout}s timeout")

            nested = dict(nested_steps[next_step])
            run_if = nested.pop("run_if", None)
            skip_if = nested.pop("skip_if", None)
            if isinstance(run_if, dict) and not condition_matches(cycle_outputs, run_if):
                item = {"index": next_step, "type": nested.get("type"), "status": "skipped", "reason": "run_if_false"}
                cycle_outputs.append(item)
                next_step += 1
                write_checkpoint(self.config, session, {
                    "status": "running", "action_id": action["id"], "cycle": cycle,
                    "next_step": next_step, "cycle_outputs": cycle_outputs, "in_progress": None,
                })
                continue
            if isinstance(skip_if, dict) and condition_matches(cycle_outputs, skip_if):
                item = {"index": next_step, "type": nested.get("type"), "status": "skipped", "reason": "skip_if_true"}
                cycle_outputs.append(item)
                next_step += 1
                write_checkpoint(self.config, session, {
                    "status": "running", "action_id": action["id"], "cycle": cycle,
                    "next_step": next_step, "cycle_outputs": cycle_outputs, "in_progress": None,
                })
                continue

            attempts = max(1, min(int(nested.pop("retry_attempts", retry_attempts_default)), 20))
            delay = max(0.0, min(float(nested.pop("retry_delay_seconds", retry_delay_default)), 30.0))
            backoff = max(1.0, min(float(nested.pop("retry_backoff", retry_backoff_default)), 10.0))
            continue_on_error = bool(nested.get("continue_on_error", False))
            last_error: Exception | None = None
            result: Any = None

            for attempt in range(1, attempts + 1):
                write_checkpoint(self.config, session, {
                    "status": "retrying" if attempt > 1 else "running",
                    "action_id": action["id"],
                    "cycle": cycle,
                    "next_step": next_step,
                    "cycle_outputs": cycle_outputs,
                    "in_progress": {"step": next_step, "type": nested.get("type"), "attempt": attempt},
                })
                try:
                    result = self._step(action, nested)
                    if isinstance(result, dict) and result.get("exit_code", 0) != 0:
                        raise RuntimeError(
                            f"nested step returned exit_code={result.get('exit_code')}: "
                            f"{result.get('stderr') or result.get('stdout') or ''}"
                        )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < attempts:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(delay * (backoff ** (attempt - 1)), remaining, 30.0))

            executed_steps += 1
            if last_error is not None:
                item = {
                    "index": next_step,
                    "type": nested.get("type"),
                    "status": "failed",
                    "error": str(last_error),
                    "attempts": attempts,
                }
                cycle_outputs.append(item)
                next_step += 1
                write_checkpoint(self.config, session, {
                    "status": "running" if continue_on_error else "failed",
                    "action_id": action["id"],
                    "cycle": cycle,
                    "next_step": next_step if continue_on_error else next_step - 1,
                    "cycle_outputs": cycle_outputs,
                    "in_progress": None,
                    "last_error": str(last_error),
                })
                if continue_on_error:
                    continue
                raise RuntimeError(
                    f"desktop.loop session {session} step {next_step - 1} failed after {attempts} attempts: {last_error}"
                ) from last_error

            cycle_outputs.append({
                "index": next_step,
                "type": nested.get("type"),
                "status": "succeeded",
                "result": result,
                "attempts": attempt,
            })
            next_step += 1
            write_checkpoint(self.config, session, {
                "status": "running",
                "action_id": action["id"],
                "cycle": cycle,
                "next_step": next_step,
                "cycle_outputs": cycle_outputs,
                "in_progress": None,
            })

        reason = "max_cycles"
        final = write_checkpoint(self.config, session, {
            "status": "completed",
            "action_id": action["id"],
            "reason": reason,
            "cycle": max(0, cycle - 1 if cycle >= max_cycles else cycle),
            "next_step": len(nested_steps),
            "last_outputs": cycle_outputs,
            "in_progress": None,
        })
        if step.get("until") and bool(step.get("require_until", False)):
            raise RuntimeError(f"desktop.loop session {session} reached max_cycles={max_cycles} without matching until")
        return {
            "session": session,
            "resumed": resumed,
            "completed": True,
            "reason": reason,
            "cycles_completed": max_cycles,
            "executed_steps": executed_steps,
            "last_outputs": cycle_outputs,
            "checkpoint": final,
        }

    def _git_sync(self, workspace: str | None, step: dict[str, Any], timeout: int) -> dict[str, Any]:
        cwd = workspace_for(self.config, workspace)
        status = run_process([self.config.git, "status", "--porcelain"], cwd, timeout, max_output_bytes=self.config.max_output_bytes)
        if status["exit_code"] != 0:
            return status
        if status["stdout"].strip() and not step.get("allow_dirty", False):
            raise RuntimeError("workspace.git_sync refused: workspace has local changes")
        remote = str(step.get("remote", "origin"))
        fetch = run_process([self.config.git, "fetch", remote], cwd, timeout, max_output_bytes=self.config.max_output_bytes)
        if fetch["exit_code"] != 0:
            return fetch
        branch = run_process([self.config.git, "branch", "--show-current"], cwd, timeout, max_output_bytes=self.config.max_output_bytes)
        if branch["exit_code"] != 0 or not branch["stdout"].strip():
            return branch
        branch_name = branch["stdout"].strip()
        pull = run_process([self.config.git, "pull", "--ff-only", remote, branch_name], cwd, timeout, max_output_bytes=self.config.max_output_bytes)
        return {"exit_code": pull["exit_code"], "remote": remote, "branch": branch_name, "stdout": pull["stdout"], "stderr": pull["stderr"]}

    def _cdp_status(self) -> dict[str, Any]:
        endpoint = self.config.browser_cdp_endpoint
        if not endpoint:
            return {"configured": False, "reachable": False}
        try:
            request = urllib.request.Request(
                endpoint.rstrip("/") + "/json/version",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            return {
                "configured": True,
                "reachable": True,
                "endpoint": endpoint,
                "browser": payload.get("Browser"),
            }
        except Exception as exc:
            return {
                "configured": True,
                "reachable": False,
                "endpoint": endpoint,
                "error": str(exc),
            }

    def _find_interactive_browser(self) -> Path:
        configured = self.config.browser_interactive_executable
        if configured:
            path = Path(configured).expanduser()
            if path.is_file():
                return path.resolve()
            raise RuntimeError(f"configured interactive browser executable not found: {path}")

        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        suffixes = [
            Path("Google/Chrome/Application/chrome.exe"),
            Path("Microsoft/Edge/Application/msedge.exe"),
        ]
        for root in roots:
            if not root:
                continue
            for suffix in suffixes:
                candidate = Path(root) / suffix
                if candidate.is_file():
                    return candidate.resolve()
        raise RuntimeError("no supported interactive browser found (Chrome or Edge)")

    def _browser_interactive(self, step: dict[str, Any]) -> dict[str, Any]:
        if "browser" not in self.config.capabilities:
            raise RuntimeError("this agent does not advertise browser capability")
        if not self.config.allow_visible_gui_launch:
            raise RuntimeError("visible browser launch is disabled by interaction_policy.allow_visible_gui_launch")

        status = self._cdp_status()
        op = str(step.get("op", "ensure"))
        if op == "status":
            return status
        if op not in {"launch", "ensure"}:
            raise ValueError(f"unsupported browser.interactive op: {op}")
        if status.get("reachable"):
            return {**status, "already_running": True}

        endpoint = self.config.browser_cdp_endpoint
        profile = self.config.browser_interactive_user_data_dir
        if not endpoint or not profile:
            raise RuntimeError("interactive browser requires browser.cdp_endpoint and browser.interactive_user_data_dir")

        parsed = urlparse(endpoint)
        port = parsed.port or 80
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("interactive browser CDP endpoint must be loopback")

        executable = self._find_interactive_browser()
        profile.mkdir(parents=True, exist_ok=True)
        args = [
            str(executable),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        url = step.get("url")
        if url:
            args.append(str(url))

        # Visible browsers must outlive the isolated Action worker. Workers run inside a
        # kill-on-close Windows Job Object, so launching Chrome directly with Popen here
        # would make a successful browser.interactive action kill its own browser when the
        # worker exits. Launch through the long-lived Interactive Host instead.
        startup_timeout = min(float(step.get("startup_timeout_seconds", 15)), 60.0)
        command = subprocess.list2cmdline(args)
        launch = self.interactive.request(
            {
                "type": "windows.ui",
                "start": command,
                "start_timeout_seconds": max(1, int(startup_timeout)),
                "actions": [{"op": "sleep", "seconds": 0.1}],
            },
            min(max(startup_timeout + 10.0, 15.0), float(self.config.interactive_timeout_seconds)),
        )
        deadline = time.monotonic() + startup_timeout
        last = self._cdp_status()
        while not last.get("reachable") and time.monotonic() < deadline:
            time.sleep(0.25)
            last = self._cdp_status()
        if not last.get("reachable"):
            raise RuntimeError(f"interactive browser launched but CDP did not become reachable: {last.get('error', 'unknown error')}")
        return {
            **last,
            "already_running": False,
            "visible": True,
            "physical_input_injected": False,
            "launch_host": "interactive",
            "launch": launch,
        }

    @staticmethod
    def _browser_page_matches(page: Any, spec: dict[str, Any] | None) -> bool:
        if not spec:
            return True
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        try:
            title = str(page.title() or "")
        except Exception:
            title = ""
        checks = (
            ("url", url, False),
            ("url_contains", url, True),
            ("title", title, False),
            ("title_contains", title, True),
        )
        for key, actual, contains in checks:
            expected = spec.get(key)
            if expected is None:
                continue
            expected_text = str(expected)
            if contains:
                if expected_text not in actual:
                    return False
            elif actual != expected_text:
                return False
        return True

    @staticmethod
    def _browser_page_summary(page: Any, index: int) -> dict[str, Any]:
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        try:
            title = str(page.title() or "")
        except Exception:
            title = ""
        try:
            closed = bool(page.is_closed())
        except Exception:
            closed = False
        return {"index": index, "url": url, "title": title, "closed": closed}

    def _select_cdp_page(self, context: Any, step: dict[str, Any]) -> tuple[Any, bool]:
        pages = [page for page in context.pages if not page.is_closed()]
        match = step.get("page_match")
        if isinstance(match, dict):
            matching = [page for page in pages if self._browser_page_matches(page, match)]
            if matching:
                return matching[-1], False
        if bool(step.get("reuse_page", False)) and pages:
            return pages[-1], False
        return context.new_page(), True

    def _connect_cdp_browser(self, playwright: Any, step: dict[str, Any]) -> Any:
        if not self.config.browser_cdp_endpoint:
            raise RuntimeError("browser.cdp_endpoint is not configured")
        auto_recover = bool(step.get("auto_recover", True))
        attempts = max(1, min(int(step.get("connect_attempts", 3)), 8))
        delay = max(0.05, min(float(step.get("connect_retry_delay_seconds", 0.5)), 5.0))
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return playwright.chromium.connect_over_cdp(self.config.browser_cdp_endpoint)
            except Exception as exc:
                last_error = exc
                if not auto_recover or attempt + 1 >= attempts:
                    break
                if not self._cdp_status().get("reachable"):
                    self._browser_interactive({
                        "op": "ensure",
                        "startup_timeout_seconds": step.get("startup_timeout_seconds", 15),
                    })
                time.sleep(delay * (attempt + 1))
        raise RuntimeError(f"failed to connect to interactive browser over CDP after {attempts} attempts: {last_error}")

    def _browser(self, step: dict[str, Any], workspace: str | None) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("browser support requires: pip install -e .[browser] && playwright install chromium") from exc

        connection = str(step.get("connection", "managed"))
        headless = bool(step.get("headless", True))
        if connection == "managed" and self.config.browser_headless_only and not headless:
            raise RuntimeError("headed managed browser is disabled; use browser.interactive for explicit user-visible login")
        if "browser" not in self.config.capabilities:
            raise RuntimeError("this agent does not advertise browser capability")

        outputs: list[Any] = []

        def browser_path(value: str) -> Path:
            return resolve_path(self.config, workspace, value)

        with sync_playwright() as p:
            external_browser = False
            owned_page = False
            if connection == "cdp":
                if bool(step.get("auto_recover", True)) and not self._cdp_status().get("reachable"):
                    self._browser_interactive({
                        "op": "ensure",
                        "startup_timeout_seconds": step.get("startup_timeout_seconds", 15),
                    })
                browser = self._connect_cdp_browser(p, step)
                if not browser.contexts:
                    raise RuntimeError("CDP browser has no browser context")
                context = browser.contexts[0]
                page, owned_page = self._select_cdp_page(context, step)
                external_browser = True
            else:
                kwargs: dict[str, Any] = {"headless": headless}
                if self.config.browser_channel:
                    kwargs["channel"] = self.config.browser_channel
                if self.config.browser_user_data_dir:
                    context = p.chromium.launch_persistent_context(str(self.config.browser_user_data_dir), accept_downloads=True, **kwargs)
                    page = context.pages[0] if context.pages else context.new_page()
                    browser = None
                else:
                    browser = p.chromium.launch(**kwargs)
                    context = browser.new_context(accept_downloads=True)
                    page = context.new_page()
            try:
                def locator(action: dict[str, Any]):
                    scope: Any = page
                    if action.get("frame"):
                        scope = page.frame_locator(action["frame"])
                    return scope.locator(action["selector"])

                for action in step.get("actions", []):
                    op = action["op"]
                    timeout_ms = action.get("timeout_ms", 30000)
                    if op == "goto":
                        response = page.goto(action["url"], wait_until=action.get("wait_until", "load"), timeout=timeout_ms)
                        outputs.append({"op": op, "url": page.url, "status": response.status if response else None})
                    elif op == "click":
                        locator(action).click(timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "dblclick":
                        locator(action).dblclick(timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "fill":
                        locator(action).fill(action.get("text", ""), timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "press":
                        locator(action).press(action["key"], timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "check":
                        locator(action).check(timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "uncheck":
                        locator(action).uncheck(timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "select_option":
                        selected = locator(action).select_option(value=action.get("value"), label=action.get("label"), index=action.get("index"), timeout=timeout_ms)
                        outputs.append({"op": op, "selected": selected})
                    elif op == "hover":
                        locator(action).hover(timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "wait_for":
                        locator(action).wait_for(state=action.get("state", "visible"), timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "wait_for_url":
                        page.wait_for_url(action["url"], timeout=timeout_ms, wait_until=action.get("wait_until", "load"))
                        outputs.append({"op": op, "url": page.url})
                    elif op == "wait_for_load_state":
                        page.wait_for_load_state(action.get("state", "load"), timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "reload":
                        response = page.reload(wait_until=action.get("wait_until", "load"), timeout=timeout_ms)
                        outputs.append({"op": op, "url": page.url, "status": response.status if response else None})
                    elif op == "wait_for_text":
                        expected = str(action["text"])
                        page.wait_for_function(
                            "text => !!document.body && document.body.innerText.includes(text)",
                            arg=expected,
                            timeout=timeout_ms,
                        )
                        outputs.append({"op": op, "text": expected})
                    elif op == "wait_for_function":
                        page.wait_for_function(action["expression"], arg=action.get("arg"), timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "bring_to_front":
                        page.bring_to_front()
                        outputs.append({"op": op})
                    elif op == "sleep":
                        seconds = max(0.0, min(float(action.get("seconds", 1.0)), 60.0))
                        time.sleep(seconds)
                        outputs.append({"op": op, "seconds": seconds})
                    elif op == "text":
                        text = locator(action).inner_text(timeout=timeout_ms)
                        outputs.append({"op": op, "text": text[: self.config.max_output_bytes]})
                    elif op == "content":
                        content = page.content()
                        outputs.append({"op": op, "html": content[: self.config.max_output_bytes]})
                    elif op == "attribute":
                        value = locator(action).get_attribute(action["name"], timeout=timeout_ms)
                        outputs.append({"op": op, "value": value})
                    elif op == "input_value":
                        outputs.append({"op": op, "value": locator(action).input_value(timeout=timeout_ms)})
                    elif op == "count":
                        outputs.append({"op": op, "count": locator(action).count()})
                    elif op == "screenshot":
                        path = browser_path(action["path"])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if action.get("selector"):
                            locator(action).screenshot(path=str(path), timeout=timeout_ms)
                        else:
                            page.screenshot(path=str(path), full_page=bool(action.get("full_page", False)))
                        outputs.append({"op": op, "path": str(path)})
                    elif op == "evaluate":
                        value = page.evaluate(action["expression"], action.get("arg"))
                        outputs.append({"op": op, "value": value})
                    elif op == "upload":
                        files = action.get("files", action.get("path"))
                        if isinstance(files, list):
                            files = [str(browser_path(str(x))) for x in files]
                        else:
                            files = str(browser_path(str(files)))
                        locator(action).set_input_files(files, timeout=timeout_ms)
                        outputs.append({"op": op})
                    elif op == "download":
                        save_as = browser_path(action["save_as"])
                        save_as.parent.mkdir(parents=True, exist_ok=True)
                        with page.expect_download(timeout=timeout_ms) as download_info:
                            locator(action).click(timeout=timeout_ms)
                        download = download_info.value
                        download.save_as(str(save_as))
                        outputs.append({"op": op, "path": str(save_as), "suggested_filename": download.suggested_filename})
                    elif op == "click_popup":
                        with page.expect_popup(timeout=timeout_ms) as popup_info:
                            locator(action).click(timeout=timeout_ms)
                        page = popup_info.value
                        page.wait_for_load_state(action.get("wait_until", "load"), timeout=timeout_ms)
                        outputs.append({"op": op, "url": page.url})
                    elif op == "new_page":
                        page = context.new_page()
                        outputs.append({"op": op, "page_index": len(context.pages) - 1})
                    elif op == "pages":
                        outputs.append({
                            "op": op,
                            "pages": [self._browser_page_summary(item, index) for index, item in enumerate(context.pages)],
                        })
                    elif op == "switch_page_matching":
                        spec = action.get("match")
                        if not isinstance(spec, dict):
                            raise ValueError("switch_page_matching requires match object")
                        matches = [
                            (index, item)
                            for index, item in enumerate(context.pages)
                            if not item.is_closed() and self._browser_page_matches(item, spec)
                        ]
                        if not matches:
                            raise RuntimeError(f"no browser page matched: {spec}")
                        index, page = matches[-1]
                        outputs.append({"op": op, "page_index": index, "url": page.url, "title": page.title()})
                    elif op == "switch_page":
                        pages = context.pages
                        index = int(action["index"])
                        page = pages[index]
                        outputs.append({"op": op, "page_index": index, "url": page.url})
                    elif op == "close_page":
                        page.close()
                        pages = context.pages
                        page = pages[-1] if pages else context.new_page()
                        outputs.append({"op": op})
                    elif op == "cookies_get":
                        outputs.append({"op": op, "cookies": context.cookies(action.get("urls"))})
                    elif op == "cookies_add":
                        context.add_cookies(action["cookies"])
                        outputs.append({"op": op})
                    elif op == "cookies_clear":
                        context.clear_cookies()
                        outputs.append({"op": op})
                    elif op == "storage_state":
                        path_value = action.get("path")
                        if path_value:
                            path = browser_path(path_value)
                            path.parent.mkdir(parents=True, exist_ok=True)
                            context.storage_state(path=str(path))
                            outputs.append({"op": op, "path": str(path), "state": None})
                        else:
                            outputs.append({"op": op, "path": None, "state": context.storage_state()})
                    elif op == "pdf":
                        path = browser_path(action["path"])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        page.pdf(path=str(path), format=action.get("format", "A4"), print_background=bool(action.get("print_background", True)))
                        outputs.append({"op": op, "path": str(path)})
                    elif op == "url":
                        outputs.append({"op": op, "url": page.url})
                    elif op == "title":
                        outputs.append({"op": op, "title": page.title()})
                    elif op == "snapshot":
                        body = page.locator("body").inner_text(timeout=timeout_ms)
                        outputs.append({
                            "op": op,
                            "url": page.url,
                            "title": page.title(),
                            "text": body[: self.config.max_output_bytes],
                            "pages": [self._browser_page_summary(item, index) for index, item in enumerate(context.pages)],
                        })
                    else:
                        raise ValueError(f"unsupported browser op: {op}")
                    settle = max(0.0, min(float(action.get("settle_seconds", 0.0)), 30.0))
                    if settle:
                        time.sleep(settle)
                return {
                    "url": page.url,
                    "title": page.title(),
                    "outputs": outputs,
                    "connection": connection,
                    "headless": headless if connection == "managed" else None,
                    "visible_session": external_browser,
                    "physical_input_injected": False,
                }
            finally:
                if external_browser:
                    if owned_page and not page.is_closed():
                        page.close()
                else:
                    context.close()
                    if browser:
                        browser.close()
