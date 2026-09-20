from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from powershell_contract import facts, powershell_ast, powershell_harness


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
CONTRACT_PATH = DEPLOY / "release_integrity_contract.json"


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
        "deploy/release_integrity.ps1 text eol=lf"
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


def _function(path: Path, name: str) -> str:
    return next(item["extent_text"] for item in powershell_ast(path)["facts"] if item["kind"] == "function" and item["name"] == name)


def _if_extent(path: Path, condition: str, minimum_start: int = 0) -> str:
    source = path.read_bytes().decode("utf-8")
    fact = next(
        item
        for item in facts(path, "if")
        if item["condition_text"] == condition and item["start"] >= minimum_start
    )
    return source[fact["start"] : fact["end"]]


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


def test_builder_checks_contract_before_signing_key_resolution(tmp_path: Path) -> None:
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
