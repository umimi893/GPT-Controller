# Security

GPT Controller is intentionally powerful software. Treat enrollment as granting a remote execution channel to the Windows machine.

## Control repository

The control repository **must remain private**. Anyone with write access to its control branch can submit Actions that the enrolled runtime may execute according to local policy.

Use GitHub 2FA/passkeys and restrict repository access. If you suspect control-repository compromise, stop the GPT Controller Scheduled Task, revoke repository access/tokens, rotate affected credentials and inspect recent Actions/Results before reenrolling.

## Local permissions

The default configuration is non-interfering:

- physical mouse/keyboard injection: disabled
- foreground activation: disabled
- visible GUI launch: disabled
- absolute paths: disabled
- browser automation: headless by default

These capabilities can only be enabled in local machine configuration; Actions cannot escalate them.

## Secrets

Do not store API keys, passwords, private keys, cookies, deployment credentials, or production `.env` files in this public repository. Keep machine secrets local and reference them from locally configured scripts/profiles when necessary.

## Reporting

If you find a vulnerability in the public runtime, open a GitHub security advisory or contact the repository owner privately rather than publishing exploit details in a public issue.
