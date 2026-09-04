param(
    [string]$InstallRoot = 'C:\GPT-Controller',
    [string]$SourceRemote = 'https://github.com/umimi893/GPT-Controller.git',
    [switch]$Elevated
)
$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    $script = $PSCommandPath.Replace("'", "''")
    $root = $InstallRoot.Replace("'", "''")
    $remote = $SourceRemote.Replace("'", "''")
    $cmd = "& '$script' -InstallRoot '$root' -SourceRemote '$remote' -Elevated"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -Wait -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded)
    exit $p.ExitCode
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
}

function Find-Exe {
    param([string]$Name, [string[]]$Candidates = @())
    Refresh-Path
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in $Candidates) {
        $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
        if (Test-Path -LiteralPath $expanded) { return $expanded }
    }
    return $null
}

function Ensure-WingetPackage {
    param([string]$CommandName, [string]$PackageId, [string[]]$Candidates = @())
    $found = Find-Exe -Name $CommandName -Candidates $Candidates
    if ($found) { return $found }
    $winget = Find-Exe -Name 'winget.exe'
    if (-not $winget) { throw 'winget is required. Install Microsoft App Installer, then run Install GPT Controller.bat again.' }
    Write-Host "Installing $PackageId ..." -ForegroundColor Cyan
    & $winget install --id $PackageId -e --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) { throw "winget failed to install $PackageId (exit $LASTEXITCODE)." }
    Refresh-Path
    $found = Find-Exe -Name $CommandName -Candidates $Candidates
    if (-not $found) { throw "$PackageId was installed but $CommandName could not be located. Sign out/in or reboot, then run the installer again." }
    return $found
}

Write-Host 'GPT Controller Windows bootstrap' -ForegroundColor Cyan
$git = Ensure-WingetPackage -CommandName 'git.exe' -PackageId 'Git.Git' -Candidates @('C:\Program Files\Git\cmd\git.exe')
$gh = Ensure-WingetPackage -CommandName 'gh.exe' -PackageId 'GitHub.cli' -Candidates @('C:\Program Files\GitHub CLI\gh.exe')
$python = Ensure-WingetPackage -CommandName 'python.exe' -PackageId 'Python.Python.3.12' -Candidates @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", 'C:\Program Files\Python312\python.exe')
$pwsh = Ensure-WingetPackage -CommandName 'pwsh.exe' -PackageId 'Microsoft.PowerShell' -Candidates @('C:\Program Files\PowerShell\7\pwsh.exe')

foreach ($dir in @((Split-Path $git -Parent),(Split-Path $gh -Parent),(Split-Path $python -Parent),(Split-Path $pwsh -Parent))) {
    if ($env:Path -notlike "*$dir*") { $env:Path = "$dir;$env:Path" }
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$SourcePath = Join-Path $InstallRoot 'source'
if (Test-Path (Join-Path $SourcePath '.git')) {
    & $git -C $SourcePath fetch origin main
    if ($LASTEXITCODE -ne 0) { throw 'git fetch failed for installed GPT Controller source.' }
    & $git -C $SourcePath checkout main
    if ($LASTEXITCODE -ne 0) { throw 'git checkout main failed.' }
    & $git -C $SourcePath pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) { throw 'git pull --ff-only failed.' }
} else {
    if (Test-Path $SourcePath) { Remove-Item -LiteralPath $SourcePath -Recurse -Force }
    & $git clone --branch main --single-branch $SourceRemote $SourcePath
    if ($LASTEXITCODE -ne 0) { throw 'Failed to clone the public GPT Controller repository.' }
}

& $pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $SourcePath 'scripts\bootstrap-install.ps1') -InstallRoot $InstallRoot -Python $python
exit $LASTEXITCODE
