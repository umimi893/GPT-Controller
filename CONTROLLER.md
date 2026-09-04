# GPT Controller Controller Contract

> **ChatGPT sessions:** start with [`CHATGPT_CONTROLLER.md`](CHATGPT_CONTROLLER.md). GPT Controller is reached through the GitHub connector and the `gpt-controller-control` branch; the absence of a direct `Agent1` tool or a mounted `C:\GPT-Controller` path is **not** evidence that GPT Controller is unavailable.

The Controller is the brain. GPT Controller is only an execution substrate.

## Controller responsibilities

For every Action, the Controller should:

1. choose a unique immutable Action ID;
2. choose target routing explicitly;
3. specify `requires` capabilities when relevant;
4. specify a logical workspace rather than a machine path where possible;
5. synchronize a Git-backed workspace explicitly when fresh source is required;
6. issue only the required execution steps;
7. inspect the returned Result before deciding the next Action;
8. create a new Action ID for retries or recovery.

## Recommended two-PC routing

For a Desktop-first setup with Notebook fallback:

```json
{
  "created_at": "<UTC now>",
  "target": {
    "mode": "ordered",
    "order": ["desktop-pc", "worker-pc"],
    "fallback_after_seconds": 8
  }
}
```

Use exact targeting when a task physically depends on one machine, such as a machine-local file, device, GPU, or logged-in application.

## Workspace synchronization

Do not assume two PCs have identical working trees merely because they share a logical workspace name.

When the remote Git repository is authoritative and a clean fast-forward update is appropriate, send `workspace.git_sync` before the work. If it fails because the target workspace is dirty or diverged, inspect that state and decide the recovery yourself. Never ask GPT Controller to guess which changes to keep.

## Per-machine interaction contract

Interaction safety is configured locally on each Agent. The default remains non-interfering.

Before issuing UI work, prefer exact agent targeting and use `agent.info` when the current policy is not already known. Never route a step that depends on physical input or foreground activation through `target.mode="any"` or ordered fallback.

When `non_interference=true`, do not generate Actions that move the physical mouse, inject OS keyboard input, or explicitly steal foreground focus. Prefer headless `browser.playwright` and accessibility/UIA operations.

A dedicated worker machine may opt out with `non_interference=false` and separately enable `allow_physical_input`, `allow_foreground_activation`, and `allow_visible_gui_launch`. On such a machine, `windows.ui` may launch a visible app and use `click`, `click_input`, `type_keys`, `set_focus`, and `move_mouse` only when the corresponding local permissions are enabled.

The intended two-PC setup is a protected Desktop and an opt-in dedicated Notebook. Controller logic must respect the policy reported by the exact target rather than assuming both machines have the same UI permissions.

## Browser strategy

Prefer semantic locators and stable selectors. Use explicit waits around navigation or dynamic content. File uploads/downloads should use workspace-relative paths. Use `evaluate` only when ordinary Playwright operations cannot express the required page interaction.

## Result publication and read-after-write

A freshly pushed Result may briefly return HTTP 404 from a connector or read replica. The first 404 is not evidence that the Result was lost.

After a durable Claim:

1. retry the Result read with short bounded backoff;
2. if the Result path is still temporarily invisible, inspect the `gpt-controller result <action_id> ...` commit on `gpt-controller-control`;
3. never replay the Action solely because an immediate Result read returned 404.

GPT Controller already publishes the Result and `queue/done` in one durable result commit and retries failed pushes without re-executing the Action.

## Long or quoting-sensitive operations

Do not embed large PowerShell programs in `powershell.exec`. Put the durable procedure in the target repository and execute it from the logical workspace with `process.exec`, for example:

```json
{
  "workspace": "example-project",
  "steps": [
    {
      "type": "process.exec",
      "program": "pwsh.exe",
      "args": ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "scripts/verify-and-deploy-headless.ps1"]
    }
  ]
}
```

Use `powershell.exec` for short diagnostics and small glue operations only. Repository-owned scripts are easier to review, test, rerun, and quote correctly.

## Ambiguous results

An `ambiguous` result means a claim was durable but the prior runtime died before a durable result. Do not replay the same Action ID. Inspect the target state, then create a new recovery Action if appropriate.

## Principle

The Controller may be sophisticated. The Agent must remain boring.
