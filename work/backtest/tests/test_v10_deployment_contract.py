from pathlib import Path

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
    assert "$bootstrapHash = (Get-FileHash" in text and ".Hash.ToLowerInvariant()" in text
    assert "$existingHash = (Get-FileHash" in text and "$manifestHash = ([string]$manifestEntry.sha256).ToLowerInvariant()" in text
    assert text.index("$bootstrapHash") < text.index(". $bootstrapPath")


def test_stage_validates_containers_before_mutation_and_rejects_reparse_points() -> None:
    text = source("stage_signed_upgrader_windows.ps1")
    assert "function Assert-StageContainer" in text
    assert "ReparsePoint" in text
    assert text.index("Assert-StageContainer $deployRoot") < text.index("$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)", text.index("Assert-StageContainer $deployRoot"))
    assert "New staged file hash mismatch before ACL" in text


def test_smoke_uses_exact_trusted_host_and_script_path() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0"' in text
    assert "-cne $ExpectedPSHome" in text
    assert "TrustedScript" in text and "-cne $TrustedScript" in text
    assert "PSModulePath" in text and "Import-Module ScheduledTasks" in text


def test_smoke_output_uses_modify_runner_rights() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "-RunnerRights ([Security.AccessControl.FileSystemRights]::Modify)" in text


def test_smoke_keeps_request_locks_until_after_stop_then_seals() -> None:
    text = source("run_super1_demo_smoke_windows.ps1")
    assert "$transactionRequestEvidence" in text and "$activeRequestEvidence" in text
    assert text.index("Stop-Super1SecureRuntime") < text.index("$transactionRequestEvidence.lock.Dispose()")
    assert text.index("$activeRequestEvidence.lock.Dispose()") < text.index("Remove-Item -LiteralPath $activeRequest")
    assert text.index("Remove-Item -LiteralPath $activeRequest") < text.index("Seal-Super1SecureEvidenceTree")


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


def test_builder_and_integrity_use_v10_baselines() -> None:
    builder = source("build_signed_windows_release.ps1")
    integrity = source("release_integrity.ps1")
    assert '"-v10"' in builder
    assert "$passedCount -lt 258" in builder
    assert "$artifactPassedCount -lt 115" in builder
    assert "-lt 258" in integrity and "-lt 115" in integrity


def test_runbook_has_single_v10_incoming_staging_command() -> None:
    docs = (ROOT.parent.parent / "docs" / "LIVE_OPERATIONS_TR.md").read_text(encoding="utf-8")
    assert "v10" in docs.lower()
    assert docs.count("-ExpectedPythonSha256") == 1
    assert docs.count("-ExpectedTerminalSha256") == 1
    assert "-BootstrapIntegrityScript" in docs


def test_v7_v8_v9_are_not_builder_defaults() -> None:
    text = source("build_signed_windows_release.ps1")
    assert "-v7" not in text and "-v8" not in text


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
