[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract

function Test-Super1Administrator {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

if (-not (Test-Super1Administrator)) {
    $child = Start-Process `
        -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"") `
        -Verb RunAs `
        -Wait `
        -PassThru
    exit ([int]$child.ExitCode)
}

function Get-Super1Hash([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Write-Super1AtomicJson([string]$Path, [object]$Value) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = ConvertTo-Json -InputObject $Value -Depth 16 -Compress
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($json + [Environment]::NewLine)
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}

function Write-Super1StopRequest([string]$Path, [string]$LeaseId, [string]$LeaseSha256, [string]$InvocationNonce, [string]$Reason) {
    Write-Super1AtomicJson -Path $Path -Value ([ordered]@{
        schema_version = 1
        request_id = [Guid]::NewGuid().ToString()
        lease_id = $LeaseId
        lease_sha256 = $LeaseSha256
        invocation_nonce = $InvocationNonce
        requested_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        reason = $Reason
        requested_by = "Super1StartFailClosed"
    })
}

function Revoke-Super1Lease([string]$LeasePath, [string]$Reason) {
    if (-not (Test-Path -LiteralPath $LeasePath -PathType Leaf)) { return }
    $lease = Get-Content -Raw -LiteralPath $LeasePath | ConvertFrom-Json
    $lease.state = "REVOKED"
    $lease.revoked_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    $lease.revocation_reason = $Reason
    Write-Super1AtomicJson -Path $LeasePath -Value $lease
}

$root = [string]$Contract.root
$app = [string]$Contract.app
$state = [string]$Contract.state
$control = [string]$Contract.control
$leasePath = Join-Path $control "session-lease.json"
$configPath = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.runtime_config)
$manifestPath = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.manifest)
$calendarPath = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.calendar)
$releaseManifestPath = [string]$Contract.release_manifest
$releaseArchivePath = [string]$Contract.release_archive
$releaseSignaturePath = [string]$Contract.release_signature
$required = @(
    [string]$Contract.terminal,
    [string]$Contract.python,
    $app,
    [string]$Contract.runtime_trust,
    $configPath,
    $manifestPath,
    $calendarPath,
    $releaseManifestPath,
    $releaseArchivePath,
    $releaseSignaturePath
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Super1 start dependency is missing: $path" }
    if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 start dependency is a reparse point: $path"
    }
}
$taskHelper = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.secure_task_helper)
. $taskHelper
$boundRunnerSid = Assert-Super1SecureTaskBindings `
    -Root $root `
    -MainTask ([string]$Contract.main_task) `
    -WatchdogTask ([string]$Contract.watchdog_task)
$freeDrive = [IO.Path]::GetPathRoot($root).TrimEnd('\').TrimEnd(':')
$free = (Get-PSDrive -Name $freeDrive -ErrorAction Stop).Free
if ($free -lt 1GB) { throw "Super1 state volume has less than 1 GiB free." }
foreach ($latch in @(
    (Join-Path $state "fatal_latch.json"),
    (Join-Path $state "runtime\broker_recovery_required.json"),
    (Join-Path $state "UNSAFE_STOP_NO_SEND.json"),
    (Join-Path $state "RESTART_BUDGET_EXHAUSTED_NO_SEND.json")
)) {
    if (Test-Path -LiteralPath $latch -PathType Leaf) {
        throw "Super1 fail-closed latch is set; repair/recovery is required before start: $latch"
    }
}
$activeStop = Join-Path $control "stop-request.json"
if (Test-Path -LiteralPath $activeStop -PathType Leaf) {
    throw "Super1 has an active stop request; complete/archive it before starting."
}

$runtime = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$calendar = Get-Content -Raw -LiteralPath $calendarPath | ConvertFrom-Json
$release = Get-Content -Raw -LiteralPath $releaseManifestPath | ConvertFrom-Json
$integrityPath = Join-Path $app "deploy\release_integrity.ps1"
. $integrityPath
$signedRelease = Assert-SignedReleaseArchive `
    -Archive $releaseArchivePath `
    -ExpectedProfile "super1" `
    -RequireProvenance

$dbPath = Join-Path $state "orders\idempotency.sqlite3"
if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) { throw "Super1 order ledger is missing." }
$dbProbe = & ([string]$Contract.python) -I -E -B -c @"
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
cols = {row[1] for row in c.execute('PRAGMA table_info(order_event_outbox)')}
assert 'event_id' in cols
assert c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_order_event_outbox_event_id'").fetchone()
c.close()
print('READY')
"@ $dbPath
if ($LASTEXITCODE -ne 0 -or (($dbProbe -join "`n") -notmatch "READY")) {
    throw "Super1 SQLite schema/quick_check gate failed."
}
if ([string]$release.release_id -cne [string]$signedRelease.release_id) {
    throw "Installed release manifest does not match the signed release archive."
}
foreach ($sealedPath in @(
    "live_forward/super1_xm_mt5_demo_config_v5.json",
    "research_candidates/super1/super1_manifest_v5.json"
)) {
    $entry = @($signedRelease.files | Where-Object { [string]$_.path -ceq $sealedPath })
    $localPath = Get-Super1RuntimeAppPath -RelativePath $sealedPath
    if ($entry.Count -ne 1 -or [string]$entry[0].sha256 -cne (Get-Super1Hash $localPath)) {
        throw "Signed release hash does not match the installed sealed file: $sealedPath"
    }
}
if ([string]$runtime.environment -cne "XM_MT5_DEMO_ORDER" -or
    [string]$runtime.account_mode -cne "DEMO_ORDER" -or
    -not [bool]$runtime.deployment_binding_required -or
    @($runtime.PSObject.Properties.Name | Where-Object { $_ -in @("account_login", "expected_server", "expected_company") }).Count -ne 0 -or
    [bool]$runtime.manual_required -ne $false -or
    -not [bool]$runtime.live_order_approval_required -or
    [string]$runtime.authorized_operator_sid -notmatch '^S-\d-(?:\d+-)+\d+$') {
    throw "Super1 signed config is not an exact XM demo config."
}
$bindingJson = & ([string]$Contract.python) -I -E -B -c @"
import json,sys
sys.path.insert(0, sys.argv[1])
from backtest.live.deployment_binding import load_verified_deployment_binding
b=load_verified_deployment_binding(sys.argv[2],sys.argv[3],sys.argv[4])
print(json.dumps(b.lease_fields(),sort_keys=True))
"@ $app ([string]$runtime.deployment_binding_path) ([string]$runtime.deployment_binding_signature_path) (Get-Super1RuntimeAppPath -RelativePath ([string]$runtime.deployment_binding_public_key_path))
if ($LASTEXITCODE -ne 0) { throw "Private signed deployment binding verification failed." }
$privateBinding = ($bindingJson -join "`n") | ConvertFrom-Json
if ([string]$manifest.deployment.target -cne "LOCAL_WINDOWS_PC" -or
    -not [bool]$manifest.deployment.isolation_required -or
    -not [bool]$manifest.deployment.demo_order_execution_enabled -or
    [bool]$manifest.deployment.real_money_live_enabled -or
    [bool]$manifest.deployment.real_money_execution_allowed -or
    -not [bool]$manifest.deployment.existing_campaign_must_remain_untouched -or
    [bool]$manifest.deployment.unattended_execution_allowed -or
    -not [bool]$manifest.deployment.daily_manual_start_required) {
    throw "Super1 release manifest is not local/manual/demo-only."
}
$terminalPin = Join-Path ([string]$Contract.runtime_trust) "terminal_runtime_pin.json"
$powershellPin = Join-Path ([string]$Contract.runtime_trust) "powershell_runtime_pin.json"
foreach ($pinPath in @($terminalPin, $powershellPin)) {
    if (-not (Test-Path -LiteralPath $pinPath -PathType Leaf)) { throw "Super1 runtime pin is missing: $pinPath" }
}
$terminalPinPayload = Get-Content -Raw -LiteralPath $terminalPin | ConvertFrom-Json
if ([IO.Path]::GetFullPath([string]$terminalPinPayload.terminal_path) -cne [IO.Path]::GetFullPath([string]$Contract.terminal) -or
    [string]$terminalPinPayload.terminal_sha256 -cne (Get-Super1Hash ([string]$Contract.terminal))) {
    throw "Super1 terminal runtime pin does not match the contract terminal."
}
if ([string]$runtime.rth_session_calendar.sha256 -cne (Get-Super1Hash $calendarPath)) {
    throw "Super1 RTH calendar hash differs from signed config."
}
. (Join-Path $PSScriptRoot "super1_binding_proof.ps1")
$bindingProof = Invoke-Super1BindingProof `
    -RuntimeContract $Contract `
    -Root $root `
    -App $app `
    -State $state
if (-not [bool]$bindingProof.ready) {
    throw "Super1 exact demo binding/permission/flat preflight did not pass."
}
$zone = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$now = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $zone)
$dateKey = $now.ToString("yyyy-MM-dd")
$session = @($calendar.sessions | Where-Object { [string]$_.date -ceq $dateKey })
if ($session.Count -ne 1 -or [string]$session[0].state -ceq "CLOSED") {
    throw "Super1 cannot start: New York trade date is not an open covered session."
}
$sessionEnd = [TimeSpan]::ParseExact([string]$session[0].end, "hh\:mm", $null)
$startWindow = [TimeSpan]::ParseExact("09:20", "hh\:mm", $null)
$orderWindow = [TimeSpan]::ParseExact("09:30", "hh\:mm", $null)
$stopWindow = [TimeSpan]::ParseExact("13:00", "hh\:mm", $null)
if ($now.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday) -or
    $now.TimeOfDay -lt $startWindow -or
    $now.TimeOfDay -ge $stopWindow -or
    $now.TimeOfDay -ge $sessionEnd) {
    throw "Super1 start is outside the signed New York session window."
}

$runnerSid = ([Security.Principal.NTAccount]::new("$env:COMPUTERNAME\$($Contract.runner_account)")).Translate(
    [Security.Principal.SecurityIdentifier]
).Value
if ([string]$boundRunnerSid -cne [string]$runnerSid) {
    throw "Super1 task runner SID does not match the contract Runner account."
}
if ([string]$runtime.authorized_operator_sid -ceq [string]$runnerSid) {
    throw "Signed authorized operator SID must differ from the Runner SID."
}
if (-not (Test-Path -LiteralPath $control -PathType Container)) {
    New-Item -ItemType Directory -Path $control | Out-Null
    & icacls.exe $control /inheritance:r /grant:r "SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)" "${runnerSid}:(OI)(CI)(RX)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect Super1 control directory." }
}
$mutex = [Threading.Mutex]::new($false, [string]$Contract.order_mutex)
$held = $false
try {
    $held = $mutex.WaitOne(30000)
    if (-not $held) { throw "Could not acquire Super1 order transport mutex." }
    if (Test-Path -LiteralPath $leasePath -PathType Leaf) {
        try {
            $existing = Get-Content -Raw -LiteralPath $leasePath | ConvertFrom-Json
            if ([string]$existing.state -ceq "ACTIVE") { throw "An active Super1 manual lease already exists." }
        }
        catch [System.Management.Automation.RuntimeException] { throw }
        catch { }
    }
    $lockPath = Join-Path $state "campaign_lock.json"
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw "Fresh campaign lock is missing." }
    $lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$lock.campaign_id)) { throw "Campaign lock has no unique campaign_id." }
    $expiryLocal = [DateTimeOffset]::new($now.Date.Add($sessionEnd), $now.Offset)
    $hardStopLocal = [DateTimeOffset]::new($now.Date.Add($stopWindow), $now.Offset)
    if ($hardStopLocal -lt $expiryLocal) { $expiryLocal = $hardStopLocal }
    $orderNotBeforeLocal = [DateTimeOffset]::new($now.Date.Add($orderWindow), $now.Offset)
    $lease = [ordered]@{
        schema_version = 1
        lease_id = [Guid]::NewGuid().ToString()
        invocation_nonce = [Guid]::NewGuid().ToString()
        campaign_id = [string]$lock.campaign_id
        trade_date_ny = $dateKey
        issued_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        not_before_utc = [DateTimeOffset]::new($now.Date.Add($startWindow), $now.Offset).ToUniversalTime().ToString("o")
        order_not_before_utc = $orderNotBeforeLocal.ToUniversalTime().ToString("o")
        expires_at_utc = $expiryLocal.ToUniversalTime().ToString("o")
        release_id = [string]$release.release_id
        release_manifest_sha256 = Get-Super1Hash $releaseManifestPath
        app_manifest_sha256 = Get-Super1Hash $manifestPath
        config_sha256 = Get-Super1Hash $configPath
        candidate_sha256 = [string]$runtime.candidate_file_sha256
        harness_sha256 = [string]$lock.harness_hash
        runner_sid = [string]$runnerSid
        authorized_operator_sid = [string]$runtime.authorized_operator_sid
        machine_binding = $env:COMPUTERNAME.ToUpperInvariant()
        account_binding_sha256 = [string]$privateBinding.account_binding_sha256
        account_binding_signature_sha256 = [string]$privateBinding.account_binding_signature_sha256
        binding_id = [string]$privateBinding.binding_id
        magic_number = [int]$runtime.magic_number
        mode = "DEMO_ORDER"
        state = "ACTIVE"
        revoked_at_utc = $null
    }
    Write-Super1AtomicJson -Path $leasePath -Value $lease
    foreach ($taskName in @([string]$Contract.main_task, [string]$Contract.watchdog_task)) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        if ([string]$task.State -ne "Running") { Start-ScheduledTask -TaskName $taskName -ErrorAction Stop }
    }
}
catch {
    try { Revoke-Super1Lease -LeasePath $leasePath -Reason $_.Exception.Message } catch { }
    throw
}
finally {
    if ($held) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}

$deadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
do {
    $mainHealthPath = [string]$Contract.health
    $watchdogHealthPath = [string]$Contract.watchdog_status
    $mainFresh = $false; $watchdogFresh = $false
    if (Test-Path -LiteralPath $mainHealthPath -PathType Leaf) {
        try {
            $health = Get-Content -Raw -LiteralPath $mainHealthPath | ConvertFrom-Json
            $healthStateOk = [string]$health.state -eq "RUNNING" -or [string]$health.state -eq "READY_WAITING_WINDOW"
            $mainFresh = $healthStateOk -and
                ([DateTimeOffset]::Parse([string]$health.updated_at).ToUniversalTime() -gt [DateTimeOffset]::Parse($lease.issued_at_utc).ToUniversalTime()) -and
                [string]$health.lease_id -ceq [string]$lease.lease_id -and
                [string]$health.invocation_nonce -ceq [string]$lease.invocation_nonce -and
                [string]$health.runner_sid -ceq [string]$runnerSid
            if ($mainFresh) {
                $mainPid = [int]$health.process_id
                $process = Get-CimInstance Win32_Process -Filter "ProcessId = $mainPid" -ErrorAction Stop
                $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
                $ownerSid = (New-Object Security.Principal.NTAccount("$($owner.Domain)\$($owner.User)")).Translate([Security.Principal.SecurityIdentifier]).Value
                $mainFresh = [string]$process.ExecutablePath -ceq [string]$Contract.python -and
                    $ownerSid -ceq [string]$runnerSid -and
                    [string]$process.CommandLine -match [regex]::Escape((Get-Super1RuntimeAppPath -RelativePath "scripts\run_super1_xm_mt5_forward.py"))
            }
        } catch { $mainFresh = $false }
    }
    if (Test-Path -LiteralPath $watchdogHealthPath -PathType Leaf) {
        try {
            $wd = Get-Content -Raw -LiteralPath $watchdogHealthPath | ConvertFrom-Json
            $watchdogFresh = [string]$wd.state -ceq "HEALTHY" -and
                ([DateTimeOffset]::Parse([string]$wd.updated_at_utc).ToUniversalTime() -gt [DateTimeOffset]::Parse($lease.issued_at_utc).ToUniversalTime()) -and
                [string]$wd.lease_id -ceq [string]$lease.lease_id
        } catch { $watchdogFresh = $false }
    }
    $smokeFresh = $false
    $activeRequest = Join-Path $control "active.json"
    if (Test-Path -LiteralPath $activeRequest -PathType Leaf) {
        try {
            $request = Get-Content -Raw -LiteralPath $activeRequest | ConvertFrom-Json
            $smokeFresh = [string]$request.kind -ceq "smoke" -and
                (Test-Path -LiteralPath ([string]$request.result_path) -PathType Leaf) -and
                $watchdogFresh
        }
        catch { $smokeFresh = $false }
    }
    if (($mainFresh -and $watchdogFresh) -or $smokeFresh) {
        Write-Host "Super1 started with fresh main/watchdog health." -ForegroundColor Green
        exit 0
    }
    Start-Sleep -Seconds 2
} while ([DateTimeOffset]::UtcNow -lt $deadline)

$mutex = [Threading.Mutex]::new($false, [string]$Contract.order_mutex)
try {
    if ($mutex.WaitOne(30000)) {
        Revoke-Super1Lease -LeasePath $leasePath -Reason "fresh health timeout"
        Write-Super1StopRequest -Path (Join-Path $control "stop-request.json") `
            -LeaseId ([string]$lease.lease_id) `
            -LeaseSha256 (Get-Super1Hash $leasePath) `
            -InvocationNonce ([string]$lease.invocation_nonce) `
            -Reason "START_HEALTH_TIMEOUT"
        [void]$mutex.ReleaseMutex()
    }
}
finally { $mutex.Dispose() }
throw "Super1 start did not produce fresh main and watchdog health within 90 seconds."
