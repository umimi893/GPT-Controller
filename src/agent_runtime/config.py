from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .util import load_json


@dataclass(frozen=True)
class Config:
    repo_path: Path
    remote: str
    branch: str
    poll_seconds: int
    agent_id: str
    capabilities: frozenset[str]
    workspaces: dict[str, Path]
    managed_git_workspaces: frozenset[str]
    managed_git_expected_branches: dict[str, str]
    allow_absolute_paths: bool
    powershell: str
    git: str
    default_timeout_seconds: int
    max_output_bytes: int
    browser_user_data_dir: Path | None
    browser_channel: str | None
    browser_headless_only: bool
    browser_cdp_endpoint: str | None
    browser_interactive_user_data_dir: Path | None
    browser_interactive_executable: str | None
    non_interference: bool
    allow_physical_input: bool
    allow_foreground_activation: bool
    allow_visible_gui_launch: bool
    interactive_spool: Path
    interactive_timeout_seconds: int
    deploy_profiles: dict[str, dict[str, Any]]

    @staticmethod
    def load(path: Path) -> "Config":
        raw = load_json(path)
        browser = raw.get("browser", {})
        interaction = raw.get("interaction_policy", {})
        interactive = raw.get("interactive_host", {})
        non_interference = bool(interaction.get("non_interference", True))
        allow_physical_input = bool(interaction.get("allow_physical_input", False))
        allow_foreground_activation = bool(interaction.get("allow_foreground_activation", False))
        allow_visible_gui_launch = bool(interaction.get("allow_visible_gui_launch", False))
        browser_headless_only = bool(browser.get("headless_only", True))

        # Non-interference remains the default and is enforced per machine. A dedicated
        # worker PC may explicitly opt out and then selectively enable intrusive UI
        # primitives in its own local config. This keeps shared/controller actions
        # deterministic while allowing a sacrificial/dedicated laptop to be fully driven.
        if non_interference and (allow_physical_input or allow_foreground_activation):
            raise ValueError(
                "interaction_policy.non_interference=true is incompatible with physical input or foreground activation"
            )
        if non_interference and not browser_headless_only:
            raise ValueError(
                "browser.headless_only=false requires interaction_policy.non_interference=false"
            )

        cdp_endpoint = browser.get("cdp_endpoint")
        if cdp_endpoint is not None:
            cdp_endpoint = str(cdp_endpoint)
            parsed_cdp = urlparse(cdp_endpoint)
            if parsed_cdp.scheme != "http" or parsed_cdp.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("browser.cdp_endpoint must be a loopback http URL")

        workspaces = {k: Path(v).expanduser().resolve() for k, v in raw.get("workspaces", {}).items()}
        managed_raw = raw.get("managed_git_workspaces")
        if managed_raw is None:
            # Backward-compatible default for existing GPT Controller installs.
            managed_git_workspaces = frozenset({"example-project"} if "example-project" in workspaces else set())
        else:
            if not isinstance(managed_raw, list) or not all(isinstance(x, str) and x for x in managed_raw):
                raise ValueError("managed_git_workspaces must be a string array")
            unknown = sorted(set(managed_raw) - set(workspaces))
            if unknown:
                raise ValueError(f"managed_git_workspaces contains unknown workspace(s): {', '.join(unknown)}")
            managed_git_workspaces = frozenset(managed_raw)

        expected_raw = raw.get("managed_git_expected_branches")
        if expected_raw is None:
            managed_git_expected_branches = {"example-project": "main"} if "example-project" in managed_git_workspaces else {}
        else:
            if not isinstance(expected_raw, dict):
                raise ValueError("managed_git_expected_branches must be an object")
            if not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in expected_raw.items()):
                raise ValueError("managed_git_expected_branches must map workspace names to non-empty branch names")
            unknown_expected = sorted(set(expected_raw) - set(managed_git_workspaces))
            if unknown_expected:
                raise ValueError(
                    "managed_git_expected_branches contains unmanaged workspace(s): "
                    + ", ".join(unknown_expected)
                )
            managed_git_expected_branches = dict(expected_raw)

        return Config(
            repo_path=Path(raw["control_repo"]).expanduser().resolve(),
            remote=raw.get("remote", "origin"),
            branch=raw.get("branch", "gpt-controller-control"),
            poll_seconds=int(raw.get("poll_seconds", 3)),
            agent_id=str(raw["agent_id"]),
            capabilities=frozenset(str(x) for x in raw.get("capabilities", ["general", "git", "browser", "windows-ui"])),
            workspaces=workspaces,
            managed_git_workspaces=managed_git_workspaces,
            managed_git_expected_branches=managed_git_expected_branches,
            allow_absolute_paths=bool(raw.get("allow_absolute_paths", False)),
            powershell=raw.get("powershell", "pwsh"),
            git=raw.get("git", "git"),
            default_timeout_seconds=int(raw.get("default_timeout_seconds", 300)),
            max_output_bytes=int(raw.get("max_output_bytes", 2_000_000)),
            browser_user_data_dir=(Path(browser["user_data_dir"]).expanduser().resolve() if browser.get("user_data_dir") else None),
            browser_channel=browser.get("channel"),
            browser_headless_only=browser_headless_only,
            browser_cdp_endpoint=cdp_endpoint,
            browser_interactive_user_data_dir=(
                Path(browser["interactive_user_data_dir"]).expanduser().resolve()
                if browser.get("interactive_user_data_dir")
                else None
            ),
            browser_interactive_executable=(
                str(browser["interactive_executable"]) if browser.get("interactive_executable") else None
            ),
            non_interference=non_interference,
            allow_physical_input=allow_physical_input,
            allow_foreground_activation=allow_foreground_activation,
            allow_visible_gui_launch=allow_visible_gui_launch,
            interactive_spool=Path(interactive.get("spool", "C:/ProgramData/GPT-Controller/interactive")).expanduser().resolve(),
            interactive_timeout_seconds=int(interactive.get("timeout_seconds", 120)),
            deploy_profiles=dict(raw.get("deploy_profiles", {})),
        )
