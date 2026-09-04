from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.desktop_stability import (
    clear_checkpoint,
    condition_matches,
    load_checkpoint,
    wait_for_download,
    write_checkpoint,
)
from agent_runtime.protocol import validate_action


def _config(tmp_path: Path):
    control = tmp_path / "control"
    control.mkdir()
    return SimpleNamespace(repo_path=control, agent_id="worker-pc")


def test_checkpoint_roundtrip(tmp_path: Path):
    config = _config(tmp_path)
    assert load_checkpoint(config, "job-1") is None
    record = write_checkpoint(config, "job-1", {"status": "running", "cycle": 2, "next_step": 4})
    assert record["cycle"] == 2
    loaded = load_checkpoint(config, "job-1")
    assert loaded and loaded["next_step"] == 4
    assert clear_checkpoint(config, "job-1") is True
    assert load_checkpoint(config, "job-1") is None


def test_loop_condition_language_supports_nested_paths_and_boolean_composition():
    outputs = [
        {"status": "succeeded", "result": {"outputs": [{"exists": True, "text": "ready now"}]}}
    ]
    assert condition_matches(outputs, {"source": 0, "path": "result.outputs.0.exists", "equals": True})
    assert condition_matches(outputs, {"all": [
        {"source": 0, "path": "status", "equals": "succeeded"},
        {"source": 0, "path": "result.outputs.0.text", "contains": "ready"},
    ]})
    assert condition_matches(outputs, {"not": {"source": 0, "path": "status", "equals": "failed"}})


def test_download_wait_tracks_new_file_until_stable(tmp_path: Path):
    target = tmp_path / "report.zip"

    def writer():
        time.sleep(0.05)
        target.write_bytes(b"abc")
        time.sleep(0.05)
        target.write_bytes(b"abcdef")

    thread = threading.Thread(target=writer)
    thread.start()
    result = wait_for_download(
        str(tmp_path), patterns=["*.zip"], timeout_seconds=2, stable_seconds=0.05, poll_seconds=0.01
    )
    thread.join()
    assert result["name"] == "report.zip"
    assert result["bytes"] == 6


def test_protocol_accepts_resumable_desktop_loop_and_high_level_ui_ops():
    validate_action({
        "protocol": "q-agent-v4",
        "id": "desktop-loop-test",
        "target": {"mode": "agent", "agent": "worker-pc"},
        "resume_from_checkpoint": True,
        "resume_attempts": 4,
        "steps": [{
            "type": "desktop.loop",
            "session": "desktop-loop-test",
            "max_cycles": 500,
            "timeout_seconds": 3600,
            "until": {"source": 0, "path": "result.outputs.0.exists", "equals": True},
            "steps": [
                {"type": "windows.ui", "desktop": True, "window": {"title_re": ".*Explorer.*"}, "actions": [
                    {"op": "explorer_navigate", "path": "C:/Temp"},
                    {"op": "explorer_new_folder", "name": "Agent1"},
                ]},
                {"type": "download.wait", "directory": "C:/Users/test/Downloads", "patterns": ["*.zip"]},
            ],
        }],
    })


def test_protocol_accepts_file_dialog_ops():
    validate_action({
        "protocol": "q-agent-v4",
        "id": "dialog-test",
        "target": {"mode": "agent", "agent": "worker-pc"},
        "steps": [{
            "type": "windows.ui",
            "desktop": True,
            "window": {"title_re": ".*"},
            "actions": [
                {"op": "file_dialog_path", "path": "C:/Temp"},
                {"op": "file_dialog_filename", "filename": "result.txt"},
                {"op": "file_dialog_accept"},
            ],
        }],
    })


def test_resilient_bus_source_requeues_explicit_resumable_loops():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/resilient_bus.py").read_text(encoding="utf-8")
    assert 'action.get("resume_from_checkpoint") is True' in source
    assert 'step.get("type") == "desktop.loop"' in source
    assert 'queue" / "pending"' in source


def test_supervisor_source_retries_resumable_infrastructure_failures():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/process_supervisor.py").read_text(encoding="utf-8")
    assert 'resume_attempts' in source
    assert 'infrastructure_failure' in source
    assert 'self._execute_once(action' in source


def test_interactive_host_contains_dialog_and_explorer_primitives():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    for token in (
        'file_dialog_path', 'file_dialog_filename', 'file_dialog_accept',
        'explorer_navigate', 'explorer_select', 'explorer_open', 'explorer_new_folder',
        'explorer_rename', 'explorer_delete', 'explorer_copy', 'explorer_cut', 'explorer_paste',
    ):
        assert token in source


def test_executor_recurses_into_loop_for_managed_workspace_guards():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/executor.py").read_text(encoding="utf-8")
    assert 'if kind == "desktop.loop"' in source
    assert 'visit(nested, workspace)' in source


def test_recovery_handles_malformed_running_action_without_unbound_local():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/resilient_bus.py").read_text(encoding="utf-8")
    assert 'except Exception:\n                action = None' in source


def test_protocol_checks_resume_attempts_range():
    bad = {
        "protocol": "q-agent-v4", "id": "bad-resume",
        "target": {"mode": "agent", "agent": "worker-pc"},
        "resume_from_checkpoint": True, "resume_attempts": 11,
        "steps": [{"type": "desktop.loop", "session": "bad-resume", "steps": [{"type": "noop"}]}],
    }
    try:
        validate_action(bad)
    except ValueError as exc:
        assert "resume_attempts" in str(exc)
    else:
        raise AssertionError("resume_attempts=11 should be rejected")


def test_interactive_revalidates_input_desktop_per_intrusive_action():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    assert 'if op in _INTRUSIVE_UI_OPS:' in source
    assert 'input_desktop = _ensure_default_input_desktop(config, op)' in source


def test_explorer_selection_prefers_uia_select_before_mouse_click():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    select_pos = source.index('item.select()')
    click_pos = source.index('item.click_input()', select_pos)
    assert select_pos < click_pos
    assert 'if not selected:' in source[select_pos:click_pos + 100]


def test_focus_window_verifies_foreground_instead_of_silently_ignoring_failure():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    assert 'def _focus_window(target: Any, config: Config | None = None, op: str = "focus")' in source
    assert 'GetForegroundWindow' in source
    assert 'could not focus target window' in source


def test_focus_window_has_thread_input_activation_fallback():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    assert "def _force_foreground_window(handle: int) -> bool:" in source
    assert "AttachThreadInput" in source
    assert "BringWindowToTop" in source
    assert "SetForegroundWindow" in source


def test_focus_window_has_physical_fallback_only_for_dedicated_input_policy():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    assert "def _physical_focus_fallback" in source
    assert "not config.allow_physical_input" in source
    assert "_ensure_default_input_desktop(config, op)" in source
    assert "mouse.click" in source


def test_unicode_clipboard_paste_waits_before_restore():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    start = source.index('def _paste_unicode')
    end = source.index('def _wrapper', start)
    chunk = source[start:end]
    assert 'keyboard.send_keys("^v"' in chunk
    assert 'time.sleep(0.45)' in chunk
    assert chunk.index('time.sleep(0.45)') < chunk.index('_clipboard_set_text(previous)')


def test_interactive_host_run_level_is_machine_configurable_and_defaults_limited():
    source = (Path(__file__).resolve().parents[1] / "scripts/install-interactive-host.ps1").read_text(encoding="utf-8")
    assert "$configData.interactive_host.run_level" in source
    assert "$runLevel = 'Limited'" in source
    assert "$runLevel = 'Highest'" in source
    assert "-RunLevel $runLevel" in source


def test_auto_dialog_discovery_is_available_without_fixed_title():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    assert "def _discover_dialog(" in source
    assert 'if op == "discover_dialog":' in source
    assert 'kind = str(action.get("kind", "file"))' in source
    assert 'FileNameControlHost' in source
    assert 'active_handle' in source


def test_auto_dialog_discovery_retargets_following_file_dialog_ops():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    discover = source.index('if op == "discover_dialog":')
    retarget = source.index('window = _discover_dialog(desktop, window, action)', discover)
    filename = source.index('elif op == "file_dialog_path":', retarget)
    assert discover < retarget < filename


def test_native_common_dialog_does_not_require_descendant_readiness():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    block = source[source.index('def _looks_like_file_dialog'):source.index('def _win32_dialog_handles')]
    class_pos = block.index('if class_name == "#32770":')
    return_pos = block.index('return True', class_pos)
    filename_pos = block.index('FileNameControlHost', return_pos)
    assert class_pos < return_pos < filename_pos


def test_dialog_discovery_timeout_reports_visible_windows():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    assert 'last_seen: list[str] = []' in source
    assert 'UIA={detail}' in source
    assert 'Win32={native}' in source


def test_dialog_discovery_uses_win32_fallback_and_active_hwnd():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    assert "def _win32_dialog_handles()" in source
    assert "desktop.window(handle=active_handle).wrapper_object()" in source
    assert "for row in _win32_dialog_handles():" in source
    assert "Win32={native}" in source


def test_dialog_classifier_does_not_use_broad_save_open_text_hints():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    block = source[source.index('def _looks_like_file_dialog'):source.index('def _win32_dialog_handles')]
    assert 'class_name == "#32770"' in block
    assert 'FileNameControlHost' in block
    assert 'hints =' not in block


def test_file_dialog_accept_prefers_primary_automation_id_before_titles():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    block = source[source.index('elif op == "file_dialog_accept":'):source.index('elif op == "file_dialog_cancel":')]
    primary = block.index('button_automation_ids')
    automation = block.index('automation_id', primary)
    expected = block.index('expected = [title.casefold()', automation)
    assert primary < automation < expected
    assert '["1"]' in block

