param(
    [string]$InstallRoot = 'C:\GPT-Controller',
    [string]$TaskName = 'GPT Controller Runtime',
    [switch]$System
)
$ErrorActionPreference = 'Stop'
$pythonw = "$InstallRoot\venv\Scripts\pythonw.exe"
$config = "$InstallRoot\agent.config.json"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Run install.ps1 first." }
if (-not (Test-Path $config)) { throw "Missing $config." }

$configJson = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
$controlRepo = [Environment]::ExpandEnvironmentVariables([string]$configJson.control_repo)
if ([string]::IsNullOrWhiteSpace($controlRepo) -or -not (Test-Path (Join-Path $controlRepo '.git'))) {
    throw "Agent control repository is not initialized: $controlRepo. Run install.ps1 or update.ps1 to repair the installation."
}
$controlParent = Split-Path -Parent $controlRepo
$heartbeatPath = Join-Path (Join-Path $controlParent 'state') "$($configJson.agent_id)-runtime-heartbeat.json"
$maintenancePath = Join-Path $InstallRoot 'state\maintenance.lock'

# Use pythonw so the always-on runtime never creates a visible console window.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m agent_runtime --config `"$config`"" -WorkingDirectory $InstallRoot
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

if ($System) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $mode = 'SYSTEM/startup'
} else {
    $userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest
    $mode = "current-user/logon ($userId)"
}

# Register-ScheduledTask -Force replaces the task definition, but Windows can keep an
# already-running instance of the old action alive. Older GPT Controller installs launched
# gpt-controller.exe/python.exe and therefore could leave a console-backed runtime (and cmd.exe)
# alive until logoff or reboot. Explicitly retire that legacy instance before replacing
# the task with the pythonw.exe definition.
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    $existingAction = @($existingTask.Actions | Select-Object -First 1)
    $existingExecute = ''
    if ($existingAction.Count -gt 0) {
        $existingExecute = [Environment]::ExpandEnvironmentVariables([string]$existingAction[0].Execute).Trim('"')
    }
    $desiredExecute = [Environment]::ExpandEnvironmentVariables([string]$pythonw).Trim('"')
    $legacyConsoleRuntime = -not [string]::Equals($existingExecute, $desiredExecute, [StringComparison]::OrdinalIgnoreCase)

    if ($legacyConsoleRuntime -and $existingTask.State -eq 'Running') {
        Write-Host "Stopping legacy console-backed runtime before hidden-runtime migration: $existingExecute"
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 200
            $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            $currentState = if ($currentTask) { [string]$currentTask.State } else { 'Stopped' }
        } while ($currentState -eq 'Running' -and (Get-Date) -lt $deadline)
        if ($currentState -eq 'Running') {
            throw "Legacy console-backed runtime did not stop before hidden-runtime migration: $TaskName"
        }
    }
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

# Scheduler restart policies cover process exits. The watchdog additionally catches a
# runtime that Windows still reports as Running but whose heartbeat stopped advancing.
# The watchdog itself always runs as SYSTEM in Session 0. Do not bind a SYSTEM task to
# Microsoft Store / WindowsApps pwsh: package executables can be user-scoped and may not
# resolve for SYSTEM. watchdog-task.ps1 deliberately stays Windows PowerShell 5.1 compatible
# so the in-box executable is a stable machine-level recovery dependency.
$watchdogName = "$TaskName Watchdog"
$watchdogScript = Join-Path $PSScriptRoot 'watchdog-task.ps1'
$systemPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $systemPowerShell)) {
    throw "Missing in-box Windows PowerShell required by SYSTEM watchdog: $systemPowerShell"
}
$watchdogAction = New-ScheduledTaskAction `
    -Execute $systemPowerShell `
    -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdogScript`" -TaskName `"$TaskName`" -InstallRoot `"$InstallRoot`" -HeartbeatPath `"$heartbeatPath`" -HeartbeatMaxAgeSeconds 90 -MaintenancePath `"$maintenancePath`"" `
    -WorkingDirectory $InstallRoot
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$watchdogPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $watchdogName -Action $watchdogAction -Trigger $watchdogTrigger -Settings $watchdogSettings -Principal $watchdogPrincipal -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 1
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started hidden scheduled task: $TaskName [$mode] state=$($task.State)"
Write-Host "Installed hidden SYSTEM heartbeat watchdog: $watchdogName (every 1 minute; stale after 90 seconds; battery-safe)"

if ($System) {
    Write-Warning 'SYSTEM mode requires Git credentials that are available to the SYSTEM account. Prefer default current-user mode until machine-scoped Git authentication is configured.'
}
