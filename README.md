# GPT Controller

**GPT Controller is a public Windows execution runtime that lets ChatGPT act as the controller of your own PC through a private GitHub command bus.**

It is based on the proven Q-Agent V4 architecture, but this repository contains only the reusable runtime and public setup flow. It does **not** contain any private control history, machine configuration, credentials, deployment profiles, or personal project settings.

## What it is

GPT Controller deliberately contains no LLM and does not call the OpenAI API. **No OpenAI API key is required.**

```text
ChatGPT
   |
   | GitHub connector
   v
YOUR PRIVATE gpt-controller-control repository
   |
   | Action JSON
   v
GPT Controller Runtime on Windows
   |
   +-- PowerShell / processes / files / Git
   +-- Playwright browser automation
   +-- optional Windows UI Automation
   |
   v
Result JSON -> private control repository -> ChatGPT
```

The design rule is simple:

> **ChatGPT decides. GPT Controller executes.**

The runtime is deterministic. It routes, claims, executes and reports explicit Actions. It does not invent tasks, plan work, or autonomously retry ambiguous side effects.

## One-click Windows install

### Requirements

- Windows 10 or Windows 11
- `winget` (normally provided by Microsoft App Installer)
- a GitHub account
- ChatGPT with the GitHub connector available

Download this repository as a ZIP or clone it, then double-click:

```text
Install GPT Controller.bat
```

The installer will:

1. request Administrator permission;
2. install missing Git, Python 3.12, PowerShell 7 and GitHub CLI with `winget`;
3. ask you to sign in to GitHub if needed;
4. clone the canonical GPT Controller source to `C:\GPT-Controller\source`;
5. create a **private** repository named `gpt-controller-control` in your GitHub account if it does not already exist;
6. initialize the `gpt-controller-control` command/result branch;
7. install the runtime in `C:\GPT-Controller`;
8. register the Windows Scheduled Task;
9. run a one-shot validation.

The installer will never create a public control repository. A public control repository would effectively expose remote-command capability and is refused.

## Connect ChatGPT

After installation, open the private repository shown by the installer (normally `YOUR_USERNAME/gpt-controller-control`). Make sure your ChatGPT GitHub connector has permission to access that private repository.

Then you can tell ChatGPT something like:

```text
Use my GPT Controller. The control repository is YOUR_USERNAME/gpt-controller-control.
Read its README first and use the gpt-controller-control branch.
```

The private control repository contains the canonical controller entrypoint and queue layout.

## Update / repair

Run:

```text
Update GPT Controller.bat
```

The updater safely stops the runtime, fast-forwards the public source checkout, repairs dependencies, runs tests, reinstalls scheduled tasks, restarts the runtime and verifies a fresh live heartbeat. If the update fails, the updater attempts to roll the source back to the previous commit.

## Security model

**Write access to your private control repository is effectively remote shell access to enrolled PCs.**

- Keep the control repository private.
- Use strong GitHub account security and 2FA/passkeys.
- Give the ChatGPT GitHub connector access only to repositories it needs.
- Never commit secrets or production credentials into this public source repository.
- Keep `allow_absolute_paths=false` unless you understand the consequences.
- Physical mouse/keyboard input, foreground activation and visible GUI launch are disabled by default and can only be enabled in local machine configuration.

See [SECURITY.md](SECURITY.md) and [INTERACTION_POLICY.md](INTERACTION_POLICY.md).

## Supported Action types

- `noop`
- `agent.info`
- `powershell.exec`
- `process.exec`
- `file.read/write/append/mkdir/delete/copy/move`
- `git.exec`
- `workspace.git_sync`
- `browser.playwright`
- `browser.interactive`
- `windows.ui`
- `download.wait`
- `desktop.checkpoint`
- `desktop.loop`
- `deploy.exec`

The wire protocol remains `q-agent-v4` for compatibility with the proven runtime implementation. The public product name, installation, task names and control branch are GPT Controller names.

## Reliability model

GPT Controller retains the Q-Agent V4 reliability architecture:

- claim is pushed before side effects;
- duplicate Action IDs are not replayed;
- each claimed Action runs in an isolated worker process;
- Windows Job Objects terminate descendant process trees when a worker ends;
- a separate watchdog supervises the long-lived runtime;
- claimed-but-unresolved work becomes ambiguous instead of being blindly replayed;
- arbitrary workspaces are never automatically reset, stashed or cleaned.

## Manual / advanced setup

The main scripts are under `scripts/`:

- `bootstrap-windows.ps1` — one-click prerequisite/bootstrap entrypoint
- `bootstrap-install.ps1` — GitHub control repo + runtime enrollment
- `install.ps1` — Python/runtime dependency installation
- `install-service.ps1` — background Scheduled Task
- `install-interactive-host.ps1` — optional logged-in user UI host
- `update.ps1` — safe updater/repair path

Read [CHATGPT_CONTROLLER.md](CHATGPT_CONTROLLER.md), [CONTROLLER.md](CONTROLLER.md), and [SPEC.md](SPEC.md) for protocol details.

## License

MIT. See [LICENSE](LICENSE).
