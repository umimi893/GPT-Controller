param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [string]$InstallRoot = 'C:\GPT-Controller',
    [string]$HeartbeatPath = '',
    [int]$HeartbeatMaxAgeSeconds = 90,
    [int]$StepTimeoutGraceSeconds = 120,
    [int]$PublishingMaxAgeSeconds = 3600,
    [string]$MaintenancePath = ''
)
$ErrorActionPreference = 'Stop'

function ConvertTo-UtcOffset {
    param([object]$Value)
    if ($null -eq $Value) { throw 'timestamp is null' }
    if ($Value -is [DateTimeOffset]) { return $Value.ToUniversalTime() }
    if ($Value -is [DateTime]) { return ([DateTimeOffset]$Value).ToUniversalTime() }
    return [DateTimeOffset]::Parse([string]$Value).ToUniversalTime()
}

function Write-RecoveryRecord {
    param(
        [string]$Reason,
        [object]$Heartbeat
    )
    try {
        $stateDir = Join-Path $InstallRoot 'state'
        New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
        $record = [ordered]@{
            protocol = 'q-agent-v4-watchdog-recovery'
            recovered_at = [DateTimeOffset]::UtcNow.ToString('o')
            task_name = $TaskName
            reason = $Reason
            runtime_pid = if ($Heartbeat) { $Heartbeat.pid } else { $null }
            action_id = if ($Heartbeat) { $Heartbeat.action_id } else { $null }
            state = if ($Heartbeat) { $Heartbeat.state } else { $null }
            step_index = if ($Heartbeat) { $Heartbeat.step_index } else { $null }
            step_type = if ($Heartbeat) { $Heartbeat.step_type } else { $null }
        }
        $record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $stateDir 'watchdog-recovery-latest.json') -Encoding UTF8
        ($record | ConvertTo-Json -Compress -Depth 8) | Add-Content -LiteralPath (Join-Path $stateDir 'watchdog-recovery.log') -Encoding UTF8
    }
    catch {
        # Diagnostics must never prevent recovery.
    }
}

function Stop-RuntimeTree {
    param([object]$Heartbeat)
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    catch {}

    $runtimePid = 0
    if ($Heartbeat -and [int]::TryParse([string]$Heartbeat.pid, [ref]$runtimePid) -and $runtimePid -gt 0) {
        try {
            & taskkill.exe /PID $runtimePid /T /F *> $null
        }
        catch {}
    }
    Start-Sleep -Milliseconds 750
}

try {
    if (-not $MaintenancePath) {
        $MaintenancePath = Join-Path $InstallRoot 'state\maintenance.lock'
    }

    if (Test-Path -LiteralPath $MaintenancePath) {
        $maintenanceAge = ((Get-Date) - (Get-Item -LiteralPath $MaintenancePath).LastWriteTime).TotalMinutes
        if ($maintenanceAge -le 30) {
            exit 0
        }
        Remove-Item -LiteralPath $MaintenancePath -Force -ErrorAction SilentlyContinue
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $restartReason = $null
    $heartbeat = $null

    if ($HeartbeatPath -and (Test-Path -LiteralPath $HeartbeatPath)) {
        try {
            # Keep this script Windows PowerShell 5.1 compatible. Timestamp conversion is
            # normalized by ConvertTo-UtcOffset instead of relying on PowerShell 7-only
            # ConvertFrom-Json switches.
            $heartbeat = Get-Content -LiteralPath $HeartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            $restartReason = 'heartbeat unreadable'
        }
    }

    if (-not $restartReason -and $task.State -ne 'Running') {
        $restartReason = "task state is $($task.State)"
    }
    elseif (-not $restartReason -and $HeartbeatPath) {
        if ($heartbeat) {
            try {
                $updated = ConvertTo-UtcOffset $heartbeat.updated_at
                $ageSeconds = ([DateTimeOffset]::UtcNow - $updated).TotalSeconds
                if ($ageSeconds -gt $HeartbeatMaxAgeSeconds) {
                    $restartReason = "heartbeat stale ($([math]::Round($ageSeconds))s)"
                }
            }
            catch {
                $restartReason = 'heartbeat timestamp unreadable'
            }

            if (-not $restartReason -and [string]$heartbeat.state -eq 'executing' -and $heartbeat.step_started_at -and $heartbeat.step_timeout_seconds) {
                try {
                    $stepStarted = ConvertTo-UtcOffset $heartbeat.step_started_at
                    $stepTimeout = [int]$heartbeat.step_timeout_seconds
                    $stepAge = ([DateTimeOffset]::UtcNow - $stepStarted).TotalSeconds
                    if ($stepTimeout -gt 0 -and $stepAge -gt ($stepTimeout + $StepTimeoutGraceSeconds)) {
                        $restartReason = "live runtime stuck in step $($heartbeat.step_index) [$($heartbeat.step_type)] for $([math]::Round($stepAge))s; deadline=$($stepTimeout + $StepTimeoutGraceSeconds)s"
                    }
                }
                catch {
                    $restartReason = 'step deadline metadata unreadable'
                }
            }

            if (-not $restartReason -and [string]$heartbeat.state -eq 'publishing_result' -and $heartbeat.state_started_at) {
                try {
                    $publishingStarted = ConvertTo-UtcOffset $heartbeat.state_started_at
                    $publishingAge = ([DateTimeOffset]::UtcNow - $publishingStarted).TotalSeconds
                    if ($publishingAge -gt $PublishingMaxAgeSeconds) {
                        $restartReason = "result publication stuck for $([math]::Round($publishingAge))s"
                    }
                }
                catch {
                    $restartReason = 'result publication timestamp unreadable'
                }
            }
        }
        else {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
            if ($info.LastRunTime -and $info.LastRunTime -ne [datetime]::MinValue) {
                $runAgeSeconds = ((Get-Date) - $info.LastRunTime).TotalSeconds
                if ($runAgeSeconds -gt $HeartbeatMaxAgeSeconds) {
                    $restartReason = 'heartbeat missing'
                }
            }
        }
    }

    if ($restartReason) {
        Write-Host "Recovering ${TaskName}: $restartReason"
        Write-RecoveryRecord -Reason $restartReason -Heartbeat $heartbeat
        Stop-RuntimeTree -Heartbeat $heartbeat
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ($task.State -ne 'Running') {
            throw "Task did not return to Running state: $TaskName state=$($task.State)"
        }
    }
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
