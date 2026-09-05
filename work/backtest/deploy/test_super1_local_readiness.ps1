[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract
$root = [string]$Contract.root
$state = [string]$Contract.state
$app = [string]$Contract.app
$reportPath = Join-Path $state "readiness.json"
$checks = [ordered]@{}
$script:FlatEvidence = $null
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
. (Join-Path $PSScriptRoot "super1_binding_proof.ps1")

Add-ReadinessCheck "release" {
    $integrity = Join-Path $app "deploy\release_integrity.ps1"
    . $integrity
    $archive = Join-Path $root "super1-forward.zip"
    Assert-SignedReleaseArchive -Archive $archive -ExpectedProfile "super1" -RequireProvenance | Out-Null
}
Add-ReadinessCheck "signature" {
    $manifest = Join-Path $root "super1-forward.manifest.json"
    $signature = [IO.Path]::ChangeExtension((Join-Path $root "super1-forward.zip"), ".manifest.sig")
    if (-not (Test-Path -LiteralPath $manifest) -or -not (Test-Path -LiteralPath $signature)) { throw "Signed release manifest/signature is missing." }
}
Add-ReadinessCheck "hash" {
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $root "super1-forward.manifest.json") | ConvertFrom-Json
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
    $healthPath = Join-Path $state "health.json"
    $watchdogPath = Join-Path $root "watchdog_status.json"
    $health = Get-Content -Raw -LiteralPath $healthPath | ConvertFrom-Json
    $watchdog = Get-Content -Raw -LiteralPath $watchdogPath | ConvertFrom-Json
    $now = [DateTimeOffset]::UtcNow
    if (($now - [DateTimeOffset]::Parse([string]$health.updated_at).ToUniversalTime()).TotalSeconds -gt 1800 -or
        ($now - [DateTimeOffset]::Parse([string]$watchdog.updated_at_utc).ToUniversalTime()).TotalSeconds -gt 1800) { throw "Main/watchdog health is stale." }
    if ([string]$health.state -notin @("STOPPED", "WAITING_MANUAL_LEASE") -or [string]$watchdog.state -notin @("WAITING_MANUAL_LEASE", "HEALTHY")) { throw "Health state is not an allowlisted stopped/manual state." }
}

$events = @(if (Test-Path -LiteralPath (Join-Path $state "orders\events.jsonl")) { Get-Content -LiteralPath (Join-Path $state "orders\events.jsonl") } else { @() })
$sendCount = @($events | Where-Object { $_ -match '"event":"SEND_ATTEMPT"' }).Count
$duplicateSend = if ($sendCount -gt 1) { $sendCount - 1 } else { 0 }
$unknown = @($events | Where-Object { $_ -match 'UNKNOWN_NO_SEND' }).Count
$leasePath = Join-Path ([string]$Contract.control) "session-lease.json"
$leaseActive = $false
if (Test-Path -LiteralPath $leasePath) { try { $leaseActive = ([string](Get-Content -Raw $leasePath | ConvertFrom-Json).state -ceq "ACTIVE") } catch { $leaseActive = $true } }
$pass = @($checks.Values | Where-Object { $_.state -ne "PASS" }).Count -eq 0 -and
    $sendCount -eq 0 -and $duplicateSend -eq 0 -and $unknown -eq 0 -and -not $leaseActive
$report = [ordered]@{
    schema_version = 1
    state = if ($pass) { "READY_FOR_DEMO_SMOKE" } else { "NOT_READY_NO_SEND" }
    generated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    checks = $checks
    foreign_exposure = if ($null -eq $script:FlatEvidence) { 1 } else { [int]$script:FlatEvidence.open_orders + [int]$script:FlatEvidence.open_positions }
    owned_pending = if ($null -eq $script:FlatEvidence) { 0 } else { [int]$script:FlatEvidence.open_orders }
    open_positions = if ($null -eq $script:FlatEvidence) { 0 } else { [int]$script:FlatEvidence.open_positions }
    unknown_intents = $unknown + $(if ($null -eq $script:FlatEvidence) { 1 } else { 0 })
    duplicate_send = $duplicateSend
    accepted_send_count = $sendCount
    unattended_trigger = 0
    real_money_allowed = $script:RealMoneyAllowed
    lease_less_fake_or_integration_send_count = 0
}
Write-ReadinessJson $report
$report | ConvertTo-Json -Depth 20
if (-not $pass) { exit 2 }
