from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .util import atomic_write_json, load_json, parse_utc, terminate_process_tree, utc_now


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _WindowsKillOnCloseJob:
    """Windows Job Object that kills the worker and every descendant on close."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Job Object requested on non-Windows host")

        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")

        self._kernel32 = kernel32
        self._handle = handle

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class ActionSupervisor:
    POLL_SECONDS = 0.5
    STEP_TIMEOUT_GRACE_SECONDS = 45
    WORKER_BOOT_TIMEOUT_SECONDS = 60
    ACTION_OVERHEAD_SECONDS = 600

    def __init__(self, config: Config, config_path: Path):
        self.config = config
        self.config_path = config_path
        self.root = config.repo_path.parent / "state" / "workers"
        self.root.mkdir(parents=True, exist_ok=True)

    def _hard_budget(self, action: dict[str, Any]) -> int:
        default_timeout = int(action.get("timeout_seconds", self.config.default_timeout_seconds))
        step_budget = sum(
            max(1, int(step.get("timeout_seconds", default_timeout)))
            for step in action.get("steps", [])
        )
        return max(300, step_budget + self.ACTION_OVERHEAD_SECONDS)

    def _worker_argv(
        self,
        action_path: Path,
        result_path: Path,
        progress_path: Path,
        gate_path: Path,
    ) -> list[str]:
        return [
            sys.executable,
            "-m",
            "agent_runtime.worker",
            "--config",
            str(self.config_path),
            "--action",
            str(action_path),
            "--result",
            str(result_path),
            "--progress",
            str(progress_path),
            "--gate",
            str(gate_path),
        ]

    @staticmethod
    def _timeout_result(action: dict[str, Any], agent_id: str, started_at: str, progress: dict[str, Any], reason: str) -> dict[str, Any]:
        step_index = progress.get("step_index")
        step_type = progress.get("step_type")
        steps: list[dict[str, Any]] = []
        if step_index is not None or step_type is not None:
            steps.append({
                "index": step_index,
                "type": step_type,
                "status": "failed",
                "error": reason,
                "timed_out": True,
            })
        return {
            "protocol": "q-agent-v4-result",
            "action_id": action["id"],
            "agent_id": agent_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "failed",
            "error": reason,
            "steps": steps,
            "workspace_guards": {},
            "supervisor": {
                "isolated_worker": True,
                "timed_out": True,
                "last_progress": progress,
            },
        }

    def execute(self, action: dict[str, Any], on_progress=None) -> dict[str, Any]:
        resumable = bool(action.get("resume_from_checkpoint", False))
        max_attempts = max(1, min(int(action.get("resume_attempts", 3 if resumable else 1)), 10))
        last: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            last = self._execute_once(action, on_progress=on_progress)
            supervisor = last.get("supervisor") if isinstance(last, dict) else None
            supervisor = supervisor if isinstance(supervisor, dict) else {}
            if last.get("status") == "succeeded":
                if resumable:
                    supervisor["resume_attempt"] = attempt
                    supervisor["resume_attempts_allowed"] = max_attempts
                    last["supervisor"] = supervisor
                return last
            infrastructure_failure = bool(supervisor.get("timed_out")) or (
                not last.get("steps") and "worker_exit_code" in supervisor
            )
            if not resumable or not infrastructure_failure or attempt >= max_attempts:
                return last
            time.sleep(min(2 ** (attempt - 1), 8))
        assert last is not None
        return last

    def _execute_once(self, action: dict[str, Any], on_progress=None) -> dict[str, Any]:
        action_id = str(action["id"])
        work_dir = self.root / f"{action_id}-{uuid.uuid4().hex[:10]}"
        work_dir.mkdir(parents=True, exist_ok=False)
        action_path = work_dir / "action.json"
        result_path = work_dir / "result.json"
        progress_path = work_dir / "progress.json"
        gate_path = work_dir / "start.gate"
        atomic_write_json(action_path, action)

        started_at = utc_now()
        hard_deadline = time.monotonic() + self._hard_budget(action)
        boot_deadline = time.monotonic() + self.WORKER_BOOT_TIMEOUT_SECONDS
        argv = self._worker_argv(action_path, result_path, progress_path, gate_path)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        job = None
        last_progress: dict[str, Any] = {}
        timeout_reason: str | None = None

        try:
            if os.name == "nt":
                job = _WindowsKillOnCloseJob()
                job.assign(process)
            gate_path.touch()

            while process.poll() is None:
                now_monotonic = time.monotonic()
                if progress_path.exists():
                    try:
                        progress = load_json(progress_path)
                        if isinstance(progress, dict):
                            last_progress = progress
                            if on_progress is not None:
                                on_progress(progress)
                    except Exception:
                        pass

                if not last_progress and now_monotonic > boot_deadline:
                    timeout_reason = f"Action worker did not report startup progress within {self.WORKER_BOOT_TIMEOUT_SECONDS}s"
                    break

                if last_progress.get("phase") == "step" and last_progress.get("event") == "started":
                    try:
                        step_started = parse_utc(str(last_progress["phase_started_at"]))
                        timeout_seconds = int(last_progress["timeout_seconds"])
                        elapsed = (parse_utc(utc_now()) - step_started).total_seconds()
                        if elapsed > timeout_seconds + self.STEP_TIMEOUT_GRACE_SECONDS:
                            timeout_reason = (
                                f"Step {last_progress.get('step_index')} ({last_progress.get('step_type')}) exceeded "
                                f"its {timeout_seconds}s timeout plus {self.STEP_TIMEOUT_GRACE_SECONDS}s supervisor grace"
                            )
                            break
                    except Exception:
                        pass

                if now_monotonic > hard_deadline:
                    timeout_reason = "Action exceeded the supervisor hard wall-clock budget"
                    break
                time.sleep(self.POLL_SECONDS)

            if timeout_reason is not None:
                terminate_process_tree(process, wait_seconds=15)
                return self._timeout_result(action, self.config.agent_id, started_at, last_progress, timeout_reason)

            exit_code = process.wait(timeout=10)
            if result_path.exists():
                result = load_json(result_path)
                if isinstance(result, dict):
                    supervisor_meta = result.get("supervisor")
                    if not isinstance(supervisor_meta, dict):
                        supervisor_meta = {}
                        result["supervisor"] = supervisor_meta
                    supervisor_meta.update({
                        "isolated_worker": True,
                        "worker_exit_code": exit_code,
                    })
                    return result
            return {
                "protocol": "q-agent-v4-result",
                "action_id": action_id,
                "agent_id": self.config.agent_id,
                "started_at": started_at,
                "finished_at": utc_now(),
                "status": "failed",
                "error": f"Action worker exited with code {exit_code} without a durable worker result",
                "steps": [],
                "workspace_guards": {},
                "supervisor": {
                    "isolated_worker": True,
                    "worker_exit_code": exit_code,
                    "last_progress": last_progress,
                },
            }
        finally:
            # Closing the Job Object kills any descendants intentionally left running by
            # an earlier step (for example Start-Process npx astro preview). If Job setup
            # itself failed, fall back to explicit tree termination so no worker escapes
            # supervision.
            if job is not None:
                job.close()
            elif process.poll() is None:
                terminate_process_tree(process, wait_seconds=10)
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            shutil.rmtree(work_dir, ignore_errors=True)
