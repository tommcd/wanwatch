# setup_wan_task.ps1 - install the "WAN Monitor" scheduled task.
# Works on Windows PowerShell 5 and PowerShell 7 (repetition is set via
# trigger parameters, not property assignment, which PS7 rejects).
#
# Usually invoked via:  wanctl task-install
# Direct use:  .\setup_wan_task.ps1 -MonitorPath C:\...\wan_monitor.sh -LogPath C:\...\wan_log.csv

param(
    [Parameter(Mandatory=$true)][string]$MonitorPath,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [string]$TaskName = 'WAN Monitor',
    [string]$Bash = 'C:\Program Files\Git\bin\bash.exe'
)

if (-not (Test-Path $Bash))        { throw "bash.exe not found at $Bash" }
if (-not (Test-Path $MonitorPath)) { throw "monitor script not found at $MonitorPath" }

# Convert C:\Users\x\... -> /c/Users/x/... for bash
function To-BashPath([string]$p) {
    if ($p -match '^([A-Za-z]):\\(.*)$') {
        return '/' + $Matches[1].ToLower() + '/' + ($Matches[2] -replace '\\', '/')
    }
    return $p -replace '\\', '/'
}

$monBash = To-BashPath $MonitorPath
$logBash = To-BashPath $LogPath

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute $Bash -Argument "-l -c `"$monBash $logBash`""

# Trigger 1: at boot. Trigger 2: a one-time trigger dated in the past that
# repeats forever - the watchdog. The past date matters: repetition only
# begins once its trigger has fired.
$t1 = New-ScheduledTaskTrigger -AtStartup
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-5) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# S4U: no console window, survives logoff, no stored password.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger @($t1, $t2) -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered '$TaskName' with S4U (no console window)." -ForegroundColor Green
}
catch {
    Write-Warning "S4U failed ($($_.Exception.Message)); falling back to Interactive."
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger @($t1, $t2) -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered as Interactive - a console window will appear; do not close it." -ForegroundColor Yellow
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime | Format-List
Write-Host "LastTaskResult 267009 = running; NextRunTime should be ~5 min ahead." -ForegroundColor Cyan
