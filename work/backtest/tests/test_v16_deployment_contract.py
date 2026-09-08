import os
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from powershell_contract import facts, powershell_ast, powershell_harness

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_BASE_ROOT = Path(os.environ.get("CONTRACT_BASE_ROOT", str(ROOT))).resolve()
CONTRACT_REPO_ROOT = Path(os.environ.get("CONTRACT_REPO_ROOT", str(ROOT.parents[1]))).resolve()
RELEASE_GENERATION = os.environ.get("CONTRACT_RELEASE_GENERATION", "v16")
RELEASE_NUMBER = RELEASE_GENERATION[1:]
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
    cleanup_text = fn["extent_text"]
    outer = max((item for item in facts(path, "try") if item["scope"] == "top-level" and item["has_catch"] and item["has_finally"]), key=lambda item: item["end"] - item["start"])
    outer_finally = "finally " + outer["finally_text"]
    assert "requestlockreleaseauthorized" in cleanup_text.lower()
    assert "Dispose" in cleanup_text and "if (-not $stopped)" in cleanup_text
    assert "TransactionRequestEvidence.Value = $null" not in cleanup_text.split("if (-not $stopped)", 1)[1].split("}", 1)[0]
    completed = powershell_harness(
        "$cleanupErrors=[Collections.Generic.List[string]]::new();"
        "function Add-SmokeCleanupError([string]$Message){[void]$cleanupErrors.Add($Message)};"
        + cleanup_text + r'''
function Invoke-Scenario([bool]$StaleAuthorization) {
  $events=[Collections.Generic.List[string]]::new(); $directory=Join-Path $env:TEMP ('otobt-lock-' + [Guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $directory | Out-Null
  $txLock=$null; $activeLock=$null
  try {
    function Stop-Super1SecureRuntime { [void]$events.Add('stop'); throw 'stop failure' }
    function Assert-Super1SecureStopped { [void]$events.Add('stopped'); throw 'not stopped' }
    function Assert-Super1SecureTaskBindings { [void]$events.Add('bindings') }
    function Get-Super1SecureTaskXml { param([string]$TaskName); [void]$events.Add("xml:$TaskName"); return 'xml' }
    $txPath=Join-Path $directory 'tx'; $activePath=Join-Path $directory 'active'; $txLock=[IO.File]::Open($txPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read); $activeLock=[IO.File]::Open($activePath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
    $tx=[pscustomobject]@{lock=$txLock}; $active=[pscustomobject]@{lock=$activeLock}; $authorized=$StaleAuthorization; $confirmed=$StaleAuthorization; $transactionRequestEvidence=$tx; $activeRequestEvidence=$active
    $OriginalPSModulePath='before'; $env:PSModulePath='trusted'
    try { Invoke-Super1SmokeCleanup -Root 'C:Super1' -MainTask 'Super1XM' -WatchdogTask 'Super1Watchdog' -ActiveRequest (Join-Path $directory 'missing-active') -Transaction (Join-Path $directory 'missing-tx') -TransactionRequestEvidence ([ref]$transactionRequestEvidence) -ActiveRequestEvidence ([ref]$activeRequestEvidence) -RequestLockReleaseAuthorized ([ref]$authorized) -StoppedConfirmed ([ref]$confirmed) -SavedMainXml 'xml' -SavedWatchdogXml 'xml' }
'''
        + outer_finally + r'''
    if ($authorized -or $confirmed -or $txLock.SafeFileHandle.IsClosed -or $activeLock.SafeFileHandle.IsClosed -or $transactionRequestEvidence -ne $tx -or $activeRequestEvidence -ne $active) { throw 'failed closed-state protection' }
    try { [IO.File]::Open($txPath,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::Read); throw 'second writer unexpectedly opened' } catch [IO.IOException] { }
  } finally { if($txLock){$txLock.Dispose()}; if($activeLock){$activeLock.Dispose()}; Remove-Item $directory -Recurse -Force }
}
Invoke-Scenario $false
Invoke-Scenario $true
function Invoke-RecoveryScenario {
  $events=[Collections.Generic.List[string]]::new(); $directory=Join-Path $env:TEMP ('otobt-recovery-' + [Guid]::NewGuid().ToString('N')); $transactionDirectory=Join-Path $directory 'transaction'; New-Item -ItemType Directory -Path $transactionDirectory | Out-Null
  $txLock=$null; $activeLock=$null; $script:sealed=0; $script:stoppedCalls=0
  try {
    function Stop-Super1SecureRuntime { [void]$events.Add('stop'); throw 'stop failure' }
    function Assert-Super1SecureStopped { $script:stoppedCalls++; [void]$events.Add("stopped:$script:stoppedCalls"); if($script:stoppedCalls -eq 1){throw 'first check failed'} }
    function Assert-Super1SecureTaskBindings { [void]$events.Add('bindings') }
    function Get-Super1SecureTaskXml { param([string]$TaskName); return 'xml' }
    function Seal-Super1SecureEvidenceTree { param([string]$Path); $script:sealed++ }
    function Assert-Super1SecureSealedTree { param([string]$Path) }
    $txPath=Join-Path $directory 'tx'; $activePath=Join-Path $directory 'active'; [IO.File]::WriteAllText($activePath,'active'); $txLock=[IO.File]::Open($txPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read); $activeLock=[IO.File]::Open($activePath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
    $txHandle=$txLock.SafeFileHandle; $activeHandle=$activeLock.SafeFileHandle; $tx=[pscustomobject]@{lock=$txLock}; $active=[pscustomobject]@{lock=$activeLock}; $authorized=$false; $confirmed=$false; $transactionRequestEvidence=$tx; $activeRequestEvidence=$active; $OriginalPSModulePath='before'; $env:PSModulePath='trusted'
    try { Invoke-Super1SmokeCleanup -Root 'C:Super1' -MainTask 'Super1XM' -WatchdogTask 'Super1Watchdog' -ActiveRequest $activePath -Transaction $transactionDirectory -TransactionRequestEvidence ([ref]$transactionRequestEvidence) -ActiveRequestEvidence ([ref]$activeRequestEvidence) -RequestLockReleaseAuthorized ([ref]$authorized) -StoppedConfirmed ([ref]$confirmed) -SavedMainXml 'xml' -SavedWatchdogXml 'xml' }
'''
        + outer_finally + r'''
    if ($transactionRequestEvidence -ne $null -or $activeRequestEvidence -ne $null -or -not $authorized -or -not $confirmed -or (Test-Path $activePath) -or $sealed -ne 1 -or -not $txHandle.IsClosed -or -not $activeHandle.IsClosed) { throw 'successful second stopped check did not complete cleanup' }
  } finally { if($txLock){$txLock.Dispose()}; if($activeLock){$activeLock.Dispose()}; Remove-Item $directory -Recurse -Force }
}
Invoke-RecoveryScenario
''',
    )
    assert completed.returncode == 0, completed.stderr


def test_smoke_cleanup_error_blocks_pass_output() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    cleanup = next(item for item in nodes["facts"] if item["kind"] == "function" and item["name"] == "Invoke-Super1SmokeCleanup")
    result = next(item for item in nodes["facts"] if item["kind"] == "command" and item["name"] == "ConvertTo-Json")
    successes = [item for item in nodes["facts"] if item["kind"] == "assignment" and item["left"] == "$success"]
    assert any(item["start"] > cleanup["end"] for item in successes)
    assert any(item["start"] > result["start"] for item in successes)
    outer = max((item for item in facts(path, "try") if item["scope"] == "top-level" and item["has_catch"] and item["has_finally"]), key=lambda item: item["end"] - item["start"])
    outer_finally = "finally " + outer["finally_text"]
    completed = powershell_harness(
        "$cleanupErrors=[Collections.Generic.List[string]]::new();"
        "function Add-SmokeCleanupError([string]$Message){[void]$cleanupErrors.Add($Message)};"
        + r'''
$script:activeDisposed=0; $OriginalPSModulePath='before'; $env:PSModulePath='trusted'; $requestLockReleaseAuthorized=$true; $stoppedConfirmed=$true
$txLock=[pscustomobject]@{}; Add-Member -InputObject $txLock -MemberType ScriptMethod -Name Dispose -Value { throw 'transaction dispose failure' }
$activeLock=[pscustomobject]@{}; Add-Member -InputObject $activeLock -MemberType ScriptMethod -Name Dispose -Value { $script:activeDisposed++ }
$transactionRequestEvidence=[pscustomobject]@{lock=$txLock}; $activeRequestEvidence=[pscustomobject]@{lock=$activeLock}
try { $bufferedSummaryJson=$null }
'''
        + outer_finally + r'''
if ($transactionRequestEvidence -eq $null -or $activeRequestEvidence -ne $null -or $script:activeDisposed -ne 1 -or $env:PSModulePath -ne 'before' -or $cleanupErrors.Count -eq 0 -or $bufferedSummaryJson) { exit 1 }
''',
    )
    assert completed.returncode == 0, completed.stderr


def test_smoke_production_has_no_test_bypass_and_requires_confirm_demo() -> None:
    path = DEPLOY / "run_super1_demo_smoke_windows.ps1"
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    params = next(item for item in nodes["facts"] if item["kind"] == "param_block")
    details = {item["name"]: item["mandatory"] for item in params["param_details"]}
    assert details.get("ConfirmDemo") is True
    assert "ContractTestOnly" not in params["params"]
    assert not any(item["user_path"].lower() == "contracttestonly" for item in nodes["facts"] if item["kind"] == "variable")
    assert not any(item["scope"] == "top-level" for item in nodes["facts"] if item["kind"] == "return")
    bypass = powershell_harness(
        "$ContractTestOnly=$true; & $env:OTOBT_HARNESS_ARG0 -ConfirmDemo:$false",
        str(path),
    )
    assert bypass.returncode != 0
    assert "Demo smoke requires -ConfirmDemo." in (bypass.stdout + bypass.stderr)
    real = powershell_harness("& $env:OTOBT_HARNESS_ARG0 -ConfirmDemo", str(path))
    real_output = real.stdout + real.stderr
    assert real.returncode != 0
    assert "VariableIsUndefined" not in real_output
    assert "did not produce a successful result" not in real_output
    assert "Demo smoke requires -ConfirmDemo." not in real_output


def test_watchdog_outbox_rewrites_full_array_and_delivers_one_item_per_invocation() -> None:
    path = DEPLOY / "watchdog_windows.ps1"
    completed = powershell_harness(
        r'''
$root = Join-Path $env:TEMP ("otobt-outbox-" + [Guid]::NewGuid().ToString("N")); New-Item -ItemType Directory -Path $root | Out-Null
$status = Join-Path $root "watchdog_status.json"; $credential = Join-Path $root "watchdog_telegram.dat"; [IO.File]::WriteAllText($credential, "test")
try {
  . $env:OTOBT_HARNESS_ARG0 -MainTaskName "unused" -HealthPath $status -ProcessPattern "unused" -StatusPath $status -LibraryOnly
  $sent = [Collections.Generic.List[string]]::new()
  function Send-TelegramText([string]$Message) { [void]$sent.Add($Message); return [pscustomobject]@{ ok = $true; result = [pscustomobject]@{ message_id = $sent.Count } } }
  $now = [DateTimeOffset]::UtcNow.ToString("o")
  $items = @(
    [pscustomobject]@{ key="event-a"; event_id="event-a"; dedupe_key="event-a"; state="PENDING"; message="A"; queued_at_utc=$now; attempt_count=0; attempted_at_utc=""; last_attempt_utc=""; next_attempt_utc=$now; telegram_message_id=0 },
    [pscustomobject]@{ key="event-b"; event_id="event-b"; dedupe_key="event-b"; state="PENDING"; message="B"; queued_at_utc=$now; attempt_count=0; attempted_at_utc=""; last_attempt_utc=""; next_attempt_utc=$now; telegram_message_id=0 }
  )
  Write-TelegramOutbox -Entries $items
  Invoke-TelegramOutboxDelivery | Out-Null
  $afterOne = @(Read-TelegramOutbox)
  if ($sent.Count -ne 1 -or $afterOne.Count -ne 2 -or $afterOne[0].state -ne "ACKED" -or $afterOne[1].state -ne "PENDING" -or $afterOne[0].event_id -ne "event-a") { throw "first delivery did not preserve the full array" }
  Invoke-TelegramOutboxDelivery | Out-Null
  $afterTwo = @(Read-TelegramOutbox)
  if ($sent.Count -ne 2 -or $afterTwo.Count -ne 2 -or @($afterTwo | Where-Object state -ne "ACKED").Count -ne 0 -or $afterTwo[1].event_id -ne "event-b") { throw "second delivery did not deliver exactly the remaining item" }
  $afterTwo[0].state = "IN_FLIGHT"; $afterTwo[0].telegram_message_id = 0; Write-TelegramOutbox -Entries $afterTwo; $script:deliveredTelegramKeys.Clear()
  Invoke-TelegramOutboxDelivery | Out-Null
  if ($sent.Count -ne 3) { throw "IN_FLIGHT recovery was not at-least-once" }
} finally { if (Test-Path $root) { Remove-Item $root -Recurse -Force } }
''',
        str(path),
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_smoke_binds_launcher_scalar_and_result_hash_to_producer() -> None:
    nodes = ast("run_super1_demo_smoke_windows.ps1")
    text = "\n".join(item["text"] for item in nodes["facts"] if item["kind"] == "command")
    assert any(item["left"] == "$launcherSha256" for item in nodes["facts"] if item["kind"] == "assignment")
    assert "ExpectedLauncherSha256" in text
    assert any(item["member"] == "result_sha256" for item in nodes["facts"] if item["kind"] == "member")


def test_smoke_seals_before_restart_and_requires_fresh_health() -> None:
    commands_for_smoke = commands("run_super1_demo_smoke_windows.ps1")
    seal = next(item for item in commands_for_smoke if item["name"] == "Seal-Super1SecureEvidenceTree")
    restart = next(
        item for item in commands_for_smoke
        if item["name"] == "Join-Path"
        and "start_super1_local_windows.ps1" in item["text"]
        and item["start"] > seal["start"]
    )
    assert seal["start"] < restart["start"]
    stop_commands = [
        item for item in commands_for_smoke
        if item["name"] == "Join-Path" and "stop_super1_local_windows.ps1" in item["text"]
    ]
    assert stop_commands
    assert "Start-ScheduledTask" not in (
        DEPLOY / "run_super1_demo_smoke_windows.ps1"
    ).read_text(encoding="utf-8")
    smoke_source = (DEPLOY / "run_super1_demo_smoke_windows.ps1").read_text(encoding="utf-8")
    assert "$restartStarted" in smoke_source
    assert "healthUpdatedAt -le $restartStarted" in smoke_source
    assert "watchdogUpdatedAt -le $restartStarted" in smoke_source
    conditions = "\n".join(str(clause["condition"]) for item in facts(DEPLOY / "run_super1_demo_smoke_windows.ps1", "if") for clause in item["clauses"])
    assert 'state -ne "RUNNING"' in smoke_source
    assert 'state -notin @("HEALTHY", "WAITING_MANUAL_LEASE")' in smoke_source
    assert "Get-ScheduledTask -TaskName $MainTask" in smoke_source
    assert conditions


def test_builder_and_integrity_use_v16_baselines() -> None:
    builder = ast("build_signed_windows_release.ps1")
    integrity = ast("release_integrity.ps1")
    builder_source = (DEPLOY / "build_signed_windows_release.ps1").read_text(encoding="utf-8")
    integrity_source = (DEPLOY / "release_integrity.ps1").read_text(encoding="utf-8")
    builder_text = "\n".join(item["text"] for item in builder["facts"] if item["kind"] == "command") + "\n" + "\n".join(item["right_text"] for item in builder["facts"] if item["kind"] == "assignment")
    integrity_text = "\n".join(item["text"] for item in integrity["facts"] if item["kind"] == "command") + "\n" + "\n".join(item["right_text"] for item in integrity["facts"] if item["kind"] == "assignment") + "\n" + "\n".join(item["condition_text"] for item in integrity["facts"] if item["kind"] == "if")
    assert f"-{RELEASE_GENERATION}" in builder_text
    assert "artifact_test_files.json" in builder_source
    assert "$passedCount" in builder_source and "$artifactPassedCount" in builder_source
    assert "Assert-ManifestTestGate" in integrity_source
    assert '"${Prefix}_nodeid_sha256"' in integrity_source


def test_builder_uses_only_v16_default() -> None:
    text = "\n".join(item["text"] for item in commands("build_signed_windows_release.ps1")) + "\n" + "\n".join(item["right_text"] for item in facts(DEPLOY / "build_signed_windows_release.ps1", "assignment"))
    assert all(f"-v{version}" not in text for version in (7, 8, 9, 10, 11, 12, 13, 14) if f"v{version}" != RELEASE_GENERATION)


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
    canonical_reader = next(item for item in facts(path, "function") if item["name"] == "Get-CanonicalArtifactTestFiles")
    artifact_validator = next(item for item in facts(path, "function") if item["name"] == "Assert-ReleaseArtifactTestFiles")
    assert "RequireProvenance" in verifier["extent_text"]
    assert "files" in {item["member"] for item in facts(path, "member")}
    assert "Get-CanonicalArtifactTestFiles" in artifact_validator["extent_text"]
    assert any(item["name"] == "Assert-ReleaseArtifactTestFiles" and item["scope"] == "function:Assert-SignedReleaseArchive" for item in facts(path, "command"))
    canonical = json.loads((DEPLOY / "artifact_test_files.json").read_text(encoding="utf-8"))["artifact_test_files"]
    correct = "@{artifact_test_files=@(" + ",".join("'" + item + "'" for item in canonical) + ")}"
    cases = [
        (correct, 0),
        ("@{}", 1),
        ("@{artifact_test_files=@(" + ",".join("'" + item + "'" for item in canonical[:-1]) + ")}", 1),
        ("@{artifact_test_files=@(" + ",".join("'" + item + "'" for item in canonical) + ",'extra.py')}", 1),
        ("@{artifact_test_files=@(" + ",".join("'" + item + "'" for item in canonical[:-1]) + ",'test_audit_ledger.py')}", 1),
        ("@{artifact_test_files=@(" + ",".join("'" + item + "'" for item in canonical[:-1]) + ",'TEST_V16_DEPLOYMENT_CONTRACT.PY')}", 1),
        ("@{artifact_test_files=@(" + ",".join("'" + item + "'" for item in reversed(canonical)) + ")}", 1),
    ]
    script = canonical_reader["extent_text"] + "\n"
    script += "function Assert-ReleaseArtifactTestFiles" + artifact_validator["extent_text"].split("function Assert-ReleaseArtifactTestFiles", 1)[1] + "\n"
    script += f"$root = '{DEPLOY.parent.as_posix()}'; function Invoke-ArtifactCase([object]$m){{try{{Assert-ReleaseArtifactTestFiles -Manifest $m -Root $root; return 0}}catch{{return 1}}}}\n"
    script += "$results=@();\n" + "\n".join(f"$results += Invoke-ArtifactCase ([pscustomobject]{value})" for value, _ in cases) + "\n$results -join ','"
    result = powershell_harness(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == ",".join(str(expected) for _, expected in cases)


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


def test_probe_control_acl_contract_is_ast_bound_to_task_runner_sid() -> None:
    def exact_call(nodes: dict, name: str, args: list[str], scope: str = "top-level") -> dict:
        matches = [
            item
            for item in nodes["facts"]
            if item["kind"] == "command"
            and item["name"] == name
            and item["scope"] == scope
            and item["args"] == args
            and not item["unreachable"]
        ]
        assert len(matches) == 1, (name, args, scope, matches)
        return matches[0]

    def assignment_binds_call(nodes: dict, left: str, call: dict) -> None:
        matches = [
            item
            for item in nodes["facts"]
            if item["kind"] == "assignment"
            and item["left"] == left
            and item["start"] <= call["start"] <= item["end"]
        ]
        assert len(matches) == 1, (left, call, matches)

    def bound_task_runner_call(nodes: dict, args: list[str], runner_name: str) -> dict:
        calls = [
            item
            for item in nodes["facts"]
            if item["kind"] == "command"
            and item["name"] == "Assert-Super1SecureTaskBindings"
            and item["scope"] == "top-level"
            and item["args"] == args
            and not item["unreachable"]
        ]
        bound = [
            call
            for call in calls
            if any(
                item["kind"] == "assignment"
                and item["left"] == runner_name
                and item["start"] <= call["start"] <= item["end"]
                for item in nodes["facts"]
            )
        ]
        assert len(bound) == 1, (args, runner_name, calls, bound)
        assignment_binds_call(nodes, runner_name, bound[0])
        return bound[0]

    smoke = ast("run_super1_demo_smoke_windows.ps1")
    bound_task_runner_call(
        smoke,
        ["-Root", "$Root", "-MainTask", "$MainTask", "-WatchdogTask", "$WatchdogTask"],
        "$RunnerSid",
    )
    exact_call(smoke, "Assert-Super1SecureDirectoryAcl", ["-Path", "$Archive"])
    exact_call(
        smoke,
        "Assert-Super1SecureDirectoryAcl",
        ["-Path", "$Control", "-RunnerSid", "$RunnerSid"],
    )
    control_calls = [
        item
        for item in smoke["facts"]
        if item["kind"] == "command"
        and item["name"] == "Assert-Super1SecureDirectoryAcl"
        and item["args"][:2] == ["-Path", "$Control"]
        and not item["unreachable"]
    ]
    assert all("-RunnerRights" not in item["args"] and "Modify" not in item["args"] for item in control_calls)

    for script, binding_args, runner_name in (
        (
            "check_super1_flat_windows.ps1",
            ["-Root", "$Root", "-MainTask", "$Task", "-WatchdogTask", "$WatchdogTask"],
            "$runnerSid",
        ),
        (
            "rollover_super1_campaign_windows.ps1",
            ["-Root", "$Root", "-MainTask", "$MainTask", "-WatchdogTask", "$WatchdogTask"],
            "$runnerSid",
        ),
    ):
        nodes = ast(script)
        bound_task_runner_call(nodes, binding_args, runner_name)

    flat = ast("check_super1_flat_windows.ps1")
    exact_call(flat, "Assert-Super1SecureDirectoryAcl", ["-Path", "$ArchiveRoot"])
    exact_call(flat, "Assert-Super1SecureDirectoryAcl", ["-Path", "$ProbeControl", "-RunnerSid", "$runnerSid"])
    rollover = ast("rollover_super1_campaign_windows.ps1")
    exact_call(rollover, "Assert-Super1SecureDirectoryAcl", ["-Path", "$ArchiveRoot"])
    exact_call(rollover, "Assert-Super1SecureDirectoryAcl", ["-Path", "$ProbeControl", "-RunnerSid", "$runnerSid"])
    runtime = ast("run_super1_windows.ps1")
    exact_call(runtime, "Assert-Super1SecureDirectoryAcl", ["-Path", "$ProbeControl", "-RunnerSid", "$RunnerSid"])

    upgrader = ast("upgrade_super1_signed_app_windows.ps1")
    exact_call(upgrader, "Initialize-Super1ProbeControl", ["-Path", "$ProbeControl", "-RunnerSid", "$runnerSid"])
    exact_call(
        upgrader,
        "Protect-Super1RuntimeConfigFiles",
        ["-Paths", "@($ServerConfigPath, $PasswordConfigPath)", "-RunnerSid", "$runnerSid"],
    )
    exact_call(
        upgrader,
        "Protect-Super1TerminalRuntime",
        [
            "-PointerPath",
            "$TerminalPointer",
            "-ExpectedRoot",
            "$TerminalRoot",
            "-ExpectedSha256",
            "$ExpectedTerminalSha256",
            "-RunnerSid",
            "$runnerSid",
            "-CallerSid",
            "$callerSid",
        ],
    )
    exact_call(
        upgrader,
        "Set-ExactSuper1DirectoryAcl",
        ["-Path", "$Path", "-RightsBySid", "$rights"],
        "function:Initialize-Super1ProbeControl",
    )
    exact_call(
        upgrader,
        "Assert-ExactSuper1DirectoryAcl",
        ["-Path", "$Path", "-RightsBySid", "$rights"],
        "function:Initialize-Super1ProbeControl",
    )
    runtime_config_calls = [
        item
        for item in upgrader["facts"]
        if item["kind"] == "command"
        and item["name"] in {"Set-ExactSuper1FileAcl", "Assert-ExactSuper1FileAcl"}
        and item["scope"] == "function:Protect-Super1RuntimeConfigFiles"
        and not item["unreachable"]
    ]
    assert [item["name"] for item in runtime_config_calls] == ["Set-ExactSuper1FileAcl", "Assert-ExactSuper1FileAcl"]
    assert all(item["args"] == ["-Path", "$path", "-RightsBySid", "$rights"] for item in runtime_config_calls)
