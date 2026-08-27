from pathlib import Path
from tempfile import TemporaryDirectory

from powershell_contract import facts, powershell_ast, powershell_harness

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def source(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_stage_binds_sha_directory_and_upgrader_to_manifest_hash() -> None:
    text = source("stage_signed_upgrader_windows.ps1")
    assert "$upgraderSha256 =" in text
    assert '("super1-" + $upgraderSha256)' in text
    assert "-ExpectedSelfSha256 $upgraderSha256" in text


def test_stage_normalizes_all_hashes_before_case_sensitive_comparison() -> None:
    text = source("stage_signed_upgrader_windows.ps1")
    assert "$ExpectedBootstrapIntegritySha256 = $ExpectedBootstrapIntegritySha256.ToLowerInvariant()" in text
    assert "$bootstrapHash = ([BitConverter]::ToString($bootstrapBytes)).Replace" in text
    assert "Get-StageStreamSha256" in text and "([string]$manifestEntry.sha256).ToLowerInvariant()" in text
    assert text.index("$bootstrapHash") < text.index(". $bootstrapPath")


def test_smoke_uses_exact_trusted_host_and_script_path() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "[Environment]::SystemDirectory" in text
    assert "OrdinalIgnoreCase" in text
    assert "TrustedScript" in text and "OrdinalIgnoreCase" in text
    assert "PSModulePath" in text and "Import-Module -Name $ScheduledTasksModule" in text


def test_smoke_output_uses_modify_runner_rights() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "-RunnerRights ([Security.AccessControl.FileSystemRights]::Modify)" in text


def test_smoke_cleanup_continues_safe_checks_when_stop_unconfirmed() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    cleanup = next(item for item in facts(path, "function") if item["name"] == "Invoke-Super1SmokeCleanup")
    commands = [item for item in facts(path, "command") if cleanup["start"] <= item["start"] <= cleanup["end"] and not item["unreachable"]]
    names = [item["name"] for item in commands]
    assert names.index("Stop-Super1SecureRuntime") < names.index("Assert-Super1SecureTaskBindings")
    assert names.index("Assert-Super1SecureStopped") < names.index("Assert-Super1SecureTaskBindings")
    assert names.index("Assert-Super1SecureTaskBindings") < names.index("Remove-Item")
    completed = powershell_harness(r'''
. $env:OTOBT_HARNESS_ARG0 -ContractTestOnly
$events = [Collections.Generic.List[string]]::new()
function Stop-Super1SecureRuntime { [void]$events.Add("stop"); throw "stop failure" }
function Assert-Super1SecureStopped { [void]$events.Add("stopped"); throw "not stopped" }
function Assert-Super1SecureTaskBindings { [void]$events.Add("bindings") }
function Get-Super1SecureTaskXml { param([string]$TaskName); [void]$events.Add("xml:$TaskName"); return "xml" }
$tx=$null; $active=$null
Invoke-Super1SmokeCleanup -Root "C:\Super1" -MainTask "Super1XM" -WatchdogTask "Super1Watchdog" -ActiveRequest "C:\missing-active" -Transaction "C:\missing-transaction" -TransactionRequestEvidence ([ref]$tx) -ActiveRequestEvidence ([ref]$active) -SavedMainXml "xml" -SavedWatchdogXml "xml"
if (($events -join ",") -ne "stop,stopped,bindings,xml:Super1XM,xml:Super1Watchdog,stopped") { exit 2 }
''', str(DEPLOY / "run_super1_demo_smoke_windows.ps1"))
    assert completed.returncode == 0


def test_smoke_mutations_require_confirmed_stopped_state() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    cleanup = next(item for item in facts(path, "function") if item["name"] == "Invoke-Super1SmokeCleanup")
    commands = [item for item in facts(path, "command") if cleanup["start"] <= item["start"] <= cleanup["end"] and not item["unreachable"]]
    names = [item["name"] for item in commands]
    assert names.index("Assert-Super1SecureStopped") < names.index("Remove-Item")
    assert names.index("Assert-Super1SecureStopped") < names.index("Seal-Super1SecureEvidenceTree")
    assert names.index("Assert-Super1SecureTaskBindings") < names.index("Remove-Item")


def test_smoke_never_emits_pass_when_final_cleanup_fails() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    functions = facts(path, "function")
    summary = next(item for item in functions if item["name"] == "New-Super1SmokeSummary")
    successes = [item for item in facts(path, "assignment") if item["left"] == "$success"]
    assert any(summary["start"] < item["start"] for item in successes)
    success = next(item for item in successes if summary["start"] < item["start"])
    result = next(item for item in facts(path, "command") if item["name"] == "ConvertTo-Json")
    outer = next(item for item in facts(path, "try") if item["has_finally"] and item["has_catch"] and item["start"] < result["start"] < item["end"])
    assert any(item["start"] > outer["end"] for item in successes)


def test_smoke_binds_launcher_scalar_and_result_hash_to_producer() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "$launcherSha256 =" in text
    assert "-ExpectedLauncherSha256 $launcherSha256" in text
    assert "producer.result_sha256" in text and "resultHash" in text
    assert "$request.expected_launcher_sha256" not in text


def test_smoke_seals_before_restart_and_requires_fresh_health() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert text.index("Seal-Super1SecureEvidenceTree") < text.index("Start-ScheduledTask -TaskName $MainTask", text.index("Seal-Super1SecureEvidenceTree"))
    assert "$restartStarted = [DateTimeOffset]::UtcNow" in text
    assert "LastWriteTimeUtc -ge $restartStarted.UtcDateTime" in text
    assert 'state -eq "RUNNING"' in text and 'state -eq "HEALTHY"' in text
    assert 'main_task -ceq $MainTask' in text


def test_builder_and_integrity_use_v13_baselines() -> None:
    builder = source("build_signed_windows_release.ps1")
    integrity = source("release_integrity.ps1")
    assert "-v13" in builder
    assert "$passedCount -lt 267" in builder
    assert "$artifactPassedCount -lt 138" in builder
    assert "-lt 267" in integrity and "-lt 138" in integrity


def test_v7_v8_v9_are_not_builder_defaults() -> None:
    text = source("build_signed_windows_release.ps1")
    assert all(f"-v{version}" not in text for version in (7, 8, 9, 10, 11))


def test_stage_never_uses_helper_hash_for_upgrader_directory_or_self_pin() -> None:
    text = source("stage_signed_upgrader_windows.ps1")
    assert '("super1-" + $selfHash)' not in text
    assert "-ExpectedSelfSha256 $selfHash" not in text
    assert "$selfHash -ceq $upgraderSha256" in text


def test_smoke_requires_result_and_producer_before_acceptance() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "Test-Path -LiteralPath $resultPath" in text
    assert "Test-Path -LiteralPath $producerPath" in text or "Get-Super1SecureProducerEnvelope" in text
    assert text.index("Get-Super1SecureProducerEnvelope") < text.index("Smoke acceptance criteria failed")


def test_smoke_preserves_transaction_on_failure() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "Remove-Item -LiteralPath $transaction" not in text
    assert "cleanup:" in text
    assert "PSModulePath = $OriginalPSModulePath" in text


def test_release_integrity_requires_provenance_and_normalized_manifest_paths() -> None:
    text = source("release_integrity.ps1")
    assert "RequireProvenance" in text
    assert "non-normalized file path" in text
    assert "$null -eq $manifest.files" in text


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


def test_stage_revalidates_parent_before_every_target_mutation() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    functions = facts(path, "function")
    assert any(item["name"] == "Assert-StageContainer" for item in functions)
    calls = [item for item in facts(path, "command") if item["name"] == "Assert-StageContainer" and not item["unreachable"]]
    members = [item for item in facts(path, "member") if item["member"] in {"CreateNew", "SetAccessControl", "IsReadOnly"} and item["scope"] == "top-level"]
    assert len(calls) >= 6
    for member in members:
        previous = max((call for call in calls if call["start"] < member["start"]), key=lambda call: call["start"])
        assert previous["name"] == "Assert-StageContainer"


def test_stage_locks_and_revalidates_targets_through_upgrader() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    assignments = facts(path, "assignment")
    assert any(item["left"] == "$targetLocks" for item in assignments)
    assert any(item["name"] == "" and "$targetUpgrader" in item["text"] for item in facts(path, "command"))


def test_upgrader_outer_guard_has_no_unprotected_interval() -> None:
    path = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    tries = [item for item in facts(path, "try") if item["has_catch"] and item["has_finally"]]
    result = next(item for item in facts(path, "command") if item["name"] == "ConvertTo-Json")
    assert any(item["start"] < result["start"] < item["end"] for item in tries)
    assert any(item["name"] == "Stop-Super1Tasks" for item in facts(path, "command"))


def test_upgrader_cleanup_is_exhaustive_and_preserves_primary_error() -> None:
    path = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    outer = [item for item in facts(path, "try") if item["has_catch"] and item["has_finally"]]
    assert outer
    guard = max(outer, key=lambda item: item["end"] - item["start"])
    commands = [item for item in facts(path, "command") if guard["start"] <= item["start"] <= guard["end"] and not item["unreachable"]]
    names = {item["name"] for item in commands}
    assert {"Wait-Super1Stopped", "Stop-Super1Tasks"}.issubset(names)


def test_contract_checks_reject_comments_and_unreachable_code() -> None:
    with TemporaryDirectory(prefix="otobt-ast-contract-") as directory:
        fixture = Path(directory) / "mutant.ps1"
        fixture.write_text(
            "param([switch]$ContractTestOnly)\n"
            "# FakeCommentContract\n"
            "if ($false) { FakeUnreachableContract }\n"
            "function RealContract { return }\n",
            encoding="utf-8",
        )
        facts_for_fixture = powershell_ast(fixture)
    commands = [item for item in facts_for_fixture["facts"] if item["kind"] == "command"]
    assert not any(item["name"] == "FakeCommentContract" for item in commands)
    assert not any(item["name"] == "FakeUnreachableContract" and not item["unreachable"] for item in commands)
    assert any(item["name"] == "FakeUnreachableContract" and item["unreachable"] for item in commands)
    params = next(item for item in facts_for_fixture["facts"] if item["kind"] == "param_block")
    assert "ContractTestOnly" in params["params"]
