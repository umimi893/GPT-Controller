param(
    [Parameter(Mandatory=$true)][string]$Path,
    [Parameter(Mandatory=$true)][string]$RemoteUrl,
    [string]$Branch = 'gpt-controller-control'
)
$ErrorActionPreference = 'Stop'
function Invoke-Git { param([Parameter(Mandatory=$true)][string[]]$Arguments); & git @Arguments; if ($LASTEXITCODE -ne 0) { throw "git failed ($LASTEXITCODE): git $($Arguments -join ' ')" } }
New-Item -ItemType Directory -Force -Path $Path | Out-Null
Push-Location $Path
try {
    if (-not (Test-Path '.git')) { Invoke-Git -Arguments @('init') }
    Invoke-Git -Arguments @('config','user.name','GPT Controller Runtime')
    Invoke-Git -Arguments @('config','user.email','gpt-controller@localhost')
    foreach ($d in @('queue/pending','queue/running','queue/done','queue/ambiguous','queue/rejected','claims','results','rejections')) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        New-Item -ItemType File -Force -Path (Join-Path $d '.gitkeep') | Out-Null
    }
    @'
# GPT Controller private control bus

**Keep this repository private. Write access is effectively remote-command access to enrolled PCs.**

ChatGPT controller route:

```text
ChatGPT -> GitHub connector -> this PRIVATE repository -> gpt-controller-control branch
        -> queue/pending/<action-id>.json -> GPT Controller Runtime
        -> results/<action-id>.json -> ChatGPT
```

Wire protocol: `q-agent-v4`.

Minimal Action:

```json
{
  "protocol": "q-agent-v4",
  "id": "diagnostic-001",
  "target": {"mode": "any"},
  "requires": ["general"],
  "timeout_seconds": 120,
  "steps": [
    {"type": "powershell.exec", "script": "Get-Date | ConvertTo-Json -Compress", "timeout_seconds": 60}
  ]
}
```

Controller rules:

1. Never reuse an Action ID.
2. Inspect each Result before the next Action.
3. Never replay an Action merely because a fresh Result is briefly invisible.
4. Prefer logical workspace names.
5. Respect local interaction permissions; an Action cannot elevate them.
'@ | Set-Content -Encoding utf8 README.md
    Invoke-Git -Arguments @('add','.')
    & git rev-parse --verify HEAD *> $null
    if ($LASTEXITCODE -ne 0) { Invoke-Git -Arguments @('commit','-m','Initialize GPT Controller private control bus') }
    Invoke-Git -Arguments @('branch','-M',$Branch)
    $remotes = @(& git remote)
    if ($remotes -contains 'origin') { Invoke-Git -Arguments @('remote','set-url','origin',$RemoteUrl) } else { Invoke-Git -Arguments @('remote','add','origin',$RemoteUrl) }
    Invoke-Git -Arguments @('push','-u','origin',$Branch)
} finally { Pop-Location }
