# ChatGPT -> GPT Controller canonical entrypoint

GPT Controller is not a direct ChatGPT tool and it does not use an OpenAI API key. ChatGPT reaches it through the user's connected **private control repository**.

## Discovery

Ask the user for the private control repository name if it is not already known. The default installer creates:

```text
<github-user>/gpt-controller-control
```

The source repository `umimi893/GPT-Controller` is public code only. **Do not write Actions to the public source repository.**

Canonical route:

```text
ChatGPT
  -> GitHub connector
  -> <user>/gpt-controller-control (PRIVATE)
  -> gpt-controller-control branch
  -> queue/pending/<action-id>.json
  -> GPT Controller Runtime
  -> results/<action-id>.json
```

Read the control repository README before issuing work.

## Controller flow

1. Use a unique immutable Action ID.
2. Select target routing explicitly.
3. Specify capabilities with `requires` where useful.
4. Prefer logical workspace names over machine paths.
5. Create the Action at `queue/pending/<action-id>.json` on branch `gpt-controller-control`.
6. Wait for a durable claim.
7. Read `results/<action-id>.json`.
8. Inspect the Result before deciding the next Action.
9. Never reuse an Action ID for retries.

A fresh Result can briefly be invisible after a Git push. Retry the read with bounded backoff; do not replay an Action solely because of an immediate 404.

## Minimal Action

```json
{
  "protocol": "q-agent-v4",
  "id": "example-diagnostic-001",
  "target": {"mode": "any"},
  "requires": ["general"],
  "timeout_seconds": 120,
  "steps": [
    {"type": "powershell.exec", "script": "Get-Date | ConvertTo-Json -Compress", "timeout_seconds": 60}
  ]
}
```

## Core rule

**ChatGPT is the brain. GPT Controller is the execution substrate.**

The runtime contains no LLM, planner or semantic decision engine. Keep decisions in the Controller and send explicit deterministic Actions.
