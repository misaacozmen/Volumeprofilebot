[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ReadinessEvidence,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedReadinessSha256
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$OriginalPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$OriginalPythonHome = $env:PYTHONHOME
$OriginalPythonPath = $env:PYTHONPATH
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
    throw "Run Super1 rollover only with trusted 64-bit Windows PowerShell."
}
$TrustedPSModulePath = [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "Modules"))
$env:PSModulePath = $TrustedPSModulePath
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$ScheduledTasksModule = [IO.Path]::GetFullPath(
    (Join-Path $TrustedPSModulePath "ScheduledTasks\ScheduledTasks.psd1")
)
if (-not (Test-Path -LiteralPath $ScheduledTasksModule -PathType Leaf)) {
    throw "Trusted ScheduledTasks module is missing."
}
Import-Module -Name $ScheduledTasksModule -Force -ErrorAction Stop

$Root = [IO.Path]::GetFullPath("C:\Super1")
$App = [IO.Path]::GetFullPath((Join-Path $Root "app"))
$State = [IO.Path]::GetFullPath((Join-Path $Root "state"))
$ArchiveRoot = [IO.Path]::GetFullPath((Join-Path $Root "archive"))
$Python = [IO.Path]::GetFullPath((Join-Path $Root "venv311\Scripts\python.exe"))
$MainTask = "Super1XM"
$WatchdogTask = "Super1Watchdog"
$RunScript = [IO.Path]::GetFullPath((Join-Path $Root "app\deploy\run_super1_windows.ps1"))
$TrustedScript = [IO.Path]::GetFullPath((Join-Path $App "deploy\rollover_super1_campaign_windows.ps1"))
$CurrentScript = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
if (-not $CurrentScript.Equals($TrustedScript, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run Super1 rollover only from the protected app deploy directory: $TrustedScript"
}
$SecureHelper = [IO.Path]::GetFullPath((Join-Path $App "deploy\super1_secure_task.ps1"))
if (-not (Test-Path -LiteralPath $SecureHelper -PathType Leaf) -or
    (Get-Item -LiteralPath $SecureHelper -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Protected Super1 secure-task helper is missing or is a reparse point."
}
. $SecureHelper
$InternalFlatScript = [IO.Path]::GetFullPath((Join-Path $App "deploy\check_super1_flat_windows.ps1"))
$ProbeControl = [IO.Path]::GetFullPath((Join-Path $Root "probe-control"))
$ProbeRequest = [IO.Path]::GetFullPath((Join-Path $ProbeControl "active.json"))
$TerminalPin = [IO.Path]::GetFullPath((Join-Path $App "deploy\terminal_runtime_pin.json"))
$CapitalCandidate = [IO.Path]::GetFullPath((Join-Path $Root "run_capital_forward.py.candidate"))
$CapitalTarget = [IO.Path]::GetFullPath((Join-Path $Root "app\scripts\run_capital_forward.py"))
$XmCandidate = [IO.Path]::GetFullPath((Join-Path $Root "run_xm_mt5_forward.py.candidate"))
$XmTarget = [IO.Path]::GetFullPath((Join-Path $Root "app\scripts\run_xm_mt5_forward.py"))
$SuperCandidate = [IO.Path]::GetFullPath((Join-Path $Root "run_super1_xm_mt5_forward.py.candidate"))
$SuperTarget = [IO.Path]::GetFullPath((Join-Path $Root "app\scripts\run_super1_xm_mt5_forward.py"))
$RuntimeCandidate = [IO.Path]::GetFullPath((Join-Path $Root "super1_xm_mt5_demo_config.json.candidate"))
$RuntimeTarget = [IO.Path]::GetFullPath((Join-Path $Root "app\live_forward\super1_xm_mt5_demo_config.json"))
$ManifestCandidate = [IO.Path]::GetFullPath((Join-Path $Root "super1_manifest.json.candidate"))
$ManifestTarget = [IO.Path]::GetFullPath((Join-Path $Root "app\research_candidates\super1\super1_manifest.json"))
$ContractCandidate = [IO.Path]::GetFullPath((Join-Path $Root "super1_signal_contract.json.candidate"))
$ContractTarget = [IO.Path]::GetFullPath((Join-Path $Root "app\research_candidates\super1\super1_signal_contract.json"))
$ReadinessPath = [IO.Path]::GetFullPath($ReadinessEvidence)

function Get-Sha256Lower {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Wait-RunningTaskFreshState {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][DateTimeOffset]$NotBefore,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$ExpectedState,
        [string]$ExpectedMainTask = ""
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $taskState = (Get-ScheduledTask -TaskName $TaskName).State.ToString()
        $fresh = (
            $taskState -eq "Running" -and
            (Test-Path -LiteralPath $Path -PathType Leaf) -and
            (Get-Item -LiteralPath $Path).LastWriteTimeUtc -ge $NotBefore.UtcDateTime
        )
        if ($fresh) {
            try {
                $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
                if (
                    [string]$record.state -eq $ExpectedState -and
                    ([string]::IsNullOrWhiteSpace($ExpectedMainTask) -or [string]$record.main_task -eq $ExpectedMainTask)
                ) {
                    return
                }
            }
            catch {
                # A concurrently replaced JSON file is retried until the deadline.
            }
        }
        Start-Sleep -Seconds 2
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "Task $TaskName did not publish fresh $ExpectedState state: $Path"
}

foreach ($path in @(
    $App, $State, $ArchiveRoot, $RunScript, $InternalFlatScript,
    $ProbeControl, $ProbeRequest, $TerminalPin,
    $CapitalCandidate, $CapitalTarget,
    $XmCandidate, $XmTarget, $SuperCandidate, $SuperTarget, $RuntimeCandidate, $RuntimeTarget,
    $ManifestCandidate, $ManifestTarget, $ContractCandidate, $ContractTarget, $ReadinessPath
)) {
    if (-not $path.StartsWith(($Root + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe Super1 rollover path: $path"
    }
}

foreach ($directory in @($App, $State, $ArchiveRoot)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "Required Super1 directory is missing: $directory"
    }
}
foreach ($file in @(
    $Python, $RunScript, $CapitalCandidate, $CapitalTarget, $XmCandidate, $XmTarget,
    $SuperCandidate, $SuperTarget, $RuntimeCandidate, $RuntimeTarget, $ManifestCandidate,
    $ManifestTarget, $ContractCandidate, $ReadinessPath, $SecureHelper,
    $InternalFlatScript, $TerminalPin
)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Required Super1 rollover file is missing: $file"
    }
}

$runnerSid = Assert-Super1SecureTaskBindings `
    -Root $Root `
    -MainTask $MainTask `
    -WatchdogTask $WatchdogTask
$originalTaskXml = Get-Super1SecureTaskXml -TaskName $MainTask
$originalWatchdogXml = Get-Super1SecureTaskXml -TaskName $WatchdogTask
$trustedPowerShell = $script:Super1SecurePowerShellExe

function Assert-FrozenSuper1TaskContracts {
    if ((Get-Super1SecureTaskXml -TaskName $MainTask) -cne $originalTaskXml -or
        (Get-Super1SecureTaskXml -TaskName $WatchdogTask) -cne $originalWatchdogXml) {
        throw "Super1 task XML changed during the fixed-action campaign transaction."
    }
    [void](Assert-Super1SecureTaskBindings `
        -Root $Root `
        -MainTask $MainTask `
        -WatchdogTask $WatchdogTask)
}
Assert-Super1SecureDirectoryAcl -Path $ArchiveRoot
Assert-Super1SecureDirectoryAcl -Path $ProbeControl -RunnerSid $runnerSid
if (Test-Path -LiteralPath $ProbeRequest) {
    throw "Super1 probe-control already contains an active request."
}
Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask

$tupleValidator = @'
import hashlib
import json
import sys
from pathlib import Path

app = Path(sys.argv[1]).resolve()
runtime_file = Path(sys.argv[2]).resolve()
manifest_file = Path(sys.argv[3]).resolve()
contract_file = Path(sys.argv[4]).resolve()
capital_candidate = Path(sys.argv[5]).resolve()
xm_candidate = Path(sys.argv[6]).resolve()
super_candidate = Path(sys.argv[7]).resolve()

def require(condition, message):
    if not condition:
        raise SystemExit(message)

def load(path):
    return json.loads(path.read_text(encoding="utf-8"))

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def app_path(relative):
    require(isinstance(relative, str) and relative, "empty app-relative path")
    path = (app / relative).resolve()
    try:
        path.relative_to(app)
    except ValueError:
        raise SystemExit(f"path escapes app: {relative}")
    return path

for source in (capital_candidate, xm_candidate, super_candidate):
    compile(source.read_text(encoding="utf-8"), str(source), "exec")

runtime = load(runtime_file)
manifest = load(manifest_file)
contract = load(contract_file)
candidate_path = app_path(runtime.get("candidate_path"))
contract_path = app_path(runtime.get("signal_contract_path"))
require(contract_path == (app / "research_candidates/super1/super1_signal_contract.json").resolve(), "unexpected signal contract target")
require(candidate_path.is_file(), "sealed candidate artifact is missing")
candidate = load(candidate_path)

candidate_hash = sha256(candidate_path)
contract_hash = sha256(contract_file)
runtime_hash = sha256(runtime_file)
require(candidate_hash == runtime.get("candidate_file_sha256"), "runtime candidate hash mismatch")
require(contract_hash == runtime.get("signal_contract_sha256"), "runtime contract hash mismatch")
require(candidate.get("artifact_sha256") == runtime.get("candidate_artifact_sha256"), "runtime candidate artifact mismatch")
require(candidate.get("setup_rules") == runtime.get("setup_rules"), "runtime setup rules mismatch")
candidate_risk = candidate.get("risk_rule")
runtime_risk = runtime.get("risk_rule")
require(isinstance(candidate_risk, dict) and isinstance(runtime_risk, dict), "risk rule is missing")
require({key: runtime_risk.get(key) for key in candidate_risk} == candidate_risk, "runtime risk rule mismatch")
require(candidate.get("live_enabled") is False, "candidate live_enabled must be false")
require(candidate.get("proven") is False, "candidate proven must be false")
require(candidate.get("fresh_forward_required") is True, "candidate fresh-forward gate is missing")
require(all(bool(value) for value in candidate.get("checks", {}).values()), "candidate checks are not all true")
protocol = candidate.get("selection_protocol", {})
require(protocol.get("final_holdout_used_for_selection") is False, "selection holdout contract mismatch")
require(protocol.get("final_holdout_used_for_publication") is False, "publication holdout contract mismatch")

signal_source = contract.get("signal_source", {})
overlay = contract.get("overlay_candidate", {})
order_transport = contract.get("demo_order_transport", {})
safety = contract.get("safety", {})
source_config = app_path(signal_source.get("config_path"))
source_generator = app_path(signal_source.get("generator_path"))
source_adapter = app_path(signal_source.get("payload_adapter_path"))
overlay_runtime = app_path(overlay.get("runtime_path"))
order_transport_path = app_path(order_transport.get("path"))
require(contract.get("schema_version") == 2, "contract schema mismatch")
require(contract.get("name") == "SUPER1_CANONICAL_OVERLAY_FRESH_FORWARD_V1", "contract name mismatch")
require(contract.get("status") == "DEMO_FRESH_FORWARD_UNPROVEN", "contract status mismatch")
require(contract.get("execution_scope") == "XM_MT5_DEMO_ONLY", "contract execution scope mismatch")
require(contract.get("deployment_mode") == "FROZEN_CANONICAL_PAIR_PIPELINE_WITH_SUPER1_OVERLAY", "contract deployment mode mismatch")
require(signal_source.get("kind") == "FROZEN_CANONICAL_PAIR_PIPELINE", "signal source kind mismatch")
require(source_config.is_file() and sha256(source_config) == signal_source.get("config_sha256"), "source config hash mismatch")
require(source_generator == (app / "scripts/run_capital_forward.py").resolve(), "unexpected source generator")
require(sha256(capital_candidate) == signal_source.get("generator_sha256"), "candidate generator hash mismatch")
require(source_adapter == (app / "scripts/run_forward_shadow.py").resolve(), "unexpected signal adapter")
require(source_adapter.is_file() and sha256(source_adapter) == signal_source.get("payload_adapter_sha256"), "signal adapter hash mismatch")
engine_digest = hashlib.sha256()
for engine_file in sorted((app / "backtest").glob("*.py")):
    engine_digest.update(engine_file.name.encode("utf-8"))
    engine_digest.update(engine_file.read_bytes())
require(engine_digest.hexdigest() == signal_source.get("engine_source_sha256"), "engine source hash mismatch")
require(overlay.get("path") == runtime.get("candidate_path"), "overlay path mismatch")
require(overlay.get("file_sha256") == candidate_hash, "overlay file hash mismatch")
require(overlay.get("artifact_sha256") == candidate.get("artifact_sha256"), "overlay artifact hash mismatch")
require(overlay.get("scope") == "SETUP_FILTERS_AND_RISK_SCALING_ONLY", "overlay scope mismatch")
require(overlay.get("defines_base_signals") is False, "overlay cannot define base signals")
require(overlay_runtime == (app / "scripts/run_super1_xm_mt5_forward.py").resolve(), "unexpected overlay runtime")
require(sha256(super_candidate) == overlay.get("runtime_sha256"), "overlay runtime hash mismatch")
require(order_transport_path == (app / "scripts/run_xm_mt5_forward.py").resolve(), "unexpected demo order transport")
require(sha256(xm_candidate) == order_transport.get("sha256"), "demo order transport hash mismatch")
require(order_transport.get("account_identity_gate") == "XM_FIXED_DEMO_TRADE_MODE_SERVER_COMPANY_LOGIN", "order transport identity gate mismatch")
require(safety.get("independent_super1_signal_producer_present") is False, "independent signal producer is forbidden")
require(safety.get("demo_order_execution_enabled") is True, "demo order execution must be enabled")
require(safety.get("real_money_live_enabled") is False, "real-money live mode must be disabled")
require(safety.get("fresh_forward_required") is True, "contract fresh-forward gate is missing")
require(safety.get("real_money_execution_allowed") is False, "real-money execution must be forbidden")
require(safety.get("deployed_pipeline_historical_parity_proven") is False, "pipeline parity cannot be claimed")
require(safety.get("candidate_research_results_apply_to_deployed_pipeline") is False, "candidate results cannot be applied to deployed pipeline")
require("live_enabled" not in safety, "ambiguous live_enabled safety field is forbidden")
require(runtime.get("execution") == "MT5_DEMO_ORDERS", "runtime execution mode mismatch")
require(runtime.get("account_mode") == "DEMO_ORDER", "runtime account mode mismatch")
require(runtime.get("deployment_mode") == "FROZEN_CANONICAL_PAIR_PIPELINE_WITH_SUPER1_OVERLAY", "runtime deployment mode mismatch")

research_inputs = [item for item in candidate.get("provenance", {}).get("inputs", []) if item.get("role") == "research_dataset"]
require(len(research_inputs) == 1, "candidate research dataset provenance is not unique")
deployment = manifest.get("deployment", {})
require(manifest.get("name") == "Super1", "manifest name mismatch")
require(manifest.get("status") == "LOCAL_FRESH_FORWARD_CANDIDATE_UNPROVEN", "manifest status mismatch")
require(manifest.get("candidate_path") == runtime.get("candidate_path"), "manifest candidate path mismatch")
require(manifest.get("candidate_file_sha256") == candidate_hash, "manifest candidate hash mismatch")
require(manifest.get("candidate_artifact_sha256") == candidate.get("artifact_sha256"), "manifest artifact mismatch")
require(manifest.get("signal_contract_path") == runtime.get("signal_contract_path"), "manifest contract path mismatch")
require(manifest.get("signal_contract_sha256") == contract_hash, "manifest contract hash mismatch")
require(manifest.get("config_sha256") == runtime_hash, "manifest runtime hash mismatch")
require(manifest.get("deployment_mode") == "FROZEN_CANONICAL_PAIR_PIPELINE_WITH_SUPER1_OVERLAY", "manifest deployment mode mismatch")
require(manifest.get("overlay_candidate_research_dataset_sha256") == research_inputs[0].get("sha256"), "manifest research dataset mismatch")
require(manifest.get("overlay_candidate_research_result_sha256") == candidate.get("full_evaluation_result_sha256"), "manifest research result mismatch")
require(manifest.get("deployed_pipeline_historical_parity_proven") is False, "manifest parity cannot be claimed")
require("deployed_pipeline_result_sha256" in manifest and manifest.get("deployed_pipeline_result_sha256") is None, "manifest deployed result must be null")
require("data_sha256" not in manifest and "result_sha256" not in manifest, "legacy ambiguous manifest hashes are forbidden")
require(deployment.get("demo_order_execution_enabled") is True, "manifest demo execution must be enabled")
require(deployment.get("real_money_live_enabled") is False, "manifest real-money live mode must be disabled")
require(deployment.get("real_money_execution_allowed") is False, "manifest real-money execution must be forbidden")
require("live_enabled" not in deployment, "ambiguous deployment live_enabled field is forbidden")
require(manifest.get("fresh_forward_required") is True, "manifest fresh-forward gate is missing")
require(manifest.get("proven") is False, "manifest proven must be false")
'@
$tupleValidator | & $Python -I -E -B - $App $RuntimeCandidate $ManifestCandidate $ContractCandidate `
    $CapitalCandidate $XmCandidate $SuperCandidate
if ($LASTEXITCODE -ne 0) {
    throw "Super1 staged candidate tuple validation failed."
}

$runtime = Get-Content -LiteralPath $RuntimeCandidate -Raw | ConvertFrom-Json
if (
    [int]$runtime.account_login -ne 1301910045 -or
    [string]::IsNullOrWhiteSpace([string]$runtime.expected_server) -or
    [string]::IsNullOrWhiteSpace([string]$runtime.expected_company)
) {
    throw "Super1 staged runtime has an unexpected broker identity."
}
$readinessPattern = '^' + [regex]::Escape($ArchiveRoot) +
    '\\readiness-[a-f0-9]{32}\\output\\result\.json$'
if ($ReadinessPath -cnotmatch $readinessPattern) {
    throw "Super1 rollover accepts only a sealed private readiness result."
}
$readinessTransaction = Split-Path -Parent (Split-Path -Parent $ReadinessPath)
Assert-Super1SecureSealedTree -Path $readinessTransaction
$readinessHash = Get-Super1SecureSha256 -Path $ReadinessPath
if ($readinessHash -cne $ExpectedReadinessSha256.ToLowerInvariant()) {
    throw "Super1 readiness evidence does not match the mandatory SHA-256 pin."
}
$readinessLock = [IO.File]::Open(
    $ReadinessPath,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
try {
    $flat = [IO.File]::ReadAllText($ReadinessPath) | ConvertFrom-Json
    if ((Get-Super1SecureSha256 -Path $ReadinessPath) -cne $readinessHash) {
        throw "Super1 readiness evidence changed while read-locked."
    }
    $flatAge = [DateTimeOffset]::UtcNow -
        [DateTimeOffset]::Parse([string]$flat.checked_at_utc).ToUniversalTime()
    $flatCheckedAt = [DateTimeOffset]::Parse(
        [string]$flat.checked_at_utc
    ).ToUniversalTime()
    $marketSchedule = Get-Super1SecureMarketScheduleState `
        -CheckedAt $flatCheckedAt `
        -Runtime $runtime
    $identityChecks = @($flat.identity_checks.PSObject.Properties | ForEach-Object { [bool]$_.Value })
    $permissionChecks = @($flat.permission_checks.PSObject.Properties | ForEach-Object { [bool]$_.Value })
    $preflight = $flat.order_transport_preflight
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
    $pin = [IO.File]::ReadAllText($TerminalPin) | ConvertFrom-Json
    $terminalPath = [IO.Path]::GetFullPath([string]$flat.terminal_path)
    $terminalDataPath = [IO.Path]::GetFullPath([string]$flat.terminal_data_path)
    $evidenceRoot = [IO.Path]::GetFullPath([string]$flat.evidence_root)
    $readinessOutput = [IO.Path]::GetFullPath((Split-Path -Parent $ReadinessPath))
    if ($flatAge.TotalSeconds -lt -5 -or $flatAge.TotalSeconds -gt 90 -or
        [string]$flat.evidence_nonce -notmatch '^[a-f0-9]{32}$' -or
        [string]$flat.profile -cne "super1" -or
        -not [bool]$flat.ready -or -not [bool]$flat.flat -or
        -not [bool]$flat.identity_ready -or -not [bool]$flat.transport_ready -or
        $identityChecks.Count -lt 5 -or $permissionChecks.Count -lt 5 -or
        -not [bool]$flat.identity_checks.windows_profile -or
        $identityChecks -contains $false -or $permissionChecks -contains $false -or
        [int]$flat.account_login -ne [int]$runtime.account_login -or
        [string]$flat.server -cne [string]$runtime.expected_server -or
        [string]$flat.company -cne [string]$runtime.expected_company -or
        [int]$flat.open_orders -ne 0 -or [int]$flat.open_positions -ne 0 -or
        [string]$flat.permission.state -cne "READY" -or
        [bool]$flat.transport_preflight_deferred -ne [bool]$preflightDeferred -or
        -not ($preflightPass -or $preflightDeferred) -or
        [bool]$preflight.order_send_called -or
        -not $terminalPath.Equals(
            [IO.Path]::GetFullPath([string]$pin.terminal_path),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not $terminalDataPath.Equals(
            [IO.Path]::GetFullPath((Split-Path -Parent ([string]$pin.terminal_path))),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not $evidenceRoot.StartsWith(
            ($readinessOutput + [IO.Path]::DirectorySeparatorChar),
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Super1 live broker readiness/flat verification is invalid or stale."
    }
}
finally { $readinessLock.Dispose() }
Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask

$stateAcl = Get-Acl -LiteralPath $State
$stamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$archive = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot "campaign-$stamp"))
if (-not $archive.StartsWith(($ArchiveRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe Super1 archive path: $archive"
}
$archivedState = Join-Path $archive "state"
$contractPreviouslyPresent = Test-Path -LiteralPath $ContractTarget -PathType Leaf
$candidatePairs = @(
    [pscustomobject]@{ Source = $CapitalCandidate; Staged = (Join-Path $archive "run_capital_forward.py.candidate"); Target = $CapitalTarget; Backup = (Join-Path $archive "run_capital_forward.py.previous") },
    [pscustomobject]@{ Source = $XmCandidate; Staged = (Join-Path $archive "run_xm_mt5_forward.py.candidate"); Target = $XmTarget; Backup = (Join-Path $archive "run_xm_mt5_forward.py.previous") },
    [pscustomobject]@{ Source = $SuperCandidate; Staged = (Join-Path $archive "run_super1_xm_mt5_forward.py.candidate"); Target = $SuperTarget; Backup = (Join-Path $archive "run_super1_xm_mt5_forward.py.previous") },
    [pscustomobject]@{ Source = $RuntimeCandidate; Staged = (Join-Path $archive "super1_xm_mt5_demo_config.json.candidate"); Target = $RuntimeTarget; Backup = (Join-Path $archive "super1_xm_mt5_demo_config.json.previous") },
    [pscustomobject]@{ Source = $ManifestCandidate; Staged = (Join-Path $archive "super1_manifest.json.candidate"); Target = $ManifestTarget; Backup = (Join-Path $archive "super1_manifest.json.previous") },
    [pscustomobject]@{ Source = $ContractCandidate; Staged = (Join-Path $archive "super1_signal_contract.json.candidate"); Target = $ContractTarget; Backup = (Join-Path $archive "super1_signal_contract.json.previous") }
)
$tasksStopped = $false
$requestEvidence = $null
$transactionRequestEvidence = $null
$initTransaction = $null
$healthRaw = $null
$watchdogRaw = $null

try {
    if (Test-Path -LiteralPath $archive) {
        throw "Super1 archive already exists: $archive"
    }
    New-Item -ItemType Directory -Path $archive | Out-Null

    $tasksStopped = $true
    Stop-Super1SecureRuntime -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask
    Assert-FrozenSuper1TaskContracts
    $internalFlatRaw = @(& $trustedPowerShell `
        -NoProfile `
        -NonInteractive `
        -ExecutionPolicy Bypass `
        -File $InternalFlatScript `
        -KeepStopped) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($internalFlatRaw)) {
        throw "Super1 internal pre-mutation broker flat probe failed."
    }
    $internalFlat = $internalFlatRaw | ConvertFrom-Json
    $internalChecked = [DateTimeOffset]::Parse(
        [string]$internalFlat.checked_at_utc
    ).ToUniversalTime()
    $internalAge = [DateTimeOffset]::UtcNow - $internalChecked
    if ([string]$internalFlat.state -cne "READY_FLAT_SEALED" -or
        [string]$internalFlat.readiness_sha256 -notmatch '^[a-f0-9]{64}$' -or
        (Get-Super1SecureSha256 -Path ([string]$internalFlat.readiness_evidence)) -cne
            [string]$internalFlat.readiness_sha256 -or
        [int]$internalFlat.account_login -ne [int]$runtime.account_login -or
        [string]$internalFlat.server -cne [string]$runtime.expected_server -or
        [string]$internalFlat.company -cne [string]$runtime.expected_company -or
        [int]$internalFlat.open_orders -ne 0 -or
        [int]$internalFlat.open_positions -ne 0 -or
        -not [bool]$internalFlat.task_xml_unchanged -or
        # The broker timestamp precedes terminal shutdown, task-XML comparison,
        # and sealing of the evidence tree.  Those fail-closed steps can exceed
        # 15 seconds on Windows Server; tasks/processes are asserted stopped
        # again immediately below, so use the same bounded age as the supplied
        # sealed readiness proof.
        $internalAge.TotalSeconds -lt -5 -or $internalAge.TotalSeconds -gt 90) {
        throw "Super1 internal pre-mutation broker evidence is invalid or stale."
    }
    Assert-Super1SecureSealedTree -Path ([string]$internalFlat.evidence_transaction)
    Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask
    foreach ($pair in $candidatePairs) {
        if ($pair.Target -eq $ContractTarget -and -not $contractPreviouslyPresent) {
            continue
        }
        Copy-Item -LiteralPath $pair.Target -Destination $pair.Backup
    }
    foreach ($pair in $candidatePairs) {
        Move-Item -LiteralPath $pair.Source -Destination $pair.Staged
    }

    Move-Item -LiteralPath $State -Destination $archivedState
    New-Item -ItemType Directory -Path $State | Out-Null
    Set-Acl -LiteralPath $State -AclObject $stateAcl
    foreach ($pair in $candidatePairs) {
        Copy-Item -LiteralPath $pair.Staged -Destination $pair.Target -Force
        if ((Get-Sha256Lower -Path $pair.Staged) -ne (Get-Sha256Lower -Path $pair.Target)) {
            throw "Super1 deployed file hash mismatch: $($pair.Target)"
        }
    }

    $initId = [Guid]::NewGuid().ToString("N")
    $initNonce = [Guid]::NewGuid().ToString("N")
    $initTransaction = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot "readiness-$initId"))
    $initOutput = [IO.Path]::GetFullPath((Join-Path $initTransaction "output"))
    $initResult = [IO.Path]::GetFullPath((Join-Path $initOutput "result.json"))
    $initProducer = [IO.Path]::GetFullPath((Join-Path $initOutput "producer.json"))
    $initTransactionRequest = [IO.Path]::GetFullPath((Join-Path $initTransaction "request.json"))
    New-Super1SecureDirectory -Path $initTransaction -RunnerSid $runnerSid
    New-Super1SecureDirectory `
        -Path $initOutput `
        -RunnerSid $runnerSid `
        -RunnerRights ([Security.AccessControl.FileSystemRights]::Modify)
    $initRequestedAt = [DateTimeOffset]::UtcNow
    $launcherSha256 = Get-Super1SecureSha256 -Path $RunScript
    $requestJson = [ordered]@{
        schema_version = 1
        kind = "rollover_init"
        transaction_id = $initId
        nonce = $initNonce
        requested_at_utc = $initRequestedAt.ToString("o")
        expected_runner_sid = $runnerSid
        expected_launcher_sha256 = $launcherSha256
        request_path = $initTransactionRequest
        result_path = $initResult
        producer_path = $initProducer
    } | ConvertTo-Json -Compress
    $transactionRequestEvidence = New-Super1SecureLockedFile `
        -Path $initTransactionRequest `
        -Content $requestJson `
        -RunnerSid $runnerSid
    $requestEvidence = New-Super1SecureLockedFile `
        -Path $ProbeRequest `
        -Content $requestJson `
        -RunnerSid $runnerSid
    Start-ScheduledTask -TaskName $MainTask
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        $initState = [string](Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop).State
    } while ($initState -in @("Running", "Queued") -and [DateTimeOffset]::UtcNow -lt $deadline)
    if ($initState -in @("Running", "Queued")) {
        throw "Super1 campaign initializer timed out."
    }
    Stop-Super1SecureRuntime -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask
    Assert-FrozenSuper1TaskContracts
    $taskResult = [int](Get-ScheduledTaskInfo -TaskName $MainTask -ErrorAction Stop).LastTaskResult
    if ($taskResult -ne 0 -or -not (Test-Path -LiteralPath $initResult -PathType Leaf)) {
        throw "Super1 campaign initialization task failed: task=$taskResult"
    }
    $requestEvidence.lock.Dispose()
    $requestEvidence = $null
    $transactionRequestEvidence.lock.Dispose()
    $transactionRequestEvidence = $null
    Remove-Item -LiteralPath $ProbeRequest -Force
    Seal-Super1SecureEvidenceTree -Path $initTransaction
    $initProducerBinding = Get-Super1SecureProducerEnvelope `
        -ProducerPath $initProducer `
        -RequestPath $initTransactionRequest `
        -ResultPath $initResult `
        -TransactionId $initId `
        -Nonce $initNonce `
        -Kind "rollover_init" `
        -RunnerSid $runnerSid `
        -ExpectedLauncherPath $RunScript `
        -ExpectedLauncherSha256 $launcherSha256 `
        -ExpectedExitCode 0 `
        -NotBefore $initRequestedAt
    $initPayload = $initProducerBinding.result_payload
    $initAge = [DateTimeOffset]::UtcNow -
        [DateTimeOffset]::Parse([string]$initPayload.checked_at_utc).ToUniversalTime()
    if ([int]$initPayload.schema_version -ne 1 -or
        [string]$initPayload.evidence_nonce -cne $initNonce -or
        [string]$initPayload.state -cne "INITIALIZED" -or
        [int]$initPayload.exit_code -ne 0 -or
        $initAge.TotalSeconds -lt -5 -or $initAge.TotalSeconds -gt 45 -or
        -not (Test-Path -LiteralPath (Join-Path $State "campaign_lock.json") -PathType Leaf)) {
        throw "Super1 sealed campaign initialization evidence failed."
    }

    $rolloutStarted = [DateTimeOffset]::UtcNow
    Start-ScheduledTask -TaskName $MainTask
    $healthPath = Join-Path $State "health.json"
    Wait-RunningTaskFreshState -TaskName $MainTask -Path $healthPath -NotBefore $rolloutStarted `
        -TimeoutSeconds 90 -ExpectedState "RUNNING"
    Start-ScheduledTask -TaskName $WatchdogTask
    $watchdogPath = Join-Path $Root "watchdog_status.json"
    Wait-RunningTaskFreshState -TaskName $WatchdogTask -Path $watchdogPath -NotBefore $rolloutStarted `
        -TimeoutSeconds 90 -ExpectedState "HEALTHY" -ExpectedMainTask $MainTask
    $healthRaw = Get-Content -LiteralPath $healthPath -Raw
    $watchdogRaw = Get-Content -LiteralPath $watchdogPath -Raw
    $healthRecord = $healthRaw | ConvertFrom-Json
    $watchdogRecord = $watchdogRaw | ConvertFrom-Json
    if (
        [string]$healthRecord.state -ne "RUNNING" -or
        -not (Test-Path -LiteralPath (Join-Path $State "campaign_lock.json") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $State "fatal_latch.json") -PathType Leaf)
    ) {
        throw "Super1 post-rollover health/campaign/fatal-latch gate failed."
    }
    if (
        [string]$watchdogRecord.state -ne "HEALTHY" -or
        [string]$watchdogRecord.main_task -ne $MainTask
    ) {
        throw "Super1 post-rollover watchdog gate failed."
    }
    Assert-FrozenSuper1TaskContracts
}
catch {
    $failure = $_
    $rollbackErrors = @()
    try {
        Stop-Super1SecureRuntime -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask
    }
    catch { $rollbackErrors += "stopped-state gate: $($_.Exception.Message)" }
    if ($requestEvidence -and $requestEvidence.lock) {
        try { $requestEvidence.lock.Dispose(); $requestEvidence = $null }
        catch { $rollbackErrors += "probe read-lock cleanup: $($_.Exception.Message)" }
    }
    if ($transactionRequestEvidence -and $transactionRequestEvidence.lock) {
        try { $transactionRequestEvidence.lock.Dispose(); $transactionRequestEvidence = $null }
        catch { $rollbackErrors += "transaction request read-lock cleanup: $($_.Exception.Message)" }
    }
    if (Test-Path -LiteralPath $ProbeRequest -PathType Leaf) {
        try { Remove-Item -LiteralPath $ProbeRequest -Force }
        catch { $rollbackErrors += "probe request cleanup: $($_.Exception.Message)" }
    }
    try { Assert-FrozenSuper1TaskContracts }
    catch { $rollbackErrors += "fixed task contract changed: $($_.Exception.Message)" }
    if ($rollbackErrors.Count -ne 0) {
        throw "Super1 rollover failed ($($failure.Exception.Message)); rollback not safe: $($rollbackErrors -join '; ')"
    }

    foreach ($pair in $candidatePairs) {
        try {
            if ($pair.Target -eq $ContractTarget -and -not $contractPreviouslyPresent) {
                Remove-Item -LiteralPath $ContractTarget -Force -ErrorAction SilentlyContinue
            }
            elseif (Test-Path -LiteralPath $pair.Backup -PathType Leaf) {
                Copy-Item -LiteralPath $pair.Backup -Destination $pair.Target -Force
            }
        }
        catch { $rollbackErrors += "file restore $($pair.Target): $($_.Exception.Message)" }
    }

    try {
        if (Test-Path -LiteralPath $archivedState -PathType Container) {
            if (Test-Path -LiteralPath $State) {
                Move-Item -LiteralPath $State -Destination (Join-Path $archive "failed-new-state")
            }
            Move-Item -LiteralPath $archivedState -Destination $State
        }
    }
    catch { $rollbackErrors += "state restore: $($_.Exception.Message)" }

    foreach ($pair in $candidatePairs) {
        try {
            if ((Test-Path -LiteralPath $pair.Staged -PathType Leaf) -and -not (Test-Path -LiteralPath $pair.Source)) {
                Move-Item -LiteralPath $pair.Staged -Destination $pair.Source
            }
        }
        catch { $rollbackErrors += "candidate restore $($pair.Source): $($_.Exception.Message)" }
    }

    if ($tasksStopped -and $rollbackErrors.Count -eq 0) {
        try {
            Start-ScheduledTask -TaskName $MainTask
            Start-ScheduledTask -TaskName $WatchdogTask
        }
        catch {
            $rollbackErrors += "task restart: $($_.Exception.Message)"
            Stop-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
            Stop-ScheduledTask -TaskName $MainTask -ErrorAction SilentlyContinue
        }
    }
    if ($rollbackErrors.Count -ne 0) {
        throw "Super1 rollover failed ($($failure.Exception.Message)); rollback incomplete: $($rollbackErrors -join '; ')"
    }
    throw $failure
}
finally {
    if ($requestEvidence -and $requestEvidence.lock) {
        $requestEvidence.lock.Dispose()
    }
    if ($transactionRequestEvidence -and $transactionRequestEvidence.lock) {
        $transactionRequestEvidence.lock.Dispose()
    }
    if (Test-Path -LiteralPath $ProbeRequest -PathType Leaf) {
        Remove-Item -LiteralPath $ProbeRequest -Force -ErrorAction SilentlyContinue
    }
    $env:PSModulePath = $OriginalPSModulePath
    $env:PYTHONHOME = $OriginalPythonHome
    $env:PYTHONPATH = $OriginalPythonPath
}

[pscustomobject]@{
    archive = $archive
    readiness_evidence = $ReadinessPath
    readiness_sha256 = $readinessHash
    internal_readiness_evidence = [string]$internalFlat.readiness_evidence
    internal_readiness_sha256 = [string]$internalFlat.readiness_sha256
    initialization_evidence = $initResult
    initialization_sha256 = [string]$initProducerBinding.result_sha256
    task_xml_unchanged = (
        (Get-Super1SecureTaskXml -TaskName $MainTask) -ceq $originalTaskXml -and
        (Get-Super1SecureTaskXml -TaskName $WatchdogTask) -ceq $originalWatchdogXml
    )
    main = (Get-ScheduledTask -TaskName $MainTask).State.ToString()
    watchdog = (Get-ScheduledTask -TaskName $WatchdogTask).State.ToString()
    campaign_lock = (Join-Path $State "campaign_lock.json")
    capital_code_hash = (Get-FileHash -LiteralPath $CapitalTarget -Algorithm SHA256).Hash
    xm_code_hash = (Get-FileHash -LiteralPath $XmTarget -Algorithm SHA256).Hash
    super1_code_hash = (Get-FileHash -LiteralPath $SuperTarget -Algorithm SHA256).Hash
    runtime_hash = (Get-FileHash -LiteralPath $RuntimeTarget -Algorithm SHA256).Hash
    manifest_hash = (Get-FileHash -LiteralPath $ManifestTarget -Algorithm SHA256).Hash
    signal_contract_hash = (Get-FileHash -LiteralPath $ContractTarget -Algorithm SHA256).Hash
} | ConvertTo-Json -Compress
$healthRaw
$watchdogRaw
