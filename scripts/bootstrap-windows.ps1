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

$LogDir = Join-Path $InstallRoot 'logs'
$LogPath = Join-Path $LogDir 'install.log'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$transcriptStarted = $false

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
}

function Find-Exe {
    param([string]$Name, [string[]]$Candidates = @())
    Refresh-Path
    foreach ($candidate in $Candidates) {
        $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
        if (Test-Path -LiteralPath $expanded) { return $expanded }
    }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-PythonExecutable {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) { return $false }
    # Fresh Windows installs often expose a Microsoft Store alias named python.exe.
    # It exists on PATH but is not an installed Python interpreter.
    if ($Path -match '\\Microsoft\\WindowsApps\\python(?:3)?\.exe$') { return $false }
    try {
        $output = & $Path -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $text = (($output | Out-String).Trim())
        if ($text -notmatch '^(\d+)\.(\d+)$') { return $false }
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        return ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10))
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        'C:\Program Files\Python313\python.exe',
        'C:\Program Files\Python312\python.exe',
        'C:\Program Files\Python311\python.exe',
        'C:\Program Files\Python310\python.exe'
    )
    foreach ($candidate in $candidates) {
        if (Test-PythonExecutable -Path $candidate) { return $candidate }
    }
    Refresh-Path
    foreach ($name in @('python.exe','python3.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-PythonExecutable -Path $cmd.Source)) { return $cmd.Source }
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
    & $winget install --id $PackageId -e --accept-package-agreements --accept-source-agreements --silent --disable-interactivity
    $wingetCode = $LASTEXITCODE
    Refresh-Path
    $found = Find-Exe -Name $CommandName -Candidates $Candidates
    if ($found) { return $found }
    throw "winget could not make $PackageId available (exit $wingetCode). Install it manually, then run Install GPT Controller.bat again."
}

function Ensure-Python {
    $found = Find-Python
    if ($found) { return $found }
    $winget = Find-Exe -Name 'winget.exe'
    if (-not $winget) { throw 'winget is required. Install Microsoft App Installer, then run Install GPT Controller.bat again.' }
    Write-Host 'Installing Python 3.12 ...' -ForegroundColor Cyan
    & $winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements --silent --disable-interactivity
    $wingetCode = $LASTEXITCODE
    Refresh-Path
    $found = Find-Python
    if ($found) { return $found }
    throw "Python 3.12 installation did not produce a usable Python 3.10+ interpreter (winget exit $wingetCode). If Windows Store aliases are enabled, disable the python.exe App Execution Alias or install Python from python.org, then retry."
}

try {
    Start-Transcript -Path $LogPath -Append -Force | Out-Null
    $transcriptStarted = $true
    Write-Host 'GPT Controller Windows bootstrap' -ForegroundColor Cyan
    Write-Host "Install log: $LogPath"

    $git = Ensure-WingetPackage -CommandName 'git.exe' -PackageId 'Git.Git' -Candidates @('C:\Program Files\Git\cmd\git.exe')
    $gh = Ensure-WingetPackage -CommandName 'gh.exe' -PackageId 'GitHub.cli' -Candidates @('C:\Program Files\GitHub CLI\gh.exe')
    $python = Ensure-Python
    $pwsh = Ensure-WingetPackage -CommandName 'pwsh.exe' -PackageId 'Microsoft.PowerShell' -Candidates @('C:\Program Files\PowerShell\7\pwsh.exe')

    foreach ($dir in @((Split-Path $git -Parent),(Split-Path $gh -Parent),(Split-Path $python -Parent),(Split-Path $pwsh -Parent))) {
        if ($env:Path -notlike "*$dir*") { $env:Path = "$dir;$env:Path" }
    }

    Write-Host "Git:        $git"
    Write-Host "GitHub CLI: $gh"
    Write-Host "Python:     $python"
    Write-Host "PowerShell: $pwsh"

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
    if ($LASTEXITCODE -ne 0) { throw "bootstrap-install.ps1 failed with exit code $LASTEXITCODE." }
    Write-Host ''
    Write-Host 'GPT Controller installation completed successfully.' -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ''
    Write-Host 'GPT Controller installation FAILED.' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Full log: $LogPath" -ForegroundColor Yellow
    exit 1
}
finally {
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}
