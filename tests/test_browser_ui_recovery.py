from __future__ import annotations

from agent_runtime.executor import Executor


class FakePage:
    def __init__(self, url: str, title: str, closed: bool = False):
        self.url = url
        self._title = title
        self._closed = closed

    def title(self):
        return self._title

    def is_closed(self):
        return self._closed


def test_page_matching_supports_exact_and_contains():
    page = FakePage("https://analytics.google.com/analytics/web/#/home", "アナリティクス | ホーム")
    assert Executor._browser_page_matches(page, {"url_contains": "analytics.google.com", "title_contains": "ホーム"})
    assert Executor._browser_page_matches(page, {"url": page.url, "title": page.title()})
    assert not Executor._browser_page_matches(page, {"url_contains": "search.google.com"})


def test_page_summary_survives_basic_page_object():
    page = FakePage("https://example.com/", "Example")
    assert Executor._browser_page_summary(page, 2) == {
        "index": 2,
        "url": page.url,
        "title": page.title(),
        "closed": False,
    }


def test_executor_source_contains_recovery_and_observation_ops():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/executor.py").read_text(encoding="utf-8")
    for token in ("auto_recover", "switch_page_matching", "wait_for_text", "snapshot", "browser_cdp_status"):
        assert token in source


def test_wait_for_function_uses_keyword_arg_for_current_playwright_api():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/executor.py").read_text(encoding="utf-8")
    assert "arg=expected" in source
    assert 'arg=action.get("arg")' in source
    assert 'page.wait_for_function(action["expression"], action.get("arg")' not in source


def test_interactive_source_contains_observation_ops():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    for token in ("cursor_position", "input_desktop", "get_rect", 'elif op == "summary"'):
        assert token in source


def test_interactive_browser_launch_escapes_worker_job_via_interactive_host():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/executor.py").read_text(encoding="utf-8")
    start = source.index("    def _browser_interactive(")
    end = source.index("    @staticmethod", start)
    block = source[start:end]
    assert "self.interactive.request(" in block
    assert '"type": "windows.ui"' in block
    assert "subprocess.list2cmdline(args)" in block
    assert "subprocess.Popen(args" not in block
    assert '"launch_host": "interactive"' in block


def test_interactive_host_cleans_processing_spool_after_durable_response():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "src/agent_runtime/interactive_host.py").read_text(encoding="utf-8")
    start = source.index("def serve(config: Config)")
    block = source[start:]
    assert block.count("processing_path.unlink(missing_ok=True)") >= 2


def test_protocol_accepts_recovery_operations():
    from agent_runtime.protocol import validate_action
    validate_action({
        "protocol": "q-agent-v4",
        "id": "recovery-ops-test",
        "target": {"mode": "agent", "agent": "worker-pc"},
        "steps": [
            {"type": "browser.interactive", "op": "ensure"},
            {
                "type": "browser.playwright",
                "connection": "cdp",
                "actions": [
                    {"op": "wait_for_text", "text": "Ready"},
                    {"op": "wait_for_function", "expression": "() => true"},
                    {"op": "switch_page_matching", "match": {"url_contains": "example.com"}},
                    {"op": "snapshot"},
                ],
            },
        ],
    })
