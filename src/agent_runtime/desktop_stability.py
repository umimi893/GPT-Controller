from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Any

from .util import atomic_write_json, load_json, utc_now

_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download")


def _session_path(config: Any, session_id: str) -> Path:
    if not _SESSION_RE.fullmatch(session_id):
        raise ValueError("invalid desktop session id")
    root = config.repo_path.parent / "state" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.json"


def load_checkpoint(config: Any, session_id: str) -> dict[str, Any] | None:
    path = _session_path(config, session_id)
    if not path.exists():
        return None
    data = load_json(path)
    return data if isinstance(data, dict) else None


def write_checkpoint(config: Any, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = _session_path(config, session_id)
    record = {
        "protocol": "q-agent-v4-desktop-session",
        "session_id": session_id,
        "agent_id": config.agent_id,
        "updated_at": utc_now(),
        **payload,
    }
    atomic_write_json(path, record)
    return record


def clear_checkpoint(config: Any, session_id: str) -> bool:
    path = _session_path(config, session_id)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _dig(value: Any, path: str | None) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def condition_matches(outputs: list[Any], condition: dict[str, Any] | None) -> bool:
    if not condition:
        return False
    if "all" in condition:
        items = condition["all"]
        return isinstance(items, list) and all(condition_matches(outputs, item) for item in items)
    if "any" in condition:
        items = condition["any"]
        return isinstance(items, list) and any(condition_matches(outputs, item) for item in items)
    if "not" in condition:
        item = condition["not"]
        return isinstance(item, dict) and not condition_matches(outputs, item)

    source = condition.get("source", "last")
    if source == "last":
        value: Any = outputs[-1] if outputs else None
    elif source == "outputs":
        value = outputs
    elif isinstance(source, int):
        try:
            value = outputs[source]
        except IndexError:
            return False
    else:
        return False

    try:
        value = _dig(value, condition.get("path"))
    except (KeyError, IndexError, TypeError, ValueError):
        return bool(condition.get("missing", False))

    if "equals" in condition:
        return value == condition["equals"]
    if "not_equals" in condition:
        return value != condition["not_equals"]
    if "contains" in condition:
        needle = condition["contains"]
        try:
            return needle in value
        except TypeError:
            return str(needle) in str(value)
    if "matches" in condition:
        return bool(re.search(str(condition["matches"]), str(value)))
    if "truthy" in condition:
        return bool(value) is bool(condition["truthy"])
    if "exists" in condition:
        return bool(condition["exists"])
    return bool(value)


def _expand_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded).resolve()


def wait_for_download(
    directory: str,
    *,
    patterns: list[str] | None = None,
    timeout_seconds: float = 300.0,
    stable_seconds: float = 1.5,
    poll_seconds: float = 0.25,
    min_bytes: int = 1,
    partial_suffixes: list[str] | None = None,
    allow_existing: bool = False,
) -> dict[str, Any]:
    root = _expand_path(directory)
    patterns = patterns or ["*"]
    suffixes = tuple((partial_suffixes or list(_DEFAULT_PARTIAL_SUFFIXES)))
    started = time.time()
    deadline = time.monotonic() + max(0.1, timeout_seconds)

    def files() -> list[Path]:
        if not root.exists():
            return []
        return [p for p in root.iterdir() if p.is_file()]

    baseline = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in files()}
    stable: dict[str, tuple[int, int, float]] = {}
    observed_partials: set[str] = set()

    while time.monotonic() < deadline:
        current = files()
        partials = [p for p in current if p.name.lower().endswith(tuple(s.lower() for s in suffixes))]
        observed_partials.update(str(p) for p in partials)
        candidates: list[Path] = []
        for p in current:
            if p in partials:
                continue
            if not any(fnmatch.fnmatch(p.name, pattern) for pattern in patterns):
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            if stat.st_size < min_bytes:
                continue
            before = baseline.get(str(p))
            changed = before is None or before != (stat.st_size, stat.st_mtime_ns)
            if not allow_existing and not changed and stat.st_mtime < started - 1.0:
                continue
            candidates.append(p)

        candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        now = time.monotonic()
        for p in candidates:
            try:
                stat = p.stat()
            except OSError:
                continue
            key = str(p)
            prior = stable.get(key)
            signature = (stat.st_size, stat.st_mtime_ns)
            if prior is None or prior[:2] != signature:
                stable[key] = (signature[0], signature[1], now)
                continue
            if now - prior[2] >= max(0.0, stable_seconds):
                related_partial = any(
                    q.name.startswith(p.name) or q.stem == p.name
                    for q in partials
                )
                if related_partial:
                    continue
                return {
                    "directory": str(root),
                    "path": key,
                    "name": p.name,
                    "bytes": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                    "stable_seconds": stable_seconds,
                    "observed_partial_files": sorted(observed_partials),
                    "elapsed_seconds": round(time.time() - started, 3),
                }
        time.sleep(max(0.01, poll_seconds))

    raise TimeoutError(
        f"download did not complete in {root} within {timeout_seconds}s; "
        f"patterns={patterns} partials={sorted(observed_partials)}"
    )
