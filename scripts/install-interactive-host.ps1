param(
    [string]$InstallRoot = 'C:\GPT-Controller',
    [string]$TaskName = 'GPT Controller Interactive Host'
)
$ErrorActionPreference = 'Stop'
$pythonw = "$InstallRoot\venv\Scripts\pythonw.exe"
$config = "$InstallRoot\agent.config.json"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Run install.ps1 first." }
if (-not (Test-Path $config)) { throw "Missing $config." }

# Keep the default shared/desktop installation at limited integrity. A dedicated worker
# machine may explicitly opt into an elevated Interactive Host in its local config so
# foreground control also works when an elevated MMC/admin window currently owns focus.
$configData = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
$configuredRunLevel = [string]$configData.interactive_host.run_level
if ([string]::IsNullOrWhiteSpace($configuredRunLevel) -or $configuredRunLevel -ieq 'limited') {
    $runLevel = 'Limited'
}
elseif ($configuredRunLevel -ieq 'highest') {
    $runLevel = 'Highest'
}
else {
    throw "interactive_host.run_level must be 'limited' or 'highest', got: $configuredRunLevel"
}

$spool = 'C:\ProgramData\GPT-Controller\interactive'
New-Item -ItemType Directory -Force -Path "$spool\requests", "$spool\processing", "$spool\responses" | Out-Null
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $spool /grant "${current}:(OI)(CI)M" /T /C | Out-Null

# The Interactive Host itself must run in the signed-in user's session, but pythonw keeps
# it console-free. Its interaction permissions are still controlled by the local policy.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m agent_runtime.interactive_host --config `"$config`"" -WorkingDirectory $InstallRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $current
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $current -LogonType Interactive -RunLevel $runLevel

# Register-ScheduledTask -Force updates the task definition, but Windows can leave the
# already-running process from the old definition alive. That is especially dangerous for
# this host because it loads interaction policy only at process start: a permission or
# run-level change would appear installed while the stale process keeps the old policy.
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 200
        $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $currentState = if ($currentTask) { [string]$currentTask.State } else { 'Stopped' }
    } while ($currentState -eq 'Running' -and (Get-Date) -lt $deadline)
    if ($currentState -eq 'Running') {
        throw "Existing Interactive Host task did not stop before reinstall: $TaskName"
    }
}

# A previous launcher can have spawned the base interpreter before Task Scheduler reports
# the task stopped. Retire only GPT Controller Interactive Host processes scoped to this install;
# never touch unrelated Python processes.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'agent_runtime\.interactive_host' -and
        ($_.ExecutablePath -like "$InstallRoot\*" -or $_.CommandLine -like "*$InstallRoot*")
    } |
    ForEach-Object {
        try { & taskkill.exe /PID $_.ProcessId /T /F *> $null } catch {}
    }
Start-Sleep -Milliseconds 300

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

# Keep the actual Interactive Host in the user session, but its watchdog in SYSTEM/Session 0.
# Use the in-box Windows PowerShell rather than Store/WindowsApps pwsh so the recovery task
# has a machine-level executable that remains resolvable for SYSTEM.
$watchdogName = "$TaskName Watchdog"
$watchdogScript = Join-Path $PSScriptRoot 'watchdog-task.ps1'
$maintenancePath = Join-Path $InstallRoot 'state\maintenance.lock'
$systemPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $systemPowerShell)) {
    throw "Missing in-box Windows PowerShell required by SYSTEM watchdog: $systemPowerShell"
}
$watchdogAction = New-ScheduledTaskAction `
    -Execute $systemPowerShell `
    -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdogScript`" -TaskName `"$TaskName`" -InstallRoot `"$InstallRoot`" -MaintenancePath `"$maintenancePath`"" `
    -WorkingDirectory $InstallRoot
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$watchdogPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $watchdogName -Action $watchdogAction -Trigger $watchdogTrigger -Settings $watchdogSettings -Principal $watchdogPrincipal -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Milliseconds 500
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Installed hidden user-session Interactive Host: $TaskName state=$($task.State) runLevel=$runLevel"
Write-Host "Installed hidden SYSTEM watchdog: $watchdogName (every 5 minutes; battery-safe)"
