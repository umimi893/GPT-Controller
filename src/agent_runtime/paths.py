from __future__ import annotations

from pathlib import Path

from .config import Config


def workspace_for(config: Config, name: str | None) -> Path:
    if not name:
        raise ValueError("workspace is required")
    try:
        return config.workspaces[name]
    except KeyError:
        raise ValueError(f"unknown workspace: {name}")


def resolve_path(config: Config, workspace: str | None, value: str) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        if not config.allow_absolute_paths:
            raise ValueError("absolute paths are disabled")
        return p.resolve()
    root = workspace_for(config, workspace)
    target = (root / p).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace")
    return target
