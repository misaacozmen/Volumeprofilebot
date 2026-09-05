[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [switch]$ConfirmDemo
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$RuntimeContract = Assert-Super1RuntimeContract
$OriginalPSModulePath = $env:PSModulePath
$Root = [string]$RuntimeContract.root
$App = [string]$RuntimeContract.app
$Deploy = Join-Path $App "deploy"
$MainTask = [string]$RuntimeContract.main_task
$WatchdogTask = [string]$RuntimeContract.watchdog_task
$transaction = $null
$activeRequest = $null
$transactionRequestEvidence = $null
$activeRequestEvidence = $null
$mainXml = $null
$watchdogXml = $null
$success = $false
$runtimeReady = $false
$requestLockReleaseAuthorized = $false
$stoppedConfirmed = $false
$cleanupErrors = [Collections.Generic.List[string]]::new()
$primaryError = $null
$bufferedSummaryJson = $null

function Add-SmokeCleanupError([string]$Message) { [void]$cleanupErrors.Add("cleanup: " + $Message) }

function Invoke-Super1SmokeCleanup {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MainTask,
        [Parameter(Mandatory = $true)][string]$WatchdogTask,
        [Parameter(Mandatory = $true)][AllowNull()][string]$ActiveRequest,
        [Parameter(Mandatory = $true)][AllowNull()][string]$Transaction,
        [Parameter(Mandatory = $true)][AllowNull()][ref]$TransactionRequestEvidence,
        [Parameter(Mandatory = $true)][AllowNull()][ref]$ActiveRequestEvidence,
        [Parameter(Mandatory = $true)][ref]$RequestLockReleaseAuthorized,
        [Parameter(Mandatory = $true)][ref]$StoppedConfirmed,
        [Parameter(Mandatory = $true)][AllowNull()][string]$SavedMainXml,
        [Parameter(Mandatory = $true)][AllowNull()][string]$SavedWatchdogXml
    )
    $stopped = $false
    $StoppedConfirmed.Value = $false
    $RequestLockReleaseAuthorized.Value = $false
    try { & (Join-Path $Root "app\deploy\stop_super1_local_windows.ps1") } catch { Add-SmokeCleanupError "runtime stop: $($_.Exception.Message)" }
    $StoppedConfirmed.Value = $false; $RequestLockReleaseAuthorized.Value = $false; $stopped = $false
    try { Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $stopped = $true; $StoppedConfirmed.Value = $true; $RequestLockReleaseAuthorized.Value = $true } catch { Add-SmokeCleanupError "stopped-state verification: $($_.Exception.Message)" }
    try { Assert-Super1SecureTaskBindings -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask | Out-Null } catch { Add-SmokeCleanupError "task binding: $($_.Exception.Message)" }
    try { if ($SavedMainXml -and (Get-Super1SecureTaskXml -TaskName $MainTask) -cne $SavedMainXml) { throw "main XML changed" } } catch { Add-SmokeCleanupError "main XML restore check: $($_.Exception.Message)" }
    try { if ($SavedWatchdogXml -and (Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $SavedWatchdogXml) { throw "watchdog XML changed" } } catch { Add-SmokeCleanupError "watchdog XML restore check: $($_.Exception.Message)" }
    if (-not $stopped) {
        $StoppedConfirmed.Value = $false; $RequestLockReleaseAuthorized.Value = $false; $stopped = $false
        try { Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $stopped = $true; $StoppedConfirmed.Value = $true; $RequestLockReleaseAuthorized.Value = $true } catch { Add-SmokeCleanupError "final stopped-state verification: $($_.Exception.Message)" }
    }
    if (-not $stopped) {
        return
    }
    try { if ($TransactionRequestEvidence.Value) { $TransactionRequestEvidence.Value.lock.Dispose(); $TransactionRequestEvidence.Value = $null } } catch { Add-SmokeCleanupError "transaction request lock: $($_.Exception.Message)" }
    try { if ($ActiveRequestEvidence.Value) { $ActiveRequestEvidence.Value.lock.Dispose(); $ActiveRequestEvidence.Value = $null } } catch { Add-SmokeCleanupError "active request lock: $($_.Exception.Message)" }
    try { if ($ActiveRequest -and (Test-Path -LiteralPath $ActiveRequest)) { Remove-Item -LiteralPath $ActiveRequest -Force }; if ($ActiveRequest -and (Test-Path -LiteralPath $ActiveRequest)) { throw "active request remains" } } catch { Add-SmokeCleanupError "active request removal: $($_.Exception.Message)" }
    try { if ($Transaction -and (Test-Path -LiteralPath $Transaction)) { Seal-Super1SecureEvidenceTree -Path $Transaction; Assert-Super1SecureSealedTree -Path $Transaction } } catch { Add-SmokeCleanupError "transaction seal: $($_.Exception.Message)" }
    $StoppedConfirmed.Value = $false; $RequestLockReleaseAuthorized.Value = $false; $stopped = $false
    try { Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $stopped = $true; $StoppedConfirmed.Value = $true; $RequestLockReleaseAuthorized.Value = $true } catch { Add-SmokeCleanupError "final stopped-state verification: $($_.Exception.Message)" }
}

function New-Super1SmokeSummary {
    param(
        [Parameter(Mandatory = $true)][object]$Result,
        [Parameter(Mandatory = $true)][object]$Producer,
        [Parameter(Mandatory = $true)][object]$PreFlat,
        [Parameter(Mandatory = $true)][object]$PostFlat,
        [Parameter(Mandatory = $true)][string]$Transaction,
        [Parameter(Mandatory = $true)][DateTimeOffset]$HealthCheckedAt,
        [Parameter(Mandatory = $true)][DateTimeOffset]$WatchdogCheckedAt
    )
    return [ordered]@{
        state = [string]$Result.state
        demo_verified = [bool]$Result.demo_verified
        cancelled = $Result.cancelled
        open_orders_after = [int]$Result.open_orders_after
        open_positions_after = [int]$Result.open_positions_after
        unknown_exposure_after = [int]$Result.unknown_exposure_after
        pre_flat_evidence = [string]$PreFlat.readiness_evidence
        smoke_evidence = $Transaction
        post_flat_evidence = [string]$PostFlat.readiness_evidence
        result_sha256 = [string]$Producer.result_sha256
        producer_sha256 = [string]$Producer.producer_sha256
        health_checked_at = $HealthCheckedAt.ToString("o")
        watchdog_checked_at = $WatchdogCheckedAt.ToString("o")
    }
}

try {
    if (-not $ConfirmDemo) { throw "Demo smoke requires -ConfirmDemo." }
    $ExpectedPSHome = [IO.Path]::GetFullPath((Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0"))
    $ExpectedPowerShell = [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "powershell.exe"))
    $CurrentPowerShell = [IO.Path]::GetFullPath([Diagnostics.Process]::GetCurrentProcess().MainModule.FileName)
    if (-not [Environment]::Is64BitProcess -or $PSEdition -cne "Desktop" -or
        -not [IO.Path]::GetFullPath($PSHOME).Equals($ExpectedPSHome, [StringComparison]::OrdinalIgnoreCase) -or
        -not $CurrentPowerShell.Equals($ExpectedPowerShell, [StringComparison]::OrdinalIgnoreCase)) { throw "Super1 smoke requires the trusted 64-bit Windows PowerShell host." }
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Super1 smoke requires an elevated Administrator shell." }
    $TrustedScript = [IO.Path]::GetFullPath((Join-Path $Deploy "run_super1_demo_smoke_windows.ps1"))
    if (-not [IO.Path]::GetFullPath($PSCommandPath).Equals($TrustedScript, [StringComparison]::OrdinalIgnoreCase)) { throw "Smoke wrapper path is not the exact trusted app path." }
    $TrustedModules = [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "Modules"))
    $ScheduledTasksModule = [IO.Path]::GetFullPath((Join-Path $TrustedModules "ScheduledTasks\ScheduledTasks.psd1"))
    $TrustedTaskHelper = [IO.Path]::GetFullPath((Join-Path $Deploy "super1_secure_task.ps1"))
    if (-not (Test-Path -LiteralPath $ScheduledTasksModule -PathType Leaf) -or (Get-Item -LiteralPath $ScheduledTasksModule -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "ScheduledTasks manifest is missing or unsafe." }
    if (-not (Test-Path -LiteralPath $TrustedTaskHelper -PathType Leaf) -or (Get-Item -LiteralPath $TrustedTaskHelper -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Trusted secure task helper is missing or unsafe." }
    $env:PSModulePath = $TrustedModules
    $loadedScheduledTasks = @(Import-Module -Name $ScheduledTasksModule -Force -PassThru -ErrorAction Stop)
    if ($loadedScheduledTasks.Count -ne 1) { throw "Exactly one ScheduledTasks module must load." }
    $Module = $loadedScheduledTasks[0]
    if (-not [IO.Path]::GetFullPath([string]$Module.Path).Equals($ScheduledTasksModule, [StringComparison]::OrdinalIgnoreCase)) { throw "Unexpected ScheduledTasks module path." }
    . $TrustedTaskHelper
    $RunnerSid = [string](Assert-Super1SecureTaskBindings -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask)
    if ($RunnerSid -notmatch '^S-1-5-21-' -or $RunnerSid -in @([string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value, "S-1-5-18", "S-1-5-32-544")) { throw "Invalid task-bound Super1 runner SID." }
    $State = [string]$RuntimeContract.state; $Archive = Join-Path $Root "archive"; $Control = [string]$RuntimeContract.control; $launcher = Join-Path $Deploy "run_super1_windows.ps1"
    $runId = [Guid]::NewGuid().ToString("N"); $nonce = [Guid]::NewGuid().ToString("N"); $transaction = Join-Path $Archive ("readiness-" + $runId); $output = Join-Path $transaction "output"; $requestPath = Join-Path $transaction "request.json"; $resultPath = Join-Path $output "result.json"; $producerPath = Join-Path $output "producer.json"; $activeRequest = Join-Path $Control "active.json"
    $mainXml = Get-Super1SecureTaskXml -TaskName $MainTask; $watchdogXml = Get-Super1SecureTaskXml -TaskName $WatchdogTask; $runtimeReady = $true
    Assert-Super1SecureDirectoryAcl -Path $Archive; Assert-Super1SecureDirectoryAcl -Path $Control -RunnerSid $RunnerSid
    New-Super1SecureDirectory -Path $transaction -RunnerSid $RunnerSid | Out-Null; New-Super1SecureDirectory -Path $output -RunnerSid $RunnerSid -RunnerRights ([Security.AccessControl.FileSystemRights]::Modify) | Out-Null
    $mainBackup = New-Super1SecureLockedFile -Path (Join-Path $transaction "main.xml") -Content ($mainXml + [Environment]::NewLine) -RunnerSid $RunnerSid; $mainBackup.lock.Dispose()
    $watchdogBackup = New-Super1SecureLockedFile -Path (Join-Path $transaction "watchdog.xml") -Content ($watchdogXml + [Environment]::NewLine) -RunnerSid $RunnerSid; $watchdogBackup.lock.Dispose()
    if (Test-Path -LiteralPath $activeRequest) { throw "Super1 active request already exists." }
    Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $stoppedConfirmed = $true; $requestLockReleaseAuthorized = $true
    $flat = (& (Join-Path $Deploy "check_super1_flat_windows.ps1") -KeepStopped | Select-Object -Last 1 | ConvertFrom-Json); if ([string]$flat.state -cne "READY_FLAT_SEALED") { throw "Sealed pre-flat check failed." }
    $requestedAt = [DateTimeOffset]::UtcNow; $launcherSha256 = [string](Get-Super1SecureSha256 -Path $launcher); $requestPayload = [ordered]@{ schema_version = 1; kind = "smoke"; transaction_id = $runId; nonce = $nonce; requested_at_utc = $requestedAt.ToString("o"); expected_runner_sid = $RunnerSid; expected_launcher_sha256 = $launcherSha256; result_path = $resultPath; producer_path = $producerPath; request_path = $requestPath }; $requestJson = $requestPayload | ConvertTo-Json -Compress
    $transactionRequestEvidence = New-Super1SecureLockedFile -Path $requestPath -Content ($requestJson + [Environment]::NewLine) -RunnerSid $RunnerSid; $activeRequestEvidence = New-Super1SecureLockedFile -Path $activeRequest -Content ($requestJson + [Environment]::NewLine) -RunnerSid $RunnerSid
    $requestLockReleaseAuthorized = $false; $stoppedConfirmed = $false; & (Join-Path $Deploy "start_super1_local_windows.ps1"); $deadline = [DateTimeOffset]::UtcNow.AddSeconds(120)
    do { $mainState = [string](Get-ScheduledTask -TaskName $MainTask).State; $watchdogState = [string](Get-ScheduledTask -TaskName $WatchdogTask).State; if ($mainState -notin @("Running", "Queued") -and $watchdogState -notin @("Running", "Queued") -and (Test-Path -LiteralPath $resultPath)) { break }; if ([DateTimeOffset]::UtcNow -ge $deadline) { throw "Super1 smoke task timed out." }; Start-Sleep -Seconds 1 } while ($true)
    & (Join-Path $Deploy "stop_super1_local_windows.ps1"); $stoppedConfirmed = $false; $requestLockReleaseAuthorized = $false; Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $stoppedConfirmed = $true; $requestLockReleaseAuthorized = $true
    if ([int](Get-ScheduledTaskInfo -TaskName $MainTask).LastTaskResult -ne 0) { throw "Super1 smoke LastTaskResult was not 0." }
    if ((Get-Super1SecureTaskXml -TaskName $MainTask) -cne $mainXml) { throw "Super1 main task XML changed during smoke." }; if ((Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $watchdogXml) { throw "Super1 watchdog task XML changed during smoke." }
    $transactionRequestEvidence.lock.Dispose(); $transactionRequestEvidence = $null; $activeRequestEvidence.lock.Dispose(); $activeRequestEvidence = $null; Remove-Item -LiteralPath $activeRequest -Force
    Seal-Super1SecureEvidenceTree -Path $transaction
    $producer = Get-Super1SecureProducerEnvelope -ProducerPath $producerPath -RequestPath $requestPath -ResultPath $resultPath -TransactionId $runId -Nonce $nonce -Kind "smoke" -RunnerSid $RunnerSid -ExpectedLauncherPath $launcher -ExpectedLauncherSha256 $launcherSha256 -ExpectedExitCode 0 -NotBefore $requestedAt
    $resultHash = [string]$producer.result_sha256; $result = $producer.result_payload
    if ([string]$result.state -cne "PASS" -or $result.demo_verified -ne $true -or [string]$result.cancelled.state -cne "CANCELLED" -or [int]$result.open_orders_after -ne 0 -or [int]$result.open_positions_after -ne 0 -or [int]$result.unknown_exposure_after -ne 0) { throw "Smoke acceptance criteria failed." }
    $postFlat = (& (Join-Path $Deploy "check_super1_flat_windows.ps1") -KeepStopped | Select-Object -Last 1 | ConvertFrom-Json); if ([string]$postFlat.state -cne "READY_FLAT_SEALED") { throw "Sealed post-flat check failed." }
    $restartStarted = [DateTimeOffset]::UtcNow; & (Join-Path $Deploy "start_super1_local_windows.ps1"); $healthPath = Join-Path $State "health.json"; $healthPayload = Get-Content -Raw $healthPath | ConvertFrom-Json; if ([string]$healthPayload.state -ne "RUNNING") { throw "Fresh main health was not observed." }; $healthUpdatedAt = [DateTimeOffset]::Parse([string]$healthPayload.updated_at); if ($healthUpdatedAt -le $restartStarted) { throw "Main health predates the restart request." }; $healthCheckedAt = [DateTimeOffset]::UtcNow
    $watchdogRecord = Get-Content -Raw (Join-Path $Root "watchdog_status.json") | ConvertFrom-Json; if ([string]$watchdogRecord.state -notin @("HEALTHY", "WAITING_MANUAL_LEASE")) { throw "Fresh watchdog health was not observed." }; $watchdogUpdatedAt = [DateTimeOffset]::Parse([string]$watchdogRecord.updated_at_utc); if ($watchdogUpdatedAt -le $restartStarted) { throw "Watchdog health predates the restart request." }; $watchdogCheckedAt = [DateTimeOffset]::UtcNow; & (Join-Path $Deploy "stop_super1_local_windows.ps1")
    if (-not (Test-Path (Join-Path $State "campaign_lock.json")) -or (Test-Path (Join-Path $State "fatal_latch.json")) -or (Test-Path (Join-Path $State "launcher_failure.json"))) { throw "Campaign lock/fatal-latch/launcher-failure contract failed." }
    $finalRunnerSid = [string](Assert-Super1SecureTaskBindings -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask); if ($finalRunnerSid -cne $RunnerSid -or (Get-Super1SecureTaskXml -TaskName $MainTask) -cne $mainXml -or (Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $watchdogXml) { throw "Super1 task bindings or XML changed after smoke." }
    $summary = New-Super1SmokeSummary -Result $result -Producer $producer -PreFlat $flat -PostFlat $postFlat -Transaction $transaction -HealthCheckedAt $healthCheckedAt -WatchdogCheckedAt $watchdogCheckedAt
    $summaryJson = $summary | ConvertTo-Json -Depth 8
    $bufferedSummaryJson = $summary | ConvertTo-Json -Depth 8
}
catch {
    $primaryError = $_
    if ($runtimeReady) { Invoke-Super1SmokeCleanup -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask -ActiveRequest $activeRequest -Transaction $transaction -TransactionRequestEvidence ([ref]$transactionRequestEvidence) -ActiveRequestEvidence ([ref]$activeRequestEvidence) -RequestLockReleaseAuthorized ([ref]$requestLockReleaseAuthorized) -StoppedConfirmed ([ref]$stoppedConfirmed) -SavedMainXml $mainXml -SavedWatchdogXml $watchdogXml }
}
finally {
    try { if ($requestLockReleaseAuthorized -and $stoppedConfirmed -and $transactionRequestEvidence) { $transactionRequestEvidence.lock.Dispose(); $transactionRequestEvidence = $null } elseif ($transactionRequestEvidence) { Add-SmokeCleanupError "unauthorized transaction request lock release" } } catch { Add-SmokeCleanupError "outer transaction lock disposal: $($_.Exception.Message)" }
    try { if ($requestLockReleaseAuthorized -and $stoppedConfirmed -and $activeRequestEvidence) { $activeRequestEvidence.lock.Dispose(); $activeRequestEvidence = $null } elseif ($activeRequestEvidence) { Add-SmokeCleanupError "unauthorized active request lock release" } } catch { Add-SmokeCleanupError "outer active lock disposal: $($_.Exception.Message)" }
    try { $env:PSModulePath = $OriginalPSModulePath } catch { Add-SmokeCleanupError "PSModulePath restore: $($_.Exception.Message)" }
}

if ($primaryError) {
    if ($cleanupErrors.Count -gt 0) { throw [Exception]::new("Super1 smoke failed; cleanup incomplete: $($cleanupErrors -join '; ')", $primaryError.Exception) }
    throw $primaryError
}
if ($cleanupErrors.Count -gt 0) { throw "Super1 smoke cleanup incomplete: $($cleanupErrors -join '; ')" }
if (-not $bufferedSummaryJson) { throw "Super1 smoke did not produce a successful result." }
$success = $true
$bufferedSummaryJson
