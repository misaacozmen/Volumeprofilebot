[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$OutputRoot,
    [Parameter(Mandatory = $true)][string]$Failure
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = [IO.Path]::GetFullPath($OutputRoot)
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$scriptRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot "scripts"))
$failureHelper = [IO.Path]::GetFullPath((Join-Path $repoRoot "deploy\super1_rollover_failure.ps1"))
$contractPath = [IO.Path]::GetFullPath((Join-Path $repoRoot "tests\fixtures\super1_o01_case_contract.json"))
if (-not (Test-Path -LiteralPath $failureHelper -PathType Leaf) -or
    -not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
    throw "O01 policy helper or immutable case contract is missing."
}
. $failureHelper
$contract = Get-Content -LiteralPath $contractPath -Raw | ConvertFrom-Json
if ([int]$contract.schema_version -ne 1 -or $null -eq $contract.cases) {
    throw "O01 immutable case contract is invalid."
}

function Write-HarnessJson {
    param([string]$Path, [object]$Payload)
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    $Payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha.Dispose()
    }
}

function Get-CaseInventory {
    param([string]$CaseRoot)
    $rows = foreach ($path in Get-ChildItem -LiteralPath $CaseRoot -Recurse -File | Sort-Object FullName) {
        [ordered]@{
            path = $path.FullName.Substring($CaseRoot.Length).TrimStart('\', '/') -replace '\\', '/'
            bytes = $path.Length
            sha256 = Get-Sha256Hex -Path $path.FullName
        }
    }
    return @($rows)
}

function Invoke-O01Case {
    param([Parameter(Mandatory = $true)][object]$Case, [Parameter(Mandatory = $true)][string]$CaseRoot)
    $state = Join-Path $CaseRoot "state"
    $archive = Join-Path $CaseRoot "archive\campaign-001"
    [void][IO.Directory]::CreateDirectory($state)
    [void][IO.Directory]::CreateDirectory($archive)
    Write-HarnessJson (Join-Path $state "campaign_lock.json") @{
        schema_version = 1; campaign = "CURRENT_SUPER1_CAMPAIGN"; created_at = "source-preserved"
    }
    if ([bool]$Case.fatalLatchPreexisting) {
        Write-HarnessJson (Join-Path $state "fatal_latch.json") @{ state = "PREEXISTING_FATAL"; preserved = $true }
    }
    if ([bool]$Case.recoveryLatchPreexisting) {
        Write-HarnessJson (Join-Path $state "runtime\broker_recovery_required.json") @{ state = "PREEXISTING_RECOVERY"; preserved = $true }
    }

    $failureCallback = if ($Case.PSObject.Properties["failureCallback"]) { [string]$Case.failureCallback } else { "" }
    $writerBox = @{ value = $null }
    $metrics = @{ callback_io_failures = 0; evidence_written = $false; health_written = $false; stop_main = 0; stop_watchdog = 0 }
    $callbackOutcome = {
        param([string]$Name)
        if ($Name -eq $failureCallback) {
            return [pscustomobject]@{ status = "FAIL"; error = "injected O01 callback failure: $Name" }
        }
        return [pscustomobject]@{ status = "PASS" }
    }
    $before = Get-CaseInventory $CaseRoot
    $orchestration = Invoke-Super1RolloverCatchPolicy `
        -StateRoot $state `
        -BrokerSideEffectPossible ([bool]$Case.brokerSideEffectPossible) `
        -TasksStopped ([bool]$Case.tasksStopped) `
        -AppMutation ([bool]$Case.appMutation) `
        -StateMutation ([bool]$Case.stateMutation) `
        -TaskXmlMutation ([bool]$Case.taskXmlMutation) `
        -FatalLatchPreexisting ([bool]$Case.fatalLatchPreexisting) `
        -RecoveryLatchPreexisting ([bool]$Case.recoveryLatchPreexisting) `
        -Restore { return (& $callbackOutcome "Restore") } `
        -VerifyOldHashes { return (& $callbackOutcome "VerifyOldHashes") } `
        -VerifyOldTaskXml { return (& $callbackOutcome "VerifyOldTaskXml") } `
        -StartOldMain { return (& $callbackOutcome "StartOldMain") } `
        -StartOldWatchdog { return (& $callbackOutcome "StartOldWatchdog") } `
        -VerifyOldHealth { return (& $callbackOutcome "VerifyOldHealth") } `
        -StopOldMain {
            $metrics.stop_main = [int]$metrics.stop_main + 1
            return (& $callbackOutcome "StopOldMain")
        } `
        -StopOldWatchdog {
            $metrics.stop_watchdog = [int]$metrics.stop_watchdog + 1
            return (& $callbackOutcome "StopOldWatchdog")
        } `
        -WriteLatch {
            if ($failureCallback -eq "WriteLatch") {
                return [pscustomobject]@{ status = "FAIL"; latch_preexisting = $false; recovery_preexisting = $false; latch_written = $false; recovery_written = $false; paths = @{}; errors = @("injected inaccessible latch target") }
            }
            $writer = Write-Super1FailureLatchesCreateNew `
                -StateRoot $state `
                -FailureMessage "O01 synthetic failure: $($Case.name)" `
                -FailureType "O01.SyntheticFailure"
            $writerBox.value = $writer
            return [pscustomobject]@{
                status = [string]$writer.status
                latch_preexisting = [bool]$writer.latch_preexisting
                recovery_preexisting = [bool]$writer.recovery_preexisting
                latch_written = [bool]$writer.latch_written
                recovery_written = [bool]$writer.recovery_written
                paths = $writer.paths
                errors = @($writer.errors)
            }
        } `
        -WriteEvidence {
            if ($failureCallback -eq "WriteEvidence") {
                $metrics.callback_io_failures++
                try {
                    Set-Content -LiteralPath (Join-Path $archive "missing-parent\failure-evidence.json") -Value "unreachable" -ErrorAction Stop
                }
                catch {
                    return [pscustomobject]@{ status = "FAIL"; error = "evidence I/O boundary: $($_.Exception.Message)" }
                }
                return [pscustomobject]@{ status = "FAIL"; error = "evidence I/O target unexpectedly writable" }
            }
            Write-HarnessJson (Join-Path $archive "failure-evidence.json") @{ state = "BROKER_SIDE_EFFECT_POSSIBLE"; automatic_restart = $false }
            $metrics.evidence_written = $true
            return [pscustomobject]@{ status = "PASS" }
        } `
        -WriteHealth {
            Write-HarnessJson (Join-Path $state "health.json") @{ state = "CRITICAL_STOP"; recovery_required = $true; automatic_restart = $false }
            $metrics.health_written = $true
            return [pscustomobject]@{ status = "PASS" }
        }
    $after = Get-CaseInventory $CaseRoot
    $actualTrace = @($orchestration.callback_trace | ForEach-Object { [string]$_.name })
    if ([string]$orchestration.status -ne [string]$Case.expectedStatus -or
        (ConvertTo-Json $actualTrace -Compress) -ne (ConvertTo-Json @($Case.expectedTrace) -Compress)) {
        throw "O01 case contract mismatch: $($Case.name)"
    }

    $gateExit = $null
    $latchConsumed = $false
    $persistentLatch = Test-Path -LiteralPath (Join-Path $state "fatal_latch.json") -PathType Leaf
    $persistentRecovery = Test-Path -LiteralPath (Join-Path $state "runtime\broker_recovery_required.json") -PathType Leaf
    if ([bool]$orchestration.persistent_gate_proven) {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        $gateCode = 'import sys; sys.path.insert(0, sys.argv[2]); from pathlib import Path; from run_capital_forward import assert_no_fatal_latch; assert_no_fatal_latch(Path(sys.argv[1]))'
        $gateProcess = Start-Process -FilePath $python -ArgumentList @("-c", $gateCode, $state, $scriptRoot) -Wait -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $CaseRoot "gate-state.stderr.txt") -RedirectStandardOutput (Join-Path $CaseRoot "gate-state.stdout.txt")
        $gateExit = $gateProcess.ExitCode
        $latchConsumed = $gateExit -ne 0
    }
    $negativeRoot = Join-Path $CaseRoot "disconnected-gate"
    [void][IO.Directory]::CreateDirectory($negativeRoot)
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $gateCode = 'import sys; sys.path.insert(0, sys.argv[2]); from pathlib import Path; from run_capital_forward import assert_no_fatal_latch; assert_no_fatal_latch(Path(sys.argv[1]))'
    $negativeProcess = Start-Process -FilePath $python -ArgumentList @("-c", $gateCode, $negativeRoot, $scriptRoot) -Wait -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $CaseRoot "gate-disconnected.stderr.txt") -RedirectStandardOutput (Join-Path $CaseRoot "gate-disconnected.stdout.txt")
    $negativeExit = $negativeProcess.ExitCode
    Write-HarnessJson (Join-Path $CaseRoot "call-trace.json") @{
        callback_trace = $orchestration.callback_trace; callback_counts = $orchestration.callback_counts
        before_inventory = $before; after_inventory = $after; economic_inventory_unchanged = $true; automatic_restart = $false
    }
    return [ordered]@{
        schema_version = 1; case = $Case.name; state = "CRITICAL_STOP"
        input_vector = @{ brokerSideEffectPossible = [bool]$Case.brokerSideEffectPossible; tasksStopped = [bool]$Case.tasksStopped; appMutation = [bool]$Case.appMutation; stateMutation = [bool]$Case.stateMutation; taskXmlMutation = [bool]$Case.taskXmlMutation; fatalLatchPreexisting = [bool]$Case.fatalLatchPreexisting; recoveryLatchPreexisting = [bool]$Case.recoveryLatchPreexisting }
        policy_status = $orchestration.status; callback_trace = $orchestration.callback_trace; callback_counts = $orchestration.callback_counts
        latch_status = $orchestration.latch_status
        latch_tuple = @{ latch_preexisting = $orchestration.latch_preexisting; recovery_preexisting = $orchestration.recovery_preexisting; latch_written = $orchestration.latch_written; recovery_written = $orchestration.recovery_written; persistent_gate_proven = $orchestration.persistent_gate_proven }
        persistent_latch = $persistentLatch; persistent_recovery = $persistentRecovery
        process_recreation_gate_exit_code = $gateExit; process_recreation_latch_consumed = $latchConsumed
        negative_gate_disconnect_exit_code = $negativeExit; negative_gate_control_failed = ($negativeExit -eq 0)
        automatic_restart = $false; stopped_proof = $false; failure_evidence_written = $metrics.evidence_written; health_written = $metrics.health_written
        before_after_inventory_equal = ($before | ConvertTo-Json -Compress) -eq ($after | ConvertTo-Json -Compress)
        production_failure_helper = $failureHelper
    }
}

if (Test-Path -LiteralPath $root) {
    if ((Get-ChildItem -LiteralPath $root -Force | Measure-Object).Count -ne 0) { throw "Harness output root must be new and empty: $root" }
}
else { [void][IO.Directory]::CreateDirectory($root) }
$allNames = @($contract.cases | ForEach-Object { [string]$_.name })
if ($Failure -eq "all") { $names = $allNames }
else {
    if ($Failure -notin $allNames) { throw "Unknown O01 case: $Failure" }
    $names = @($Failure)
}
$results = foreach ($name in $names) {
    $case = @($contract.cases | Where-Object { [string]$_.name -eq $name })[0]
    Invoke-O01Case -Case $case -CaseRoot (Join-Path $root $name)
}
@($results) | ConvertTo-Json -Depth 14
