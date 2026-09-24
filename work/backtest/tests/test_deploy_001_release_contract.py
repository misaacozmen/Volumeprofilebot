from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from functools import lru_cache
from pathlib import Path

import pytest

from powershell_contract import facts, powershell_ast, powershell_harness


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
CONTRACT_PATH = DEPLOY / "release_integrity_contract.json"


def test_release_builder_requires_and_forwards_owner_symlink_fixture(
    tmp_path: Path,
) -> None:
    builder_path = DEPLOY / "build_signed_windows_release.ps1"
    parsed = powershell_ast(builder_path)
    assert parsed["errors"] == []
    param_block = next(item for item in parsed["facts"] if item["kind"] == "param_block")
    fixture_parameter = next(
        item for item in param_block["param_details"]
        if item["name"].casefold() == "symlinkfixtureroot"
    )
    assert fixture_parameter["mandatory"] is True

    pytest_commands = [
        item for item in facts(builder_path, "command")
        if "-m pytest" in item["text"] and "--symlink-fixture-root" in item["text"]
    ]
    assert len(pytest_commands) == 2
    for command in pytest_commands:
        assert "tests" in command["text"]
        assert "-p no:cacheprovider" in command["text"]
        assert "--symlink-fixture-root" in command["text"]
        assert "$SymlinkFixtureRoot" in command["text"]

    top_level = [item for item in facts(builder_path, "command") if item["scope"] == "top-level"]
    fixture_gate = next(
        item for item in top_level
        if "Assert-OwnerSymlinkFixture" in item["text"]
    )
    private_key_gate = next(
        item for item in top_level
        if item["name"].casefold() == "test-path"
        and "$PrivateKeyPath" in item["text"]
    )
    assert fixture_gate["start"] < private_key_gate["start"]


def test_release_builder_collection_parser_preserves_whitespace_parameter_ids() -> None:
    builder_path = DEPLOY / "build_signed_windows_release.ps1"
    function = next(
        item["extent_text"] for item in facts(builder_path, "function")
        if item["name"] == "Get-CollectionNodeIds"
    )
    node_ids = [
        "tests/test_broker_identity_env.py::test_invalid_account_login_is_rejected_before_connection[   ]",
        "tests/test_broker_identity_env.py::test_invalid_account_login_is_rejected_before_connection[None]",
    ]
    literals = ",".join("'" + node_id + "'" for node_id in node_ids)
    script = (
        function
        + f"\n$ids = Get-CollectionNodeIds -Output @({literals})"
        + "\nConvertTo-Json -InputObject @($ids) -Compress"
    )
    result = powershell_harness(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == node_ids


def test_release_builder_fixture_guard_accepts_owner_link_and_rejects_invalid_root(
    tmp_path: Path,
    pytestconfig: pytest.Config,
) -> None:
    builder_path = DEPLOY / "build_signed_windows_release.ps1"
    function = next(
        item["extent_text"] for item in facts(builder_path, "function")
        if item["name"] == "Assert-OwnerSymlinkFixture"
    )
    owner_root = pytestconfig.getoption("--symlink-fixture-root")
    assert owner_root, "release-builder fixture test requires --symlink-fixture-root"
    accepted = powershell_harness(
        function + "\nAssert-OwnerSymlinkFixture -Root $args[0] | Out-Null; 'PASS'",
        str(owner_root),
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "PASS" in accepted.stdout

    wrong_root = tmp_path / "wrong-fixture"
    (wrong_root / "input").mkdir(parents=True)
    (wrong_root / "output").mkdir()
    (wrong_root / "outside").mkdir()
    (wrong_root / "input" / "allowed.txt").write_text("allowed\n", encoding="utf-8")
    (wrong_root / "outside" / "outside.txt").write_text("outside\n", encoding="utf-8")
    (wrong_root / "output" / "escape-symlink.txt").write_text("not a symlink\n", encoding="utf-8")
    rejected = powershell_harness(
        function + "\nAssert-OwnerSymlinkFixture -Root $args[0] | Out-Null; 'UNEXPECTED_PASS'",
        str(wrong_root),
    )
    assert rejected.returncode != 0
    assert "LINK_FIXTURE_REQUIRED" in rejected.stderr


def test_release_builder_rejects_bad_fixture_before_key_or_release_work(tmp_path: Path) -> None:
    builder = DEPLOY / "build_signed_windows_release.ps1"
    invalid_root = tmp_path / "invalid-owner-fixture"
    invalid_root.mkdir()
    result = powershell_harness(
        '& $args[0] -Profile super1 -OutputArchive $args[1] '
        '-SymlinkFixtureRoot $args[2] -PrivateKeyPath $args[3]',
        str(builder),
        str(tmp_path / "never-created.zip"),
        str(invalid_root),
        str(tmp_path / "missing-key.dpapi"),
    )
    assert result.returncode != 0
    assert "LINK_FIXTURE_ROOT_INVALID" in result.stderr
    assert "DPAPI release signing key is missing" not in result.stderr
    assert not (tmp_path / "never-created.zip").exists()


def contract() -> dict[str, object]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_release_integrity_contract_binds_the_exact_git_source_and_bytes() -> None:
    value = contract()
    helper = DEPLOY / str(value["helper_path"])[len("deploy/") :]
    payload = helper.read_bytes()

    assert value["source_commit"] == "65b4ff10f9f01618df4af3249217a97ea9eb1e88"
    assert value["source_tree"] == "accc7a0d662691f0ee3e978bc83f3036ea93a1be"
    assert value["source_blob_sha1"] == subprocess.check_output(
        ["git", "rev-parse", f"{value['source_commit']}:work/backtest/{value['helper_path']}"],
        cwd=ROOT.parents[1],
        text=True,
    ).strip()
    assert len(payload) == value["byte_length"] == 21876
    assert payload.startswith(b"\xef\xbb\xbf") is False
    assert b"\r" not in payload
    assert payload.count(b"\n") == value["lf_count"] == 491
    assert hashlib.sha256(payload).hexdigest() == value["sha256_lf"]
    assert (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines() == [
        "deploy/release_integrity.ps1 text eol=lf",
        "docs/DEPLOY_001_EVIDENCE/** -text",
        "docs/DEPLOY_001_EVIDENCE_FINAL_20260922_*/** -text",
    ]


def test_both_upgraders_have_exactly_the_contract_pin() -> None:
    expected = str(contract()["sha256_lf"])
    pattern = re.compile(r'\$ExpectedIntegrityScriptSha256\s*=\s*"([0-9a-f]{64})"')
    for name in ("upgrade_super1_signed_app_windows.ps1", "upgrade_forward_shadow_windows.ps1"):
        matches = pattern.findall((DEPLOY / name).read_text(encoding="utf-8"))
        assert matches == [expected]


def test_builder_byte_gate_rejects_crlf_bom_and_single_byte_corruption(tmp_path: Path) -> None:
    builder = (DEPLOY / "build_signed_windows_release.ps1").read_text(encoding="utf-8")
    functions = builder[
        builder.index("function Get-ByteSha256") : builder.index("function Assert-ReleaseIntegrityFile")
    ]
    value = contract()
    contract_literal = (
        "[pscustomobject]@{"
        f"byte_length={value['byte_length']}; bom=$false; cr_count=0; "
        f"lf_count={value['lf_count']}; sha256_lf='{value['sha256_lf']}'"
        "}"
    )
    helper_bytes = (DEPLOY / "release_integrity.ps1").read_bytes()
    cases = {
        "valid": helper_bytes,
        "crlf": helper_bytes.replace(b"\n", b"\r\n"),
        "bom": b"\xef\xbb\xbf" + helper_bytes,
        "single-byte": helper_bytes[:-1] + bytes([helper_bytes[-1] ^ 1]),
    }
    for name, payload in cases.items():
        path = tmp_path / f"{name}.ps1"
        path.write_bytes(payload)
        result = powershell_harness(
            functions
            + f"$contract={contract_literal};"
            + "Assert-ReleaseIntegrityPayload -Bytes ([IO.File]::ReadAllBytes($args[0])) "
            + "-Contract $contract -Label 'test';"
            + "'PASS'",
            str(path),
        )
        if name == "valid":
            assert result.returncode == 0, result.stderr
            assert "PASS" in result.stdout
        else:
            assert result.returncode != 0


def test_preflight_failure_cannot_reach_runtime_stop_or_mutation() -> None:
    forward = (DEPLOY / "upgrade_forward_shadow_windows.ps1").read_text(encoding="utf-8")
    main = forward[forward.index("$CandidateResults =") :]
    first_stop = main.index("Stop-ForwardRuntime\n")
    assert main.index("$ExternalReleaseManifest = Assert-SignedReleaseArchive") < first_stop
    assert main.index("$integrityHash = (Get-FileHash") < first_stop
    assert main.index("Export-ScheduledTask -TaskName $MainTask") < first_stop
    assert main.index("$runtimeControlEntered = $true") < first_stop
    assert main.index("Set-ForwardAccountRightsExact", first_stop) > first_stop
    assert main.index("Protect-ForwardRoot -RunnerSid $RunnerSid", first_stop) > first_stop
    assert "Stop-ForwardRuntimeEarly" not in forward

    super1 = (DEPLOY / "upgrade_super1_signed_app_windows.ps1").read_text(encoding="utf-8")
    runtime_flag = super1.index("$runtimeControlEntered = $true")
    first_stop = super1.index("Stop-Super1RuntimeForRollback", runtime_flag)
    assert super1.index("Assert-SignedReleaseArchive `") < first_stop
    assert super1.index("$runtimeControlEntered = $true") < first_stop
    failure = super1.index("$failure = $_")
    assert super1.index("if (-not $runtimeControlEntered)", failure) < super1.index(
        "$rollbackErrors", failure
    )


@lru_cache(maxsize=None)
def _ast(path: Path) -> dict[str, object]:
    return powershell_ast(path)


def _facts(path: Path, kind: str) -> list[dict[str, object]]:
    return [item for item in _ast(path)["facts"] if item["kind"] == kind]


def _function(path: Path, name: str) -> str:
    return next(item["extent_text"] for item in _ast(path)["facts"] if item["kind"] == "function" and item["name"] == name)


def _if_extent(path: Path, condition: str, minimum_start: int = 0) -> str:
    source = path.read_bytes().decode("utf-8")
    fact = next(
        item
        for item in _facts(path, "if")
        if item["condition_text"] == condition and item["start"] >= minimum_start
    )
    return source[fact["start"] : fact["end"]]


def _main_try(path: Path) -> dict[str, object]:
    return max(
        (
            item
            for item in _facts(path, "try")
            if item["scope"] == "top-level" and item["has_catch"] and item["has_finally"]
        ),
        key=lambda item: int(item["end"]) - int(item["start"]),
    )


def _forward_phase_fragments(path: Path) -> list[tuple[int, str]]:
    source = path.read_bytes().decode("utf-8")
    main = _main_try(path)
    main_start = int(main["start"])
    main_end = int(main["end"])
    assignment = next(
        item
        for item in _facts(path, "assignment")
        if item["scope"] == "top-level"
        and item["left"] == "$runtimeControlEntered"
        and item["right_text"].strip() == "$true"
        and main_start <= int(item["start"]) < main_end
    )
    stop = next(
        item
        for item in _facts(path, "command")
        if item["scope"] == "top-level"
        and item["name"] == "Stop-ForwardRuntime"
        and main_start <= int(item["start"]) < main_end
    )
    return [
        (int(assignment["start"]), source[int(assignment["start"]) : int(assignment["end"])]),
        (int(stop["start"]), source[int(stop["start"]) : int(stop["end"])]),
    ]


def _run_forward_phase_harness(
    path: Path,
    *,
    initial_stop_failure: bool,
    late_error: bool,
) -> subprocess.CompletedProcess[str]:
    main = _main_try(path)
    forward_stop = _function(path, "Stop-ForwardRuntime")
    forward_assert = _function(path, "Assert-TaskPairStopped")
    forward_no_python = _function(path, "Assert-NoForwardPythonProcesses")
    close_locks = _function(path, "Close-SignedReleaseLocks")
    phase = "\n".join(fragment for _, fragment in sorted(_forward_phase_fragments(path)))
    stop_failure_setup = "$true" if initial_stop_failure else "$false"
    late_failure = (
        "$script:stopFails = $true\n        throw [Exception]::new(\"INJECTED_LATE_FAILURE\")"
        if late_error
        else "throw [Exception]::new(\"INJECTED_PHASE_FAILURE\")"
    )
    script = (
        r'''
$cleanupErrors = [Collections.Generic.List[string]]::new()
$runtimeControlEntered = $false
$failureToReport = $null
$UpgradeSucceeded = $false
$PreviousPSModulePath = $null
$PreviousPSModulePathCaptured = $false
$PreviousServerEnv = $null
$PreviousTerminalEnv = $null
$PreviousPythonEnvironment = @{}
$TargetRunnerPassword = $null
$RunnerProbeTerminalConfigLock = $null
$ProductionTerminalConfigLock = $null
$RunnerProbeTerminalConfigCreated = $true
$ValidationRootCreated = $false
$SignedReleaseLocks = [Collections.Generic.List[IDisposable]]::new()
$ArchiveBoundaryHardened = $false
$SelfReadLock = $null
$RunnerProbeTerminalConfig = "C:\fixture\runner-terminal-probe.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.ini"
$MainTask = "main"
$WatchdogTask = "watch"
$script:stopFails = STOP_FAILURE_SETUP
$script:events = [Collections.Generic.List[string]]::new()
function Stop-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    [void]$script:events.Add("TASK_STOP:$TaskName")
}
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    [pscustomobject]@{ State = if ($script:stopFails) { "Running" } else { "Stopped" } }
}
function Start-Sleep {
    param([int]$Milliseconds)
    [void]$script:events.Add("STOP_FAILURE")
    throw "INJECTED_STOP_FAILURE"
}
function Get-ForwardPythonProcesses { @() }
function Stop-Process { [CmdletBinding()] param([int]$Id, [switch]$Force) }
function Test-Path {
    param([string]$LiteralPath, [string]$PathType)
    $true
}
function Remove-Item {
    param([string]$LiteralPath, [switch]$Recurse, [switch]$Force)
    [void]$script:events.Add("DELETE:$LiteralPath")
}
''' + forward_assert + forward_stop + forward_no_python + close_locks + r'''
try {
    try {
        __PHASE__
        __LATE_FAILURE__
    }
    ''' + str(main["catch_text"]) + r'''
    finally ''' + str(main["finally_text"]) + r'''
} catch { $observed = $_ }
if (-not $observed) { throw "phase failure was not observed" }
if ($script:events -like "DELETE:*") { throw "probe file was deleted" }
if (-not $RunnerProbeTerminalConfigCreated) { throw "probe ownership was cleared" }
if (__LATE_MODE__ -and @($script:events | Where-Object { $_ -eq "STOP_FAILURE" }).Count -ne 2) {
    throw "late rollback/final stop attempts were not both observed"
}
if (__INITIAL_MODE__ -and @($script:events | Where-Object { $_ -eq "STOP_FAILURE" }).Count -ne 3) {
    throw "initial stop failure did not enter rollback/final stop chain"
}
'PHASE_CHAIN_PASS'
'''
    )
    script = (
        script.replace("STOP_FAILURE_SETUP", stop_failure_setup)
        .replace("__PHASE__", phase)
        .replace("__LATE_FAILURE__", late_failure)
        .replace("__LATE_MODE__", "$true" if late_error else "$false")
        .replace("__INITIAL_MODE__", "$true" if initial_stop_failure else "$false")
    )
    return powershell_harness(script)


def test_ast_harness_executes_real_stop_bodies_and_phase_guards() -> None:
    forward = DEPLOY / "upgrade_forward_shadow_windows.ps1"
    super1 = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    forward_assert = _function(forward, "Assert-TaskPairStopped")
    forward_stop = _function(forward, "Stop-ForwardRuntime")
    super1_stop = _function(super1, "Stop-Super1RuntimeForRollback")
    forward_finally_gate = _if_extent(forward, "$runtimeControlEntered", minimum_start=100000)
    forward_catch_gate = _if_extent(forward, "-not $runtimeControlEntered")
    super1_finally_gate = _if_extent(super1, "$runtimeHelpersReady -and $runtimeControlEntered")
    super1_catch_gate = _if_extent(super1, "-not $runtimeControlEntered")

    result = powershell_harness(
        r'''
$script:events = [Collections.Generic.List[string]]::new()
$script:partial = $false
$MainTask = "main"
$WatchdogTask = "watch"
function Stop-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    [void]$script:events.Add("stop-task:$TaskName")
}
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    [pscustomobject]@{ State = if ($script:partial) { "Running" } else { "Stopped" } }
}
function Start-Sleep {
    param([int]$Milliseconds)
    if ($script:partial) { throw "injected partial-stop failure" }
}
function Get-ForwardPythonProcesses { @() }
function Stop-Process {
    [CmdletBinding()] param([int]$Id, [switch]$Force)
    [void]$script:events.Add("kill:$Id")
}
function Assert-NoForwardPythonProcesses { [void]$script:events.Add("no-python") }
function Stop-Super1Tasks { [void]$script:events.Add("super-stop") }
function Wait-Super1Stopped {
    param([int]$TimeoutSeconds, [switch]$PreserveTerminal)
    [void]$script:events.Add("super-wait")
}
function Get-Super1TerminalProcesses { @() }
function Get-Super1PythonProcesses { @() }
function Get-UnexpectedSuper1RunnerProcesses { @() }
function Get-Super1TaskRunnerSid { "S-1-5-18" }
    ''' + forward_assert + forward_stop + super1_stop + r'''
$primaryError = [Exception]::new("forward-preflight")
try { throw $primaryError } catch { $caught = $_ }
try {
    $runtimeControlEntered = $false
    $primaryError = [Exception]::new("forward-preflight")
    ''' + forward_catch_gate + r'''
} catch { if ($_.Exception.Message -notlike "*forward-preflight*") { throw } }
$runtimeControlEntered = $false
$finalStopError = $null
$before = $script:events.Count
''' + forward_finally_gate + r'''
if ($script:events.Count -ne $before) { throw "Forward pre-entry finally mutated runtime" }
$runtimeControlEntered = $true
$finalStopError = $null
''' + forward_finally_gate + r'''
if ($finalStopError) { throw $finalStopError }
if ($script:events -notcontains "stop-task:main" -or $script:events -notcontains "stop-task:watch" -or $script:events -notcontains "no-python") { throw "Forward stopped-state gate did not execute the real stop body" }
$script:events.Clear()
$script:partial = $true
try { Stop-ForwardRuntime; throw "partial stop unexpectedly succeeded" } catch { if ($_.Exception.Message -notlike "*partial-stop*") { throw } }
if ($script:events -notcontains "stop-task:main" -or $script:events -notcontains "stop-task:watch" -or @($script:events | Where-Object { $_ -like "kill:*" }).Count -ne 0) { throw "Partial stop did not fail closed before process termination" }
$script:partial = $false
$script:events.Clear()
$runtimeHelpersReady = $false
$runtimeControlEntered = $true
$cleanupErrors = [Collections.Generic.List[string]]::new()
$before = $script:events.Count
''' + super1_finally_gate + r'''
if ($script:events.Count -ne $before) { throw "Super1 pre-entry finally mutated runtime" }
$runtimeHelpersReady = $true
$runtimeControlEntered = $true
$cleanupErrors = [Collections.Generic.List[string]]::new()
''' + super1_finally_gate + r'''
if ($script:events -notcontains "super-stop" -or $script:events -notcontains "super-wait") { throw "Super1 stopped-state gate did not execute" }
$script:events.Clear()
Stop-Super1RuntimeForRollback
if ($script:events -notcontains "super-stop" -or $script:events -notcontains "super-wait") { throw "Real Super1 stop body did not execute" }
$failure = [Exception]::new("super1-preflight")
try {
''' + super1_catch_gate + r'''
} catch { if ($_.Exception.Message -notlike "*super1-preflight*") { throw } }
'PASS'
''',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "PASS"


def _assert_forward_phase_contract(source: str) -> None:
    main = source[source.index("$CandidateResults =") :]
    first_stop = main.index("Stop-ForwardRuntime\n")
    assert main.index("$ExternalReleaseManifest = Assert-SignedReleaseArchive") < first_stop
    assert main.index("$runtimeControlEntered = $true") < first_stop
    assert main.index("Set-ForwardAccountRightsExact", first_stop) > first_stop
    catch = source.index("catch {\n    $UpgradeSucceeded = $false")
    assert source.index("if (-not $runtimeControlEntered)", catch) < source.index(
        "$rollbackErrors", catch
    )


def test_phase_mutations_are_sensitive() -> None:
    source = (DEPLOY / "upgrade_forward_shadow_windows.ps1").read_text(encoding="utf-8")
    _assert_forward_phase_contract(source)
    mutations = (
        source.replace(
            "    $ExternalReleaseManifest = Assert-SignedReleaseArchive",
            "    Stop-ForwardRuntime\n    $ExternalReleaseManifest = Assert-SignedReleaseArchive",
            1,
        ),
        source.replace(
            "    $runtimeControlEntered = $true\n    Stop-ForwardRuntime",
            "    Stop-ForwardRuntime\n    $runtimeControlEntered = $true",
            1,
        ),
        source.replace(
            "    if (-not $runtimeControlEntered) {\n        throw $primaryError\n    }\n",
            "",
            1,
        ),
    )
    for mutated in mutations:
        try:
            _assert_forward_phase_contract(mutated)
        except (AssertionError, ValueError):
            continue
        raise AssertionError("phase mutation unexpectedly passed the contract checks")


def test_mutated_forward_preflight_guard_fails_the_real_catch_harness(tmp_path: Path) -> None:
    source = (DEPLOY / "upgrade_forward_shadow_windows.ps1").read_text(encoding="utf-8")
    original_path = tmp_path / "forward-original.ps1"
    mutant_path = tmp_path / "forward-mutant.ps1"
    original_path.write_text(source, encoding="utf-8")
    mutant_path.write_text(
        source.replace(
            "    if (-not $runtimeControlEntered) {\n        throw $primaryError\n    }\n",
            "",
            1,
        ),
        encoding="utf-8",
    )

    def run_catch(path: Path) -> subprocess.CompletedProcess[str]:
        main_try = _main_try(path)
        close_locks = _function(path, "Close-SignedReleaseLocks")
        return powershell_harness(
            r'''
$events = [Collections.Generic.List[string]]::new()
$UpgradeSucceeded = $false
$runtimeControlEntered = $false
$SignedReleaseLocks = [Collections.Generic.List[IDisposable]]::new()
function Stop-ForwardRuntime {
    [void]$events.Add("STOP")
    throw "MUTANT_ROLLBACK_REACHED"
}
''' + close_locks + r'''
try {
    try { throw [Exception]::new("INJECTED_PREFLIGHT_FAILURE") }
    ''' + str(main_try["catch_text"]) + r'''
} catch { $observed = $_ }
if ($events.Count -ne 0) { throw "preflight catch reached runtime rollback" }
if ($observed.Exception.Message -notlike "*INJECTED_PREFLIGHT_FAILURE*") { throw $observed }
'ORIGINAL_GUARD_PASS'
''',
        )

    original = run_catch(original_path)
    assert original.returncode == 0, original.stderr
    assert "ORIGINAL_GUARD_PASS" in original.stdout

    mutant = run_catch(mutant_path)
    assert mutant.returncode != 0
    assert "preflight catch reached runtime rollback" in mutant.stdout + mutant.stderr


def test_forward_production_phase_chain_rejects_stop_and_rollback_mutants(
    tmp_path: Path,
) -> None:
    source = (DEPLOY / "upgrade_forward_shadow_windows.ps1").read_text(encoding="utf-8")
    original_path = tmp_path / "forward-phase-original.ps1"
    original_path.write_text(source, encoding="utf-8")

    early_stop = source.replace(
        "    $ExternalReleaseManifest = Assert-SignedReleaseArchive",
        "    Stop-ForwardRuntime\n    $ExternalReleaseManifest = Assert-SignedReleaseArchive",
        1,
    )
    flag_after_stop = source.replace(
        "    $runtimeControlEntered = $true\n    Stop-ForwardRuntime",
        "    Stop-ForwardRuntime\n    $runtimeControlEntered = $true",
        1,
    )
    rollback_gate_removed = source.replace(
        "    if (-not $rollbackRuntimeStopped) {\n        $failureToReport = [InvalidOperationException]::new(",
        "    $rollbackRuntimeStopped = $true\n    if ($false) {\n        $failureToReport = [InvalidOperationException]::new(",
        1,
    )

    def write_source(name: str, value: str) -> Path:
        path = tmp_path / name
        path.write_text(value, encoding="utf-8")
        return path

    late_original = _run_forward_phase_harness(
        original_path,
        initial_stop_failure=False,
        late_error=True,
    )
    assert late_original.returncode == 0, late_original.stderr
    assert "PHASE_CHAIN_PASS" in late_original.stdout

    initial_original = _run_forward_phase_harness(
        original_path,
        initial_stop_failure=True,
        late_error=False,
    )
    assert initial_original.returncode == 0, initial_original.stderr
    assert "PHASE_CHAIN_PASS" in initial_original.stdout

    early_result = _run_forward_phase_harness(
        write_source("forward-phase-early-stop.ps1", early_stop),
        initial_stop_failure=True,
        late_error=False,
    )
    assert early_result.returncode != 0

    flag_result = _run_forward_phase_harness(
        write_source("forward-phase-late-flag.ps1", flag_after_stop),
        initial_stop_failure=True,
        late_error=False,
    )
    assert flag_result.returncode != 0

    rollback_result = _run_forward_phase_harness(
        write_source("forward-phase-no-rollback-gate.ps1", rollback_gate_removed),
        initial_stop_failure=False,
        late_error=True,
    )
    assert rollback_result.returncode != 0


def test_zip_integrity_gate_rejects_autocrlf_bom_duplicate_and_missing_entries(
    tmp_path: Path,
) -> None:
    builder = (DEPLOY / "build_signed_windows_release.ps1").read_text(encoding="utf-8")
    functions = builder[
        builder.index("function Get-ByteSha256") : builder.index("$ReleaseIntegrityContract =")
    ]
    value = contract()
    contract_literal = (
        "[pscustomobject]@{"
        f"helper_path='deploy/release_integrity.ps1'; byte_length={value['byte_length']}; "
        f"bom=$false; cr_count=0; lf_count={value['lf_count']}; "
        f"sha256_lf='{value['sha256_lf']}'"
        "}"
    )
    helper_bytes = (DEPLOY / "release_integrity.ps1").read_bytes()
    cases = {
        "valid": [("deploy/release_integrity.ps1", helper_bytes)],
        "crlf": [("deploy/release_integrity.ps1", helper_bytes.replace(b"\n", b"\r\n"))],
        "bom": [("deploy/release_integrity.ps1", b"\xef\xbb\xbf" + helper_bytes)],
        "duplicate": [
            ("deploy/release_integrity.ps1", helper_bytes),
            ("deploy/release_integrity.ps1", helper_bytes),
        ],
        "missing": [("deploy/other.ps1", helper_bytes)],
    }
    for name, entries in cases.items():
        path = tmp_path / f"{name}.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for entry_name, payload in entries:
                archive.writestr(entry_name, payload)
        result = powershell_harness(
            functions
            + r'''
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($args[0])
try {
        Assert-ReleaseIntegrityZipEntry -Zip $zip -Contract (CONTRACT)
}
finally { $zip.Dispose() }
'ZIP_PASS'
'''.replace("CONTRACT", contract_literal),
            str(path),
        )
        if name == "valid":
            assert result.returncode == 0, result.stderr
            assert "ZIP_PASS" in result.stdout
        else:
            assert result.returncode != 0


def test_ast_main_catch_finally_preserves_preflight_ownership_and_primary_error() -> None:
    forward = DEPLOY / "upgrade_forward_shadow_windows.ps1"
    forward_try = _main_try(forward)
    forward_catch = str(forward_try["catch_text"])
    forward_finally = str(forward_try["finally_text"])
    close_locks = _function(forward, "Close-SignedReleaseLocks")
    remove_tree = _function(forward, "Remove-GeneratedTree")
    preflight = powershell_harness(
        r'''
$cleanupErrors = [Collections.Generic.List[string]]::new()
$runtimeControlEntered = $false
$failureToReport = $null
$UpgradeSucceeded = $false
$PreviousPSModulePath = "before-module-path"
$PreviousPSModulePathCaptured = $true
$PreviousServerEnv = $null
$PreviousTerminalEnv = $null
$PreviousPythonEnvironment = @{}
$env:PSModulePath = "trusted-module-path"
$TargetRunnerPassword = $null
$RunnerProbeTerminalConfigLock = $null
$ProductionTerminalConfigLock = $null
$RunnerProbeTerminalConfigCreated = $false
$ValidationRootCreated = $false
$SignedReleaseLocks = [Collections.Generic.List[IDisposable]]::new()
$ArchiveBoundaryHardened = $false
$SelfReadLock = $null
    $RunnerProbeTerminalConfig = "C:\fixture\runner-terminal-probe.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.ini"
    $ValidationRoot = "C:\fixture\upgrade-validation.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    function Test-Path {
        param([string]$LiteralPath, [string]$PathType)
        $true
    }
    function Remove-Item {
    param([string]$LiteralPath, [switch]$Recurse, [switch]$Force)
    throw "UNEXPECTED_DELETE:$LiteralPath"
}
''' + remove_tree + close_locks + r'''
try { try { throw [Exception]::new("INJECTED_PREFLIGHT_FAILURE") } ''' + forward_catch + r''' finally ''' + forward_finally + r''' } catch { $observed = $_ }
if (-not $observed) { throw "preflight failure was not observed" }
if ($observed.Exception.Message -notlike "*INJECTED_PREFLIGHT_FAILURE*") { throw $observed }
"OBSERVED=$($observed.Exception.Message)"
"ENV=$env:PSModulePath"
if ($env:PSModulePath -cne "before-module-path") { throw "PSModulePath was not restored" }
'PREFLIGHT_PASS'
''',
    )
    preflight_output = preflight.stdout + preflight.stderr
    assert preflight.returncode == 0, preflight_output
    assert "INJECTED_PREFLIGHT_FAILURE" in preflight_output
    assert "UNEXPECTED_DELETE" not in preflight_output
    assert "cleanup failed" not in preflight_output.lower()
    assert "trusted-module-path" not in preflight_output
    assert "PREFLIGHT_PASS" in preflight.stdout

    lock_probe = powershell_harness(
        r'''
Add-Type @'
using System;
using System.Collections.Generic;
public sealed class Deploy001FakeLock : IDisposable {
    public static readonly List<string> Events = new List<string>();
    private readonly string name;
    private readonly bool fail;
    public Deploy001FakeLock(string name, bool fail) { this.name = name; this.fail = fail; }
    public void Dispose() { Events.Add("DISPOSE:" + name); if (fail) throw new InvalidOperationException("dispose:" + name); }
}
'@
''' + close_locks + r'''
$locks = [Collections.Generic.List[IDisposable]]::new()
[void]$locks.Add([Deploy001FakeLock]::new("first", $true))
[void]$locks.Add([Deploy001FakeLock]::new("second", $false))
$errors = [Collections.Generic.List[string]]::new()
Close-SignedReleaseLocks -Locks $locks -Errors $errors
if ([string]::Join(",", [Deploy001FakeLock]::Events) -ne "DISPOSE:first,DISPOSE:second") { throw "all lock handles were not attempted" }
if ($locks.Count -ne 1) { throw "failed lock was incorrectly recorded as disposed" }
if ($errors.Count -ne 1) { throw "lock cleanup error was not collected" }
'LOCKS_PASS'
''',
    )
    assert lock_probe.returncode == 0, lock_probe.stderr
    assert "LOCKS_PASS" in lock_probe.stdout

    cleanup_failure = powershell_harness(
        r'''
$cleanupErrors = [Collections.Generic.List[string]]::new()
$runtimeControlEntered = $true
$failureToReport = [Exception]::new("INJECTED_PRIMARY_FAILURE")
$UpgradeSucceeded = $false
$PreviousPSModulePath = "before-module-path"
$PreviousPSModulePathCaptured = $true
$PreviousServerEnv = $null
$PreviousTerminalEnv = $null
$PreviousPythonEnvironment = @{}
$env:PSModulePath = "trusted-module-path"
$TargetRunnerPassword = $null
$RunnerProbeTerminalConfigLock = $null
$ProductionTerminalConfigLock = $null
$RunnerProbeTerminalConfigCreated = $true
$ValidationRootCreated = $false
$SignedReleaseLocks = [Collections.Generic.List[IDisposable]]::new()
$ArchiveBoundaryHardened = $false
$SelfReadLock = $null
$RunnerProbeTerminalConfig = "C:\fixture\runner-terminal-probe.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.ini"
function Stop-ForwardRuntime {}
function Assert-TaskPairStopped {}
function Assert-NoForwardPythonProcesses {}
function Test-Path {
    param([string]$LiteralPath, [string]$PathType)
    $true
}
function Remove-Item {
    param([string]$LiteralPath, [switch]$Recurse, [switch]$Force)
    throw "INJECTED_CLEANUP_FAILURE:$LiteralPath"
}
''' + close_locks + r'''
try {
    try { 'body' } finally ''' + forward_finally + r'''
} catch { $observed = $_ }
if (-not $observed) { throw "cleanup failure was not observed" }
if ($observed.Exception.Message -notlike "*INJECTED_PRIMARY_FAILURE*") { throw $observed }
if ($observed.Exception.Message -notlike "*INJECTED_CLEANUP_FAILURE*") { throw $observed }
if ($observed.Exception.Message -like "ForwardShadow cleanup failed:*") { throw "primary failure was masked" }
'CLEANUP_FAILURE_PASS'
''',
    )
    assert cleanup_failure.returncode == 0, cleanup_failure.stderr
    assert "CLEANUP_FAILURE_PASS" in cleanup_failure.stdout

    owned_cleanup = powershell_harness(
        r'''
$cleanupErrors = [Collections.Generic.List[string]]::new()
$runtimeControlEntered = $true
$failureToReport = $null
$UpgradeSucceeded = $false
$PreviousPSModulePathCaptured = $false
$PreviousServerEnv = $null
$PreviousTerminalEnv = $null
$PreviousPythonEnvironment = @{}
$TargetRunnerPassword = $null
$RunnerProbeTerminalConfigLock = $null
$ProductionTerminalConfigLock = $null
$RunnerProbeTerminalConfigCreated = $true
$ValidationRootCreated = $true
$SignedReleaseLocks = [Collections.Generic.List[IDisposable]]::new()
$ArchiveBoundaryHardened = $false
$SelfReadLock = $null
$RunnerProbeTerminalConfig = "C:\fixture\runner-terminal-probe.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.ini"
$ValidationRoot = "C:\fixture\upgrade-validation.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
$events = [Collections.Generic.List[string]]::new()
$observed = $null
function Stop-ForwardRuntime {}
function Assert-TaskPairStopped {}
function Assert-NoForwardPythonProcesses {}
function Test-Path {
    param([string]$LiteralPath, [string]$PathType)
    $true
}
function Remove-Item {
    param([string]$LiteralPath, [switch]$Recurse, [switch]$Force)
    [void]$events.Add("probe-delete")
}
function Remove-GeneratedTree {
    param([string]$Path, [string]$ExpectedLeafPattern)
    [void]$events.Add("validation-delete")
}
''' + close_locks + r'''
try { try { 'body' } finally ''' + forward_finally + r''' } catch { $observed = $_ }
if ($observed) { throw $observed }
if ($RunnerProbeTerminalConfigCreated -or $ValidationRootCreated) { throw "owned cleanup flags were not cleared" }
if ([string]::Join(',', $events) -ne "probe-delete,validation-delete") { throw "owned cleanup did not run" }
'OWNED_CLEANUP_PASS'
''',
    )
    assert owned_cleanup.returncode == 0, owned_cleanup.stderr
    assert "OWNED_CLEANUP_PASS" in owned_cleanup.stdout


def test_super1_production_phase_chain_rejects_preentry_stop_mutants(
    tmp_path: Path,
) -> None:
    source = (DEPLOY / "upgrade_super1_signed_app_windows.ps1").read_text(encoding="utf-8")
    original_path = tmp_path / "super1-phase-original.ps1"
    original_path.write_text(source, encoding="utf-8")

    def phase_parts(path: Path) -> tuple[str, str, str, str]:
        text = path.read_bytes().decode("utf-8")
        outer = _main_try(path)
        main_start = int(outer["start"])
        main_end = int(outer["end"])
        assignment = next(
            item
            for item in _facts(path, "assignment")
            if item["scope"] == "top-level"
            and item["left"] == "$runtimeControlEntered"
            and item["right_text"].strip() == "$true"
            and main_start <= int(item["start"]) < main_end
        )
        stop = next(
            item
            for item in _facts(path, "command")
            if item["scope"] == "top-level"
            and item["name"] == "Stop-Super1RuntimeForRollback"
            and main_start <= int(item["start"]) < main_end
        )
        phase = "\n".join(
            text[start:end]
            for start, end in sorted(
                (
                    (int(assignment["start"]), int(assignment["end"])),
                    (int(stop["start"]), int(stop["end"])),
                )
            )
        )
        transactional = sorted(
            (
                item
                for item in _facts(path, "try")
                if item["scope"] == "top-level" and item["has_catch"] and item["has_finally"]
            ),
            key=lambda item: int(item["end"]) - int(item["start"]),
            reverse=True,
        )[1]
        return phase, str(transactional["catch_text"]), str(outer["finally_text"]), _function(
            path, "Stop-Super1RuntimeForRollback"
        )

    def run(path: Path, *, initial_failure: bool, late_failure: bool) -> subprocess.CompletedProcess[str]:
        phase, inner_catch, outer_finally, stop_body = phase_parts(path)
        late = (
            "$script:stopFails = $true\n        throw [Exception]::new(\"INJECTED_SUPER1_LATE_FAILURE\")"
            if late_failure
            else "throw [Exception]::new(\"INJECTED_SUPER1_PHASE_FAILURE\")"
        )
        script = r'''
$cleanupErrors = [Collections.Generic.List[string]]::new()
$runtimeHelpersReady = $true
$runtimeControlEntered = $false
$RuntimeConfigEvidence = @()
$ReleaseInputLocks = [Collections.Generic.List[IDisposable]]::new()
$SelfScriptLock = $null
$PowerShellHostLock = $null
$TerminalLock = $null
$BootstrapPythonLock = $null
$IntegrityScriptLock = $null
$OriginalPSModulePath = "before-module-path"
$OriginalPythonHome = $null
$OriginalPythonPath = $null
$env:PSModulePath = "trusted-module-path"
$script:stopFails = INITIAL_FAILURE
$script:events = [Collections.Generic.List[string]]::new()
function Stop-Super1Tasks { [void]$script:events.Add("TASK_STOP") }
function Wait-Super1Stopped {
    param([int]$TimeoutSeconds, [switch]$PreserveTerminal)
    if ($script:stopFails) {
        [void]$script:events.Add("STOP_FAILURE")
        throw "INJECTED_SUPER1_STOP_FAILURE"
    }
}
function Get-Super1TerminalProcesses { @() }
function Get-Super1PythonProcesses { @() }
function Get-UnexpectedSuper1RunnerProcesses { @() }
function Get-Super1TaskRunnerSid { "S-1-5-18" }
function Stop-Process { [CmdletBinding()] param([int]$Id, [switch]$Force) }
__STOP_BODY__
try {
    try {
        __PHASE__
        __LATE__
    }
    __INNER_CATCH__
    finally { }
} catch {
    $primaryError = $_
} finally __OUTER_FINALLY__
if (-not $primaryError) { throw "Super1 phase error was not preserved" }
"EVENTS=$([string]::Join(',', $script:events))"
if (@($script:events | Where-Object { $_ -eq "STOP_FAILURE" }).Count -lt EXPECTED_FAILURES) {
    throw "Super1 rollback/final stop chain was not fully executed"
}
'SUPER1_PHASE_CHAIN_PASS'
'''
        expected_failures = "3"
        script = (
                script.replace("INITIAL_FAILURE", "$true" if initial_failure else "$false")
                .replace("__PHASE__", phase)
                .replace("__STOP_BODY__", stop_body)
                .replace("__LATE__", late)
            .replace("__INNER_CATCH__", inner_catch)
            .replace("__OUTER_FINALLY__", outer_finally)
            .replace("EXPECTED_FAILURES", expected_failures)
        )
        return powershell_harness(script)

    late_result = run(original_path, initial_failure=False, late_failure=True)
    assert late_result.returncode == 0, late_result.stdout + late_result.stderr
    assert "SUPER1_PHASE_CHAIN_PASS" in late_result.stdout
    initial_result = run(original_path, initial_failure=True, late_failure=False)
    assert initial_result.returncode == 0, initial_result.stderr
    assert "SUPER1_PHASE_CHAIN_PASS" in initial_result.stdout

    flag_after_stop = source.replace(
        "    $runtimeControlEntered = $true\n    Stop-Super1RuntimeForRollback",
        "    Stop-Super1RuntimeForRollback\n    $runtimeControlEntered = $true",
        1,
    )
    early_stop = source.replace(
        "    $ReleaseManifest = Assert-SignedReleaseArchive `",
        "    Stop-Super1RuntimeForRollback\n    $ReleaseManifest = Assert-SignedReleaseArchive `",
        1,
    )
    for name, mutant in (
        ("super1-phase-late-flag.ps1", flag_after_stop),
        ("super1-phase-early-stop.ps1", early_stop),
    ):
        mutant_path = tmp_path / name
        mutant_path.write_text(mutant, encoding="utf-8")
        result = run(mutant_path, initial_failure=True, late_failure=False)
        assert result.returncode != 0


def test_super1_ast_catch_and_finally_preflight_path_is_executed() -> None:
    super1 = DEPLOY / "upgrade_super1_signed_app_windows.ps1"
    outer = _main_try(super1)
    transactional_tries = sorted(
        (
            item
            for item in _facts(super1, "try")
            if item["scope"] == "top-level" and item["has_catch"] and item["has_finally"]
        ),
        key=lambda item: int(item["end"]) - int(item["start"]),
        reverse=True,
    )
    inner = transactional_tries[1]
    result = powershell_harness(
        r'''
$cleanupErrors = [Collections.Generic.List[string]]::new()
$runtimeHelpersReady = $false
$runtimeControlEntered = $false
$RuntimeConfigEvidence = @()
$ReleaseInputLocks = [Collections.Generic.List[IDisposable]]::new()
$SelfScriptLock = $null
$PowerShellHostLock = $null
$TerminalLock = $null
$BootstrapPythonLock = $null
$IntegrityScriptLock = $null
$OriginalPSModulePath = "before-module-path"
$OriginalPythonHome = $null
$OriginalPythonPath = $null
$env:PSModulePath = "trusted-module-path"
$primaryError = $null
$Result = $null
    try {
    try { throw [Exception]::new("INJECTED_SUPER1_PREFLIGHT_FAILURE") }
    ''' + str(inner["catch_text"]) + r'''
}
catch { $primaryError = $_ }
finally ''' + str(outer["finally_text"]) + r'''
if (-not $primaryError) { throw "Super1 preflight error was not preserved" }
if ($primaryError.Exception.Message -notlike "*INJECTED_SUPER1_PREFLIGHT_FAILURE*") { throw $primaryError }
'SUPER1_PASS'
''',
    )
    assert result.returncode == 0, result.stderr
    assert "SUPER1_PASS" in result.stdout


def test_builder_checks_contract_before_signing_key_resolution(
    tmp_path: Path,
    pytestconfig: pytest.Config,
) -> None:
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(DEPLOY / "build_signed_windows_release.ps1"),
            "-Profile",
            "forward-shadow",
            "-OutputArchive",
            str(tmp_path / "contract-check.zip"),
            "-SymlinkFixtureRoot",
            str(pytestconfig.getoption("--symlink-fixture-root")),
            "-PrivateKeyPath",
            str(tmp_path / "missing-key.dpapi"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "DPAPI release signing key is missing" in combined
    assert "Release-integrity contract" not in combined
