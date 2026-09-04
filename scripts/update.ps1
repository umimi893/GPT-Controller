param(
    [string]$InstallRoot = 'C:\GPT-Controller',
    [string]$RuntimeTask = 'GPT Controller Runtime',
    [string]$InteractiveTask = 'GPT Controller Interactive Host',
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $InstallRoot 'logs'
$UpdateLog = Join-Path $LogDir 'update.log'
$MaintenancePath = Join-Path $InstallRoot 'state\maintenance.lock'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-IsAdministrator {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Keep the BAT simple. The updater elevates itself using an encoded command so paths and
# quoting remain reliable, then the parent prints the persistent log if the child fails.
if (-not (Test-IsAdministrator)) {
    Write-Host 'Requesting Administrator permission for GPT Controller update...'
    $escapedScript = $PSCommandPath.Replace("'", "''")
    $escapedRoot = $InstallRoot.Replace("'", "''")
    $escapedRuntime = $RuntimeTask.Replace("'", "''")
    $escapedInteractive = $InteractiveTask.Replace("'", "''")
    $command = "& '$escapedScript' -InstallRoot '$escapedRoot' -RuntimeTask '$escapedRuntime' -InteractiveTask '$escapedInteractive' -Elevated"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $p = Start-Process -FilePath 'pwsh.exe' -Verb RunAs -PassThru -Wait -ArgumentList @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded
    )
    if ($p.ExitCode -ne 0 -and (Test-Path $UpdateLog)) {
        Write-Host ''
        Write-Host '---- update.log ----' -ForegroundColor Yellow
        Get-Content -LiteralPath $UpdateLog -Tail 80
        Write-Host '--------------------' -ForegroundColor Yellow
    }
    exit $p.ExitCode
}

if (-not $Elevated) {
    # Direct elevated launches are supported too.
    $Elevated = $true
}

$oldCommit = $null
$runtimeWasSystem = $false
$hadRuntime = $false
$hadInteractive = $false
$transcriptStarted = $false
$maintenanceCreated = $false

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Get-AgentRuntimeHeartbeatPath {
    $configPath = Join-Path $InstallRoot 'agent.config.json'
    if (-not (Test-Path -LiteralPath $configPath)) { return $null }
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json -DateKind String
        $controlRepo = [Environment]::ExpandEnvironmentVariables([string]$config.control_repo)
        if ([string]::IsNullOrWhiteSpace($controlRepo)) { return $null }
        $controlParent = Split-Path -Parent $controlRepo
        return Join-Path (Join-Path $controlParent 'state') "$($config.agent_id)-runtime-heartbeat.json"
    }
    catch {
        return $null
    }
}

function Stop-AgentRuntimeTree {
    param([string]$TaskName)

    $heartbeatPath = Get-AgentRuntimeHeartbeatPath
    $runtimePid = 0
    if ($heartbeatPath -and (Test-Path -LiteralPath $heartbeatPath)) {
        try {
            $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json -DateKind String
            [void][int]::TryParse([string]$heartbeat.pid, [ref]$runtimePid)
        }
        catch {
            $runtimePid = 0
        }
    }

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500

    if ($runtimePid -gt 0) {
        try { & taskkill.exe /PID $runtimePid /T /F *> $null } catch {}
    }

    # Fallback for older runtimes whose heartbeat is absent/corrupt. Scope by the GPT Controller
    # install path and module name; never kill unrelated Python processes.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -and
            $_.CommandLine -match 'agent_runtime' -and
            ($_.ExecutablePath -like "$InstallRoot\*" -or $_.CommandLine -like "*$InstallRoot*")
        } |
        ForEach-Object {
            try { & taskkill.exe /PID $_.ProcessId /T /F *> $null } catch {}
        }

    $deadline = (Get-Date).AddSeconds(15)
    do {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task -or $task.State -ne 'Running') { return }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    throw "Runtime task/process tree did not stop cleanly: $TaskName"
}

function Wait-AgentRuntimeHealthy {
    param(
        [string]$TaskName,
        [int]$TimeoutSeconds = 45
    )

    $heartbeatPath = Get-AgentRuntimeHeartbeatPath
    if (-not $heartbeatPath) {
        throw 'Cannot resolve Runtime heartbeat path after update.'
    }

    $started = [DateTimeOffset]::UtcNow
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) { throw "Runtime task disappeared after update: $TaskName" }
        if ($task.State -eq 'Running' -and (Test-Path -LiteralPath $heartbeatPath)) {
            try {
                $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json -DateKind String
                $updated = [DateTimeOffset]::Parse([string]$heartbeat.updated_at).ToUniversalTime()
                $pidValue = 0
                $pidOk = [int]::TryParse([string]$heartbeat.pid, [ref]$pidValue)
                if ($updated -ge $started.AddSeconds(-2) -and $pidOk -and $pidValue -gt 0) {
                    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
                    if ($process) {
                        Write-Host "Runtime health handshake PASS: pid=$pidValue state=$($heartbeat.state) heartbeat=$($heartbeat.updated_at)"
                        return
                    }
                }
            }
            catch {}
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "Runtime did not produce a fresh live heartbeat within ${TimeoutSeconds}s after update."
}

try {
    "`n===== GPT Controller update $(Get-Date -Format o) =====" | Add-Content -LiteralPath $UpdateLog -Encoding utf8
    Start-Transcript -Path $UpdateLog -Append -Force | Out-Null
    $transcriptStarted = $true

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $MaintenancePath) | Out-Null
    "pid=$PID`nstarted=$(Get-Date -Format o)" | Set-Content -LiteralPath $MaintenancePath -Encoding UTF8
    $maintenanceCreated = $true

    Set-Location $RepoRoot
    if (-not (Test-Path '.git')) { throw "Agent source repository not found: $RepoRoot" }

    # Refuse real user/source edits, but tolerate known generated Python metadata from
    # editable installs on older Agent versions.
    $dirty = @(git status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'git status failed.' }
    $meaningfulDirty = @($dirty | Where-Object {
        $_ -notmatch '^\?\?\s+src/[^/]+\.egg-info/' -and
        $_ -notmatch '^\?\?\s+.*__pycache__/' -and
        $_ -notmatch '^\?\?\s+.*\.pyc$'
    })
    if ($meaningfulDirty.Count -gt 0) {
        throw "Agent source has local changes. Update stopped without modifying them.`n$($meaningfulDirty -join "`n")"
    }

    $oldCommit = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve current Agent commit.' }

    $runtime = Get-ScheduledTask -TaskName $RuntimeTask -ErrorAction SilentlyContinue
    if ($runtime) {
        $hadRuntime = $true
        $runtimeWasSystem = ($runtime.Principal.UserId -eq 'SYSTEM')
    }
    $interactive = Get-ScheduledTask -TaskName $InteractiveTask -ErrorAction SilentlyContinue
    $hadInteractive = [bool]$interactive

    Write-Host "GPT Controller update starting from $oldCommit" -ForegroundColor Cyan
    if ($hadRuntime) { Stop-AgentRuntimeTree -TaskName $RuntimeTask }
    if ($hadInteractive) { Stop-ScheduledTask -TaskName $InteractiveTask -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 700

    try {
        Invoke-NativeChecked -FilePath 'git' -Arguments @('fetch','origin','main')
        Invoke-NativeChecked -FilePath 'git' -Arguments @('checkout','main')
        Invoke-NativeChecked -FilePath 'git' -Arguments @('pull','--ff-only','origin','main')

        $newCommit = (git rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve updated Agent commit.' }

        $venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
        if (-not (Test-Path $venvPython)) {
            # A source checkout with no runtime installation is a supported repair case.
            & (Join-Path $PSScriptRoot 'install.ps1') -InstallRoot $InstallRoot
            $venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
        } else {
            # Re-run the idempotent installer so missing config/control transport is
            # repaired automatically instead of leaving an updated but unusable Agent.
            & (Join-Path $PSScriptRoot 'install.ps1') -InstallRoot $InstallRoot -Python $venvPython
        }
        if (-not (Test-Path $venvPython)) { throw "Missing $venvPython after install repair." }

        # Keep the Python -c smoke-test argument free of embedded double quotes. On
        # Windows PowerShell/native argv boundaries, escaped double quotes inside a
        # single argument can be stripped before Python receives the code string.
        Invoke-NativeChecked -FilePath $venvPython -Arguments @(
            '-c',
            "import agent_runtime, agent_runtime.runtime, agent_runtime.worker, agent_runtime.process_supervisor, agent_runtime.resilient_bus; print('Agent imports OK')"
        )

        # Pytest collects both unittest.TestCase suites and pytest-style function tests.
        # Keeping one runner prevents silent coverage gaps during production updates.
        Invoke-NativeChecked -FilePath $venvPython -Arguments @('-m','pytest','-q',"$RepoRoot\tests")

        # Repair invariant: every successful update leaves the Runtime task installed
        # and healthy, even when this machine had source/dependencies but no task yet.
        if ($runtimeWasSystem) {
            & (Join-Path $PSScriptRoot 'install-service.ps1') -InstallRoot $InstallRoot -TaskName $RuntimeTask -System
        } else {
            & (Join-Path $PSScriptRoot 'install-service.ps1') -InstallRoot $InstallRoot -TaskName $RuntimeTask
        }
        $hadRuntime = $true

        if ($hadInteractive) {
            & (Join-Path $PSScriptRoot 'install-interactive-host.ps1') -InstallRoot $InstallRoot -TaskName $InteractiveTask
        }

        Wait-AgentRuntimeHealthy -TaskName $RuntimeTask -TimeoutSeconds 45
        if ($hadInteractive -and (Get-ScheduledTask -TaskName $InteractiveTask).State -ne 'Running') {
            throw 'Interactive Host task did not return to Running state.'
        }

        Write-Host ''
        if ($newCommit -eq $oldCommit) {
            Write-Host "GPT Controller is already up to date and repaired: $newCommit" -ForegroundColor Green
        } else {
            Write-Host "GPT Controller updated and repaired successfully: $oldCommit -> $newCommit" -ForegroundColor Green
        }
        Write-Host "Runtime:     $((Get-ScheduledTask -TaskName $RuntimeTask).State)"
        Write-Host "Interactive: $(if ($hadInteractive) { (Get-ScheduledTask -TaskName $InteractiveTask).State } else { 'not installed' })"
        Write-Host "Logs:        $LogDir"
    }
    catch {
        Write-Warning "Update failed: $($_.Exception.Message)"
        if ($oldCommit) {
            Write-Warning "Rolling Agent source back to $oldCommit"
            & git reset --hard $oldCommit | Out-Host
            $venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
            if (Test-Path $venvPython) {
                try {
                    Invoke-NativeChecked -FilePath $venvPython -Arguments @('-m','pip','install','-e',"${RepoRoot}[all]")
                } catch {}
            }
        }
        try {
            # Restore the Runtime even if it was absent before this update attempt;
            # update.ps1 doubles as the supported repair entry point.
            if (Test-Path (Join-Path $InstallRoot 'venv\Scripts\pythonw.exe')) {
                if ($runtimeWasSystem) {
                    & (Join-Path $PSScriptRoot 'install-service.ps1') -InstallRoot $InstallRoot -TaskName $RuntimeTask -System
                } else {
                    & (Join-Path $PSScriptRoot 'install-service.ps1') -InstallRoot $InstallRoot -TaskName $RuntimeTask
                }
            }
            if ($hadInteractive) {
                & (Join-Path $PSScriptRoot 'install-interactive-host.ps1') -InstallRoot $InstallRoot -TaskName $InteractiveTask
            }
        } catch {
            Write-Warning "Rollback restart also failed: $($_.Exception.Message)"
        }
        throw
    }
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if ($maintenanceCreated) {
        Remove-Item -LiteralPath $MaintenancePath -Force -ErrorAction SilentlyContinue
    }
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}
