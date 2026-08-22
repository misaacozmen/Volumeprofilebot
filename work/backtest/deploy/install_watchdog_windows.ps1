[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [Parameter(Mandatory = $true)]
    [string]$MainTaskName,
    [Parameter(Mandatory = $true)]
    [string]$WatchdogTaskName,
    [Parameter(Mandatory = $true)]
    [string]$HealthPath,
    [Parameter(Mandatory = $true)]
    [string]$ProcessPattern
)

$ErrorActionPreference = "Stop"
$watchdog = Join-Path $Root "app\deploy\watchdog_windows.ps1"
$status = Join-Path $Root "watchdog_status.json"
if (-not (Test-Path -LiteralPath $watchdog)) {
    throw "Watchdog script is missing: $watchdog"
}

$arguments = @(
    "-NoProfile"
    "-ExecutionPolicy Bypass"
    "-File `"$watchdog`""
    "-MainTaskName `"$MainTaskName`""
    "-HealthPath `"$HealthPath`""
    "-ProcessPattern `"$ProcessPattern`""
    "-StatusPath `"$status`""
) -join " "
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $WatchdogTaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Start-ScheduledTask -TaskName $WatchdogTaskName
