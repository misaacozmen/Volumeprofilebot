import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from powershell_contract import facts, powershell_ast, powershell_harness

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_BASE_ROOT = Path(os.environ.get("CONTRACT_BASE_ROOT", str(ROOT))).resolve()
CONTRACT_REPO_ROOT = Path(os.environ.get("CONTRACT_REPO_ROOT", str(ROOT.parents[1]))).resolve()
DEPLOY = CONTRACT_BASE_ROOT / "deploy"


def ast(name: str) -> dict:
    return powershell_ast(DEPLOY / name)


def commands(name: str) -> list[dict]:
    return facts(DEPLOY / name, "command")


def test_stage_binds_sha_directory_and_upgrader_to_manifest_hash() -> None:
    nodes = ast("stage_signed_upgrader_windows.ps1")
    assert any(item["left"] == "$upgraderSha256" for item in nodes["facts"] if item["kind"] == "assignment")
    command_text = "\n".join(item["text"] for item in nodes["facts"] if item["kind"] == "command")
    assert '("super1-" + $upgraderSha256)' in command_text
    assert "-ExpectedSelfSha256 $upgraderSha256" in command_text


def test_stage_normalizes_all_hashes_before_case_sensitive_comparison() -> None:
    nodes = ast("stage_signed_upgrader_windows.ps1")
    assignments = nodes["facts"]
    assert any(item["left"] == "$ExpectedBootstrapIntegritySha256" for item in assignments if item["kind"] == "assignment")
    assert any(item["left"] == "$bootstrapHash" for item in assignments if item["kind"] == "assignment")
    assert any(item["name"] == "Get-StageStreamSha256" for item in assignments if item["kind"] == "command")
    assert any(item["name"] == "Get-StageStreamSha256" for item in assignments if item["kind"] == "command")


def test_smoke_uses_exact_trusted_host_and_script_path() -> None:
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    command_text = "\n".join(item["text"] for item in nodes["facts"] if item["kind"] == "command")
    member_names = {item["member"] for item in nodes["facts"] if item["kind"] == "member"}
    assert {"SystemDirectory", "OrdinalIgnoreCase"}.issubset(member_names)
    assert any(item["left"] == "$TrustedScript" for item in nodes["facts"] if item["kind"] == "assignment")
    assert "Import-Module" in {item["name"] for item in nodes["facts"] if item["kind"] == "command"}
    assert "PSModulePath" in command_text


def test_smoke_output_uses_modify_runner_rights() -> None:
    assert any("RunnerRights" in item["text"] and "Modify" in item["text"] for item in commands("run_super1_demo_smoke_windows.ps1"))


def test_smoke_stop_unconfirmed_retains_request_locks() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    fn = next(item for item in ast("run_super1_demo_smoke_windows.ps1")["facts"] if item["kind"] == "function" and item["name"] == "Invoke-Super1SmokeCleanup")
    text = fn["extent_text"]
    assert "requestlockreleaseauthorized" in text.lower()
    assert "Dispose" in text and "if (-not $stopped)" in text
    assert "TransactionRequestEvidence.Value = $null" not in text.split("if (-not $stopped)", 1)[1].split("}", 1)[0]
    completed = powershell_harness(
        "$cleanupErrors=[Collections.Generic.List[string]]::new();"
        "function Add-SmokeCleanupError([string]$Message){[void]$cleanupErrors.Add($Message)};"
        + text + r'''
$events=[Collections.Generic.List[string]]::new(); $directory=Join-Path $env:TEMP ('otobt-lock-' + [Guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $directory | Out-Null
try {
  function Stop-Super1SecureRuntime { [void]$events.Add('stop'); throw 'stop failure' }
  function Assert-Super1SecureStopped { [void]$events.Add('stopped'); throw 'not stopped' }
  function Assert-Super1SecureTaskBindings { [void]$events.Add('bindings') }
  function Get-Super1SecureTaskXml { param([string]$TaskName); [void]$events.Add("xml:$TaskName"); return 'xml' }
  $txPath=Join-Path $directory 'tx'; $activePath=Join-Path $directory 'active'; $txLock=[IO.File]::Open($txPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read); $activeLock=[IO.File]::Open($activePath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
  $tx=[pscustomobject]@{lock=$txLock}; $active=[pscustomobject]@{lock=$activeLock}; $authorized=$false; $confirmed=$false
  Invoke-Super1SmokeCleanup -Root 'C:Super1' -MainTask 'Super1XM' -WatchdogTask 'Super1Watchdog' -ActiveRequest (Join-Path $directory 'missing-active') -Transaction (Join-Path $directory 'missing-tx') -TransactionRequestEvidence ([ref]$tx) -ActiveRequestEvidence ([ref]$active) -RequestLockReleaseAuthorized ([ref]$authorized) -StoppedConfirmed ([ref]$confirmed) -SavedMainXml 'xml' -SavedWatchdogXml 'xml'
  if ($authorized -or $confirmed -or $tx.lock.IsClosed -or $active.lock.IsClosed) { exit 2 }
} finally { if($txLock){$txLock.Dispose()}; if($activeLock){$activeLock.Dispose()}; Remove-Item $directory -Recurse -Force }
''',
    )
    assert completed.returncode == 0, completed.stderr


def test_smoke_cleanup_error_blocks_pass_output() -> None:
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    cleanup = next(item for item in nodes["facts"] if item["kind"] == "function" and item["name"] == "Invoke-Super1SmokeCleanup")
    result = next(item for item in nodes["facts"] if item["kind"] == "command" and item["name"] == "ConvertTo-Json")
    successes = [item for item in nodes["facts"] if item["kind"] == "assignment" and item["left"] == "$success"]
    assert any(item["start"] > cleanup["end"] for item in successes)
    assert any(item["start"] > result["start"] for item in successes)


def test_smoke_production_has_no_test_bypass_and_requires_confirm_demo() -> None:
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    params = next(item for item in nodes["facts"] if item["kind"] == "param_block")
    details = {item["name"]: item["mandatory"] for item in params["param_details"]}
    assert details.get("ConfirmDemo") is True
    assert "ContractTestOnly" not in params["params"]
    assert not any(item["name"] == "return" and item["scope"] == "top-level" for item in nodes["facts"] if item["kind"] == "command")


def test_smoke_binds_launcher_scalar_and_result_hash_to_producer() -> None:
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    text = "\n".join(item["text"] for item in nodes["facts"] if item["kind"] == "command")
    assert any(item["left"] == "$launcherSha256" for item in nodes["facts"] if item["kind"] == "assignment")
    assert "ExpectedLauncherSha256" in text
    assert any(item["member"] == "result_sha256" for item in nodes["facts"] if item["kind"] == "member")


def test_smoke_seals_before_restart_and_requires_fresh_health() -> None:
    commands_for_smoke = commands("run_super1_demo_smoke_windows.ps1")
    seal = next(item for item in commands_for_smoke if item["name"] == "Seal-Super1SecureEvidenceTree")
    restart = next(item for item in commands_for_smoke if item["name"] == "Start-ScheduledTask" and item["start"] > seal["start"])
    assert seal["start"] < restart["start"]
    assignments = {item["left"] for item in facts(DEPLOY / "run_super1_demo_smoke_windows.ps1", "assignment")}
    members = {item["member"] for item in facts(DEPLOY / "run_super1_demo_smoke_windows.ps1", "member")}
    assert "$restartStarted" in assignments and "LastWriteTimeUtc" in members
    conditions = "\n".join(str(clause["condition"]) for item in facts(DEPLOY / "run_super1_demo_smoke_windows.ps1", "if") for clause in item["clauses"])
    assert 'state -eq "RUNNING"' in conditions and 'state -eq "HEALTHY"' in conditions
    assert "main_task" in conditions


def test_builder_and_integrity_use_v14_baselines() -> None:
    builder = ast("build_signed_windows_release.ps1")
    integrity = ast("release_integrity.ps1")
    builder_text = "\n".join(item["text"] for item in builder["facts"] if item["kind"] == "command") + "\n" + "\n".join(item["right_text"] for item in builder["facts"] if item["kind"] == "assignment")
    integrity_text = "\n".join(item["text"] for item in integrity["facts"] if item["kind"] == "command") + "\n" + "\n".join(item["right_text"] for item in integrity["facts"] if item["kind"] == "assignment") + "\n" + "\n".join(item["condition_text"] for item in integrity["facts"] if item["kind"] == "if")
    assert "-v14" in builder_text
    assert "test_v14_deployment_contract.py" in builder_text
    assert "$passedCount" in builder_text and "$artifactPassedCount" in builder_text
    assert "267" in integrity_text and "138" in integrity_text


def test_builder_uses_only_v14_default() -> None:
    text = "\n".join(item["text"] for item in commands("build_signed_windows_release.ps1")) + "\n" + "\n".join(item["right_text"] for item in facts(DEPLOY / "build_signed_windows_release.ps1", "assignment"))
    assert all(f"-v{version}" not in text for version in (7, 8, 9, 10, 11, 12, 13))


def test_stage_never_uses_helper_hash_for_upgrader_directory_or_self_pin() -> None:
    nodes = ast("stage_signed_upgrader_windows.ps1")
    text = "\n".join(item["text"] for item in nodes["facts"] if item["kind"] == "command") + "\n" + "\n".join(item["condition_text"] for item in nodes["facts"] if item["kind"] == "if")
    assert '("super1-" + $selfHash)' not in text
    assert "-ExpectedSelfSha256 $upgraderSha256" in text
    assert "$selfHash -ceq $upgraderSha256" in text


def test_smoke_requires_result_and_producer_before_acceptance() -> None:
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    smoke_commands = commands("run_super1_demo_smoke_windows.ps1")
    assert {"Test-Path", "Get-Super1SecureProducerEnvelope"}.issubset({item["name"] for item in smoke_commands})


def test_smoke_preserves_transaction_on_failure() -> None:
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    text = "\n".join(item["text"] for item in nodes["facts"] if item["kind"] == "command")
    assert not any(item["name"] == "Remove-Item" and "$transaction" in item["text"] for item in nodes["facts"] if item["kind"] == "command")
    assert "PSModulePath" in text


def test_release_integrity_requires_provenance_and_normalized_manifest_paths() -> None:
    path = DEPLOY / "release_integrity.ps1"
    verifier = next(item for item in facts(path, "function") if item["name"] == "Assert-SignedReleaseArchive")
    assert "RequireProvenance" in verifier["extent_text"]
    assert "files" in {item["member"] for item in facts(path, "member")}


def test_smoke_trusts_exact_process_and_module_manifest() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    nodes = facts(path, "command")
    assert {item["name"] for item in nodes} >= {"Import-Module", "Get-ScheduledTask"}
    assert any(item["member"] == "GetCurrentProcess" for item in facts(path, "member"))
    assert any(item["name"] == "Join-Path" for item in nodes)


def test_smoke_restores_module_path_on_preflight_failure() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    assignments = facts(path, "assignment")
    module_mutation = next(item for item in assignments if item["left"] == "$env:PSModulePath")
    imports = [item for item in facts(path, "command") if item["name"] == "Import-Module"]
    assert imports
    assert any(
        item["has_finally"] and item["start"] < module_mutation["start"] and item["end"] >= imports[0]["end"]
        for item in facts(path, "try")
    )
    completed = powershell_harness('$env:PSModulePath="before"; try { $env:PSModulePath="trusted"; throw "x" } catch {} finally { $env:PSModulePath="before" }; if($env:PSModulePath -ne "before"){exit 1}')
    assert completed.returncode == 0, completed.stderr


def test_producer_snapshot_returns_exact_locked_result_and_exit_code() -> None:
    path = DEPLOY / "super1_secure_task.ps1"
    functions = facts(path, "function")
    producer = next(item for item in functions if item["name"] == "Get-Super1SecureProducerEnvelope")
    readers = [item for item in facts(path, "command") if item["name"] == "New-Object"]
    try_blocks = [item for item in facts(path, "try") if producer["start"] <= item["start"] <= producer["end"]]
    assert len(readers) >= 3
    assert len([item for item in try_blocks if item["has_finally"]]) >= 4
    completed = powershell_harness(r'''
Import-Module Microsoft.PowerShell.Utility -Force
. $env:OTOBT_HARNESS_ARG0
$directory = Join-Path $env:TEMP ("otobt-producer-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $directory | Out-Null
try {
    $request = Join-Path $directory "request.json"
    $result = Join-Path $directory "result.json"
    $producer = Join-Path $directory "producer.json"
    $launcher = Join-Path $PSHOME "powershell.exe"
    $now = [DateTimeOffset]::UtcNow
    $resultPayload = [ordered]@{ state = "PASS" }
    [IO.File]::WriteAllText($result, ($resultPayload | ConvertTo-Json -Compress) + "`n")
    $requestPayload = [ordered]@{ schema_version = 1; kind = "test"; transaction_id = "t"; nonce = "n"; requested_at_utc = $now.ToString("o"); expected_runner_sid = "S-1-5-21-test"; expected_launcher_sha256 = (Get-Super1SecureSha256 $launcher); result_path = $result; producer_path = $producer; request_path = $request }
    [IO.File]::WriteAllText($request, ($requestPayload | ConvertTo-Json -Compress) + "`n")
    $resultHash = Get-Super1SecureSha256 $result
    $producerPayload = [ordered]@{ schema_version = 1; kind = "test"; transaction_id = "t"; nonce = "n"; request_sha256 = (Get-Super1SecureSha256 $request); result_sha256 = $resultHash; exit_code = 0; producer_runner_sid = "S-1-5-21-test"; producer_process_id = 9999; launcher_sha256 = (Get-Super1SecureSha256 $launcher); launcher_path = $launcher; started_at_utc = $now.ToString("o"); produced_at_utc = $now.ToString("o") }
    [IO.File]::WriteAllText($producer, ($producerPayload | ConvertTo-Json -Compress) + "`n")
    $snapshot = Get-Super1SecureProducerEnvelope -ProducerPath $producer -RequestPath $request -ResultPath $result -TransactionId "t" -Nonce "n" -Kind "test" -RunnerSid "S-1-5-21-test" -ExpectedLauncherPath $launcher -ExpectedLauncherSha256 (Get-Super1SecureSha256 $launcher) -ExpectedExitCode 0 -NotBefore $now
    if ($snapshot.result_payload.state -ne "PASS" -or $snapshot.result_sha256 -ne $resultHash -or $snapshot.producer_exit_code -ne 0) { exit 2 }
    try { Get-Super1SecureProducerEnvelope -ProducerPath $producer -RequestPath $request -ResultPath $result -TransactionId "t" -Nonce "n" -Kind "test" -RunnerSid "S-1-5-21-test" -ExpectedLauncherPath $launcher -ExpectedLauncherSha256 (Get-Super1SecureSha256 $launcher) -ExpectedExitCode 7 -NotBefore $now; exit 3 } catch {}
    [IO.File]::WriteAllText($result, '{"state":"CHANGED"}`n')
    try { Get-Super1SecureProducerEnvelope -ProducerPath $producer -RequestPath $request -ResultPath $result -TransactionId "t" -Nonce "n" -Kind "test" -RunnerSid "S-1-5-21-test" -ExpectedLauncherPath $launcher -ExpectedLauncherSha256 (Get-Super1SecureSha256 $launcher) -ExpectedExitCode 0 -NotBefore $now; exit 4 } catch {}
} finally { if (Test-Path $directory) { Remove-Item $directory -Recurse -Force } }
''', str(path))
    assert completed.returncode == 0, completed.stderr


def test_stage_parent_target_lock_and_buffered_output_contract() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    functions = facts(path, "function")
    assert any(item["name"] == "Assert-StageContainer" for item in functions)
    calls = [item for item in facts(path, "command") if item["name"] == "Assert-StageContainer" and not item["unreachable"]]
    members = [item for item in facts(path, "member") if item["member"] in {"CreateNew", "SetAccessControl", "IsReadOnly"} and item["scope"] == "top-level"]
    assert len(calls) >= 6
    for member in members:
        previous = max((call for call in calls if call["start"] < member["start"]), key=lambda call: call["start"])
        assert previous["name"] == "Assert-StageContainer"
    target_locks = [item for item in facts(path, "assignment") if item["left"] == "$targetLocks"]
    assert target_locks
    assert any(item["name"] == "" and "$targetUpgrader" in item["text"] for item in facts(path, "command"))
    assert any(item["name"] == "Assert-StageTarget" and item["scope"] == "top-level" for item in facts(path, "command"))


def test_upgrader_early_preflight_preserves_primary_error() -> None:
    path = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    nodes = ast("upgrade_super1_signed_app_windows.ps1")
    assignments = [item for item in nodes["facts"] if item["kind"] == "assignment"]
    required = {"$SelfScriptLock", "$PowerShellHostLock", "$TerminalLock", "$BootstrapPythonLock", "$IntegrityScriptLock", "$RuntimeConfigEvidence", "$Result", "$primaryError", "$cleanupErrors", "$runtimeHelpersReady", "$runtimeControlEntered"}
    assert required.issubset({item["left"] for item in assignments})
    completed = powershell_harness(
        "$ErrorActionPreference='Stop'; & $env:OTOBT_HARNESS_ARG0 -Archive C:\\missing.zip -ExpectedPythonSha256 ('0'*64) -ExpectedTerminalSha256 ('0'*64) -ExpectedSelfSha256 ('0'*64)",
        str(path),
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "variable has not been set" not in combined.lower()
    assert "cleanup incomplete" not in combined.lower()


def test_upgrader_cleanup_state_precedes_outer_guard_and_runtime_calls_are_gated() -> None:
    path = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    nodes = ast("upgrade_super1_signed_app_windows.ps1")
    tries = [item for item in nodes["facts"] if item["kind"] == "try" and item["has_catch"] and item["has_finally"]]
    result = next(item for item in nodes["facts"] if item["kind"] == "command" and item["name"] == "ConvertTo-Json")
    outer = max(tries, key=lambda item: item["end"] - item["start"])
    assert any(item["start"] < outer["start"] for item in nodes["facts"] if item["kind"] == "assignment" and item["left"] == "$runtimeHelpersReady")
    assert outer["start"] < result["start"] < outer["end"]
    runtime_calls = [item for item in nodes["facts"] if item["kind"] == "command" and item["name"] in {"Stop-Super1Tasks", "Wait-Super1Stopped"} and item["if_branches"]]
    assert {item["name"] for item in runtime_calls} == {"Stop-Super1Tasks", "Wait-Super1Stopped"}
    assert all("runtimeHelpersReady" in str(item["if_branches"]) and "runtimeControlEntered" in str(item["if_branches"]) for item in runtime_calls)


def test_upgrader_cleanup_is_exhaustive_and_preserves_handles_and_inner_error() -> None:
    path = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    outer = [item for item in facts(path, "try") if item["has_catch"] and item["has_finally"]]
    assert outer
    guard = max(outer, key=lambda item: item["end"] - item["start"])
    commands = [item for item in facts(path, "command") if guard["start"] <= item["start"] <= guard["end"] and not item["unreachable"]]
    names = {item["name"] for item in commands}
    assert {"Wait-Super1Stopped", "Stop-Super1Tasks"}.issubset(names)
    assert not any(item["name"] == "Get-Variable" for item in commands)
    assert any(item["left"] == "$RuntimeConfigEvidence" for item in facts(path, "assignment"))


def test_contract_validators_reject_all_nonexecuting_spoof_fixtures() -> None:
    with TemporaryDirectory(prefix="otobt-ast-contract-") as directory:
        fixture = Path(directory) / "mutant.ps1"
        fixture.write_text(
            "param([switch]$ContractTestOnly)\n"
            "# CommentOnlySpoof\n"
            "$string = 'StringLiteralSpoof'\n"
            "if ($false) { FalseSpoof }\n"
            "if (0) { ZeroSpoof }\n"
            "if ($false -eq $true) { ComparisonSpoof }\n"
            "function NeverCalled { FunctionSpoof }\n"
            "try { WrongRegion } catch { }\n"
            "OutsideGuard\n",
            encoding="utf-8",
        )
        facts_for_fixture = powershell_ast(fixture)
    commands = [item for item in facts_for_fixture["facts"] if item["kind"] == "command"]
    assert not any("CommentOnlySpoof" in item["text"] for item in commands)
    names = {item["name"] for item in commands}
    assert {"FalseSpoof", "ZeroSpoof", "ComparisonSpoof", "FunctionSpoof", "WrongRegion", "OutsideGuard"}.issubset(names)
    for spoof in {"FalseSpoof", "ZeroSpoof", "ComparisonSpoof"}:
        item = next(item for item in commands if item["name"] == spoof)
        assert item["unreachable"]
    function_spoof = next(item for item in commands if item["name"] == "FunctionSpoof")
    wrong_region = next(item for item in commands if item["name"] == "WrongRegion")
    outside_guard = next(item for item in commands if item["name"] == "OutsideGuard")
    assert function_spoof["scope"] == "function:NeverCalled"
    assert not any(region["has_finally"] for region in wrong_region["try_regions"])
    assert not outside_guard["if_branches"]
    smoke_params = next(item for item in facts(DEPLOY / "run_super1_demo_smoke_windows.ps1", "param_block"))
    assert "ContractTestOnly" not in smoke_params["params"]
    params = next(item for item in facts_for_fixture["facts"] if item["kind"] == "param_block")
    assert "ContractTestOnly" in params["params"]
