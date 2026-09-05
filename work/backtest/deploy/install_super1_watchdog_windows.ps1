[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract
$watchdog = [string](Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.watchdog))
$status = Join-Path ([string]$Contract.root) "watchdog_status.json"
if (-not (Test-Path -LiteralPath $watchdog -PathType Leaf)) {
    throw "Watchdog script is missing: $watchdog"
}
$powershell = Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe"
$arguments = @(
    "-NoProfile"
    "-ExecutionPolicy Bypass"
    "-File `"$watchdog`""
    "-MainTaskName `"$([string]$Contract.main_task)`""
    "-HealthPath `"$([string]$Contract.health)`""
    "-StatusPath `"$status`""
) -join " "
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 0 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# Deliberately no trigger: start_super1_local_windows.ps1 starts this task
# only after creating and validating the daily manual lease.
Register-ScheduledTask `
    -TaskName ([string]$Contract.watchdog_task) `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
