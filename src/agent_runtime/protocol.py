from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .util import parse_utc

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SUPPORTED = {
    "noop",
    "agent.info",
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
    "browser.interactive",
    "windows.ui",
    "download.wait",
    "desktop.checkpoint",
    "desktop.loop",
    "deploy.exec",
}

INTRUSIVE_WINDOWS_UI_OPS = {
    "click", "click_input", "type_keys", "focus", "set_focus", "maximize", "minimize", "restore",
    "move_resize", "move_mouse", "click_at", "double_click_at", "right_click_at", "scroll", "drag",
    "hotkey", "type_text", "clipboard_set",
    "file_dialog_path", "file_dialog_filename", "file_dialog_accept", "file_dialog_cancel",
    "explorer_navigate", "explorer_select", "explorer_open", "explorer_new_folder",
    "explorer_rename", "explorer_delete", "explorer_copy", "explorer_cut", "explorer_paste",
}


def _require_str(value: dict[str, Any], key: str, where: str) -> None:
    if not isinstance(value.get(key), str) or not value[key]:
        raise ValueError(f"{where}: {key} is required and must be a non-empty string")


def _require_list(value: dict[str, Any], key: str, where: str, *, non_empty: bool = False) -> None:
    item = value.get(key)
    if not isinstance(item, list) or (non_empty and not item):
        suffix = "non-empty " if non_empty else ""
        raise ValueError(f"{where}: {key} must be a {suffix}array")


def _workspace_available(action: dict[str, Any], step: dict[str, Any]) -> bool:
    return isinstance(step.get("workspace", action.get("workspace")), str) and bool(step.get("workspace", action.get("workspace")))


def _require_exact_agent_target(action: dict[str, Any], where: str) -> None:
    target = action.get("target")
    if not isinstance(target, dict) or target.get("mode") != "agent" or not isinstance(target.get("agent"), str) or not target["agent"]:
        raise ValueError(f"{where}: intrusive or desktop UI requires exact target.mode='agent'")


def _validate_point(value: Any, where: str) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{where}: coordinates must be [x, y]")


def _validate_browser_action(action: dict[str, Any], where: str) -> None:
    _require_str(action, "op", where)
    op = action["op"]

    selector_ops = {
        "click", "dblclick", "fill", "press", "check", "uncheck", "select_option",
        "hover", "wait_for", "text", "attribute", "input_value", "upload",
        "download", "click_popup",
    }
    if op in selector_ops:
        _require_str(action, "selector", where)

    if op == "goto":
        _require_str(action, "url", where)
    elif op == "press":
        _require_str(action, "key", where)
    elif op == "attribute":
        _require_str(action, "name", where)
    elif op == "screenshot":
        _require_str(action, "path", where)
    elif op == "evaluate":
        _require_str(action, "expression", where)
    elif op == "upload":
        if not action.get("files") and not action.get("path"):
            raise ValueError(f"{where}: upload requires files or path")
    elif op == "download":
        _require_str(action, "save_as", where)
    elif op == "wait_for_text":
        _require_str(action, "text", where)
    elif op == "wait_for_function":
        _require_str(action, "expression", where)
    elif op == "switch_page":
        if "index" not in action:
            raise ValueError(f"{where}: index is required")
    elif op == "switch_page_matching":
        if not isinstance(action.get("match"), dict) or not action["match"]:
            raise ValueError(f"{where}: match must be a non-empty object")
    elif op == "cookies_add":
        _require_list(action, "cookies", where, non_empty=True)
    elif op == "pdf":
        _require_str(action, "path", where)


def _validate_step(action: dict[str, Any], step: dict[str, Any], index: int) -> None:
    where = f"step {index}"
    kind = step.get("type")
    if kind not in SUPPORTED:
        raise ValueError(f"{where}: unsupported type {kind!r}")

    if "timeout_seconds" in step:
        try:
            timeout = int(step["timeout_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{where}: timeout_seconds must be an integer") from exc
        if timeout <= 0:
            raise ValueError(f"{where}: timeout_seconds must be positive")

    if kind in {"noop", "agent.info"}:
        return

    if kind == "powershell.exec":
        _require_str(step, "script", where)
        return

    if kind == "process.exec":
        _require_str(step, "program", where)
        if "args" in step and not isinstance(step["args"], list):
            raise ValueError(f"{where}: args must be an array")
        if "env" in step and not isinstance(step["env"], dict):
            raise ValueError(f"{where}: env must be an object")
        return

    if kind in {"file.read", "file.write", "file.append", "file.mkdir", "file.delete"}:
        _require_str(step, "path", where)
        if not _workspace_available(action, step):
            raise ValueError(f"{where}: workspace is required for file operations")
        return

    if kind in {"file.copy", "file.move"}:
        _require_str(step, "source", where)
        _require_str(step, "destination", where)
        if not _workspace_available(action, step):
            raise ValueError(f"{where}: workspace is required for file operations")
        return

    if kind == "git.exec":
        _require_list(step, "args", where, non_empty=True)
        if not _workspace_available(action, step):
            raise ValueError(f"{where}: workspace is required for git.exec")
        return

    if kind == "workspace.git_sync":
        if not _workspace_available(action, step):
            raise ValueError(f"{where}: workspace is required for workspace.git_sync")
        return

    if kind == "deploy.exec":
        _require_str(step, "profile", where)
        if "args" in step and not isinstance(step["args"], list):
            raise ValueError(f"{where}: args must be an array")
        return

    if kind == "download.wait":
        _require_str(step, "directory", where)
        if "patterns" in step and (not isinstance(step["patterns"], list) or not all(isinstance(x, str) and x for x in step["patterns"])):
            raise ValueError(f"{where}: download.wait patterns must be a string array")
        _require_exact_agent_target(action, where)
        return

    if kind == "desktop.checkpoint":
        _require_str(step, "session", where)
        op = step.get("op", "read")
        if op not in {"read", "write", "clear"}:
            raise ValueError(f"{where}: desktop.checkpoint op must be read, write, or clear")
        if op == "write" and "data" in step and not isinstance(step["data"], dict):
            raise ValueError(f"{where}: desktop.checkpoint data must be an object")
        _require_exact_agent_target(action, where)
        return

    if kind == "desktop.loop":
        _require_str(step, "session", where)
        _require_list(step, "steps", where, non_empty=True)
        max_cycles = int(step.get("max_cycles", 1))
        if max_cycles < 1 or max_cycles > 1000:
            raise ValueError(f"{where}: desktop.loop max_cycles must be between 1 and 1000")
        if "until" in step and not isinstance(step["until"], dict):
            raise ValueError(f"{where}: desktop.loop until must be an object")
        for nested_index, nested in enumerate(step["steps"]):
            if not isinstance(nested, dict):
                raise ValueError(f"{where}.steps[{nested_index}]: object required")
            if nested.get("type") == "desktop.loop":
                raise ValueError(f"{where}.steps[{nested_index}]: nested desktop.loop is not supported")
            _validate_step(action, nested, index)
        _require_exact_agent_target(action, where)
        return

    if kind == "browser.playwright":
        connection = step.get("connection", "managed")
        if connection not in {"managed", "cdp"}:
            raise ValueError(f"{where}: browser.playwright connection must be 'managed' or 'cdp'")
        _require_list(step, "actions", where, non_empty=True)
        for action_index, browser_action in enumerate(step["actions"]):
            if not isinstance(browser_action, dict):
                raise ValueError(f"{where}.actions[{action_index}]: object required")
            _validate_browser_action(browser_action, f"{where}.actions[{action_index}]")
        return

    if kind == "browser.interactive":
        _require_str(step, "op", where)
        if step["op"] not in {"launch", "ensure", "status"}:
            raise ValueError(f"{where}: browser.interactive op must be 'launch', 'ensure', or 'status'")
        if "url" in step and (not isinstance(step["url"], str) or not step["url"]):
            raise ValueError(f"{where}: url must be a non-empty string")
        return

    if kind == "windows.ui":
        has_connect = isinstance(step.get("connect"), dict) and bool(step["connect"])
        has_start = isinstance(step.get("start"), str) and bool(step["start"])
        has_desktop = step.get("desktop") is True
        if sum((has_connect, has_start, has_desktop)) != 1:
            raise ValueError(f"{where}: windows.ui requires exactly one of a non-empty connect object, start string, or desktop=true")
        _require_list(step, "actions", where, non_empty=True)
        intrusive = has_start or has_desktop
        for action_index, ui_action in enumerate(step["actions"]):
            action_where = f"{where}.actions[{action_index}]"
            if not isinstance(ui_action, dict):
                raise ValueError(f"{action_where}: object required")
            _require_str(ui_action, "op", action_where)
            op = ui_action["op"]
            intrusive = intrusive or op in INTRUSIVE_WINDOWS_UI_OPS
            if op == "type_keys" and not isinstance(ui_action.get("keys"), str):
                raise ValueError(f"{action_where}: type_keys requires a string keys value")
            if op == "hotkey" and (not isinstance(ui_action.get("keys"), str) or not ui_action["keys"]):
                raise ValueError(f"{action_where}: hotkey requires a non-empty string keys value")
            if op == "type_text" and not isinstance(ui_action.get("text"), str):
                raise ValueError(f"{action_where}: type_text requires a string text value")
            if op == "clipboard_set" and not isinstance(ui_action.get("text"), str):
                raise ValueError(f"{action_where}: clipboard_set requires a string text value")
            if op in {"file_dialog_path", "explorer_navigate"}:
                _require_str(ui_action, "path", action_where)
            if op == "file_dialog_filename":
                _require_str(ui_action, "filename", action_where)
            if op in {"explorer_select", "explorer_open", "explorer_rename", "explorer_delete", "explorer_copy", "explorer_cut"}:
                _require_str(ui_action, "name", action_where)
            if op == "explorer_new_folder":
                _require_str(ui_action, "name", action_where)
            if op == "explorer_rename":
                _require_str(ui_action, "new_name", action_where)
            if op in {"move_mouse", "click_at", "double_click_at", "right_click_at"} and "coords" in ui_action:
                _validate_point(ui_action["coords"], action_where)
            if op in {"click_at", "double_click_at", "right_click_at"} and "coords" not in ui_action:
                raise ValueError(f"{action_where}: {op} requires coords")
            if op == "drag":
                _validate_point(ui_action.get("start"), action_where)
                _validate_point(ui_action.get("end"), action_where)
            if op == "move_resize":
                for field in ("x", "y", "width", "height"):
                    if field not in ui_action:
                        raise ValueError(f"{action_where}: move_resize requires {field}")
        if intrusive:
            _require_exact_agent_target(action, where)
        return


def _validate_target(action: dict[str, Any]) -> None:
    target = action.get("target")
    if target is None:
        return
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    mode = target.get("mode", "any")
    if mode == "any":
        return
    if mode == "agent":
        if not isinstance(target.get("agent"), str) or not target["agent"]:
            raise ValueError("target.agent is required")
        return
    if mode == "ordered":
        order = target["order"]
        if not isinstance(order, list) or not order or not all(isinstance(x, str) and x for x in order):
            raise ValueError("target.order must be a non-empty string array")
        if len(set(order)) != len(order):
            raise ValueError("target.order contains duplicates")
        delay = int(target.get("fallback_after_seconds", 8))
        if delay < 0 or delay > 3600:
            raise ValueError("target.fallback_after_seconds out of range")
        if not action.get("created_at"):
            raise ValueError("created_at is required for ordered target fallback")
        parse_utc(action["created_at"])
        return
    raise ValueError(f"unsupported target mode: {mode}")


def _validate_workspace_guard(action: dict[str, Any]) -> None:
    guard = action.get("workspace_guard")
    if guard is None:
        return
    if not isinstance(guard, dict):
        raise ValueError("workspace_guard must be an object")
    unknown = set(guard) - {"allow_dirty", "allow_branch_mismatch"}
    if unknown:
        raise ValueError(f"workspace_guard contains unsupported field(s): {', '.join(sorted(unknown))}")
    if "allow_dirty" in guard and not isinstance(guard["allow_dirty"], bool):
        raise ValueError("workspace_guard.allow_dirty must be boolean")
    if "allow_branch_mismatch" in guard and not isinstance(guard["allow_branch_mismatch"], bool):
        raise ValueError("workspace_guard.allow_branch_mismatch must be boolean")


def validate_action(action: dict[str, Any]) -> None:
    if action.get("protocol") != "q-agent-v4":
        raise ValueError("protocol must be q-agent-v4")
    action_id = action.get("id")
    if not isinstance(action_id, str) or not ID_RE.fullmatch(action_id):
        raise ValueError("invalid id")
    if not isinstance(action.get("steps"), list) or not action["steps"]:
        raise ValueError("steps must be a non-empty array")
    if len(action["steps"]) > 500:
        raise ValueError("too many steps")
    for i, step in enumerate(action["steps"]):
        if not isinstance(step, dict):
            raise ValueError(f"step {i}: object required")
        _validate_step(action, step, i)
    resumable = action.get("resume_from_checkpoint", False)
    if not isinstance(resumable, bool):
        raise ValueError("resume_from_checkpoint must be boolean")
    if "resume_attempts" in action:
        try:
            resume_attempts = int(action["resume_attempts"])
        except (TypeError, ValueError) as exc:
            raise ValueError("resume_attempts must be an integer") from exc
        if resume_attempts < 1 or resume_attempts > 10:
            raise ValueError("resume_attempts must be between 1 and 10")
    if resumable:
        if not all(isinstance(step, dict) and step.get("type") == "desktop.loop" for step in action["steps"]):
            raise ValueError("resume_from_checkpoint=true requires every top-level step to be desktop.loop")
        _require_exact_agent_target(action, "action")

    requires = action.get("requires", [])
    if not isinstance(requires, list) or not all(isinstance(x, str) and x for x in requires):
        raise ValueError("requires must be a string array")
    _validate_workspace_guard(action)
    _validate_target(action)
    expires = action.get("expires_at")
    if expires and parse_utc(expires) <= datetime.now(timezone.utc):
        raise ValueError("action expired")
