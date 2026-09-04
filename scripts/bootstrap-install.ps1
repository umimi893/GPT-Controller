param(
    [string]$InstallRoot = 'C:\GPT-Controller',
    [string]$Python = 'python',
    [string]$AgentId = '',
    [string]$ControlRepoName = 'gpt-controller-control',
    [switch]$InstallInteractiveHost,
    [switch]$SystemRuntime
)
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Test-IsAdministrator {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Invoke-Native {
    param([Parameter(Mandatory=$true)][string]$FilePath,[Parameter(Mandatory=$true)][string[]]$Arguments,[switch]$Capture,[switch]$AllowFailure)
    $old = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($Capture) { $output = & $FilePath @Arguments 2>$null } else { & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host $_ }; $output = $null }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
    if (-not $AllowFailure -and $code -ne 0) { throw "Command failed ($code): $FilePath $($Arguments -join ' ')" }
    [pscustomobject]@{ ExitCode=$code; Output=$output }
}

if (-not (Test-IsAdministrator)) { throw 'GPT Controller bootstrap must run as Administrator. Use Install GPT Controller.bat.' }
if ([string]::IsNullOrWhiteSpace($AgentId)) { $AgentId = $env:COMPUTERNAME.ToLowerInvariant() }
$gh = (Get-Command gh.exe -ErrorAction Stop).Source
$git = (Get-Command git.exe -ErrorAction Stop).Source

$auth = Invoke-Native -FilePath $gh -Arguments @('auth','status') -Capture -AllowFailure
if ($auth.ExitCode -ne 0) {
    Write-Host 'GitHub sign-in is required. A browser window may open.' -ForegroundColor Yellow
    Invoke-Native -FilePath $gh -Arguments @('auth','login','--hostname','github.com','--git-protocol','https','--web') | Out-Null
}
$login = ((Invoke-Native -FilePath $gh -Arguments @('api','user','--jq','.login') -Capture).Output | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($login)) { throw 'Could not determine the authenticated GitHub username.' }
$controlFullName = "$login/$ControlRepoName"
$view = Invoke-Native -FilePath $gh -Arguments @('repo','view',$controlFullName,'--json','visibility','--jq','.visibility') -Capture -AllowFailure
if ($view.ExitCode -ne 0) {
    Write-Host "Creating PRIVATE control repository: $controlFullName" -ForegroundColor Cyan
    Invoke-Native -FilePath $gh -Arguments @('repo','create',$controlFullName,'--private','--description','Private command/result bus for GPT Controller') | Out-Null
} else {
    $visibility = (($view.Output | Out-String).Trim()).ToUpperInvariant()
    if ($visibility -ne 'PRIVATE') { throw "Refusing to use $controlFullName because it is $visibility. The GPT Controller control repository must be PRIVATE." }
}

& (Join-Path $PSScriptRoot 'install.ps1') -InstallRoot $InstallRoot -Python $Python -AgentId $AgentId

$ControlPath = Join-Path $InstallRoot 'control'
$ConfigPath = Join-Path $InstallRoot 'agent.config.json'
$Branch = 'gpt-controller-control'
$RemoteUrl = "https://github.com/$controlFullName.git"
$probe = Invoke-Native -FilePath $git -Arguments @('ls-remote','--heads',$RemoteUrl,$Branch) -Capture -AllowFailure
$branchExists = $probe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace((($probe.Output | Out-String).Trim()))

if ($branchExists) {
    if (Test-Path (Join-Path $ControlPath '.git')) {
        Invoke-Native -FilePath $git -Arguments @('-C',$ControlPath,'remote','set-url','origin',$RemoteUrl) | Out-Null
        Invoke-Native -FilePath $git -Arguments @('-C',$ControlPath,'fetch','origin',$Branch) | Out-Null
        Invoke-Native -FilePath $git -Arguments @('-C',$ControlPath,'checkout',$Branch) | Out-Null
        Invoke-Native -FilePath $git -Arguments @('-C',$ControlPath,'pull','--ff-only','origin',$Branch) | Out-Null
    } else {
        if (Test-Path $ControlPath) { Remove-Item -LiteralPath $ControlPath -Recurse -Force }
        Invoke-Native -FilePath $git -Arguments @('clone','--branch',$Branch,'--single-branch',$RemoteUrl,$ControlPath) | Out-Null
    }
} else {
    if (Test-Path $ControlPath) { Remove-Item -LiteralPath $ControlPath -Recurse -Force }
    & (Join-Path $PSScriptRoot 'init-control-repo.ps1') -Path $ControlPath -RemoteUrl $RemoteUrl -Branch $Branch
}

$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$config.agent_id = $AgentId
$config.control_repo = ($ControlPath -replace '\\','/')
$config.branch = $Branch
$config.remote = 'origin'
$config.capabilities = @('general','git','browser') + $(if ($InstallInteractiveHost) { @('windows-ui') } else { @() })
$config.interaction_policy.non_interference = $true
$config.interaction_policy.allow_physical_input = $false
$config.interaction_policy.allow_foreground_activation = $false
$config.interaction_policy.allow_visible_gui_launch = $false
$config.interactive_host.spool = 'C:/ProgramData/GPT-Controller/interactive'
$config.workspaces = [pscustomobject]@{ scratch = ((Join-Path $InstallRoot 'scratch') -replace '\\','/') }
$config.managed_git_workspaces = @()
$config.managed_git_expected_branches = [pscustomobject]@{}
$config.browser.user_data_dir = ((Join-Path $InstallRoot 'browser-profile') -replace '\\','/')
$config.browser.interactive_user_data_dir = ((Join-Path $InstallRoot 'browser-interactive-profile') -replace '\\','/')
$config.browser.headless_only = $true
$config.deploy_profiles = [pscustomobject]@{}
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot 'scratch'),(Join-Path $InstallRoot 'browser-profile') | Out-Null
$config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8

$agentExe = Join-Path $InstallRoot 'venv\Scripts\gpt-controller.exe'
& $agentExe --config $ConfigPath --once
if ($LASTEXITCODE -ne 0) { throw 'GPT Controller one-shot validation failed.' }
if ($SystemRuntime) { & (Join-Path $PSScriptRoot 'install-service.ps1') -InstallRoot $InstallRoot -System } else { & (Join-Path $PSScriptRoot 'install-service.ps1') -InstallRoot $InstallRoot }
if ($InstallInteractiveHost) { & (Join-Path $PSScriptRoot 'install-interactive-host.ps1') -InstallRoot $InstallRoot }

$setup = @"
GPT Controller installation complete.

Private control repository:
https://github.com/$controlFullName

Next step:
1. In ChatGPT, connect GitHub and grant access to the private repository above.
2. Tell ChatGPT: Use my GPT Controller. The control repository is $controlFullName. Read its README and use branch $Branch.

No OpenAI API key is required.
"@
$setupPath = Join-Path $InstallRoot 'CHATGPT_SETUP.txt'
$setup | Set-Content -LiteralPath $setupPath -Encoding UTF8
Write-Host ''
Write-Host 'GPT Controller bootstrap complete.' -ForegroundColor Green
Write-Host "Agent ID:     $AgentId"
Write-Host "Control repo: https://github.com/$controlFullName"
Write-Host "Config:       $ConfigPath"
Write-Host "Setup notes:  $setupPath"
