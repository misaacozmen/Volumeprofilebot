$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$CurrentPrincipal = [Security.Principal.WindowsPrincipal]::new($CurrentIdentity)
if (-not $CurrentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process `
        -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"") `
        -Verb RunAs
    exit 0
}

$NewYorkZone = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$NewYorkNow = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $NewYorkZone)
$SessionStart = [TimeSpan]::ParseExact("09:20", "hh\:mm", $null)
$SessionEnd = [TimeSpan]::ParseExact("13:00", "hh\:mm", $null)
if ($NewYorkNow.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday) -or
    $NewYorkNow.TimeOfDay -lt $SessionStart -or
    $NewYorkNow.TimeOfDay -ge $SessionEnd) {
    throw "Super1 can be started only on weekdays from 09:20 through 12:59 New York time. Current New York time: $($NewYorkNow.ToString('yyyy-MM-dd HH:mm:ss zzz'))"
}

foreach ($TaskName in @("Super1XM", "Super1Watchdog")) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ([string]$Task.State -ne "Running") {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    }
}

Start-Sleep -Seconds 8
$Main = Get-ScheduledTask -TaskName "Super1XM" -ErrorAction Stop
$Watchdog = Get-ScheduledTask -TaskName "Super1Watchdog" -ErrorAction Stop
if ([string]$Main.State -ne "Running" -or [string]$Watchdog.State -ne "Running") {
    throw "Super1 tasks did not reach Running state. main=$($Main.State) watchdog=$($Watchdog.State)"
}
Write-Host "Super1 started. New York time: $($NewYorkNow.ToString('yyyy-MM-dd HH:mm:ss zzz'))" -ForegroundColor Green
