# GPT Controller Protocol Specification

## 1. Roles

- **Controller (ChatGPT):** chooses goals, steps, target routing, retries, synchronization strategy, and recovery.
- **Git:** transport, arbitration point, and durable audit log.
- **Runtime Supervisor:** validates, filters, claims, supervises isolated Action workers, and reports. It never invents a step.
- **Action Worker:** executes exactly one already-claimed Action inside a bounded process lifetime.
- **Interactive Host:** local user-session executor for the restricted `windows.ui` surface only.
- **External Watchdog:** Windows SYSTEM task that detects both dead runtimes and live-but-stuck steps and restarts the Runtime Supervisor.

## 2. Per-agent interaction policy

Non-interferinge is the default, not a fleet-wide hard-coded invariant.

Each machine has a local `interaction_policy`:

- `non_interference=true` is the default and MUST reject physical input and explicit foreground activation;
- `allow_physical_input=true` may be used only when `non_interference=false`;
- `allow_foreground_activation=true` may be used only when `non_interference=false`;
- `allow_visible_gui_launch=true` permits explicit visible application/browser launch;
- `browser.headless_only=false` may be used only when `non_interference=false`.

The Controller MUST exact-target UI work that depends on intrusive permissions. It MUST NOT rely on ordered/any routing for a step that requires physical input or foreground activation because local policies may differ across Agents.

On a non-interfering Agent, Windows UI automation remains limited to accessibility/UIA control patterns that do not require physical input or explicit focus stealing. On an opted-in dedicated Agent, the Interactive Host MAY execute policy-gated physical/foreground operations such as `click_input`, `type_keys`, `set_focus`, and `move_mouse`.

Policy remains local-only: an Action cannot elevate these permissions.

## 3. Action envelope

Required:

```json
{
  "protocol": "q-agent-v4",
  "id": "unique-action-id",
  "steps": [{"type": "noop"}]
}
```

Optional fields:

- `created_at`: ISO-8601 UTC timestamp; required for ordered fallback routing
- `expires_at`: ISO-8601 UTC timestamp
- `target_agent`: legacy single-agent/`*` target
- `target`: structured routing rule
- `requires`: capability names
- `workspace`: logical workspace name
- `timeout_seconds`: default step timeout

## 4. Routing

### Any

```json
"target": {"mode": "any"}
```

Every capability-eligible Agent may attempt to claim. Git claim arbitration determines the executor.

### Exact agent

```json
"target": {"mode": "agent", "agent": "desktop-pc"}
```

Only that Agent is eligible.

### Ordered fallback

```json
"created_at": "2026-08-30T00:00:00Z",
"target": {
  "mode": "ordered",
  "order": ["desktop-pc", "worker-pc"],
  "fallback_after_seconds": 8
}
```

Rank 0 is eligible immediately. Rank N becomes eligible after `N * fallback_after_seconds` from `created_at`. This gives the preferred machine a deterministic first claim window without requiring an Agent planner or fleet broker.

`requires` is a set-subset check against the local configured capability set.

## 5. Claim-before-side-effect rule

An Agent MUST NOT execute any Action side effect until it has atomically moved the Action from pending to its running path, committed the claim, and successfully pushed that claim to the control branch.

If claim push fails, the Agent MUST resynchronize and MUST NOT execute.

## 6. Supervised execution and timeout containment

A durable claim MUST be executed by a child Action Worker rather than inline in the long-lived Runtime Supervisor.

The Runtime Supervisor MUST:

1. create an isolated Worker for exactly one claimed Action;
2. on Windows, assign that Worker to a Job Object configured with `KILL_ON_JOB_CLOSE` before releasing the Worker start gate;
3. receive progress at step boundaries including step index, step type, start time, and configured timeout;
4. enforce a supervisor deadline in addition to each step's internal timeout;
5. terminate the Worker process tree when a deadline is exceeded;
6. turn timeout or supervisor infrastructure failure into a durable failed Result instead of leaving the Action permanently running;
7. close the Worker lifetime after the Action, terminating descendants intentionally left in the background by an Action step.

The Runtime Supervisor itself MUST remain available to process subsequent Actions after a Worker failure or timeout.

The Windows external watchdog MUST NOT treat a fresh heartbeat alone as proof of health. If the heartbeat reports an executing step whose start time exceeds its declared timeout plus watchdog grace, the watchdog MUST terminate the Runtime process tree and restart the scheduled Runtime task. This is a second containment layer for failures in the Runtime Supervisor itself.

## 7. Completion and crash recovery

After execution, the Agent writes a Result, moves the running Action to done, commits, and pushes.

If a durable claim exists after process restart but no durable result exists, the owning Agent marks the Action ambiguous and MUST NOT replay it.

A Result is authoritative over a stale running ledger entry. If `results/<action-id>.json` already exists, a matching stale `queue/running/...` entry MUST be reconciled to done without re-executing the Action.

The control checkout is dedicated runtime transport. On startup the Runtime MAY repair interrupted Git operations and discard uncommitted crash residue only under runtime-owned paths (`queue/`, `claims/`, `results/`, `rejections/`). It MUST refuse automatic reset if any unexpected path is locally modified. This permission does not extend to user/source workspaces.

## 8. Workspaces

Logical workspace names resolve through machine-local config. Different machines may map the same name to different paths.

`workspace.git_sync` is an explicit convenience operator:

1. fail on local changes unless `allow_dirty=true` was explicitly requested;
2. `git fetch <remote>`;
3. determine current branch;
4. `git pull --ff-only <remote> <branch>`.

It MUST NOT merge conflicts, reset, clean, stash, or discard work automatically.

The crash-repair rules for the dedicated control checkout do **not** apply to these workspaces.

## 9. Browser

`browser.playwright` MUST launch headless when `browser.headless_only=true`. A dedicated Agent with `interaction_policy.non_interference=false` MAY configure `browser.headless_only=false`. A dedicated persistent profile MAY be configured.

Supported operation families include navigation, locator interaction, inspection, tabs/popups, uploads/downloads, cookies/storage state, screenshots/PDF, iframe locators, and page JavaScript evaluation.

Browser-generated DOM input events do not count as physical input because they are confined to the isolated headless browser context.

File-bearing browser operations MUST obey workspace path restrictions.

Browser/preview/Node descendants created during an Action belong to that Action Worker lifetime. They MUST NOT survive Worker teardown unless a future explicit persistent-service primitive defines separate semantics.

## 10. Windows UI

`windows.ui` is sent over a local filesystem spool from the background Runtime to the user-session Interactive Host.

The Interactive Host MUST accept only `windows.ui` requests. It MUST enforce the local interaction policy on every intrusive operation.

Non-interfering operations include UIA-pattern-oriented operations such as Invoke, Value, Selection, Toggle, Expand/Collapse, text read, readiness wait, and close.

When `allow_visible_gui_launch=true`, a step may provide `start` instead of `connect`. When `allow_physical_input=true`, policy-gated operations may include `click`, `click_input`, `type_keys`, and `move_mouse`. `set_focus` additionally requires `allow_foreground_activation=true`.

An Action cannot override the local policy.

## 11. Result

```json
{
  "protocol": "q-agent-v4-result",
  "action_id": "unique-action-id",
  "agent_id": "desktop-pc",
  "started_at": "...",
  "finished_at": "...",
  "status": "succeeded|failed|ambiguous",
  "error": null,
  "steps": []
}
```

Supervisor metadata MAY be added to a Result for diagnostics, including whether execution used an isolated Worker, the Worker exit code, and whether the supervisor terminated a timed-out step.

## 12. Update and health invariant

A successful Agent update MUST leave the Runtime task not only registered and `Running`, but demonstrably alive through a fresh heartbeat whose PID exists.

The updater MUST explicitly terminate the old Runtime process tree before replacing runtime code. A scheduler state transition alone is not sufficient proof that an old process exited.

## 13. No autonomy

The Runtime may perform only protocol mechanics: schema validation, target/capability eligibility, Git synchronization of the control queue, durable claim arbitration, execution of explicit typed steps, timeout/process containment, bounded result publication retries, and crash-state reconciliation.

It MUST NOT infer user intent, generate new work, repair failed commands semantically, choose a different deployment strategy, or resolve user workspace conflicts on its own.
