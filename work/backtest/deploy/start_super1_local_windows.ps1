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
$releaseManifestPath = Join-Path $root "super1-forward.manifest.json"
$required = @(
    [string]$Contract.terminal,
    [string]$Contract.python,
    $app,
    [string]$Contract.runtime_trust,
    $configPath,
    $manifestPath,
    $calendarPath,
    $releaseManifestPath
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Super1 start dependency is missing: $path" }
    if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 start dependency is a reparse point: $path"
    }
}
$freeDrive = [IO.Path]::GetPathRoot($root).TrimEnd('\').TrimEnd(':')
$free = (Get-PSDrive -Name $freeDrive -ErrorAction Stop).Free
if ($free -lt 1GB) { throw "Super1 state volume has less than 1 GiB free." }
if (Test-Path -LiteralPath (Join-Path $state "fatal_latch.json") -PathType Leaf) {
    throw "Super1 fatal latch is set; repair/recovery is required before start."
}
if (Test-Path -LiteralPath (Join-Path $state "runtime\broker_recovery_required.json") -PathType Leaf) {
    throw "Super1 broker recovery latch is set; start is fail-closed."
}

$runtime = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$calendar = Get-Content -Raw -LiteralPath $calendarPath | ConvertFrom-Json
$release = Get-Content -Raw -LiteralPath $releaseManifestPath | ConvertFrom-Json
$integrityPath = Join-Path $app "deploy\release_integrity.ps1"
. $integrityPath
$signedRelease = Assert-SignedReleaseArchive `
    -Archive (Join-Path $root "super1-forward.zip") `
    -ExpectedProfile "super1" `
    -RequireProvenance
if ([string]$release.release_id -cne [string]$signedRelease.release_id) {
    throw "Installed release manifest does not match the signed release archive."
}
foreach ($sealedPath in @(
    "live_forward/super1_xm_mt5_demo_config.json",
    "research_candidates/super1/super1_manifest.json"
)) {
    $entry = @($signedRelease.files | Where-Object { [string]$_.path -ceq $sealedPath })
    $localPath = Get-Super1RuntimeAppPath -RelativePath $sealedPath
    if ($entry.Count -ne 1 -or [string]$entry[0].sha256 -cne (Get-Super1Hash $localPath)) {
        throw "Signed release hash does not match the installed sealed file: $sealedPath"
    }
}
if ([string]$runtime.environment -cne "XM_MT5_DEMO_ORDER" -or
    [string]$runtime.account_mode -cne "DEMO_ORDER" -or
    [string]$runtime.expected_server -eq "" -or
    [string]$runtime.expected_company -eq "" -or
    [bool]$runtime.manual_required -ne $false) {
    throw "Super1 signed config is not an exact XM demo config."
}
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
        campaign_id = [string]$lock.campaign_id
        trade_date_ny = $dateKey
        issued_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        not_before_utc = [DateTimeOffset]::new($now.Date.Add($startWindow), $now.Offset).ToUniversalTime().ToString("o")
        order_not_before_utc = $orderNotBeforeLocal.ToUniversalTime().ToString("o")
        expires_at_utc = $expiryLocal.ToUniversalTime().ToString("o")
        release_id = [string]$release.release_id
        app_manifest_sha256 = Get-Super1Hash $manifestPath
        config_sha256 = Get-Super1Hash $configPath
        candidate_sha256 = [string]$runtime.candidate_file_sha256
        harness_sha256 = [string]$lock.harness_hash
        runner_sid = [string]$runnerSid
        machine_binding = $env:COMPUTERNAME.ToUpperInvariant()
        expected_account_login = [int]$runtime.account_login
        expected_server = [string]$runtime.expected_server
        expected_company = [string]$runtime.expected_company
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
    $mainHealthPath = Join-Path $state "health.json"
    $watchdogHealthPath = Join-Path $root "watchdog_status.json"
    $mainFresh = $false; $watchdogFresh = $false
    if (Test-Path -LiteralPath $mainHealthPath -PathType Leaf) {
        try {
            $health = Get-Content -Raw -LiteralPath $mainHealthPath | ConvertFrom-Json
            $mainFresh = ([DateTimeOffset]::Parse([string]$health.updated_at).ToUniversalTime() -gt [DateTimeOffset]::Parse($lease.issued_at_utc).ToUniversalTime()) -and [string]$health.lease_id -ceq [string]$lease.lease_id
        } catch { $mainFresh = $false }
    }
    if (Test-Path -LiteralPath $watchdogHealthPath -PathType Leaf) {
        try {
            $wd = Get-Content -Raw -LiteralPath $watchdogHealthPath | ConvertFrom-Json
            $watchdogFresh = ([DateTimeOffset]::Parse([string]$wd.updated_at_utc).ToUniversalTime() -gt [DateTimeOffset]::Parse($lease.issued_at_utc).ToUniversalTime()) -and [string]$wd.lease_id -ceq [string]$lease.lease_id
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
try { if ($mutex.WaitOne(30000)) { Revoke-Super1Lease -LeasePath $leasePath -Reason "fresh health timeout"; [void]$mutex.ReleaseMutex() } }
finally { $mutex.Dispose() }
throw "Super1 start did not produce fresh main and watchdog health within 90 seconds."
