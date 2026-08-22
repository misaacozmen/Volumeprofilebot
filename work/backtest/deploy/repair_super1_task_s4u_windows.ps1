$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = "C:\Super1"
$TaskName = "Super1XM"
$RunnerUser = "Super1Runner"
$Launcher = Join-Path $Root "app\deploy\run_super1_windows.ps1"

Export-ScheduledTask -TaskName $TaskName |
    Set-Content -LiteralPath (Join-Path $Root "Super1XM.pre-service-user.xml") -Encoding UTF8

$ExistingRunner = Get-LocalUser -Name $RunnerUser -ErrorAction SilentlyContinue
if (-not $ExistingRunner) {
    throw "Super1Runner does not exist; use recover_super1_isolated_user.ps1 to create the user and a new DPAPI credential together."
}
Enable-LocalUser -Name $RunnerUser

$RunnerIdentity = "$env:COMPUTERNAME\$RunnerUser"
$StateRoot = Join-Path $Root "state"
$Mt5Root = Join-Path $Root "mt5"
$VenvRoot = Join-Path $Root "venv311"
$AppRoot = Join-Path $Root "app"
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
& icacls.exe $Root /grant:r "${RunnerIdentity}:(RX)" /Q | Out-Null
& icacls.exe $AppRoot /grant:r "${RunnerIdentity}:(OI)(CI)(RX)" /T /C /Q | Out-Null
& icacls.exe $VenvRoot /grant:r "${RunnerIdentity}:(OI)(CI)(RX)" /T /C /Q | Out-Null
& icacls.exe $StateRoot /grant:r "${RunnerIdentity}:(OI)(CI)(M)" /T /C /Q | Out-Null
& icacls.exe $Mt5Root /grant:r "${RunnerIdentity}:(OI)(CI)(M)" /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not apply least-privilege Super1Runner ACLs." }
if (-not (Test-Path -LiteralPath (Join-Path $Root "xm-password.runner.dpapi"))) {
    throw "Runner-bound credential is missing. Use recover_super1_isolated_user.ps1 before S4U conversion."
}
Copy-Item -LiteralPath (Join-Path $Root "xm-password.runner.dpapi") `
    -Destination (Join-Path $Root "xm-password.dpapi") -Force
$Principal = New-ScheduledTaskPrincipal -UserId $RunnerIdentity -LogonType S4U -RunLevel Limited
$SmokeTask = "Super1DpapiS4USmoke"
$SmokeScript = Join-Path $StateRoot "dpapi-s4u-smoke.ps1"
$SmokeMarker = Join-Path $StateRoot "dpapi-s4u-smoke.ok"
$SmokeSource = @'
$ErrorActionPreference = "Stop"
$Secure = (Get-Content -Raw "C:\Super1\xm-password.dpapi").Trim() | ConvertTo-SecureString
if ($null -eq $Secure) { throw "DPAPI decrypt returned null." }
[IO.File]::WriteAllText("C:\Super1\state\dpapi-s4u-smoke.ok", "OK")
'@
Set-Content -LiteralPath $SmokeScript -Value $SmokeSource -Encoding UTF8
try {
    $SmokeAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$SmokeScript`""
    $SmokeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1)
    Register-ScheduledTask -TaskName $SmokeTask -Action $SmokeAction -Trigger $SmokeTrigger `
        -Principal $Principal -Force | Out-Null
    Start-ScheduledTask -TaskName $SmokeTask
    $SmokeDeadline = (Get-Date).AddSeconds(30)
    while (-not (Test-Path -LiteralPath $SmokeMarker) -and (Get-Date) -lt $SmokeDeadline) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Path -LiteralPath $SmokeMarker)) {
        throw "Super1Runner cannot decrypt its DPAPI credential under S4U; main task was not replaced."
    }
}
finally {
    Unregister-ScheduledTask -TaskName $SmokeTask -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $SmokeScript,$SmokeMarker -Force -ErrorAction SilentlyContinue
}
$RunnerSid = ([Security.Principal.NTAccount]$RunnerIdentity).Translate(
    [Security.Principal.SecurityIdentifier]
).Value
$RightsFile = Join-Path $Root "super1-user-rights.inf"
& secedit.exe /export /cfg $RightsFile /areas USER_RIGHTS /quiet | Out-Null
$Rights = Get-Content -LiteralPath $RightsFile
$BatchIndex = -1
for ($Index = 0; $Index -lt $Rights.Count; $Index++) {
    if ($Rights[$Index] -like "SeBatchLogonRight*") { $BatchIndex = $Index; break }
}
$SidToken = "*$RunnerSid"
if ($BatchIndex -ge 0 -and $Rights[$BatchIndex] -notlike "*$SidToken*") {
    $Rights[$BatchIndex] = $Rights[$BatchIndex].TrimEnd() + ",$SidToken"
}
elseif ($BatchIndex -lt 0) {
    $VersionIndex = [Array]::IndexOf($Rights, "[Version]")
    if ($VersionIndex -lt 0) { throw "Exported user-rights policy has no Version section." }
    $Rights = @($Rights[0..($VersionIndex - 1)]) + "SeBatchLogonRight = $SidToken" + `
        @($Rights[$VersionIndex..($Rights.Count - 1)])
}
if ($BatchIndex -lt 0 -or $Rights[$BatchIndex] -like "*$SidToken*") {
    Set-Content -LiteralPath $RightsFile -Value $Rights -Encoding Unicode
    $RightsDatabase = Join-Path $Root "super1-user-rights.sdb"
    & secedit.exe /configure /db $RightsDatabase /cfg $RightsFile /overwrite `
        /areas USER_RIGHTS /quiet | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not grant batch logon right." }
    Remove-Item -LiteralPath $RightsDatabase,"$RightsDatabase.jfm" -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $RightsFile -Force -ErrorAction SilentlyContinue

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal | Out-Null
Start-ScheduledTask -TaskName $TaskName
