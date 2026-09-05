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

Stop-ScheduledTask -TaskName "Super1Watchdog" -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "Super1XM" -ErrorAction SilentlyContinue

$Deadline = (Get-Date).AddSeconds(15)
do {
    $Processes = @(
        Get-CimInstance Win32_Process -Filter "Name = 'terminal64.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -eq "C:\Super1\mt5\terminal64.exe" }
    )
    foreach ($Process in $Processes) {
        Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 250
} while ($Processes.Count -gt 0 -and (Get-Date) -lt $Deadline)

$Remaining = @(
    Get-CimInstance Win32_Process -Filter "Name = 'terminal64.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq "C:\Super1\mt5\terminal64.exe" }
)
if ($Remaining.Count -ne 0) {
    throw "Super1 MT5 terminal did not stop."
}
Write-Host "Super1 and its dedicated MT5 terminal stopped." -ForegroundColor Green
