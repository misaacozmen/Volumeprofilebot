[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [switch]$ConfirmDemo
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$OriginalPSModulePath = $env:PSModulePath
$Root = "C:\Super1"
$App = Join-Path $Root "app"
$Deploy = Join-Path $App "deploy"
$MainTask = "Super1XM"
$WatchdogTask = "Super1Watchdog"
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
    try { Stop-Super1SecureRuntime -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask } catch { Add-SmokeCleanupError "runtime stop: $($_.Exception.Message)" }
    try { Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $stopped = $true; $StoppedConfirmed.Value = $true; $RequestLockReleaseAuthorized.Value = $true } catch { Add-SmokeCleanupError "stopped-state verification: $($_.Exception.Message)" }
    try { Assert-Super1SecureTaskBindings -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask | Out-Null } catch { Add-SmokeCleanupError "task binding: $($_.Exception.Message)" }
    try { if ($SavedMainXml -and (Get-Super1SecureTaskXml -TaskName $MainTask) -cne $SavedMainXml) { throw "main XML changed" } } catch { Add-SmokeCleanupError "main XML restore check: $($_.Exception.Message)" }
    try { if ($SavedWatchdogXml -and (Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $SavedWatchdogXml) { throw "watchdog XML changed" } } catch { Add-SmokeCleanupError "watchdog XML restore check: $($_.Exception.Message)" }
    if (-not $stopped) {
        try { Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $StoppedConfirmed.Value = $true; $RequestLockReleaseAuthorized.Value = $true } catch { Add-SmokeCleanupError "final stopped-state verification: $($_.Exception.Message)" }
        return
    }
    try { if ($TransactionRequestEvidence.Value) { $TransactionRequestEvidence.Value.lock.Dispose(); $TransactionRequestEvidence.Value = $null } } catch { Add-SmokeCleanupError "transaction request lock: $($_.Exception.Message)" }
    try { if ($ActiveRequestEvidence.Value) { $ActiveRequestEvidence.Value.lock.Dispose(); $ActiveRequestEvidence.Value = $null } } catch { Add-SmokeCleanupError "active request lock: $($_.Exception.Message)" }
    try { if ($ActiveRequest -and (Test-Path -LiteralPath $ActiveRequest)) { Remove-Item -LiteralPath $ActiveRequest -Force }; if ($ActiveRequest -and (Test-Path -LiteralPath $ActiveRequest)) { throw "active request remains" } } catch { Add-SmokeCleanupError "active request removal: $($_.Exception.Message)" }
    try { if ($Transaction -and (Test-Path -LiteralPath $Transaction)) { Seal-Super1SecureEvidenceTree -Path $Transaction; Assert-Super1SecureSealedTree -Path $Transaction } } catch { Add-SmokeCleanupError "transaction seal: $($_.Exception.Message)" }
    try { Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $StoppedConfirmed.Value = $true; $RequestLockReleaseAuthorized.Value = $true } catch { Add-SmokeCleanupError "final stopped-state verification: $($_.Exception.Message)" }
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

if ($ContractTestOnly) { return }

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
    $State = Join-Path $Root "state"; $Archive = Join-Path $Root "archive"; $Control = Join-Path $Root "probe-control"; $launcher = Join-Path $Deploy "run_super1_windows.ps1"
    $runId = [Guid]::NewGuid().ToString("N"); $nonce = [Guid]::NewGuid().ToString("N"); $transaction = Join-Path $Archive ("readiness-" + $runId); $output = Join-Path $transaction "output"; $requestPath = Join-Path $transaction "request.json"; $resultPath = Join-Path $output "result.json"; $producerPath = Join-Path $output "producer.json"; $activeRequest = Join-Path $Control "active.json"
    $mainXml = Get-Super1SecureTaskXml -TaskName $MainTask; $watchdogXml = Get-Super1SecureTaskXml -TaskName $WatchdogTask; $runtimeReady = $true
    Assert-Super1SecureDirectoryAcl -Path $Archive; Assert-Super1SecureDirectoryAcl -Path $Control
    New-Super1SecureDirectory -Path $transaction -RunnerSid $RunnerSid | Out-Null; New-Super1SecureDirectory -Path $output -RunnerSid $RunnerSid -RunnerRights ([Security.AccessControl.FileSystemRights]::Modify) | Out-Null
    $mainBackup = New-Super1SecureLockedFile -Path (Join-Path $transaction "main.xml") -Content ($mainXml + [Environment]::NewLine) -RunnerSid $RunnerSid; $mainBackup.lock.Dispose()
    $watchdogBackup = New-Super1SecureLockedFile -Path (Join-Path $transaction "watchdog.xml") -Content ($watchdogXml + [Environment]::NewLine) -RunnerSid $RunnerSid; $watchdogBackup.lock.Dispose()
    if (Test-Path -LiteralPath $activeRequest) { throw "Super1 active request already exists." }
    Stop-Super1SecureRuntime -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; $stoppedConfirmed = $true; $requestLockReleaseAuthorized = $true
    $flat = (& (Join-Path $Deploy "check_super1_flat_windows.ps1") -KeepStopped | Select-Object -Last 1 | ConvertFrom-Json); if ([string]$flat.state -cne "READY_FLAT_SEALED") { throw "Sealed pre-flat check failed." }
    $requestedAt = [DateTimeOffset]::UtcNow; $launcherSha256 = [string](Get-Super1SecureSha256 -Path $launcher); $requestPayload = [ordered]@{ schema_version = 1; kind = "smoke"; transaction_id = $runId; nonce = $nonce; requested_at_utc = $requestedAt.ToString("o"); expected_runner_sid = $RunnerSid; expected_launcher_sha256 = $launcherSha256; result_path = $resultPath; producer_path = $producerPath; request_path = $requestPath }; $requestJson = $requestPayload | ConvertTo-Json -Compress
    $transactionRequestEvidence = New-Super1SecureLockedFile -Path $requestPath -Content ($requestJson + [Environment]::NewLine) -RunnerSid $RunnerSid; $activeRequestEvidence = New-Super1SecureLockedFile -Path $activeRequest -Content ($requestJson + [Environment]::NewLine) -RunnerSid $RunnerSid
    Start-ScheduledTask -TaskName $MainTask; $deadline = [DateTimeOffset]::UtcNow.AddSeconds(120)
    do { $mainState = [string](Get-ScheduledTask -TaskName $MainTask).State; $watchdogState = [string](Get-ScheduledTask -TaskName $WatchdogTask).State; if ($mainState -notin @("Running", "Queued") -and $watchdogState -notin @("Running", "Queued") -and (Test-Path -LiteralPath $resultPath)) { break }; if ([DateTimeOffset]::UtcNow -ge $deadline) { throw "Super1 smoke task timed out." }; Start-Sleep -Seconds 1 } while ($true)
    Stop-Super1SecureRuntime -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask; Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask
    if ([int](Get-ScheduledTaskInfo -TaskName $MainTask).LastTaskResult -ne 0) { throw "Super1 smoke LastTaskResult was not 0." }
    if ((Get-Super1SecureTaskXml -TaskName $MainTask) -cne $mainXml) { throw "Super1 main task XML changed during smoke." }; if ((Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $watchdogXml) { throw "Super1 watchdog task XML changed during smoke." }
    $transactionRequestEvidence.lock.Dispose(); $transactionRequestEvidence = $null; $activeRequestEvidence.lock.Dispose(); $activeRequestEvidence = $null; Remove-Item -LiteralPath $activeRequest -Force
    Seal-Super1SecureEvidenceTree -Path $transaction
    $producer = Get-Super1SecureProducerEnvelope -ProducerPath $producerPath -RequestPath $requestPath -ResultPath $resultPath -TransactionId $runId -Nonce $nonce -Kind "smoke" -RunnerSid $RunnerSid -ExpectedLauncherPath $launcher -ExpectedLauncherSha256 $launcherSha256 -ExpectedExitCode 0 -NotBefore $requestedAt
    $resultHash = [string]$producer.result_sha256; $result = $producer.result_payload
    if ([string]$result.state -cne "PASS" -or $result.demo_verified -ne $true -or [string]$result.cancelled.state -cne "CANCELLED" -or [int]$result.open_orders_after -ne 0 -or [int]$result.open_positions_after -ne 0 -or [int]$result.unknown_exposure_after -ne 0) { throw "Smoke acceptance criteria failed." }
    $postFlat = (& (Join-Path $Deploy "check_super1_flat_windows.ps1") -KeepStopped | Select-Object -Last 1 | ConvertFrom-Json); if ([string]$postFlat.state -cne "READY_FLAT_SEALED") { throw "Sealed post-flat check failed." }
    $restartStarted = [DateTimeOffset]::UtcNow; Start-ScheduledTask -TaskName $MainTask; $healthDeadline = [DateTimeOffset]::UtcNow.AddSeconds(90); do { $healthPath=Join-Path $State "health.json"; $healthPayload=if(Test-Path $healthPath){Get-Content -Raw $healthPath|ConvertFrom-Json}else{$null}; if ((Get-ScheduledTask -TaskName $MainTask).State -eq "Running" -and (Test-Path $healthPath) -and (Get-Item $healthPath).LastWriteTimeUtc -ge $restartStarted.UtcDateTime -and [string]$healthPayload.updated_at -and [DateTimeOffset]::Parse([string]$healthPayload.updated_at) -ge $restartStarted -and [string]$healthPayload.state -eq "RUNNING") { break }; if ([DateTimeOffset]::UtcNow -ge $healthDeadline) { throw "Fresh main health was not observed." }; Start-Sleep -Seconds 1 } while ($true); $healthCheckedAt = [DateTimeOffset]::UtcNow
    $watchdogRestartStarted = [DateTimeOffset]::UtcNow; Start-ScheduledTask -TaskName $WatchdogTask; $watchdogDeadline = [DateTimeOffset]::UtcNow.AddSeconds(90); do { $watchdogPath=Join-Path $Root "watchdog_status.json"; $watchdogRecord = if(Test-Path $watchdogPath){Get-Content -Raw $watchdogPath | ConvertFrom-Json}else{$null}; if ((Get-ScheduledTask -TaskName $WatchdogTask).State -eq "Running" -and (Test-Path $watchdogPath) -and (Get-Item $watchdogPath).LastWriteTimeUtc -ge $watchdogRestartStarted.UtcDateTime -and [string]$watchdogRecord.observed_at_utc -and [DateTimeOffset]::Parse([string]$watchdogRecord.observed_at_utc) -ge $watchdogRestartStarted -and [string]$watchdogRecord.state -eq "HEALTHY" -and [string]$watchdogRecord.main_task -ceq $MainTask) { break }; if ([DateTimeOffset]::UtcNow -ge $watchdogDeadline) { throw "Fresh watchdog health was not observed." }; Start-Sleep -Seconds 1 } while ($true); $watchdogCheckedAt = [DateTimeOffset]::UtcNow
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
    try { if ($requestLockReleaseAuthorized -and $transactionRequestEvidence) { $transactionRequestEvidence.lock.Dispose(); $transactionRequestEvidence = $null } elseif ($transactionRequestEvidence) { Add-SmokeCleanupError "unauthorized transaction request lock release" } } catch { Add-SmokeCleanupError "outer transaction lock disposal: $($_.Exception.Message)" }
    try { if ($requestLockReleaseAuthorized -and $activeRequestEvidence) { $activeRequestEvidence.lock.Dispose(); $activeRequestEvidence = $null } elseif ($activeRequestEvidence) { Add-SmokeCleanupError "unauthorized active request lock release" } } catch { Add-SmokeCleanupError "outer active lock disposal: $($_.Exception.Message)" }
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
