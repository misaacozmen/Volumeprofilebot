$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = "C:\ForwardShadow"
$TaskName = "ForwardShadowXM"
$Launcher = Join-Path $Root "app\deploy\run_forward_shadow_windows.ps1"
$HealthPath = Join-Path $Root "state\health.json"
$BackupXml = Join-Path $Root "ForwardShadowXM.pre-s4u.xml"

Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $BackupXml -Encoding UTF8
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""
$Trigger = New-ScheduledTaskTrigger -AtStartup
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentIdentity `
    -LogonType S4U -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "terminal64.exe" -or
        ($_.Name -like "python*.exe" -and $_.CommandLine -like "*run_xm_mt5_forward.py*")
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Principal $Principal -Settings $Settings | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    $Deadline = (Get-Date).AddSeconds(75)
    do {
        Start-Sleep -Seconds 5
        $Task = Get-ScheduledTask -TaskName $TaskName
        $Health = Get-Content -LiteralPath $HealthPath -Raw | ConvertFrom-Json
        $HealthFresh = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse([string]$Health.updated_at)).TotalSeconds -lt 20
    } until (($Task.State -eq "Running" -and $Health.state -eq "RUNNING" -and $HealthFresh) -or
        (Get-Date) -ge $Deadline)
    if ($Task.State -ne "Running" -or $Health.state -ne "RUNNING" -or -not $HealthFresh) {
        throw "S4U verification failed: task=$($Task.State), health=$($Health.state), error=$($Health.error)"
    }
}
catch {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Xml (Get-Content -LiteralPath $BackupXml -Raw) | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    throw
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State,
    @{n='User';e={$_.Principal.UserId}},@{n='LogonType';e={$_.Principal.LogonType}}
