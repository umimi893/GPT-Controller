from __future__ import annotations

import argparse
import ctypes
import logging
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .config import Config
from .util import atomic_write_json, configure_file_logging, load_json, utc_now

log = logging.getLogger("q-agent-v4.interactive")


def _target(window: Any, spec: dict[str, Any] | None) -> Any:
    if window is None:
        raise RuntimeError("this operation requires a selected window")
    return window.child_window(**spec) if spec else window


def _require_physical_input(config: Config, op: str) -> None:
    if not config.allow_physical_input:
        raise RuntimeError(f"{op} requires interaction_policy.allow_physical_input=true on this agent")


def _require_foreground(config: Config, op: str) -> None:
    if not config.allow_foreground_activation:
        raise RuntimeError(f"{op} requires interaction_policy.allow_foreground_activation=true on this agent")


def _input_desktop_name() -> str | None:
    # Return the desktop currently receiving physical input (for example Default or Screen-saver).
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetUserObjectInformationW.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL

        handle = user32.OpenInputDesktop(0, False, 0x0001)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            ok = user32.GetUserObjectInformationW(
                handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
            )
            return buffer.value if ok else None
        finally:
            user32.CloseDesktop(handle)
    except Exception:
        return None


def _cursor_accessible() -> bool:
    # Input desktop may report Default slightly before this long-lived host regains User32 access.
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        point = wintypes.POINT()
        return bool(user32.GetCursorPos(ctypes.byref(point)))
    except Exception:
        return False


_WAKE_INPUT_DESKTOP_CODE = (
    "import ctypes;"
    "from ctypes import wintypes as w;"
    "u=ctypes.WinDLL('user32',use_last_error=True);"
    "u.OpenInputDesktop.argtypes=[w.DWORD,w.BOOL,w.DWORD];"
    "u.OpenInputDesktop.restype=w.HANDLE;"
    "u.SetThreadDesktop.argtypes=[w.HANDLE];"
    "u.SetThreadDesktop.restype=w.BOOL;"
    "h=u.OpenInputDesktop(0,False,0x01ff);"
    "assert h,ctypes.get_last_error();"
    "assert u.SetThreadDesktop(h),ctypes.get_last_error();"
    "u.mouse_event(1,12,7,0,0);"
    "u.keybd_event(0x10,0,0,0);"
    "u.keybd_event(0x10,0,2,0)"
)


def _ensure_default_input_desktop(config: Config, op: str) -> str:
    # Wake a saver/lock surface and require Default before injecting user input.
    current = _input_desktop_name()
    if current == "Default" and _cursor_accessible():
        return current
    if not config.allow_physical_input:
        raise RuntimeError(
            f"{op} cannot wake input desktop {current!r}: physical input is disabled"
        )

    helper = Path(sys.executable)
    if helper.name.lower() == "pythonw.exe":
        candidate = helper.with_name("python.exe")
        if candidate.exists():
            helper = candidate
    completed = subprocess.run(
        [str(helper), "-c", _WAKE_INPUT_DESKTOP_CODE],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"{op} failed to wake input desktop {current!r}: "
            f"helper exit {completed.returncode}: {detail}"
        )

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        current = _input_desktop_name()
        if current == "Default" and _cursor_accessible():
            time.sleep(0.15)
            if _cursor_accessible():
                return current
        time.sleep(0.2)

    raise RuntimeError(
        f"{op} requires the Default input desktop, but active desktop remains {current!r}; "
        "unlock the dedicated laptop or disable its lock requirement"
    )


_INTRUSIVE_UI_OPS = {
    "focus", "set_focus", "maximize", "minimize", "restore", "move_resize",
    "click", "click_input", "type_keys", "move_mouse", "click_at",
    "double_click_at", "right_click_at", "scroll", "drag", "hotkey", "type_text",
    "file_dialog_path", "file_dialog_filename", "file_dialog_accept", "file_dialog_cancel",
    "explorer_navigate", "explorer_select", "explorer_open", "explorer_new_folder",
    "explorer_rename", "explorer_delete", "explorer_copy", "explorer_cut", "explorer_paste",
}


def _window_summary(wrapper: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, getter in (
        ("title", lambda: wrapper.window_text()),
        ("handle", lambda: int(wrapper.handle)),
        ("process_id", lambda: int(wrapper.process_id())),
        ("class_name", lambda: wrapper.class_name()),
        ("control_type", lambda: getattr(wrapper.element_info, "control_type", None)),
        ("visible", lambda: bool(wrapper.is_visible())),
        ("enabled", lambda: bool(wrapper.is_enabled())),
    ):
        try:
            out[name] = getter()
        except Exception:
            out[name] = None
    try:
        rect = wrapper.rectangle()
        out["rect"] = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:
        out["rect"] = None
    return out


def _tree(wrapper: Any, max_depth: int, max_nodes: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def visit(node: Any, depth: int) -> None:
        if len(rows) >= max_nodes or depth > max_depth:
            return
        row = _window_summary(node)
        row["depth"] = depth
        try:
            row["automation_id"] = getattr(node.element_info, "automation_id", None)
            row["name"] = getattr(node.element_info, "name", None)
        except Exception:
            row["automation_id"] = None
            row["name"] = None
        rows.append(row)
        if depth == max_depth:
            return
        try:
            children = node.children()
        except Exception:
            return
        for child in children:
            if len(rows) >= max_nodes:
                break
            visit(child, depth + 1)

    visit(wrapper, 0)
    return rows


def _coords(action: dict[str, Any], key: str = "coords") -> tuple[int, int]:
    value = action.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{key} must be [x, y]")
    return int(value[0]), int(value[1])


def _clipboard_get_text() -> str | None:
    try:
        import win32clipboard
    except ImportError as exc:
        raise RuntimeError("clipboard support requires pywin32") from exc
    win32clipboard.OpenClipboard()
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return None
        return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))
    finally:
        win32clipboard.CloseClipboard()


def _clipboard_set_text(text: str) -> None:
    try:
        import win32clipboard
    except ImportError as exc:
        raise RuntimeError("clipboard support requires pywin32") from exc
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def _paste_unicode(keyboard: Any, text: str) -> None:
    previous = None
    try:
        previous = _clipboard_get_text()
    except Exception:
        previous = None
    _clipboard_set_text(text)
    keyboard.send_keys("^v", pause=0.02)
    # Clipboard paste is consumed asynchronously by Explorer/common dialogs. Restoring
    # the previous clipboard after only 50 ms can race the target and paste stale text.
    # Keep the requested Unicode payload available long enough for the focused control
    # to consume WM_PASTE before restoring the user's clipboard.
    time.sleep(0.45)
    try:
        if previous is None:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
        else:
            _clipboard_set_text(previous)
    except Exception:
        pass


def _wrapper(target: Any) -> Any:
    try:
        return target.wrapper_object()
    except Exception:
        return target


def _best_dialog_edit(target: Any, automation_ids: list[str] | None = None) -> Any:
    wrapper = _wrapper(target)
    preferred = set(automation_ids or ["1001", "1148"])
    try:
        if str(getattr(wrapper.element_info, "control_type", "")) == "Edit" and wrapper.is_visible() and wrapper.is_enabled():
            return wrapper
    except Exception:
        pass
    try:
        edits = list(wrapper.descendants(control_type="Edit"))
    except Exception:
        edits = []
    visible = []
    for edit in edits:
        try:
            if edit.is_visible() and edit.is_enabled():
                visible.append(edit)
        except Exception:
            continue
    for edit in visible:
        try:
            if str(getattr(edit.element_info, "automation_id", "")) in preferred:
                return edit
        except Exception:
            pass
    hints = ("file name", "filename", "ファイル名", "名前")
    for edit in visible:
        try:
            label = str(getattr(edit.element_info, "name", "") or edit.window_text() or "").casefold()
            if any(hint in label for hint in hints):
                return edit
        except Exception:
            pass
    if visible:
        return visible[-1]
    raise RuntimeError("no visible enabled Edit control found in file dialog")


def _looks_like_file_dialog(wrapper: Any) -> bool:
    try:
        if not wrapper.is_visible() or not wrapper.is_enabled():
            return False
    except Exception:
        return False
    try:
        class_name = str(wrapper.class_name() or "")
    except Exception:
        class_name = ""
    try:
        descendants = list(wrapper.descendants())
    except Exception:
        descendants = []
    automation_ids: set[str] = set()
    names: list[str] = []
    for item in descendants[:500]:
        try:
            automation_ids.add(str(getattr(item.element_info, "automation_id", "") or ""))
            names.append(str(getattr(item.element_info, "name", "") or item.window_text() or "").casefold())
        except Exception:
            continue
    # Native Windows common Open/Save dialogs use #32770. UIA descendants can be
    # temporarily unavailable while the modal is being initialized, so requiring the
    # filename descendants here creates a false negative even though the dialog itself
    # is already visible and active. Ranking in _discover_dialog still prefers the
    # foreground window and the parent application's process.
    if class_name == "#32770":
        return True
    # Modern non-common dialogs must expose an actual filename control. Broad text
    # matching is deliberately avoided because ordinary apps (including ChatGPT) may
    # contain words such as Save/Open and become dangerous false positives.
    return bool({"FileNameControlHost", "1001"} & automation_ids)


def _win32_dialog_handles() -> list[dict[str, Any]]:
    try:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        rows: list[dict[str, Any]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            try:
                if not user32.IsWindowVisible(hwnd) or not user32.IsWindowEnabled(hwnd):
                    return True
                title = ctypes.create_unicode_buffer(512)
                klass = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, len(title))
                user32.GetClassNameW(hwnd, klass, len(klass))
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                rows.append({
                    "handle": int(hwnd),
                    "title": title.value,
                    "class_name": klass.value,
                    "process_id": int(pid.value),
                })
            except Exception:
                pass
            return True

        user32.EnumWindows(callback, 0)
        return rows
    except Exception:
        return []


def _discover_dialog(desktop: Any, current_window: Any, action: dict[str, Any]) -> Any:
    timeout = max(0.1, min(float(action.get("timeout_seconds", 30)), 120.0))
    deadline = time.monotonic() + timeout
    kind = str(action.get("kind", "file")).casefold()
    if kind not in {"file", "any"}:
        raise ValueError("discover_dialog kind must be 'file' or 'any'")
    title_contains = str(action.get("title_contains", "")).casefold()
    title_re = str(action.get("title_re", ""))
    class_names = {str(x).casefold() for x in action.get("class_names", []) if str(x)}

    current = _wrapper(current_window) if current_window is not None else None
    current_handle = None
    current_pid = None
    try:
        current_handle = int(current.handle)
    except Exception:
        pass
    try:
        current_pid = int(current.process_id())
    except Exception:
        pass

    def candidate_score(handle: int, pid: int, class_name: str, active_handle: int) -> tuple[int, int, int]:
        return (
            0 if active_handle and handle == active_handle else 1,
            0 if current_pid and pid == current_pid else 1,
            0 if class_name == "#32770" else 1,
        )

    def accepts(wrapper: Any, summary: dict[str, Any]) -> bool:
        handle = int(summary.get("handle") or 0)
        if current_handle and handle == current_handle:
            return False
        if not summary.get("visible") or not summary.get("enabled"):
            return False
        title = str(summary.get("title") or "")
        class_name = str(summary.get("class_name") or "")
        if title_contains and title_contains not in title.casefold():
            return False
        if title_re and not re.search(title_re, title, flags=re.IGNORECASE):
            return False
        if class_names and class_name.casefold() not in class_names:
            return False
        if kind == "file" and not _looks_like_file_dialog(wrapper):
            return False
        return True

    last_seen: list[str] = []
    while time.monotonic() < deadline:
        active_handle = 0
        try:
            import win32gui
            active_handle = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            pass

        ranked: list[tuple[int, int, int, Any]] = []
        seen_handles: set[int] = set()

        # Fast path: foreground HWND is authoritative even when UIA's top-level enumeration
        # temporarily omits a native modal dialog.
        if active_handle and active_handle != current_handle:
            try:
                active = desktop.window(handle=active_handle).wrapper_object()
                summary = _window_summary(active)
                if accepts(active, summary):
                    return desktop.window(handle=active_handle)
            except Exception:
                pass

        try:
            windows = list(desktop.windows())
        except Exception:
            windows = []
        last_seen = []
        for candidate in windows:
            try:
                summary = _window_summary(candidate)
                handle = int(summary.get("handle") or 0)
                if handle:
                    seen_handles.add(handle)
                if summary.get("visible"):
                    last_seen.append(
                        f"{summary.get('title')!r}/{summary.get('class_name')!r}/pid={summary.get('process_id')}"
                    )
                if not accepts(candidate, summary):
                    continue
                pid = int(summary.get("process_id") or 0)
                class_name = str(summary.get("class_name") or "")
                ranked.append((*candidate_score(handle, pid, class_name, active_handle), candidate))
            except Exception:
                continue

        # Win32 fallback catches native modal dialogs that UIA Desktop.windows() can omit.
        for row in _win32_dialog_handles():
            handle = int(row.get("handle") or 0)
            if not handle or handle in seen_handles or (current_handle and handle == current_handle):
                continue
            try:
                candidate = desktop.window(handle=handle).wrapper_object()
                summary = _window_summary(candidate)
                if not accepts(candidate, summary):
                    continue
                pid = int(summary.get("process_id") or row.get("process_id") or 0)
                class_name = str(summary.get("class_name") or row.get("class_name") or "")
                ranked.append((*candidate_score(handle, pid, class_name, active_handle), candidate))
            except Exception:
                continue

        if ranked:
            ranked.sort(key=lambda row: row[:3])
            return desktop.window(handle=int(ranked[0][3].handle))
        time.sleep(0.15)

    detail = "; ".join(last_seen[:12])
    native = "; ".join(
        f"{row.get('title')!r}/{row.get('class_name')!r}/pid={row.get('process_id')}"
        for row in _win32_dialog_handles()[:12]
    )
    raise TimeoutError(
        f"no matching {kind} dialog appeared within {timeout}s; UIA={detail}; Win32={native}"
    )


def _explorer_item(target: Any, name: str) -> Any:
    wrapper = _wrapper(target)
    name_fold = name.casefold()
    try:
        descendants = wrapper.descendants()
    except Exception as exc:
        raise RuntimeError(f"cannot enumerate Explorer items: {exc}") from exc
    ranked: list[tuple[int, int, Any]] = []
    type_rank = {"ListItem": 0, "DataItem": 0, "TreeItem": 2}
    for item in descendants:
        try:
            control_type = str(getattr(item.element_info, "control_type", ""))
            if control_type not in type_rank:
                continue
            title = str(item.window_text() or getattr(item.element_info, "name", "") or "")
            folded = title.casefold()
            if folded == name_fold:
                ranked.append((0, type_rank[control_type], item))
            elif name_fold in folded:
                ranked.append((1, type_rank[control_type], item))
        except Exception:
            continue
    if ranked:
        ranked.sort(key=lambda row: (row[0], row[1]))
        return ranked[0][2]
    raise RuntimeError(f"Explorer item not found: {name}")


def _force_foreground_window(handle: int) -> bool:
    """Activate a same-session top-level window despite the normal foreground lock.

    Windows may reject SetForegroundWindow from a long-lived automation host even when
    that host runs in the interactive user session. Temporarily attaching this thread
    to the foreground/target input queues makes activation deterministic without
    requiring a synthetic click.
    """
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.SetFocus.argtypes = [wintypes.HWND]
        user32.SetFocus.restype = wintypes.HWND
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        hwnd = wintypes.HWND(handle)
        current_tid = int(kernel32.GetCurrentThreadId())
        foreground = user32.GetForegroundWindow()
        foreground_tid = int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        target_tid = int(user32.GetWindowThreadProcessId(hwnd, None))
        attached: list[int] = []
        try:
            for tid in {foreground_tid, target_tid}:
                if tid and tid != current_tid and user32.AttachThreadInput(current_tid, tid, True):
                    attached.append(tid)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
            time.sleep(0.06)
            return int(user32.GetForegroundWindow() or 0) == int(handle)
        finally:
            for tid in reversed(attached):
                try:
                    user32.AttachThreadInput(current_tid, tid, False)
                except Exception:
                    pass
    except Exception:
        return False


def _physical_focus_fallback(wrapper: Any, config: Config | None, op: str) -> bool:
    if config is None or not config.allow_physical_input:
        return False
    try:
        from pywinauto import mouse
        _ensure_default_input_desktop(config, op)
        rect = wrapper.rectangle()
        width = max(1, int(rect.right) - int(rect.left))
        height = max(1, int(rect.bottom) - int(rect.top))
        x = int(rect.left) + min(max(width // 2, 20), max(width - 20, 20))
        y = int(rect.top) + min(30, max(height // 4, 8))
        mouse.click(button="left", coords=(x, y))
        time.sleep(0.08)
        try:
            import win32gui
            return int(win32gui.GetForegroundWindow()) == int(wrapper.handle)
        except Exception:
            return True
    except Exception:
        return False


def _focus_window(target: Any, config: Config | None = None, op: str = "focus") -> Any:
    wrapper = _wrapper(target)
    handle = None
    try:
        handle = int(wrapper.handle)
    except Exception:
        handle = None
    last_error: Exception | None = None
    for attempt in range(4):
        if config is not None:
            _ensure_default_input_desktop(config, op)
        try:
            try:
                if hasattr(wrapper, "is_minimized") and wrapper.is_minimized():
                    wrapper.restore()
            except Exception:
                pass
            try:
                wrapper.set_focus()
            except Exception as exc:
                last_error = exc
            if not handle:
                return wrapper
            try:
                import win32gui
                if int(win32gui.GetForegroundWindow()) == handle:
                    return wrapper
            except Exception as exc:
                last_error = exc
            if _force_foreground_window(handle):
                return wrapper
            if attempt >= 1 and _physical_focus_fallback(wrapper, config, op):
                return wrapper
        except Exception as exc:
            last_error = exc
        time.sleep(0.12 * (attempt + 1))
    raise RuntimeError(f"could not focus target window for {op}: {last_error}")


def execute_windows_ui(config: Config, step: dict[str, Any]) -> dict[str, Any]:
    actions = step.get("actions", [])
    intrusive = next(
        (str(action.get("op")) for action in actions if action.get("op") in _INTRUSIVE_UI_OPS),
        None,
    )
    input_desktop = _ensure_default_input_desktop(config, intrusive) if intrusive else _input_desktop_name()

    try:
        from pywinauto import Application, Desktop, keyboard, mouse
    except ImportError as exc:
        raise RuntimeError("Windows UI support requires pywinauto") from exc

    backend = step.get("backend", "uia")
    app: Any | None = None
    desktop = Desktop(backend=backend)
    window: Any | None = None

    if "start" in step:
        if not config.allow_visible_gui_launch:
            raise RuntimeError("visible GUI launch is disabled by interaction_policy.allow_visible_gui_launch")
        start_timeout = int(step.get("start_timeout_seconds", 30))

        # Snapshot visible top-level windows before launch. Modern packaged apps may
        # delegate from the launcher process to a different process without becoming
        # foreground immediately, so process ownership/foreground alone is insufficient.
        before_handles: set[int] = set()
        try:
            for existing in desktop.windows():
                try:
                    if existing.is_visible():
                        before_handles.add(int(existing.handle))
                except Exception:
                    pass
        except Exception:
            pass

        app = Application(backend=backend).start(step["start"], timeout=start_timeout)
        window_spec = step.get("window", {})
        if window_spec:
            candidate = desktop.window(**window_spec)
            candidate.wait("exists visible", timeout=start_timeout)
            window = candidate
        else:
            deadline = time.monotonic() + start_timeout
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                # Fast path for ordinary Win32 apps whose launched process owns the window.
                try:
                    candidate = app.top_window()
                    candidate.wait("exists visible", timeout=0.5)
                    window = candidate
                    break
                except Exception as exc:
                    last_error = exc

                # Launcher/package fallback: discover a new visible top-level window,
                # regardless of which process ultimately owns it.
                try:
                    new_candidates: list[Any] = []
                    for candidate in desktop.windows():
                        try:
                            handle = int(candidate.handle)
                            if handle in before_handles or not candidate.is_visible():
                                continue
                            new_candidates.append(candidate)
                        except Exception:
                            continue
                    if new_candidates:
                        # Prefer an enabled non-empty window, then take the first new one.
                        new_candidates.sort(
                            key=lambda c: (
                                0 if (c.is_enabled() and bool(c.window_text().strip())) else 1,
                                int(c.handle),
                            )
                        )
                        window = desktop.window(handle=int(new_candidates[0].handle))
                        break
                except Exception as exc:
                    last_error = exc

                time.sleep(0.2)

            if window is None:
                raise RuntimeError(
                    f"visible application launched but no new window became available: {last_error}"
                )
    elif "connect" in step:
        app = Application(backend=backend).connect(**step["connect"])
        window_spec = step.get("window", {})
        window = app.window(**window_spec) if window_spec else app.top_window()
    elif step.get("desktop") is True:
        window_spec = step.get("window")
        if isinstance(window_spec, dict) and window_spec:
            window = desktop.window(**window_spec)
    else:
        raise ValueError("windows.ui requires exactly one of connect, start, or desktop=true")

    outputs: list[Any] = []

    for action in step.get("actions", []):
        op = action["op"]
        if op in _INTRUSIVE_UI_OPS:
            # Long GUI steps can outlive a saver transition or Explorer shell handoff.
            # Re-validate the input desktop immediately before every physical/foreground op.
            input_desktop = _ensure_default_input_desktop(config, op)
        target = _target(window, action.get("control")) if op not in {
            "list_windows", "active_window", "click_at", "double_click_at", "right_click_at",
            "move_mouse", "scroll", "drag", "hotkey", "type_text", "clipboard_get",
            "clipboard_set", "sleep", "capture_desktop", "cursor_position", "input_desktop",
            "discover_dialog",
        } else None

        if op == "discover_dialog":
            window = _discover_dialog(desktop, window, action)
            outputs.append({"op": op, "window": _window_summary(_wrapper(window))})
        elif op == "file_dialog_path":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            wrapper = _focus_window(target, config, op)
            path = str(action.get("path", ""))
            if not path:
                raise ValueError("file_dialog_path requires path")
            keyboard.send_keys("^l", pause=0.02)
            time.sleep(float(action.get("settle_seconds", 0.15)))
            _paste_unicode(keyboard, path)
            keyboard.send_keys("{ENTER}", pause=0.02)
            time.sleep(float(action.get("after_seconds", 0.35)))
            outputs.append({"op": op, "path": path, "window": _window_summary(wrapper)})
        elif op == "file_dialog_filename":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            text = str(action.get("filename", ""))
            if not text:
                raise ValueError("file_dialog_filename requires filename")
            edit = _best_dialog_edit(target, action.get("automation_ids"))
            try:
                edit.set_edit_text(text)
            except Exception:
                edit.set_focus()
                keyboard.send_keys("^a", pause=0.02)
                _paste_unicode(keyboard, text)
            outputs.append({"op": op, "filename": text})
        elif op == "file_dialog_accept":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            wrapper = _focus_window(target, config, op)
            titles = [str(x) for x in (action.get("button_titles") or ["Save", "Open", "Select", "保存", "開く", "選択"])]
            clicked = None
            try:
                if str(getattr(wrapper.element_info, "control_type", "")) == "Button":
                    try:
                        wrapper.invoke()
                    except Exception:
                        wrapper.click_input()
                    clicked = str(wrapper.window_text() or "button")
            except Exception:
                pass
            if clicked is None:
                try:
                    buttons = wrapper.descendants(control_type="Button")
                except Exception:
                    buttons = []
                # Native common dialogs expose their primary accept button as automation id 1.
                # Prefer that stable identifier before localized button text so unrelated child
                # buttons such as a file-type "Open" control cannot be clicked by mistake.
                primary_ids = {str(x) for x in (action.get("button_automation_ids") or ["1"])}
                for button in buttons:
                    try:
                        automation_id = str(getattr(button.element_info, "automation_id", "") or "")
                        if automation_id not in primary_ids or not button.is_visible() or not button.is_enabled():
                            continue
                        name = str(button.window_text() or getattr(button.element_info, "name", "") or "button")
                        try:
                            button.invoke()
                        except Exception:
                            button.click_input()
                        clicked = name
                        break
                    except Exception:
                        continue
                if clicked is None:
                    expected = [title.casefold() for title in titles]
                    for button in buttons:
                        try:
                            name = str(button.window_text() or getattr(button.element_info, "name", "") or "")
                            folded = name.casefold()
                            if any(token == folded or token in folded for token in expected):
                                try:
                                    button.invoke()
                                except Exception:
                                    button.click_input()
                                clicked = name
                                break
                        except Exception:
                            continue
            if clicked is None:
                keyboard.send_keys("{ENTER}", pause=0.02)
            outputs.append({"op": op, "button": clicked, "fallback_enter": clicked is None})
        elif op == "file_dialog_cancel":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            _focus_window(target, config, op)
            keyboard.send_keys("{ESC}", pause=0.02)
            outputs.append({"op": op})
        elif op == "explorer_navigate":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            wrapper = _focus_window(target, config, op)
            path = str(action.get("path", ""))
            if not path:
                raise ValueError("explorer_navigate requires path")
            keyboard.send_keys("^l", pause=0.02)
            _paste_unicode(keyboard, path)
            keyboard.send_keys("{ENTER}", pause=0.02)
            time.sleep(float(action.get("after_seconds", 0.5)))
            outputs.append({"op": op, "path": path, "window": _window_summary(wrapper)})
        elif op in {"explorer_select", "explorer_open", "explorer_rename", "explorer_delete", "explorer_copy", "explorer_cut"}:
            _require_physical_input(config, op)
            _require_foreground(config, op)
            wrapper = _focus_window(target, config, op)
            name = str(action.get("name", ""))
            if not name:
                raise ValueError(f"{op} requires name")
            item = _explorer_item(wrapper, name)
            selected = False
            try:
                item.select()
                selected = True
            except Exception:
                try:
                    item.set_focus()
                    keyboard.send_keys("{SPACE}", pause=0.02)
                    selected = True
                except Exception:
                    pass
            if not selected:
                _ensure_default_input_desktop(config, op)
                item.click_input()
            if op == "explorer_open":
                keyboard.send_keys("{ENTER}", pause=0.02)
            elif op == "explorer_rename":
                new_name = str(action.get("new_name", ""))
                if not new_name:
                    raise ValueError("explorer_rename requires new_name")
                keyboard.send_keys("{F2}", pause=0.02)
                time.sleep(0.1)
                keyboard.send_keys("^a", pause=0.02)
                _paste_unicode(keyboard, new_name)
                keyboard.send_keys("{ENTER}", pause=0.02)
            elif op == "explorer_delete":
                keyboard.send_keys("{DELETE}", pause=0.02)
            elif op == "explorer_copy":
                keyboard.send_keys("^c", pause=0.02)
            elif op == "explorer_cut":
                keyboard.send_keys("^x", pause=0.02)
            outputs.append({"op": op, "name": name})
        elif op == "explorer_new_folder":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            _focus_window(target, config, op)
            name = str(action.get("name", ""))
            if not name:
                raise ValueError("explorer_new_folder requires name")
            keyboard.send_keys("^+n", pause=0.02)
            time.sleep(0.15)
            _paste_unicode(keyboard, name)
            keyboard.send_keys("{ENTER}", pause=0.02)
            outputs.append({"op": op, "name": name})
        elif op == "explorer_paste":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            _focus_window(target, config, op)
            keyboard.send_keys("^v", pause=0.02)
            outputs.append({"op": op})
        elif op == "list_windows":
            limit = max(1, min(int(action.get("limit", 100)), 500))
            title_contains = str(action.get("title_contains", "")).casefold()
            class_name = str(action.get("class_name", "")).casefold()
            rows = []
            for wrapper in desktop.windows():
                summary = _window_summary(wrapper)
                if title_contains and title_contains not in str(summary.get("title") or "").casefold():
                    continue
                if class_name and class_name != str(summary.get("class_name") or "").casefold():
                    continue
                rows.append(summary)
                if len(rows) >= limit:
                    break
            outputs.append({"op": op, "windows": rows})
        elif op == "cursor_position":
            try:
                from ctypes import wintypes
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                point = wintypes.POINT()
                if not user32.GetCursorPos(ctypes.byref(point)):
                    raise OSError(ctypes.get_last_error(), "GetCursorPos failed")
                outputs.append({"op": op, "coords": [int(point.x), int(point.y)]})
            except Exception as exc:
                outputs.append({"op": op, "coords": None, "error": str(exc)})
        elif op == "input_desktop":
            outputs.append({"op": op, "name": _input_desktop_name(), "cursor_accessible": _cursor_accessible()})
        elif op == "active_window":
            try:
                import win32gui
                handle = int(win32gui.GetForegroundWindow())
                wrapper = desktop.window(handle=handle).wrapper_object() if handle else None
                outputs.append({"op": op, "window": _window_summary(wrapper) if wrapper else None})
            except Exception as exc:
                outputs.append({"op": op, "window": None, "error": str(exc)})
        elif op == "dump_tree":
            wrapper = target.wrapper_object()
            outputs.append({
                "op": op,
                "nodes": _tree(
                    wrapper,
                    max_depth=max(0, min(int(action.get("max_depth", 4)), 12)),
                    max_nodes=max(1, min(int(action.get("max_nodes", 250)), 2000)),
                ),
            })
        elif op == "wait_ready":
            target.wait("exists enabled visible ready", timeout=int(action.get("timeout_seconds", 30)))
            outputs.append({"op": op})
        elif op == "wait_visible":
            target.wait("exists visible", timeout=int(action.get("timeout_seconds", 30)))
            outputs.append({"op": op})
        elif op == "invoke":
            target.invoke()
            outputs.append({"op": op})
        elif op in {"set_value", "set_text"}:
            target.set_edit_text(action.get("text", ""))
            outputs.append({"op": op})
        elif op == "get_text":
            outputs.append({"op": op, "text": target.window_text()})
        elif op == "exists":
            outputs.append({"op": op, "exists": bool(target.exists(timeout=float(action.get("timeout_seconds", 0))))})
        elif op == "is_visible":
            try:
                value = bool(target.is_visible())
            except Exception:
                value = False
            outputs.append({"op": op, "visible": value})
        elif op == "is_enabled":
            try:
                value = bool(target.is_enabled())
            except Exception:
                value = False
            outputs.append({"op": op, "enabled": value})
        elif op == "get_rect":
            rect = target.rectangle()
            outputs.append({"op": op, "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]})
        elif op == "summary":
            outputs.append({"op": op, "window": _window_summary(target.wrapper_object())})
        elif op == "select":
            target.select()
            outputs.append({"op": op})
        elif op == "toggle":
            target.toggle()
            outputs.append({"op": op})
        elif op == "expand":
            target.expand()
            outputs.append({"op": op})
        elif op == "collapse":
            target.collapse()
            outputs.append({"op": op})
        elif op == "close":
            target.close()
            outputs.append({"op": op})
        elif op in {"focus", "set_focus"}:
            _require_foreground(config, op)
            target.set_focus()
            outputs.append({"op": op})
        elif op == "maximize":
            _require_foreground(config, op)
            target.maximize()
            outputs.append({"op": op})
        elif op == "minimize":
            _require_foreground(config, op)
            target.minimize()
            outputs.append({"op": op})
        elif op == "restore":
            _require_foreground(config, op)
            target.restore()
            outputs.append({"op": op})
        elif op == "move_resize":
            _require_foreground(config, op)
            target.move_window(
                x=int(action["x"]), y=int(action["y"]),
                width=int(action["width"]), height=int(action["height"]),
                repaint=True,
            )
            outputs.append({"op": op})
        elif op == "click":
            _require_physical_input(config, op)
            target.click()
            outputs.append({"op": op})
        elif op == "click_input":
            _require_physical_input(config, op)
            kwargs: dict[str, Any] = {}
            for key in ("button", "coords", "double", "wheel_dist", "pressed"):
                if key in action:
                    kwargs[key] = action[key]
            target.click_input(**kwargs)
            outputs.append({"op": op})
        elif op == "type_keys":
            _require_physical_input(config, op)
            keys = action.get("keys")
            if not isinstance(keys, str):
                raise ValueError("type_keys requires a string keys value")
            kwargs = {"set_foreground": config.allow_foreground_activation}
            for key in ("pause", "with_spaces", "with_tabs", "with_newlines"):
                if key in action:
                    kwargs[key] = action[key]
            target.type_keys(keys, **kwargs)
            outputs.append({"op": op})
        elif op == "move_mouse":
            _require_physical_input(config, op)
            if action.get("coords") is not None:
                point = _coords(action)
            elif window is not None:
                rect = _target(window, action.get("control")).rectangle()
                mid = rect.mid_point()
                point = (int(mid.x), int(mid.y))
            else:
                raise ValueError("move_mouse requires coords in desktop mode")
            mouse.move(coords=point)
            outputs.append({"op": op, "coords": list(point)})
        elif op in {"click_at", "double_click_at", "right_click_at"}:
            _require_physical_input(config, op)
            point = _coords(action)
            if op == "click_at":
                mouse.click(button=str(action.get("button", "left")), coords=point)
            elif op == "double_click_at":
                mouse.double_click(button=str(action.get("button", "left")), coords=point)
            else:
                mouse.right_click(coords=point)
            outputs.append({"op": op, "coords": list(point)})
        elif op == "scroll":
            _require_physical_input(config, op)
            point = _coords(action) if "coords" in action else None
            mouse.scroll(coords=point, wheel_dist=int(action.get("wheel_dist", 1)))
            outputs.append({"op": op, "coords": list(point) if point else None})
        elif op == "drag":
            _require_physical_input(config, op)
            start = _coords(action, "start")
            end = _coords(action, "end")
            button = str(action.get("button", "left"))
            mouse.move(coords=start)
            mouse.press(button=button, coords=start)
            mouse.move(coords=end)
            mouse.release(button=button, coords=end)
            outputs.append({"op": op, "start": list(start), "end": list(end)})
        elif op == "hotkey":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            keys = action.get("keys")
            if not isinstance(keys, str) or not keys:
                raise ValueError("hotkey requires a non-empty string keys value")
            keyboard.send_keys(keys, pause=float(action.get("pause", 0.02)))
            outputs.append({"op": op})
        elif op == "type_text":
            _require_physical_input(config, op)
            _require_foreground(config, op)
            text = action.get("text")
            if not isinstance(text, str):
                raise ValueError("type_text requires a string text value")
            previous = None
            try:
                previous = _clipboard_get_text()
            except Exception:
                previous = None
            _clipboard_set_text(text)
            keyboard.send_keys("^v", pause=float(action.get("pause", 0.02)))
            time.sleep(float(action.get("settle_seconds", 0.05)))
            if action.get("restore_clipboard", True) and previous is not None:
                _clipboard_set_text(previous)
            outputs.append({"op": op, "length": len(text)})
        elif op == "clipboard_get":
            outputs.append({"op": op, "text": _clipboard_get_text()})
        elif op == "clipboard_set":
            text = action.get("text")
            if not isinstance(text, str):
                raise ValueError("clipboard_set requires a string text value")
            _clipboard_set_text(text)
            outputs.append({"op": op, "length": len(text)})
        elif op == "sleep":
            seconds = max(0.0, min(float(action.get("seconds", 1.0)), 60.0))
            time.sleep(seconds)
            outputs.append({"op": op, "seconds": seconds})
        elif op in {"capture", "capture_desktop"}:
            name = str(action.get("name", f"capture-{int(time.time() * 1000)}.png"))
            safe_name = Path(name).name
            if not safe_name.lower().endswith(".png"):
                safe_name += ".png"
            capture_dir = config.repo_path.parent / "scratch" / "ui-captures"
            capture_dir.mkdir(parents=True, exist_ok=True)
            path = capture_dir / safe_name
            if op == "capture_desktop":
                try:
                    from PIL import ImageGrab
                except ImportError as exc:
                    raise RuntimeError("desktop capture requires Pillow") from exc
                image = ImageGrab.grab(all_screens=True)
            else:
                image = target.capture_as_image()
            image.save(path, format="PNG")
            outputs.append({"op": op, "path": str(path), "size": list(image.size)})
        else:
            raise ValueError(f"unsupported windows.ui op: {op}")

    return {
        "backend": backend,
        "outputs": outputs,
        "non_interference": config.non_interference,
        "physical_input_allowed": config.allow_physical_input,
        "foreground_activation_allowed": config.allow_foreground_activation,
        "visible_gui_launch_allowed": config.allow_visible_gui_launch,
        "input_desktop": input_desktop,
    }


def serve(config: Config) -> None:
    requests = config.interactive_spool / "requests"
    processing = config.interactive_spool / "processing"
    responses = config.interactive_spool / "responses"
    requests.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)
    responses.mkdir(parents=True, exist_ok=True)

    for processing_path in sorted(processing.glob("*.json")):
        request_id = processing_path.stem
        response_path = responses / f"{request_id}.json"
        if not response_path.exists():
            atomic_write_json(response_path, {
                "protocol": "q-agent-v4-interactive-response",
                "id": request_id,
                "finished_at": utc_now(),
                "status": "failed",
                "error": "Interactive Host restarted after local claim; request is ambiguous and was NOT replayed.",
            })
        # A durable response resolves the local claim; never accumulate stale processing files.
        processing_path.unlink(missing_ok=True)
    log.info("Interactive Host started: %s", config.interactive_spool)
    while True:
        did_work = False
        for request_path in sorted(requests.glob("*.json")):
            did_work = True
            request_id = request_path.stem
            processing_path = processing / request_path.name
            response_path = responses / f"{request_id}.json"
            if response_path.exists():
                request_path.unlink(missing_ok=True)
                continue
            try:
                request_path.replace(processing_path)
                request = load_json(processing_path)
                if request.get("protocol") != "q-agent-v4-interactive-request":
                    raise ValueError("invalid interactive request protocol")
                step = request["step"]
                if step.get("type") != "windows.ui":
                    raise ValueError("interactive host accepts only windows.ui")
                result = execute_windows_ui(config, step)
                response = {
                    "protocol": "q-agent-v4-interactive-response",
                    "id": request_id,
                    "finished_at": utc_now(),
                    "status": "succeeded",
                    "result": result,
                }
            except Exception as exc:
                response = {
                    "protocol": "q-agent-v4-interactive-response",
                    "id": request_id,
                    "finished_at": utc_now(),
                    "status": "failed",
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
            atomic_write_json(response_path, response)
            # Response publication is the commit point for this local interactive claim.
            processing_path.unlink(missing_ok=True)
        if not did_work:
            time.sleep(0.25)


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT Controller user-session Interactive Host")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    config = Config.load(Path(args.config))
    level = getattr(logging, args.log_level.upper(), logging.INFO)
    configure_file_logging(config.repo_path.parent / "logs" / "interactive.log", level)
    serve(config)


if __name__ == "__main__":
    main()
