from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import pytest

from powershell_contract import facts


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def text(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_windows_installers_require_signed_archive_and_offline_hash_lock() -> None:
    installers = (
        "install_forward_shadow_windows.ps1",
        "install_super1_windows.ps1",
        "bootstrap_super1_fresh_windows.ps1",
    )
    upgraders = (
        "upgrade_forward_shadow_windows.ps1",
        "upgrade_super1_signed_app_windows.ps1",
    )
    for name in installers + upgraders:
        source = text(name)
        assert "Assert-SignedReleaseArchive" in source
    for name in installers:
        source = text(name)
        assert "Install-LockedRelease" in source
    forward_upgrade = text("upgrade_forward_shadow_windows.ps1")
    assert "Install-LockedRelease -Python $StagedPython -App $Staging" in forward_upgrade
    assert "Install-LockedRelease -Python $Python -App $Staging" not in forward_upgrade
    assert "Assert-ReleasePythonRuntime" in forward_upgrade
    integrity = text("release_integrity.ps1")
    assert "VerifyData" in integrity
    assert "--require-hashes" in integrity
    assert "--no-index" in integrity
    assert "Push-Location -LiteralPath $App" in integrity
    assert '-r "requirements-windows.lock"' in integrity
    linux = text("install_forward_shadow.sh")
    assert 'cd -- "${STAGING}"' in linux
    assert "-r requirements-linux.lock" in linux


def test_release_tree_acl_keeps_effective_permissions_on_child_files() -> None:
    source = text("release_integrity.ps1")
    protect = source[source.index("function Protect-ReleaseApp") :]
    root_acl_start = protect.index("$rootAcl = New-Object Security.AccessControl.DirectorySecurity")
    root_acl_apply = protect.index("[IO.Directory]::SetAccessControl($resolved, $rootAcl)")
    assert "$rootAcl.SetAccessRuleProtection($true, $false)" in protect
    assert "[void]$rootAcl.AddAccessRule(" in protect
    assert '$fullControlSids = @("S-1-5-18", "S-1-5-32-544", $callerSid)' in protect
    assert "$runnerSid -notin $fullControlSids" in protect
    assert "$callerSid -eq $runnerSid" in protect
    assert '& $script:ReleaseIcaclsExe $resolved /grant:r "*$($callerSid):(OI)(CI)RX" /Q' in protect
    assert "Could not downgrade the runtime caller to read/execute" in protect
    reset = protect.index('& $script:ReleaseIcaclsExe (Join-Path $resolved "*") /reset /T /C /Q')
    set_owner = protect.index('& $script:ReleaseIcaclsExe $resolved /setowner "*S-1-5-18" /T /C /Q')
    verify = protect.index("& $script:ReleaseIcaclsExe $resolved /verify /T /C /Q")
    assert root_acl_start < root_acl_apply < reset < set_owner < verify
    assert "Application tree contains a reparse point" in protect
    assert 'if ($ownerSid -ne "S-1-5-18")' in protect
    assert "Application root ACL contains an unexpected rule" in protect
    assert "[int]$rule.InheritanceFlags -ne $requiredInheritance" in protect
    assert "$rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None" in protect


def test_mt5_installer_signature_gate_is_fail_closed() -> None:
    source = text("install_forward_shadow_windows.ps1")
    assert '$Signature.Status -ne "Valid" -or' in source
    assert '$Signature.Status -eq "Valid" -and' not in source


def test_runtime_launchers_use_writable_state_not_read_only_app() -> None:
    forward = text("run_forward_shadow_windows.ps1")
    super1 = text("run_super1_windows.ps1")
    assert '--output-root (Join-Path $Root "state")' in forward
    assert '"--output-root", (Join-Path $Root "state")' in super1
    assert '"--credential-stdin"' in super1
    assert 'XM_MT5_READ_ONLY_PASSWORD = $null' not in super1


def test_installers_limit_restart_and_task_privilege() -> None:
    source = text("install_forward_shadow_windows.ps1")
    assert "-RestartCount 3" in source
    assert "-RestartCount 999" not in source
    assert "-RunLevel Highest" not in source
    source = text("install_super1_windows.ps1")
    assert "-RestartCount 3" not in source
    assert "-RestartCount 999" not in source
    assert "-RunLevel Highest" not in source
    assert "install_super1_watchdog_windows.ps1" in text("finalize_super1_fresh_windows.ps1")
    for name in ("repair_super1_task_s4u_windows.ps1", "recover_super1_isolated_user.ps1"):
        source = text(name)
        assert "-RestartCount 0" in source
        assert "-RestartCount 999" not in source


def test_super1_fresh_install_archives_existing_state_and_rolls_back() -> None:
    source = text("install_super1_windows.ps1")
    transaction = text("super1_install_transaction.ps1")
    for archived in ("app.previous", "venv311.previous", "state.previous", "control.previous", "runtime-trust.previous"):
        assert archived in source
    assert "fresh-install-" in source
    assert "Export-ScheduledTask" in source
    assert "icacls.exe" in source
    assert "Super1 task is active; refusing to replace" in source
    assert "Register-ScheduledTask -TaskName ([string]$backup.task)" in transaction
    assert "Super1 fresh-install transaction rolled back" in source
    assert "Super1 app already exists; refusing to overwrite" not in source


def test_super1_repair_does_not_reset_password_behind_dpapi_blob() -> None:
    source = text("repair_super1_task_s4u_windows.ps1")
    assert "Set-LocalUser" not in source
    assert "New-LocalUser" not in source
    assert "-LogonType Password" in source
    assert "-LogonType S4U" not in source
    assert "Read-Host" in source


def test_release_builder_keeps_private_key_outside_workspace() -> None:
    source = text("build_signed_windows_release.ps1")
    allowlist = json.loads((ROOT / "deploy" / "release_payload_allowlist.json").read_text(encoding="utf-8"))
    assert "LOCALAPPDATA" in source
    assert "release-private-key.dpapi" in source
    assert "requirements-windows.lock" in source
    assert "requirements-linux.lock" in source
    assert "Release staging is incomplete" in source
    assert "Release archive is incomplete" in source
    assert 'Where-Object { $_.Extension -in @(".pyc", ".pyo") }' in source
    assert '-Include "*.pyc","*.pyo"' not in source
    for required_runtime in (
        "deploy/check_forward_flat_windows.ps1",
        "deploy/check_super1_flat_windows.ps1",
        "deploy/forward-shadow.service",
        "deploy/release_integrity.ps1",
        "deploy/rollover_forward_shadow_campaign_windows.ps1",
        "deploy/rollover_super1_campaign_windows.ps1",
        "deploy/run_forward_shadow_windows.ps1",
        "deploy/run_super1_windows.ps1",
        "deploy/super1_binding_proof.ps1",
        "deploy/super1_secure_task.ps1",
        "deploy/upgrade_forward_shadow_windows.ps1",
        "deploy/upgrade_super1_signed_app_windows.ps1",
        "deploy/watchdog_windows.ps1",
        "forward_shadow/frozen_config.json",
        "live_forward/capital_demo_config.json",
        "research_candidates/super1/super1_signal_contract.json",
        "scripts/check_mt5_flat.py",
        "scripts/run_capital_forward.py",
        "scripts/run_super1_xm_mt5_forward.py",
        "scripts/run_xm_mt5_forward.py",
    ):
        assert any(required_runtime in profile["files"] for profile in allowlist["profiles"].values())


def test_super1_release_stages_every_sealed_candidate_provenance_input() -> None:
    source = text("build_signed_windows_release.ps1")
    allowlist = json.loads((ROOT / "deploy" / "release_payload_allowlist.json").read_text(encoding="utf-8"))
    candidate = json.loads(
        (
            ROOT
            / "research_candidates"
            / "v20_strategy_loop"
            / "nq_spx_local_fresh_forward_candidate_v1.json"
        ).read_text(encoding="utf-8")
    )

    assert "legacy order/data/broker files" in source
    super1_files = allowlist["profiles"]["super1"]["files"]
    assert "research_candidates/super1/super1_unsigned_candidate_v2.json" in super1_files
    assert "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json" not in super1_files
    assert any(item["path"].startswith("outputs/reports/") for item in candidate["provenance"]["inputs"])


def test_super1_signed_app_upgrade_is_offline_transactional_and_leaves_tasks_stopped() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")

    assert 'Assert-SignedReleaseArchive -Archive $ArchivePath -ExpectedProfile "super1"' in source
    assert "Install-LockedRelease -Python $StagedPython -App $Staging" in source
    assert '& $StagedPython -I -E -B -m compileall -q $Staging' in source
    assert "super1.validate_super1_candidate(runtime)" in source
    assert "Move-Item -LiteralPath $App -Destination $ArchivedApp" in source
    assert "Move-Item -LiteralPath $Venv -Destination $ArchivedVenv" in source
    assert "Move-Item -LiteralPath $Staging -Destination $App" in source
    assert "Move-Item -LiteralPath $StagedVenv -Destination $Venv" in source
    assert "Protect-ReleaseApp -App $App -RunnerIdentity $runnerIdentity" in source
    assert "Protect-ReleaseApp -App $Venv -RunnerIdentity $runnerIdentity" in source
    assert "Wait-Super1Stopped" in source
    assert "Get-Super1PythonProcesses" in source
    assert "Set-ScheduledTask -TaskName $MainTask" not in source
    assert "Assert-FrozenSuper1PasswordTask" in source
    assert '$script:Super1PowerShellExe' in source
    assert 'GetFullPath([string]$actions[0].Execute)' in source
    assert "RestartCount -ne 999" not in source
    assert "RestartCount -ne 0" in source
    assert "Set-ScheduledTask -TaskName $WatchdogTask -Action $watchdogAction -Settings $watchdogSettings" in source
    assert 'state = "UPGRADED_STOPPED"' in source
    assert "protected pre-hardening broker flat probe" in source
    assert "Move-Item -LiteralPath $State" not in source
    assert "EXISTING_PRESERVED" not in source
    assert "Refusing to overwrite an existing Super1 rollover candidate" in source
    assert source.count('state = "CREATED_FROM_SIGNED_APP"') == 1
    for candidate_name in (
        "run_capital_forward.py.candidate",
        "run_xm_mt5_forward.py.candidate",
        "run_super1_xm_mt5_forward.py.candidate",
        "super1_xm_mt5_demo_config.json.candidate",
        "super1_manifest.json.candidate",
        "super1_signal_contract.json.candidate",
    ):
        assert candidate_name in source


def test_super1_upgrade_uses_a_reverified_immutable_release_copy() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")

    first_verify = source.index(
        'Assert-SignedReleaseArchive -Archive $ArchivePath -ExpectedProfile "super1"'
    )
    copy = source.index("Copy-FileCreateNew -Source $component -Target $destination")
    second_verify = source.index("$verifiedReleaseManifest = Assert-SignedReleaseArchive")
    expand = source.index("Expand-Archive -LiteralPath $VerifiedArchive")
    assert first_verify < copy < second_verify < expand
    assert "Verified signed release component copy hash mismatch" in source
    assert "Protect-SignedReleaseCopy `" in source
    assert "-Path $SignedReleaseRoot `" in source
    assert "-CallerSid $callerSid `" in source
    assert "-RunnerSid $runnerSid" in source
    protect_function = source[source.index("function Protect-SignedReleaseCopy") :]
    signed_copy_function = protect_function[: protect_function.index("function Invoke-Super1OfflineValidation")]
    assert "$CallerSid = [Security.AccessControl.FileSystemRights]::ReadAndExecute" not in signed_copy_function
    assert "Verified signed release ACL is not exactly SYSTEM/Administrators" in signed_copy_function
    assert "Super1 upgrade caller unexpectedly has a direct ACE" in signed_copy_function
    assert protect_function.index("IsReadOnly") < protect_function.index(
        "Set-ExactSuper1DirectoryAcl"
    )
    assert '& $script:Super1IcaclsExe (Join-Path $Path "*") /reset /T /C /Q' in protect_function
    assert '& $script:Super1IcaclsExe $Path /setowner "*S-1-5-18" /T /C /Q' in protect_function
    assert "Verified signed release has an unexpected ACL rule" in protect_function


def test_super1_upgrade_pins_and_read_locks_the_release_trust_helper() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")
    actual = hashlib.sha256((DEPLOY / "release_integrity.ps1").read_bytes()).hexdigest()
    section = source[
        source.index("$integrityCandidates = @(") : source.index("$mainTaskDefinition =")
    ]

    assert f'$ExpectedIntegrityScriptSha256 = "{actual}"' in source
    assert 'Join-Path $PSScriptRoot "release_integrity.ps1"' in section
    assert 'Join-Path $App "deploy\\release_integrity.ps1"' in section
    first_hash = section.index("$integrityHash = (Get-FileHash")
    lock = section.index("$IntegrityScriptLock = [IO.File]::Open")
    locked_hash = section.index("changed while acquiring its read lock")
    dot_source = section.index(". $IntegrityScript")
    post_hash = section.index("changed while it was dot-sourced")
    assert first_hash < lock < locked_hash < dot_source < post_hash
    assert "[IO.FileShare]::Read" in section
    assert "$IntegrityScriptLock.Dispose()" in source


def test_super1_upgrade_runs_only_from_sha_addressed_protected_self() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")
    header = source[: source.index(")\n\n$ErrorActionPreference")]

    assert "[string]$ExpectedSelfSha256" in header
    assert '[Environment+SpecialFolder]::ProgramFiles' in source
    assert '("super1-" + $ExpectedSelfSha256)' in source
    assert '"upgrade_super1_signed_app_windows.ps1"' in source
    assert "$CurrentSelfPath.Equals(" in source
    assert "$SelfScriptLock = [IO.File]::Open" in source
    assert "[IO.FileShare]::Read" in source
    assert "mandatory self SHA-256 pin" in source
    assert "Assert-ExactSuper1DirectoryAcl `" in source
    assert "Assert-ExactSuper1FileAcl `" in source
    assert "Protected Super1 upgrader is not read-only" in source
    assert "$SelfScriptLock.Dispose()" in source
    acl_gate = source.index("$protectedDeployRights = @{")
    first_runtime_mutation = source.index("Stop-Super1RuntimeForRollback\n")
    assert acl_gate < first_runtime_mutation


def test_super1_upgrade_privately_locks_transaction_parents() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")

    parent_guard = source.index("Assert-NoUntrustedDeleteChild `")
    archive_lock = source.index("Protect-TransactionArchiveRoot `")
    transaction_create = source.index("New-Item -ItemType Directory -Path $UpgradeArchive")
    transaction_lock = source.index("Protect-TransactionArchiveRoot `", archive_lock + 1)
    signed_root_create = source.index("New-Item -ItemType Directory -Path $SignedReleaseRoot")

    assert parent_guard < archive_lock < transaction_create < transaction_lock < signed_root_create
    assert "function Set-ExactSuper1DirectoryAcl" in source
    assert "$acl.SetAccessRuleProtection($true, $false)" in source
    assert "Private transaction directory still inherits ACLs" in source
    assert "Private transaction directory is not owned by SYSTEM" in source
    assert "$parentAcl.GetOwner(" in source
    assert "Super1 runner unexpectedly has an ACE on private transaction storage" in source
    assert "Super1 upgrade caller unexpectedly has a direct ACE on private transaction storage" in source
    private_root_function = source[
        source.index("function Protect-TransactionArchiveRoot") :
        source.index("function Protect-PrivateFile")
    ]
    assert "$CallerSid = [Security.AccessControl.FileSystemRights]::FullControl" not in private_root_function
    private_file_function = source[
        source.index("function Protect-PrivateFile") : source.index("function Stop-Super1Tasks")
    ]
    assert "$CallerSid = [Security.AccessControl.FileSystemRights]::FullControl" not in private_file_function
    assert "Verified signed release has an unexpected ACL rule" in source
    assert "Transaction archive root cannot be a reparse point" in source
    assert "Verified signed release archive changed during extraction" in source
    assert "Expand-Archive -LiteralPath $ArchivePath" not in source


def test_super1_upgrade_candidates_are_preflighted_atomic_and_rolled_back() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")

    preflight = source.index("foreach ($spec in $CandidateSpecs)")
    app_swap = source.index("Move-Item -LiteralPath $App -Destination $ArchivedApp")
    candidate_function = source[
        source.index("function New-AtomicSuper1Candidate") : source.index("$App = $null")
    ]
    assert preflight < app_swap
    assert '[IO.FileMode]::CreateNew' in source
    assert '[IO.File]::Move($temporary, $Target)' in source
    assert "Join-Path $TransactionRoot" in candidate_function
    assert '"$Target.tmp-$TransactionId"' not in candidate_function
    assert (
        candidate_function.index("Protect-PrivateFile -Path $temporary")
        < candidate_function.index("[IO.File]::Move($temporary, $Target)")
        < candidate_function.index("Protect-PrivateFile -Path $Target")
    )
    assert "-TransactionRoot $UpgradeArchive" in source
    assert "Super1 candidate temporary copy hash mismatch" in source
    assert "Super1 candidate final hash mismatch" in source
    assert "foreach ($candidate in @($CreatedCandidates))" in source
    assert "foreach ($temporary in @($CandidateTempPaths))" in source


def test_super1_upgrade_rolls_back_app_venv_and_complete_task_definitions() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")

    assert '(Join-Path $UpgradeArchive "app.staging")' in source
    assert '(Join-Path $UpgradeArchive "venv311.staging")' in source
    stage_lock = source.index("Protect-ReleaseApp -App $Staging")
    venv_lock = source.index("Protect-ReleaseApp -App $StagedVenv")
    app_swap = source.index("Move-Item -LiteralPath $App -Destination $ArchivedApp")
    assert stage_lock < venv_lock < app_swap
    assert source.count("Assert-EffectiveReleaseFileAcl `") >= 3
    assert "$stagedFlatCheck, $stagedRollover, $stagedSecureHelper, $stagedFlatDiagnostic" in source
    assert '(Join-Path $App "scripts\\run_super1_xm_mt5_forward.py")' in source
    assert "$Python\n    ))" in source
    acl_function = source[
        source.index("function Assert-EffectiveReleaseFileAcl") :
        source.index("function New-AtomicSuper1Candidate")
    ]
    assert "-not $rule.IsInherited" in acl_function
    assert "Runner has write-capable access" in acl_function
    assert "Release file is not owned by SYSTEM" in acl_function
    assert '"S-1-5-18"' in acl_function
    assert '"S-1-5-32-544"' in acl_function
    assert "[IO.File]::Open" in acl_function
    assert "Release tree root is not owned by SYSTEM" in source
    root_acl_function = source[
        source.index("function Assert-RunnerReadExecuteAcl") :
        source.index("function Assert-EffectiveReleaseFileAcl")
    ]
    assert "Release tree root still inherits ACLs" in root_acl_function
    assert "Release tree root has an unexpected ACL entry" in root_acl_function
    assert "DeleteSubdirectoriesAndFiles" in root_acl_function
    assert "& $BootstrapPython -I -S -E -B -m venv $StagedVenv" in source
    assert "& $Python -m venv $StagedVenv" not in source
    assert "Install-LockedRelease -Python $Python -App $Staging" not in source
    assert "Move-Item -LiteralPath $Venv -Destination $FailedVenv" in source
    assert "Move-Item -LiteralPath $ArchivedVenv -Destination $Venv" in source
    assert "Move-Item -LiteralPath $ArchivedApp -Destination $App" in source
    assert "-Action $originalMainActions" not in source
    assert "-Settings $originalMainSettings" not in source
    assert "frozen Password task verification" in source
    assert "-Action $originalWatchdogActions" in source
    assert "-Settings $originalWatchdogSettings" in source
    assert "Export-ScheduledTask -TaskName $MainTask" in source
    assert "Export-ScheduledTask -TaskName $WatchdogTask" in source
    assert source.count("-RestartCount 0") >= 1
    assert "RestartCount -ne 999" not in source
    assert "RestartCount -ne 0" in source
    assert '"Running", "Queued"' in source
    assert source.rstrip().endswith('$Result | ConvertTo-Json -Depth 8')


def test_super1_upgrade_requires_a_stopped_runtime_before_rollback_mutation() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")

    catch = source.index("catch {\n    $failure = $_")
    stop_gate = source.index("Stop-Super1RuntimeForRollback", catch)
    task_restore = source.index("if ($watchdogActionChanged)", catch)
    candidate_cleanup = source.index("foreach ($candidate in @($CreatedCandidates))", catch)
    app_rollback = source.index("Move-Item -LiteralPath $App -Destination $FailedApp", catch)
    venv_rollback = source.index("Move-Item -LiteralPath $Venv -Destination $FailedVenv", catch)

    assert stop_gate < min(task_restore, candidate_cleanup, app_rollback, venv_rollback)
    assert "Wait-Super1Stopped -TimeoutSeconds 5" in source
    assert "Stop-Process -Id ([int]$process.ProcessId) -Force" in source
    assert "Wait-Super1Stopped -TimeoutSeconds 30" in source
    assert "rollback was not attempted because the stopped-runtime gate failed" in source


def test_super1_upgrade_never_executes_the_mutable_old_venv_as_administrator() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")
    trust = source[
        source.index("function Get-TrustedBootstrapPythonEvidence") :
        source.index("function Protect-SignedReleaseCopy")
    ]
    main = source[source.index("$App = $null") :]

    assert "[string]$ExpectedPythonSha256" in source[: source.index(")\n\n$ErrorActionPreference")]
    assert "pyvenv.cfg" in trust
    assert "duplicate bootstrap key" in trust
    assert "outside Program Files" in trust
    assert "path contains a reparse point" in trust
    assert "path has an untrusted owner" in trust
    assert "Untrusted SID can mutate" in trust
    assert 'Microsoft.PowerShell.Security\\Get-AuthenticodeSignature `' in trust
    assert "-LiteralPath $python" in trust
    assert 'SignerCertificate.Subject -notmatch "Python Software Foundation"' in trust
    assert "does not match the pinned SHA-256" in trust
    assert "[IO.FileShare]::Read" in trust
    assert "changed while its read lock was acquired" in trust
    assert "& $BootstrapPython -I -S -E -B -m venv $StagedVenv" in main
    assert "& $Python -m venv $StagedVenv" not in main
    assert main.index("$BootstrapPythonLock = $BootstrapPythonEvidence.lock") < main.index(
        "& $BootstrapPython -I -S -E -B -m venv $StagedVenv"
    ) < main.index("$BootstrapPythonLock.Dispose()")
    assert "bootstrap Python changed while creating the staged environment" in main


def test_python_and_runtime_dependencies_are_exactly_pinned() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pandas==3.0.3' in project
    assert 'MetaTrader5==5.0.6162' in project
    assert 'setuptools==81.0.0' in project


def test_shared_release_commands_are_isolated_and_use_absolute_system_tools() -> None:
    source = text("release_integrity.ps1")

    assert '& $Python -I -E -B -m pip install' in source
    assert '& $Python -m pip' not in source
    assert '$env:PYTHONHOME = $null' in source
    assert '$env:PYTHONPATH = $null' in source
    assert '(Join-Path ([Environment]::SystemDirectory) "icacls.exe")' in source
    assert '& icacls.exe' not in source


def test_super1_fixed_launcher_uses_protected_probe_and_terminal_pins() -> None:
    launcher = text("run_super1_windows.ps1")
    helper = text("super1_secure_task.ps1")

    assert '$ProbeRequest = [IO.Path]::GetFullPath((Join-Path $ProbeControl "active.json"))' in launcher
    assert 'Open-Super1SecureLockedFile -Path $ProbeRequest' in launcher
    assert '[IO.FileShare]::Read' in launcher
    assert 'terminal_runtime_pin.json' in launcher
    assert 'terminal_sha256' in launcher
    assert 'powershell_runtime_pin.json' in launcher
    assert "GetCurrentProcess().MainModule.FileName" in launcher
    assert "Microsoft.PowerShell.Security\\Get-AuthenticodeSignature" not in launcher
    assert "PowerShellHostLock" in launcher
    assert 'state = "CRITICAL_STOP"' in launcher
    assert "exited unexpectedly with persistent code" in launcher
    assert 'RedirectStandardInput = $true' in launcher
    assert 'ProvideCredential' in launcher
    assert 'kind -notin @("flat", "rollover_init")' in launcher
    assert 'BROKER_SAVED_SESSION' not in launcher
    assert 'BROKER_CREDENTIAL_FATAL' in launcher
    assert 'LauncherExitCode' not in launcher
    assert 'throw "Super1 broker credential is unavailable or cannot be decrypted."' in launcher
    trap_start = launcher.index('trap {')
    trap_end = launcher.index('\n}\n\n$script:LauncherPhase', trap_start) + 2
    assert launcher[trap_start:trap_end].rstrip().endswith('exit 78\n}')
    assert "Set-ScheduledTask" not in helper
    assert "New-ScheduledTaskAction" not in helper


def test_super1_launcher_credential_gate_decrypt_failure_is_fatal_and_does_not_start_runtime(tmp_path) -> None:
    launcher = text("run_super1_windows.ps1")
    gate_start = launcher.index("$SecurePassword = (Get-Content")
    gate_start = launcher.rfind("    try {", 0, gate_start)
    gate_end = launcher.index("\n\n    if (Test-Path", gate_start)
    credential_gate = launcher[gate_start:gate_end]
    trap_start = launcher.index("trap {")
    trap_end = launcher.index("\n}\n\n$script:LauncherPhase", trap_start) + 2
    trap_block = launcher[trap_start:trap_end]
    cleanup_start = launcher.rfind("finally {")
    cleanup_block = launcher[cleanup_start:].strip()
    assert cleanup_block.endswith("}")
    cleanup_body = cleanup_block[len("finally {") : -1]
    invalid_credential = "fixture" + "-ciphertext"
    (tmp_path / "xm-password.dpapi").write_text(invalid_credential, encoding="utf-8")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")
    root_literal = str(tmp_path).replace("'", "''")
    runtime_marker = str(tmp_path / "runtime-started.txt").replace("'", "''")
    cleanup_marker = str(tmp_path / "cleanup-completed.txt").replace("'", "''")
    script = f"""
$ErrorActionPreference = "Stop"
$null = Set-StrictMode -Version Latest
$Root = '{root_literal}'
$script:LauncherPhase = "BROKER_CREDENTIAL_GATE"
$env:XM_MT5_READ_ONLY_PASSWORD = "fixture-marker"
$PasswordPtr = [IntPtr]::Zero
$SecurePassword = $null
$PreviousBytecode = $null
$PreviousPythonHome = $null
$PreviousPythonPath = $null
$PreviousPSModulePath = $null
$PointerLock = $null
$TerminalLock = $null
$ProbeLock = $null
$TransactionRequestLock = $null
$PowerShellHostLock = $null
$PowerShellPinLock = $null
{trap_block}
try {{
{credential_gate}
}}
finally {{
{cleanup_body}
    [IO.File]::WriteAllText('{cleanup_marker}', 'cleaned')
}}
if (-not (Test-Path -LiteralPath '{runtime_marker}' -PathType Leaf)) {{
    [IO.File]::WriteAllText('{runtime_marker}', 'started')
}}
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = completed.stdout + completed.stderr
    failure_path = tmp_path / "state" / "launcher_failure.json"
    health_path = tmp_path / "state" / "health.json"
    assert completed.returncode == 78
    assert not (tmp_path / "runtime-started.txt").exists()
    assert (tmp_path / "cleanup-completed.txt").exists()
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    health = json.loads(health_path.read_text(encoding="utf-8"))
    assert failure["state"] == "LAUNCHER_FATAL_NO_SEND"
    assert failure["phase"] == "BROKER_CREDENTIAL_FATAL"
    assert health["state"] == "CRITICAL_STOP"
    assert health["last_cycle"]["execution"]["state"] == "LAUNCHER_FATAL_NO_SEND"
    helper = text("super1_secure_task.ps1")
    assert "[int]$main.Settings.RestartCount -ne 0" in helper
    assert "$mainRestartInterval -ne [TimeSpan]::FromMinutes(15)" in helper
    assert "LauncherExitCode" not in launcher
    assert invalid_credential not in output
    assert "fixture-marker" not in output
    evidence = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (failure_path, health_path)
    )
    assert invalid_credential not in evidence
    assert "fixture-marker" not in evidence


def test_super1_task_contract_freezes_password_task_and_canonicalizes_watchdog() -> None:
    helper = text("super1_secure_task.ps1")

    assert "function Assert-Super1SecureTaskBindings" in helper
    assert '$script:Super1SecurePowerShellExe' in helper
    assert '[string]$main.Principal.LogonType -cne "Password"' in helper
    assert '[string]$main.Principal.RunLevel -cne "Limited"' in helper
    assert "[int]$main.Settings.RestartCount -ne 0" in helper
    assert '[string]$watchdog.Principal.LogonType -cne "ServiceAccount"' in helper
    assert '[string]$watchdog.Principal.RunLevel -cne "Highest"' in helper
    assert "[int]$watchdog.Settings.RestartCount -ne 0" in helper
    assert "function Assert-Super1SecureNoTriggers" in helper
    assert "$triggers.Count -ne 0" in helper
    assert "function Get-Super1SecureMarketScheduleState" in helper


def test_super1_flat_check_never_changes_the_password_task_definition() -> None:
    source = text("check_super1_flat_windows.ps1")
    helper = text("super1_secure_task.ps1")

    assert 'Get-Super1SecureTaskXml -TaskName $Task' in source
    assert 'kind = "flat"' in source
    assert 'New-Super1SecureLockedFile `' in source
    assert 'producer_path = $Producer' in source
    assert 'request_path = $TransactionRequest' in source
    assert 'Get-Super1SecureProducerEnvelope `' in source
    assert 'Seal-Super1SecureEvidenceTree -Path $Transaction' in source
    assert 'task_xml_unchanged = $true' in source
    assert "$originalWatchdogXml" in source
    assert "Get-Super1SecureMarketScheduleState" in source
    assert "scheduled_closed" in source
    assert 'Preserving an existing Super1 terminal is forbidden' in source
    assert "[switch]$PreserveExistingTerminal" in source
    assert "-PreserveTerminal:$PreserveExistingTerminal" not in source
    assert "function Get-Super1SecureRunnerProcesses" in helper
    assert "-MethodName GetOwner" in helper
    assert "-AllowUnresolved" in helper
    assert "return $matches.ToArray()" in helper
    assert "Get-Super1SecureUnexpectedRunnerProcesses" in helper
    assert "unexpected_runner_pids" in helper
    assert "for ($pass = 0; $pass -lt 3; $pass++)" in helper
    assert 'Stop-Super1SecureRuntime -Root $Root' in source
    assert "Set-ScheduledTask" not in source
    assert "run_super1_windows.ps1.previous" not in source


def test_super1_rollover_consumes_two_sealed_flat_proofs_and_fixed_init() -> None:
    source = text("rollover_super1_campaign_windows.ps1")
    header = source[: source.index(")\n\n$ErrorActionPreference")]

    assert "[string]$ReadinessEvidence" in header
    assert "[string]$ExpectedReadinessSha256" in header
    assert 'Assert-Super1SecureSealedTree -Path $readinessTransaction' in source
    assert 'internal pre-mutation broker flat probe' in source
    assert '-File $InternalFlatScript `' in source
    assert '$internalAge.TotalSeconds -gt 90' in source
    assert 'kind = "rollover_init"' in source
    assert 'producer_path = $initProducer' in source
    assert 'request_path = $initTransactionRequest' in source
    assert 'Get-Super1SecureProducerEnvelope `' in source
    assert 'Seal-Super1SecureEvidenceTree -Path $initTransaction' in source
    assert '$tupleValidator | & $Python -I -E -B -' in source
    assert "Assert-Super1SecureTaskBindings" in source
    assert "Assert-FrozenSuper1TaskContracts" in source
    assert "$originalWatchdogXml" in source
    assert "Set-ScheduledTask" not in source
    assert "runScriptPatched" not in source
    assert "previousRunScript" not in source


def test_super1_rollover_preserves_state_after_runtime_start_attempt() -> None:
    source = text("rollover_super1_campaign_windows.ps1")

    assert "$brokerSideEffectPossible = $false" in source
    assert "$brokerSideEffectPossible = $true" in source
    assert 'state = "BROKER_SIDE_EFFECT_POSSIBLE"' in source
    assert 'automatic_restart = $false' in source
    assert "Invoke-Super1RolloverCatchPolicy" in source
    assert "-BrokerSideEffectPossible ([bool]$brokerSideEffectPossible)" in source


def test_super1_upgrade_pins_and_hardens_python_terminal_and_config() -> None:
    source = text("upgrade_super1_signed_app_windows.ps1")
    header = source[: source.index(")\n\n$ErrorActionPreference")]

    assert "[string]$ExpectedPythonSha256" in header
    assert "[string]$ExpectedTerminalSha256" in header
    assert "function Get-Super1TerminalPinEvidence" in source
    assert "function Protect-Super1TerminalRuntime" in source
    assert "Get-Super1TerminalProcesses" in source
    assert "Refusing to stop canonical Super1 terminal owned by unexpected SID" in source
    assert "protected pre-hardening broker flat probe" in source
    assert "-PreserveExistingTerminal" not in source
    assert "function Get-Super1RunnerProcesses" in source
    assert "Get-UnexpectedSuper1RunnerProcesses" in source
    assert "Protect-Super1RuntimeConfigFiles" in source
    assert "return $results.ToArray()" in source
    assert "if ($originalMainTaskXml -and $runnerSid -and $expectedMainArguments)" in source
    assert "[Convert]::ToBase64String(" in source
    assert 'json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))' in source
    assert 'super1_xm_mt5_demo_config.json' in source
    assert '"xm-server.txt"' not in source
    assert "xm-password.dpapi" in source
    assert 'terminal_runtime_pin.json' in source
    assert 'powershell_runtime_pin.json' in source
    assert "PowerShellHostSha256" in source
    assert "Assert-FrozenSuper1PasswordTask" in source
    assert "Initialize-Super1ProbeControl" in source
    assert "Import-Module -Name $ScheduledTasksModule" in source
    assert '& $Python -m pip' not in source
    assert '& $StagedPython -m' not in source
    assert '& icacls.exe' not in source
    launcher = text("run_super1_windows.ps1")
    assert '$script:LauncherPhase = "BOOTSTRAP_START"' in launcher
    assert '$script:LauncherPhase = "POWERSHELL_HASH_VERIFIED"' in launcher
    assert '$script:LauncherPhase = "SECURE_HELPER_GATE"' in launcher
    assert "Get-AuthenticodeSignature" not in launcher
    assert 'state\\launcher_failure.json' in launcher
    assert launcher.index('$failureJson = [ordered]@{') < launcher.index('$fatalJson = [ordered]@{')
    assert 'catch {' in launcher
    assert '$script:LauncherPhase = "BROKER_CREDENTIAL_FATAL"' in launcher
    assert 'LauncherExitCode' not in launcher
    trap_start = launcher.index('trap {')
    trap_end = launcher.index('\n}\n\n$script:LauncherPhase', trap_start) + 2
    assert launcher[trap_start:trap_end].rstrip().endswith('exit 78\n}')
    assert 'BROKER_SAVED_SESSION' not in launcher
    assert 'launcher_phase=$launcherPhase' in text("check_super1_flat_windows.ps1")


def test_watchdog_telegram_is_transition_based_and_rate_limited() -> None:
    source = text("watchdog_windows.ps1")

    assert "$TelegramRuntimeStateSchema = 2" in source
    assert "$TelegramAlertStates -contains $currentState" in source
    assert 'active_alert_categories = @()' in source
    assert '[int]$saved.schema_version -ne $TelegramRuntimeStateSchema' in source
    assert 'Where-Object { $TelegramAlertStates -contains $_ }' in source
    assert 'next_attempt_utc = $now.AddMinutes($delay).ToString("o")' in source
    assert "Get-TelegramRetryDelayMinutes" in source
    assert "return 360" in source
    assert "$script:deliveredTelegramKeys.Contains($Key)" in source
    assert "if ($null -eq $parsed)" in source
    assert 'IsNullOrWhiteSpace($message)' in source
    assert "discarded invalid Telegram outbox entry" in source
    assert '[switch]$LibraryOnly' in source


def test_stage_signed_upgrader_windows_fail_closed_contract() -> None:
    path = DEPLOY / "stage_signed_upgrader_windows.ps1"
    functions = {item["name"] for item in facts(path, "function")}
    commands = [item for item in facts(path, "command") if not item["unreachable"]]
    members = {item["member"] for item in facts(path, "member")}
    assignments = {item["left"] for item in facts(path, "assignment")}
    assert {"Assert-StageContainer", "Set-StageAcl", "Assert-StageTarget"}.issubset(functions)
    assert "CreateNew" in members and "$upgraderSha256" in assignments
    assert sum(item["name"] == "Assert-StageContainer" for item in commands) >= 6


def test_build_signed_release_enforces_dirty_git_python311_and_test_gates() -> None:
    source = text("build_signed_windows_release.ps1")

    assert 'git -C $RepoRoot status --porcelain -- $SourceRoot' in source
    assert "Git working tree is dirty; refusing release build" in source
    assert 'git -C $RepoRoot rev-parse HEAD' in source
    assert '$pyParts[0] -ne "3.11" -or $pyParts[1] -ne "CPython"' in source
    assert "Release build requires CPython 3.11" in source
    assert source.index("[void][IO.Directory]::CreateDirectory($TempRoot)") < source.index(
        '$fullCollectPath = Join-Path $TempRoot "full.collect.txt"'
    )
    assert '$pytestCmd = "$Python -m pytest -q $SourceRoot --junitxml=<full-suite>"' in source
    assert "Release build aborted: pytest test suite failed" in source
    assert "Assert-JunitMatchesInventory" in source
    assert "Get-CollectionNodeIds" in source
    assert "pytest_nodeid_sha256" in source
    assert "artifact_pytest_nodeid_sha256" in source
    assert 'tests\\v08_helpers.py' in source
    assert "deploy/stage_signed_upgrader_windows.ps1" in json.dumps(
        json.loads((ROOT / "deploy" / "release_payload_allowlist.json").read_text(encoding="utf-8"))
    )
    for key in (
        "release_id",
        "created_at_utc",
        "git_commit",
        "git_dirty",
        "python_version",
        "python_executable_sha256",
        "pytest_command",
        "pytest_passed",
        "pytest_collected_count",
        "pytest_pass_count",
        "pytest_skipped_count",
        "pytest_nodeid_sha256",
        "pytest_passed_count",
        "artifact_pytest_collected_count",
        "artifact_pytest_pass_count",
        "artifact_pytest_skipped_count",
        "artifact_pytest_nodeid_sha256",
        "archive_sha256",
        "files = $manifestFiles",
    ):
        assert key in source


def test_release_builder_requires_fresh_attestation_and_separate_artifact_hashes() -> None:
    source = text("build_signed_windows_release.ps1")

    assert "--report-dir $FreshAuditRoot" in source
    assert "Fresh engine reliability audit failed" in source
    assert "engine_package_sha256 =" in source
    assert "release_archive_sha256 =" in source
    assert "calendar_artifact_sha256 =" in source
    assert "input_dataset_sha256 =" in source
    assert "reliability_audit_ready" in source
    assert "phase0_canonical_baseline.json" not in source


def test_release_integrity_validates_entries_and_rejects_credentials_and_source_mismatch() -> None:
    source = text("release_integrity.ps1")

    assert "Forbidden credential or signing key file in archive" in source
    assert r"\.(key|pem|dpapi|pfx|cer|crt)$" in source
    assert r"(credential|password|secret|\.env)" in source
    assert "Archive is missing manifest file entry" in source
    assert "Archive file entry SHA-256 mismatch" in source
    assert "Archive contains unexpected entry not in manifest" in source
    assert "function Assert-ReleaseSourceIntegrity" in source
    assert "Production source file missing from release archive" in source
    assert "Production file content mismatch between source and release archive" in source
    assert "Release archive contains unexpected production entry not present in source tree" in source


def test_fresh_super1_install_is_manual_and_new_york_window_guarded() -> None:
    finalize = text("finalize_super1_fresh_windows.ps1")
    watchdog_installer = text("install_super1_watchdog_windows.ps1")
    start = text("start_super1_local_windows.ps1")
    stop = text("stop_super1_local_windows.ps1")

    assert "New-ScheduledTaskTrigger -AtLogOn" not in finalize
    assert "Start-ScheduledTask -TaskName $TaskName" not in finalize
    assert "-ManualStart" not in finalize
    assert "install_super1_watchdog_windows.ps1" in finalize
    assert "Register-ScheduledTask" in finalize
    assert "-RestartCount 0" in watchdog_installer
    assert "-RestartCount 999" not in watchdog_installer
    assert "Start-ScheduledTask" not in watchdog_installer
    assert 'FindSystemTimeZoneById("Eastern Standard Time")' in start
    assert 'ParseExact("09:20"' in start
    assert 'ParseExact("13:00"' in start
    assert 'Start-ScheduledTask -TaskName $taskName' in start
    assert 'Stop-ScheduledTask -TaskName $taskName' in stop
    assert 'Contract.watchdog_task' in stop
    assert 'ExecutablePath -ceq $terminal' in stop
    assert 'Contract.terminal' in stop


def test_fresh_super1_bootstrap_accepts_only_complete_cpython311() -> None:
    bootstrap = text("bootstrap_super1_fresh_windows.ps1")

    assert '[string]$PythonExe = "C:\\Program Files\\Python311\\python.exe"' in bootstrap
    assert 'Lib\\encodings\\__init__.py' in bootstrap
    assert 'Python 3.11 standard library is incomplete' in bootstrap
    assert '3.11|CPython' in bootstrap
    assert 'Requested existing Python 3.11 executable is missing' in bootstrap


def test_fresh_super1_installers_pin_the_verified_xm_installer() -> None:
    expected_hash = "FD8CA7875A13DED372492BC8C06B2DDDDEBE6B522BEA62E81BA203436B012320"
    bootstrap = text("bootstrap_super1_fresh_windows.ps1")
    resume = text("resume_super1_fresh_windows.ps1")

    for script in (bootstrap, resume):
        assert expected_hash in script
        assert "Get-FileHash" in script
        assert "SHA-256 validation failed" in script
