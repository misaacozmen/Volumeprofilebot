[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract
$root = [string]$Contract.root
$state = [string]$Contract.state
$app = [string]$Contract.app
$releaseArchive = [string]$Contract.release_archive
$releaseManifest = [string]$Contract.release_manifest
$releaseSignature = [string]$Contract.release_signature
$watchdogStatus = [string]$Contract.watchdog_status
$reportPath = Join-Path $state "readiness.json"
$checks = [ordered]@{}
$script:FlatEvidence = $null
$script:BindingEvidence = $null
$script:RealMoneyAllowed = $true

function Add-ReadinessCheck([string]$Name, [scriptblock]$Check) {
    try {
        & $Check
        $checks[$Name] = [ordered]@{ state = "PASS" }
    }
    catch {
        $checks[$Name] = [ordered]@{ state = "FAIL"; error = [string]$_.Exception.Message }
    }
}
function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Write-ReadinessJson([object]$Payload) {
    New-Item -ItemType Directory -Force -Path $state | Out-Null
    $temporary = "$reportPath.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = ConvertTo-Json -InputObject $Payload -Depth 20
        [IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $reportPath -Force
    }
    finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
. (Join-Path $PSScriptRoot "super1_secure_task.ps1")

function Invoke-Super1BindingReadiness {
    $leasePath = Join-Path ([string]$Contract.control) "session-lease.json"
    if (Test-Path -LiteralPath $leasePath -PathType Leaf) {
        $lease = Get-Content -Raw -LiteralPath $leasePath | ConvertFrom-Json
        if ([string]$lease.state -ceq "ACTIVE") { throw "Readiness requires no active manual lease." }
    }
    $runnerSid = Get-Super1SecurePrincipalSid -Identity "$env:COMPUTERNAME\$([string]$Contract.runner_account)"
    $transactionId = [Guid]::NewGuid().ToString("N")
    $nonce = [Guid]::NewGuid().ToString("N")
    $transaction = [IO.Path]::GetFullPath((Join-Path (Join-Path $root "archive") ("readiness-" + $transactionId)))
    $output = Join-Path $transaction "output"
    $resultPath = Join-Path $output "result.json"
    $producerPath = Join-Path $output "producer.json"
    $requestPath = Join-Path $transaction "request.json"
    New-Super1SecureDirectory -Path $transaction -RunnerSid $runnerSid | Out-Null
    New-Super1SecureDirectory -Path $output -RunnerSid $runnerSid -RunnerRights ([Security.AccessControl.FileSystemRights]::Modify) | Out-Null
    $launcher = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.launcher)
    $launcherSha256 = Get-Super1SecureSha256 -Path $launcher
    $requestedAt = [DateTimeOffset]::UtcNow
    $request = [ordered]@{
        schema_version = 1
        kind = "binding_readiness"
        transaction_id = $transactionId
        nonce = $nonce
        requested_at_utc = $requestedAt.ToString("o")
        expected_runner_sid = $runnerSid
        expected_launcher_sha256 = $launcherSha256
        request_path = $requestPath
        result_path = $resultPath
        producer_path = $producerPath
    } | ConvertTo-Json -Compress
    $requestLock = New-Super1SecureLockedFile -Path $requestPath -Content $request -RunnerSid $runnerSid
    $activeLock = $null
    $watchdog = $null
    try {
        $activeLock = New-Super1SecureLockedFile -Path (Join-Path ([string]$Contract.control) "active.json") -Content $request -RunnerSid $runnerSid
        Start-ScheduledTask -TaskName ([string]$Contract.main_task) -ErrorAction Stop
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
        do {
            if (Test-Path -LiteralPath $producerPath -PathType Leaf) { break }
            Start-Sleep -Milliseconds 500
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        $binding = Get-Super1SecureProducerEnvelope `
            -ProducerPath $producerPath -RequestPath $requestPath -ResultPath $resultPath `
            -TransactionId $transactionId -Nonce $nonce -Kind "binding_readiness" `
            -RunnerSid $runnerSid -ExpectedLauncherPath $launcher `
            -ExpectedLauncherSha256 $launcherSha256 -NotBefore $requestedAt -ExpectedExitCode 0
        $result = $binding.result_payload
        $transport = $result.order_transport_preflight
        $sendCalled = $transport.PSObject.Properties.Name -contains "order_send_called" -and [bool]$transport.order_send_called
        $checkCalled = $transport.PSObject.Properties.Name -contains "order_check_called" -and [bool]$transport.order_check_called
        if ($sendCalled -or $checkCalled) {
            throw "Binding readiness used a forbidden order transport operation."
        }
        Start-ScheduledTask -TaskName ([string]$Contract.watchdog_task) -ErrorAction Stop
        $watchdogDeadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
        do {
            if (Test-Path -LiteralPath $watchdogStatus -PathType Leaf) {
                try {
                    $watchdog = Get-Content -Raw -LiteralPath $watchdogStatus | ConvertFrom-Json
                    if ([string]$watchdog.state -ceq "WAITING_MANUAL_LEASE" -and [string]$watchdog.lease_id -eq "") { break }
                } catch { }
            }
            Start-Sleep -Milliseconds 500
        } while ([DateTimeOffset]::UtcNow -lt $watchdogDeadline)
        if ($null -eq $watchdog -or [string]$watchdog.state -cne "WAITING_MANUAL_LEASE" -or [string]$watchdog.lease_id -ne "") {
            throw "Watchdog did not publish a fresh lease-less WAITING_MANUAL_LEASE record."
        }
        Stop-ScheduledTask -TaskName ([string]$Contract.watchdog_task) -ErrorAction SilentlyContinue
        foreach ($taskName in @([string]$Contract.main_task, [string]$Contract.watchdog_task)) {
            if ([string](Get-ScheduledTask -TaskName $taskName -ErrorAction Stop).State -ne "Ready") {
                throw "Readiness task did not return to Ready: $taskName"
            }
        }
        if ([int]$result.open_orders -ne 0 -or [int]$result.open_positions -ne 0) {
            throw "Binding readiness did not prove flat broker exposure."
        }
        return [ordered]@{
            transaction_id = $transactionId
            nonce = $nonce
            producer = $binding
            watchdog_state = [string]$watchdog.state
            lease_active = $false
        }
    }
    finally {
        if ($activeLock) { $activeLock.Dispose() }
        $requestLock.Dispose()
        $activePath = Join-Path ([string]$Contract.control) "active.json"
        if (Test-Path -LiteralPath $activePath -PathType Leaf) { Remove-Item -LiteralPath $activePath -Force }
    }
}
. (Join-Path $PSScriptRoot "super1_binding_proof.ps1")

Add-ReadinessCheck "release" {
    $integrity = Join-Path $app "deploy\release_integrity.ps1"
    . $integrity
    Assert-SignedReleaseArchive -Archive $releaseArchive -ExpectedProfile "super1" -RequireProvenance | Out-Null
}
Add-ReadinessCheck "signature" {
    $manifest = $releaseManifest
    $signature = $releaseSignature
    if (-not (Test-Path -LiteralPath $manifest) -or -not (Test-Path -LiteralPath $signature)) { throw "Signed release manifest/signature is missing." }
}
Add-ReadinessCheck "hash" {
    $manifest = Get-Content -Raw -LiteralPath $releaseManifest | ConvertFrom-Json
    if ([string]$manifest.profile -cne "super1" -or [string]$manifest.release_id -eq "") { throw "Installed release manifest is not the Super1 profile." }
    $config = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.runtime_config)
    $candidate = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.manifest)
    if ([string]$manifest.git_dirty -ieq "true" -or (Get-Sha256 $config).Length -ne 64 -or (Get-Sha256 $candidate).Length -ne 64) { throw "Installed release hash evidence is incomplete." }
}
Add-ReadinessCheck "ACL" {
    $helper = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.secure_task_helper)
    . $helper
    $runnerSid = Get-Super1SecurePrincipalSid -Identity "$env:COMPUTERNAME\$([string]$Contract.runner_account)"
    Assert-Super1SecureDirectoryAcl -Path ([string]$Contract.runtime_trust) -RunnerSid $runnerSid
    Assert-Super1SecureDirectoryAcl -Path ([string]$Contract.control) -RunnerSid $runnerSid
}
Add-ReadinessCheck "pins" {
    $trust = [string]$Contract.runtime_trust
    foreach ($name in @("terminal_runtime_pin.json", "powershell_runtime_pin.json")) {
        $path = Join-Path $trust $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Runtime pin is missing: $name" }
        $pin = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
        if ([string]$pin.sha256 -notmatch '^[a-f0-9]{64}$' -and [string]$pin.terminal_sha256 -notmatch '^[a-f0-9]{64}$' -and [string]$pin.powershell_sha256 -notmatch '^[a-f0-9]{64}$') { throw "Runtime pin hash is invalid: $name" }
    }
}
Add-ReadinessCheck "task_contract" {
    $helper = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.secure_task_helper)
    . $helper
    $runnerSid = Assert-Super1SecureTaskBindings -Root $root -MainTask ([string]$Contract.main_task) -WatchdogTask ([string]$Contract.watchdog_task)
    foreach ($taskName in @([string]$Contract.main_task, [string]$Contract.watchdog_task)) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        if (@($task.Triggers).Count -ne 0) { throw "$taskName has an unattended trigger." }
    }
}
Add-ReadinessCheck "real_money_policy" {
    $config = Get-Content -Raw -LiteralPath (Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.runtime_config)) | ConvertFrom-Json
    $manifest = Get-Content -Raw -LiteralPath (Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.manifest)) | ConvertFrom-Json
    if ([bool]$config.real_money_execution_allowed -or [bool]$manifest.real_money_execution_allowed -or
        [bool]$manifest.real_money_live_enabled -or [string]$config.environment -cne "XM_MT5_DEMO_ORDER") {
        throw "Super1 real-money policy is not fail-closed."
    }
    $script:RealMoneyAllowed = $false
}
Add-ReadinessCheck "binding_readiness_task" {
    $script:BindingEvidence = Invoke-Super1BindingReadiness
    if ([string]$script:BindingEvidence.watchdog_state -cne "WAITING_MANUAL_LEASE" -or
        [bool]$script:BindingEvidence.lease_active) { throw "Lease-less binding readiness proof is incomplete." }
}
Add-ReadinessCheck "demo_binding_permissions" {
    $script:FlatEvidence = Invoke-Super1BindingProof -RuntimeContract $Contract -Root $root -App $app -State $state
    if (-not [bool]$script:FlatEvidence.ready) { throw "Exact XM demo binding or permission proof is not ready." }
    $requiredChecks = @(
        "login", "server", "company", "demo", "account_trade_allowed",
        "account_trade_expert", "terminal_connected", "terminal_trade_allowed",
        "terminal_tradeapi_enabled"
    )
    $allChecks = @{}
    foreach ($property in @($script:FlatEvidence.identity_checks.PSObject.Properties)) {
        $allChecks[[string]$property.Name] = [bool]$property.Value
    }
    foreach ($property in @($script:FlatEvidence.permission_checks.PSObject.Properties)) {
        $allChecks[[string]$property.Name] = [bool]$property.Value
    }
    foreach ($name in $requiredChecks) {
        if (-not $allChecks.ContainsKey($name) -or -not [bool]$allChecks[$name]) {
            throw "Exact XM demo binding permission proof failed: $name"
        }
    }
}
Add-ReadinessCheck "flat_exposure" {
    if ($null -eq $script:FlatEvidence) { throw "Flat exposure proof is unavailable." }
    if ([int]$script:FlatEvidence.open_orders -ne 0 -or [int]$script:FlatEvidence.open_positions -ne 0) {
        throw "Dedicated XM demo account is not flat."
    }
}
Add-ReadinessCheck "runtime_import" {
    $python = [string]$Contract.python
    & $python -I -E -B -c "import MetaTrader5, pandas, sqlite3; print('runtime import PASS')" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Runtime import self-test failed." }
}
Add-ReadinessCheck "db" {
    $python = [string]$Contract.python
    $db = Join-Path $state "orders\idempotency.sqlite3"
    if (-not (Test-Path -LiteralPath $db -PathType Leaf)) { throw "Order ledger database is missing." }
    & $python -I -E -B -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'; c.close()" $db
    if ($LASTEXITCODE -ne 0) { throw "SQLite quick_check failed." }
}
Add-ReadinessCheck "calendar" {
    $config = Get-Content -Raw -LiteralPath (Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.runtime_config)) | ConvertFrom-Json
    $calendar = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.calendar)
    if ([string]$config.rth_session_calendar.sha256 -cne (Get-Sha256 $calendar)) { throw "RTH calendar hash mismatch." }
    $payload = Get-Content -Raw -LiteralPath $calendar | ConvertFrom-Json
    if ([string]$payload.timezone -cne "America/New_York" -or [string]$payload.coverage.start -gt "2026-01-01" -or [string]$payload.coverage.end -lt "2026-12-31") { throw "RTH calendar coverage is invalid." }
}
Add-ReadinessCheck "main_watchdog_fresh_health" {
    $healthPath = [string]$Contract.health
    $watchdogPath = $watchdogStatus
    $health = Get-Content -Raw -LiteralPath $healthPath | ConvertFrom-Json
    $watchdog = Get-Content -Raw -LiteralPath $watchdogPath | ConvertFrom-Json
    $now = [DateTimeOffset]::UtcNow
    if (($now - [DateTimeOffset]::Parse([string]$health.updated_at).ToUniversalTime()).TotalSeconds -gt 1800 -or
        ($now - [DateTimeOffset]::Parse([string]$watchdog.updated_at_utc).ToUniversalTime()).TotalSeconds -gt 1800) { throw "Main/watchdog health is stale." }
    if ([string]$health.state -notin @("STOPPED", "WAITING_MANUAL_LEASE") -or [string]$watchdog.state -ne "WAITING_MANUAL_LEASE") { throw "Health state is not an allowlisted stopped/manual state." }
    if ([string]$health.state -in @("CRITICAL_STOP", "UNSAFE_STOP_NO_SEND", "UNKNOWN_NO_SEND") -or
        [string]$watchdog.state -in @("WATCHDOG_ERROR", "ALARM_CRITICAL", "UNKNOWN_NO_SEND")) { throw "Critical or unknown health cannot satisfy readiness." }
}

$dbPath = Join-Path $state "orders\idempotency.sqlite3"
$ledger = $null
if (Test-Path -LiteralPath $dbPath -PathType Leaf) {
    try {
        $python = [string]$Contract.python
        $ledgerJson = & $python -I -E -B -c @'
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
def count(sql):
    return int(db.execute(sql).fetchone()[0] or 0)
total = count("SELECT COUNT(*) FROM order_event_outbox")
distinct = count("SELECT COUNT(DISTINCT event_id) FROM order_event_outbox")
print(json.dumps({
    "send_armed": count("SELECT COUNT(*) FROM order_intents WHERE status='SEND_ARMED'"),
    "accepted": count("SELECT COUNT(*) FROM order_intents WHERE status='SUBMITTED' AND broker_ticket IS NOT NULL"),
    "unknown": count("SELECT COUNT(*) FROM order_intents WHERE status='SEND_UNKNOWN'"),
    "duplicate": total - distinct,
    "outbox_total": total,
    "outbox_distinct": distinct,
    "intent_total": count("SELECT COUNT(*) FROM order_intents")
}))
db.close()
'@ $dbPath
        $ledger = ($ledgerJson -join "`n") | ConvertFrom-Json
    }
    catch { $ledger = $null }
}
$sendArmed = if ($null -eq $ledger) { 1 } else { [int]$ledger.send_armed }
$acceptedSendCount = if ($null -eq $ledger) { 1 } else { [int]$ledger.accepted }
$unknown = if ($null -eq $ledger) { 1 } else { [int]$ledger.unknown }
$duplicateSend = if ($null -eq $ledger) { 1 } else { [int]$ledger.duplicate }
$triggerCount = 0
try {
    foreach ($taskName in @([string]$Contract.main_task, [string]$Contract.watchdog_task)) {
        $triggerCount += @((Get-ScheduledTask -TaskName $taskName -ErrorAction Stop).Triggers).Count
    }
}
catch { $triggerCount = 1 }
$leasePath = Join-Path ([string]$Contract.control) "session-lease.json"
$leaseActive = $false
if (Test-Path -LiteralPath $leasePath) { try { $leaseActive = ([string](Get-Content -Raw $leasePath | ConvertFrom-Json).state -ceq "ACTIVE") } catch { $leaseActive = $true } }
$pass = @($checks.Values | Where-Object { $_.state -ne "PASS" }).Count -eq 0 -and
    $sendArmed -eq 0 -and $acceptedSendCount -eq 0 -and $duplicateSend -eq 0 -and $unknown -eq 0 -and
    $triggerCount -eq 0 -and -not $leaseActive
$report = [ordered]@{
    schema_version = 1
    state = if ($pass) { "READY_FOR_ADMIN_INSTALL" } else { "NOT_READY_NO_SEND" }
    generated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    checks = $checks
    foreign_exposure = if ($null -eq $script:FlatEvidence) { 1 } else { [int]$script:FlatEvidence.open_orders + [int]$script:FlatEvidence.open_positions }
    owned_pending = if ($null -eq $script:FlatEvidence) { 0 } else { [int]$script:FlatEvidence.open_orders }
    open_positions = if ($null -eq $script:FlatEvidence) { 0 } else { [int]$script:FlatEvidence.open_positions }
    unknown_intents = $unknown
    duplicate_send = $duplicateSend
    send_armed_count = $sendArmed
    accepted_send_count = $acceptedSendCount
    unattended_trigger = $triggerCount
    real_money_allowed = $script:RealMoneyAllowed
    lease_less_fake_or_integration_send_count = if ($null -eq $ledger) { 1 } else { $acceptedSendCount + $unknown + $sendArmed }
}
Write-ReadinessJson $report
$report | ConvertTo-Json -Depth 20
if (-not $pass) { exit 2 }
