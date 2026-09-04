param(
    [string]$InstallRoot = 'C:\GPT-Controller',
    [string]$Python = 'python',
    [string]$AgentId = ''
)
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Native {
    param([Parameter(Mandatory=$true)][string]$FilePath,[Parameter(Mandatory=$true)][string[]]$Arguments,[switch]$Capture)
    $old = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($Capture) { $output = & $FilePath @Arguments 2>$null } else { & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host $_ }; $output = $null }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "Native command failed ($code): $FilePath $($Arguments -join ' ')" }
    return $output
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\ProgramData\GPT-Controller\interactive\requests','C:\ProgramData\GPT-Controller\interactive\processing','C:\ProgramData\GPT-Controller\interactive\responses' | Out-Null

$versionText = (Invoke-Native -FilePath $Python -Arguments @('-c',"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')") -Capture | Out-String).Trim()
$parts = $versionText.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) { throw "GPT Controller requires Python 3.10 or newer. Found $versionText" }
Write-Host "Using Python $versionText"

$venvPython = "$InstallRoot\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Invoke-Native -FilePath $Python -Arguments @('-m','venv',"$InstallRoot\venv") | Out-Null }
Invoke-Native -FilePath $venvPython -Arguments @('-m','pip','install','--upgrade','pip') | Out-Null
Invoke-Native -FilePath $venvPython -Arguments @('-m','pip','install','-e',"${RepoRoot}[all]") | Out-Null
Invoke-Native -FilePath $venvPython -Arguments @('-m','playwright','install','chromium') | Out-Null

$configPath = "$InstallRoot\agent.config.json"
if (-not (Test-Path $configPath)) {
    Copy-Item "$RepoRoot\examples\agent.config.example.json" $configPath
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($AgentId)) { $AgentId = $env:COMPUTERNAME.ToLowerInvariant() }
    $config.agent_id = $AgentId
    $config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $configPath -Encoding UTF8
    Write-Host "Initialized agent_id=$AgentId"
} else {
    Write-Host "Preserving existing config: $configPath"
}
Write-Host "Installed GPT Controller runtime dependencies. Config: $configPath"
