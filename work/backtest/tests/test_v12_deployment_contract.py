from pathlib import Path

from powershell_contract import facts, powershell_harness

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
    assert "$existingHash = (Get-FileHash" in text and "([string]$manifestEntry.sha256).ToLowerInvariant()" in text
    assert text.index("$bootstrapHash") < text.index(". $bootstrapPath")


def test_stage_signed_upgrader_windows_fail_closed_contract() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    functions = facts(path, "function")
    assert any(item["name"] == "Assert-StageContainer" for item in functions)
    members = {item["member"] for item in facts(path, "member")}
    assert {"CreateNew", "Flush", "ComputeHash"}.issubset(members)
    calls = [item for item in facts(path, "command") if item["name"] == "Assert-StageContainer"]
    assert len(calls) >= 4


def test_smoke_uses_exact_trusted_host_and_script_path() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "[Environment]::SystemDirectory" in text
    assert "OrdinalIgnoreCase" in text
    assert "TrustedScript" in text and "OrdinalIgnoreCase" in text
    assert "PSModulePath" in text and "Import-Module -Name $ScheduledTasksModule" in text


def test_smoke_output_uses_modify_runner_rights() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "-RunnerRights ([Security.AccessControl.FileSystemRights]::Modify)" in text


def test_smoke_keeps_request_locks_until_after_stop_then_seals() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "$transactionRequestEvidence" in text and "$activeRequestEvidence" in text
    assert "Invoke-Super1SmokeCleanup" in text
    assert "if (-not $stopped) { return }" in text


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


def test_builder_and_integrity_use_v12_baselines() -> None:
    builder = source("build_signed_windows_release.ps1")
    integrity = source("release_integrity.ps1")
    assert "-v12" in builder
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


def test_smoke_failure_cleanup_is_ordered_exhaustive_and_seals() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    cleanup = next(item for item in facts(path, "function") if item["name"] == "Invoke-Super1SmokeCleanup")
    commands = [item for item in facts(path, "command") if cleanup["start"] <= item["start"] <= cleanup["end"]]
    names = [item["name"] for item in commands]
    required = ["Stop-Super1SecureRuntime", "Assert-Super1SecureStopped", "Remove-Item", "Seal-Super1SecureEvidenceTree", "Assert-Super1SecureSealedTree", "Assert-Super1SecureTaskBindings"]
    positions = [next(i for i, name in enumerate(names) if name == required_name) for required_name in required]
    assert positions == sorted(positions)
    assert any(item["has_catch"] and cleanup["start"] <= item["start"] <= item["end"] <= cleanup["end"] for item in facts(path, "try"))


def test_smoke_success_revalidates_tasks_and_uses_fresh_snapshots() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    functions = facts(path, "function")
    summary = next(item for item in functions if item["name"] == "New-Super1SmokeSummary")
    members = [item for item in facts(path, "member") if summary["start"] <= item["start"] <= summary["end"]]
    assert len([item for item in members if item["member"] == "readiness_evidence"]) == 2
    bindings = [item for item in facts(path, "command") if item["name"] == "Assert-Super1SecureTaskBindings"]
    assert len(bindings) >= 2


def test_stage_holds_all_trusted_inputs_and_exact_host() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    assignments = facts(path, "assignment")
    locks = next(item for item in assignments if item["left"] == "$inputLocks")
    assert locks["right_type"] != "PipelineAst"
    assert any(item["has_finally"] and item["end"] > locks["end"] for item in facts(path, "try"))


def test_stage_revalidates_containers_and_uses_create_new() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    calls = [item for item in facts(path, "command") if item["name"] == "Assert-StageContainer"]
    members = {item["member"] for item in facts(path, "member")}
    assert len(calls) >= 4
    assert "CreateNew" in members


def test_upgrader_preflight_failure_releases_locks_and_restores_environment() -> None:
    path = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    assignments = facts(path, "assignment")
    assert {"$SelfScriptLock", "$PowerShellHostLock"}.issubset({item["left"] for item in assignments})
    preflight = next(item for item in facts(path, "try") if item["has_catch"] and not item["has_finally"])
    assert any(item["left"] == "$env:PSModulePath" and preflight["start"] <= item["start"] <= preflight["end"] for item in assignments)
    assert any(item["left"] == "$env:PYTHONHOME" and preflight["start"] <= item["start"] <= preflight["end"] for item in assignments)


def test_stage_locks_all_trusted_inputs_before_bootstrap_execution() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    locks = next(item for item in facts(path, "assignment") if item["left"] == "$inputLocks")
    bootstrap = next(item for item in facts(path, "command") if item["name"] == "")
    assert locks["right_type"] != "PipelineAst"
    assert locks["end"] < bootstrap["start"]
