[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
throw "LEGACY_DISABLED_USE_SIGNED_V16_RUNBOOK"
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "Run this Super1 repair from an elevated PowerShell."
}

$root = [string]$Contract.root
$app = [string]$Contract.app
$taskName = [string]$Contract.main_task
$runnerName = [string]$Contract.runner_account
$runnerIdentity = "$env:COMPUTERNAME\$runnerName"
$launcher = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.launcher)
$helper = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.secure_task_helper)
foreach ($path in @($app, $launcher, $helper)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Super1 repair dependency is missing: $path"
    }
}
if (-not (Get-LocalUser -Name $runnerName -ErrorAction SilentlyContinue)) {
    throw "Super1Runner is missing; use fresh finalization before task repair."
}
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    throw "Super1 main task is missing; use fresh finalization before task repair."
}

$runnerPassword = Read-Host "Super1Runner Windows parolasi" -AsSecureString
try {
    $runnerSid = ([Security.Principal.NTAccount]::new($runnerIdentity)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $action = New-ScheduledTaskAction `
        -Execute (Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe") `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
    $principal = New-ScheduledTaskPrincipal `
        -UserId $runnerIdentity `
        -LogonType Password `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 0 `
        -RestartInterval (New-TimeSpan -Minutes 15) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Principal $principal `
        -Settings $settings `
        -User $runnerIdentity `
        -Password $runnerPassword `
        -Force | Out-Null
    . $helper
    Assert-Super1SecureTaskBindings `
        -Root $root `
        -MainTask $taskName `
        -WatchdogTask ([string]$Contract.watchdog_task) | Out-Null
    [ordered]@{
        state = "REPAIRED_STOPPED"
        task = $taskName
        runner_sid = $runnerSid
        trigger_policy = "NONE"
        logon_type = "Password"
        restart_count = 0
        runner_password_or_broker_secret_emitted = $false
    } | ConvertTo-Json -Depth 5
}
finally {
    if ($runnerPassword) { $runnerPassword.Dispose() }
}
