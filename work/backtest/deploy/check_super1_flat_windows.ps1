[CmdletBinding()]
param(
    [switch]$KeepStopped,
    [switch]$PreserveExistingTerminal
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($PreserveExistingTerminal) {
    throw "Preserving an existing Super1 terminal is forbidden for broker evidence."
}
$OriginalPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$ExpectedPSHome = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0")
)
$CurrentPowerShell = [IO.Path]::GetFullPath(
    [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
)
if (-not [IO.Path]::GetFullPath($PSHOME).Equals(
        $ExpectedPSHome,
        [StringComparison]::OrdinalIgnoreCase
    ) -or
    -not $CurrentPowerShell.Equals(
        [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "powershell.exe")),
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Run the Super1 flat check only with trusted 64-bit Windows PowerShell."
}
$TrustedPSModulePath = [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "Modules"))
$env:PSModulePath = $TrustedPSModulePath
$ScheduledTasksModule = [IO.Path]::GetFullPath(
    (Join-Path $TrustedPSModulePath "ScheduledTasks\ScheduledTasks.psd1")
)
if (-not (Test-Path -LiteralPath $ScheduledTasksModule -PathType Leaf)) {
    throw "Trusted ScheduledTasks module is missing."
}
Import-Module -Name $ScheduledTasksModule -Force -ErrorAction Stop

$Root = [IO.Path]::GetFullPath("C:\Super1")
$App = [IO.Path]::GetFullPath((Join-Path $Root "app"))
$TrustedScript = [IO.Path]::GetFullPath((Join-Path $App "deploy\check_super1_flat_windows.ps1"))
$CurrentScript = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
if (-not $CurrentScript.Equals($TrustedScript, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run the Super1 flat check only from the protected app deploy directory: $TrustedScript"
}
$SecureHelper = [IO.Path]::GetFullPath((Join-Path $App "deploy\super1_secure_task.ps1"))
if (-not (Test-Path -LiteralPath $SecureHelper -PathType Leaf) -or
    (Get-Item -LiteralPath $SecureHelper -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Protected Super1 secure-task helper is missing or is a reparse point."
}
. $SecureHelper

$ArchiveRoot = [IO.Path]::GetFullPath((Join-Path $Root "archive"))
$ProbeControl = [IO.Path]::GetFullPath((Join-Path $Root "probe-control"))
$ProbeRequest = [IO.Path]::GetFullPath((Join-Path $ProbeControl "active.json"))
$RuntimeConfig = [IO.Path]::GetFullPath((Join-Path $App "live_forward\super1_xm_mt5_demo_config.json"))
$TerminalPin = [IO.Path]::GetFullPath((Join-Path $App "deploy\terminal_runtime_pin.json"))
$Task = "Super1XM"
$WatchdogTask = "Super1Watchdog"
$RunId = [Guid]::NewGuid().ToString("N")
$Nonce = [Guid]::NewGuid().ToString("N")
$Transaction = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot "readiness-$RunId"))
$OutputRoot = [IO.Path]::GetFullPath((Join-Path $Transaction "output"))
$Result = [IO.Path]::GetFullPath((Join-Path $OutputRoot "result.json"))
$Producer = [IO.Path]::GetFullPath((Join-Path $OutputRoot "producer.json"))
$TransactionRequest = [IO.Path]::GetFullPath((Join-Path $Transaction "request.json"))
$Launcher = [IO.Path]::GetFullPath((Join-Path $App "deploy\run_super1_windows.ps1"))

foreach ($required in @($App, $ArchiveRoot, $ProbeControl, $RuntimeConfig, $TerminalPin)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing Super1 flat-check dependency: $required"
    }
    if ((Get-Item -LiteralPath $required -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 flat-check dependency is a reparse point: $required"
    }
}
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "Run the Super1 flat check from an elevated PowerShell."
}

$runnerSid = Assert-Super1SecureTaskBindings `
    -Root $Root `
    -MainTask $Task `
    -WatchdogTask $WatchdogTask
$originalTaskXml = Get-Super1SecureTaskXml -TaskName $Task
$originalWatchdogXml = Get-Super1SecureTaskXml -TaskName $WatchdogTask
$callerSid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($runnerSid -eq $callerSid -or $runnerSid -in @("S-1-5-18", "S-1-5-32-544")) {
    throw "Super1 flat-check runner is not an isolated unprivileged identity."
}
Assert-Super1SecureDirectoryAcl -Path $ArchiveRoot
Assert-Super1SecureDirectoryAcl -Path $ProbeControl -RunnerSid $runnerSid
if (Test-Path -LiteralPath $ProbeRequest) {
    throw "Super1 probe-control already contains an active request."
}

$requestEvidence = $null
$transactionRequestEvidence = $null
$requestHash = $null
$succeeded = $false
$preserveFailedEvidence = $false
$resultEnvelope = $null
$failure = $null
$cleanupErrors = New-Object Collections.Generic.List[string]
try {
    Stop-Super1SecureRuntime `
        -Root $Root `
        -MainTask $Task `
        -WatchdogTask $WatchdogTask
    Assert-Super1SecureStopped `
        -Root $Root `
        -MainTask $Task `
        -WatchdogTask $WatchdogTask
    New-Super1SecureDirectory -Path $Transaction -RunnerSid $runnerSid
    New-Super1SecureDirectory `
        -Path $OutputRoot `
        -RunnerSid $runnerSid `
        -RunnerRights ([Security.AccessControl.FileSystemRights]::Modify)
    $requestedAt = [DateTimeOffset]::UtcNow
    $launcherSha256 = Get-Super1SecureSha256 -Path $Launcher
    $requestJson = [ordered]@{
        schema_version = 1
        kind = "flat"
        transaction_id = $RunId
        nonce = $Nonce
        requested_at_utc = $requestedAt.ToString("o")
        expected_runner_sid = $runnerSid
        expected_launcher_sha256 = $launcherSha256
        request_path = $TransactionRequest
        result_path = $Result
        producer_path = $Producer
    } | ConvertTo-Json -Compress
    $transactionRequestEvidence = New-Super1SecureLockedFile `
        -Path $TransactionRequest `
        -Content $requestJson `
        -RunnerSid $runnerSid
    $requestEvidence = New-Super1SecureLockedFile `
        -Path $ProbeRequest `
        -Content $requestJson `
        -RunnerSid $runnerSid
    $requestHash = [string]$requestEvidence.sha256

    Start-ScheduledTask -TaskName $Task
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
    do {
        Start-Sleep -Milliseconds 500
        $taskState = [string](Get-ScheduledTask -TaskName $Task -ErrorAction Stop).State
    } while ($taskState -in @("Running", "Queued") -and [DateTimeOffset]::UtcNow -lt $deadline)
    if ($taskState -in @("Running", "Queued")) {
        throw "Super1 flat-account check timed out: state=$taskState"
    }
    Stop-Super1SecureRuntime -Root $Root -MainTask $Task -WatchdogTask $WatchdogTask
    if ((Get-Super1SecureTaskXml -TaskName $Task) -cne $originalTaskXml -or
        (Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $originalWatchdogXml) {
        throw "Super1 task XML changed during the fixed-action flat check."
    }
    [void](Assert-Super1SecureTaskBindings `
        -Root $Root `
        -MainTask $Task `
        -WatchdogTask $WatchdogTask)
    $taskResult = [int](Get-ScheduledTaskInfo -TaskName $Task -ErrorAction Stop).LastTaskResult
    if ($taskResult -ne 0 -or -not (Test-Path -LiteralPath $Result -PathType Leaf)) {
        $launcherPhase = "unknown"
        $launcherFailure = Join-Path $Root "state\launcher_failure.json"
        if (Test-Path -LiteralPath $launcherFailure -PathType Leaf) {
            try {
                $launcherDiagnostic = [IO.File]::ReadAllText($launcherFailure) |
                    ConvertFrom-Json
                if ([string]$launcherDiagnostic.phase -match '^[A-Z0-9_]+$') {
                    $launcherPhase = [string]$launcherDiagnostic.phase
                }
            }
            catch { $launcherPhase = "invalid" }
        }
        throw "Super1 flat-account check failed: task=$taskResult result_exists=$(Test-Path -LiteralPath $Result -PathType Leaf) launcher_phase=$launcherPhase"
    }

    $requestEvidence.lock.Dispose()
    $requestEvidence = $null
    $transactionRequestEvidence.lock.Dispose()
    $transactionRequestEvidence = $null
    Remove-Item -LiteralPath $ProbeRequest -Force
    Seal-Super1SecureEvidenceTree -Path $Transaction
    $producerBinding = Get-Super1SecureProducerEnvelope `
        -ProducerPath $Producer `
        -RequestPath $TransactionRequest `
        -ResultPath $Result `
        -TransactionId $RunId `
        -Nonce $Nonce `
        -Kind "flat" `
        -RunnerSid $runnerSid `
        -ExpectedLauncherPath $Launcher `
        -ExpectedLauncherSha256 $launcherSha256 `
        -NotBefore $requestedAt
    $resultHash = Get-Super1SecureSha256 -Path $Result
    $resultLock = [IO.File]::Open(
        $Result,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $payload = [IO.File]::ReadAllText($Result) | ConvertFrom-Json
        if ((Get-Super1SecureSha256 -Path $Result) -cne $resultHash) {
            throw "Sealed Super1 readiness result changed while read-locked."
        }
        $runtime = [IO.File]::ReadAllText($RuntimeConfig) | ConvertFrom-Json
        $pin = [IO.File]::ReadAllText($TerminalPin) | ConvertFrom-Json
        $checked = [DateTimeOffset]::Parse([string]$payload.checked_at_utc).ToUniversalTime()
        $age = [DateTimeOffset]::UtcNow - $checked
        $marketSchedule = Get-Super1SecureMarketScheduleState `
            -CheckedAt $checked `
            -Runtime $runtime
        $identityChecks = @($payload.identity_checks.PSObject.Properties | ForEach-Object { [bool]$_.Value })
        $permissionChecks = @($payload.permission_checks.PSObject.Properties | ForEach-Object { [bool]$_.Value })
        $preflight = $payload.order_transport_preflight
        $preflightChecks = if ($preflight.PSObject.Properties["checks"]) {
            @($preflight.checks)
        } else { @() }
        $preflightRetcode = if ($preflight.PSObject.Properties["retcode"]) {
            [int]$preflight.retcode
        } else { 0 }
        $preflightPass = (
            [string]$preflight.state -ceq "PASS" -and
            @($preflightChecks).Count -ge 4
        )
        $preflightDeferred = (
            ([string]$preflight.state -ceq "NON_TRADING_DAY" -and
                [string]$preflight.reason -ceq "WEEKEND" -and
                [bool]$marketSchedule.is_weekend -and
                [bool]$marketSchedule.scheduled_closed) -or
            ([string]$preflight.state -ceq "WAITING_PREFLIGHT_WINDOW" -and
                [string]$preflight.opens_at -ceq "09:30" -and
                [bool]$marketSchedule.before_preflight_window) -or
            ([string]$preflight.state -ceq "WAITING_ORDER_CHECK" -and
                $preflightRetcode -eq 10018 -and
                [bool]$marketSchedule.scheduled_closed)
        )
        $evidenceRoot = [IO.Path]::GetFullPath([string]$payload.evidence_root)
        $terminalPath = [IO.Path]::GetFullPath([string]$payload.terminal_path)
        $terminalDataPath = [IO.Path]::GetFullPath([string]$payload.terminal_data_path)
        if ([string]$payload.evidence_nonce -cne $Nonce -or
            $age.TotalSeconds -lt -5 -or $age.TotalSeconds -gt 90 -or
            [string]$payload.profile -cne "super1" -or
            -not [bool]$payload.ready -or -not [bool]$payload.flat -or
            -not [bool]$payload.identity_ready -or -not [bool]$payload.transport_ready -or
            $identityChecks.Count -lt 5 -or $permissionChecks.Count -lt 5 -or
            -not [bool]$payload.identity_checks.windows_profile -or
            $identityChecks -contains $false -or $permissionChecks -contains $false -or
            [int]$payload.account_login -ne [int]$runtime.account_login -or
            [string]$payload.server -cne [string]$runtime.expected_server -or
            [string]$payload.company -cne [string]$runtime.expected_company -or
            [int]$payload.open_orders -ne 0 -or [int]$payload.open_positions -ne 0 -or
            [string]$payload.permission.state -cne "READY" -or
            [bool]$payload.transport_preflight_deferred -ne [bool]$preflightDeferred -or
            -not ($preflightPass -or $preflightDeferred) -or
            [bool]$preflight.order_send_called -or
            -not $evidenceRoot.StartsWith(
                ($OutputRoot + [IO.Path]::DirectorySeparatorChar),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            -not $terminalPath.Equals(
                [IO.Path]::GetFullPath([string]$pin.terminal_path),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            -not $terminalDataPath.Equals(
                [IO.Path]::GetFullPath((Split-Path -Parent ([string]$pin.terminal_path))),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$payload.markets.nq.type -cne "INDICES" -or
            [string]$payload.markets.spx.type -cne "INDICES") {
            $safeDiagnostic = [ordered]@{
                age_seconds = [Math]::Round($age.TotalSeconds, 3)
                nonce_match = ([string]$payload.evidence_nonce -ceq $Nonce)
                profile = [string]$payload.profile
                ready = [bool]$payload.ready
                flat = [bool]$payload.flat
                identity_ready = [bool]$payload.identity_ready
                transport_ready = [bool]$payload.transport_ready
                identity_check_count = $identityChecks.Count
                identity_checks_all = -not ($identityChecks -contains $false)
                permission_check_count = $permissionChecks.Count
                permission_checks_all = -not ($permissionChecks -contains $false)
                login_match = ([int]$payload.account_login -eq [int]$runtime.account_login)
                server_match = ([string]$payload.server -ceq [string]$runtime.expected_server)
                company_match = ([string]$payload.company -ceq [string]$runtime.expected_company)
                open_orders = [int]$payload.open_orders
                open_positions = [int]$payload.open_positions
                permission_state = [string]$payload.permission.state
                preflight_state = [string]$preflight.state
                preflight_retcode = $preflightRetcode
                preflight_check_count = @($preflightChecks).Count
                preflight_pass = [bool]$preflightPass
                preflight_deferred = [bool]$preflightDeferred
                payload_deferred = [bool]$payload.transport_preflight_deferred
                order_send_called = [bool]$preflight.order_send_called
                scheduled_closed = [bool]$marketSchedule.scheduled_closed
                evidence_root_match = $evidenceRoot.StartsWith(
                    ($OutputRoot + [IO.Path]::DirectorySeparatorChar),
                    [StringComparison]::OrdinalIgnoreCase
                )
                terminal_path_match = $terminalPath.Equals(
                    [IO.Path]::GetFullPath([string]$pin.terminal_path),
                    [StringComparison]::OrdinalIgnoreCase
                )
                terminal_data_path_match = $terminalDataPath.Equals(
                    [IO.Path]::GetFullPath((Split-Path -Parent ([string]$pin.terminal_path))),
                    [StringComparison]::OrdinalIgnoreCase
                )
                nq_type = [string]$payload.markets.nq.type
                spx_type = [string]$payload.markets.spx.type
            }
            # Keep the already sealed, private evidence transaction for post-failure
            # diagnosis. It contains no credentials and is required to distinguish
            # a broker/readiness mismatch without weakening the fail-closed gate.
            $preserveFailedEvidence = $true
            throw "Super1 broker readiness evidence failed the strict sealed-result gate: $($safeDiagnostic | ConvertTo-Json -Compress)"
        }
        $resultEnvelope = [pscustomobject]@{
            state = "READY_FLAT_SEALED"
            readiness_evidence = $Result
            readiness_sha256 = $resultHash
            evidence_transaction = $Transaction
            checked_at_utc = [string]$payload.checked_at_utc
            account_login = [int]$payload.account_login
            server = [string]$payload.server
            company = [string]$payload.company
            terminal_path = $terminalPath
            terminal_data_path = $terminalDataPath
            open_orders = [int]$payload.open_orders
            open_positions = [int]$payload.open_positions
            task_xml_unchanged = $true
            request_sha256 = $requestHash
            producer_sha256 = [string]$producerBinding.producer_sha256
            producer_process_id = [int]$producerBinding.producer_process_id
        }
    }
    finally { $resultLock.Dispose() }
    $succeeded = $true
}
catch { $failure = $_ }
finally {
    try { Stop-Super1SecureRuntime -Root $Root -MainTask $Task -WatchdogTask $WatchdogTask }
    catch { $cleanupErrors.Add("stopped-state enforcement: $($_.Exception.Message)") }
    if ($requestEvidence -and $requestEvidence.lock) {
        try { $requestEvidence.lock.Dispose() }
        catch { $cleanupErrors.Add("request read-lock cleanup: $($_.Exception.Message)") }
    }
    if ($transactionRequestEvidence -and $transactionRequestEvidence.lock) {
        try { $transactionRequestEvidence.lock.Dispose() }
        catch { $cleanupErrors.Add("transaction request read-lock cleanup: $($_.Exception.Message)") }
    }
    if (Test-Path -LiteralPath $ProbeRequest -PathType Leaf) {
        try { Remove-Item -LiteralPath $ProbeRequest -Force }
        catch { $cleanupErrors.Add("probe-request cleanup: $($_.Exception.Message)") }
    }
    try {
        if ((Get-Super1SecureTaskXml -TaskName $Task) -cne $originalTaskXml -or
            (Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $originalWatchdogXml) {
            $cleanupErrors.Add("fixed task XML changed")
        }
        [void](Assert-Super1SecureTaskBindings `
            -Root $Root `
            -MainTask $Task `
            -WatchdogTask $WatchdogTask)
    }
    catch { $cleanupErrors.Add("fixed task contract verification: $($_.Exception.Message)") }
    if (-not $succeeded -and -not $preserveFailedEvidence -and
        (Test-Path -LiteralPath $Transaction -PathType Container)) {
        try { Remove-Item -LiteralPath $Transaction -Recurse -Force }
        catch { $cleanupErrors.Add("failed transaction cleanup: $($_.Exception.Message)") }
    }
    if ($succeeded -and -not $KeepStopped) {
        try {
            Start-ScheduledTask -TaskName $Task
            Start-Sleep -Seconds 2
            Start-ScheduledTask -TaskName $WatchdogTask
        }
        catch { $cleanupErrors.Add("normal task restart: $($_.Exception.Message)") }
    }
    $env:PSModulePath = $OriginalPSModulePath
}

if ($cleanupErrors.Count -ne 0) {
    $message = if ($failure) { $failure.Exception.Message } else { "none" }
    throw "Super1 flat check failed/cleanup incomplete ($message): $($cleanupErrors -join '; ')"
}
if ($failure) { throw $failure }
$resultEnvelope | ConvertTo-Json -Depth 8
