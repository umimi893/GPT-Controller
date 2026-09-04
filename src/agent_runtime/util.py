from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def configure_file_logging(log_path: Path, level: int = logging.INFO) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_json(path: Path) -> Any:
    # Windows PowerShell 5 writes UTF-8 with a BOM for Set-Content -Encoding utf8.
    # utf-8-sig accepts both BOM and non-BOM UTF-8.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def terminate_process_tree(process: subprocess.Popen[bytes], *, wait_seconds: float = 10.0) -> None:
    """Best-effort process-tree termination used by all timeout paths.

    On Windows, taskkill /T handles console wrappers such as pwsh -> cmd -> node. Action
    workers additionally live in a KILL_ON_JOB_CLOSE Job Object, which is the stronger
    guarantee for descendants that intentionally outlive an intermediate parent.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=wait_seconds,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    try:
        process.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:
            pass


def run_process(
    argv: list[str],
    cwd: Path | None = None,
    timeout: int = 300,
    env: dict[str, str] | None = None,
    max_output_bytes: int = 2_000_000,
) -> dict[str, Any]:
    creationflags = 0
    if os.name == "nt":
        # GPT Controller is a background runtime. Console children such as git.exe and
        # pwsh.exe must never flash visible windows while polling/executing.
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=creationflags,
        start_new_session=(os.name != "nt"),
    )
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout_bytes, stderr_bytes = b"", b""

    stdout_bytes = stdout_bytes or b""
    stderr_bytes = stderr_bytes or b""
    if timed_out:
        timeout_message = f"GPT Controller terminated process tree after {timeout}s timeout."
        if stderr_bytes and not stderr_bytes.endswith(b"\n"):
            stderr_bytes += b"\n"
        stderr_bytes += timeout_message.encode("utf-8") + b"\n"

    stdout = stdout_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    return {
        "exit_code": 124 if timed_out else process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": len(stdout_bytes) > max_output_bytes,
        "stderr_truncated": len(stderr_bytes) > max_output_bytes,
        "timed_out": timed_out,
    }
