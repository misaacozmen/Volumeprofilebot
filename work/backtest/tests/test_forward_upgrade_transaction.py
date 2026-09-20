from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "upgrade_forward_shadow_windows.ps1"
LAUNCHER = ROOT / "deploy" / "run_forward_shadow_windows.ps1"
FLAT_CHECK = ROOT / "deploy" / "check_forward_flat_windows.ps1"
ROLLOVER = ROOT / "deploy" / "rollover_forward_shadow_campaign_windows.ps1"


def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def launcher_source() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def flat_check_source() -> str:
    return FLAT_CHECK.read_text(encoding="utf-8")


def rollover_source() -> str:
    return ROLLOVER.read_text(encoding="utf-8")


def test_forward_flat_acl_rights_are_materialized_before_cast() -> None:
    text = flat_check_source()
    assert "$runnerExpectedRights = if ($RunnerModify)" in text
    assert "[Security.AccessControl.FileSystemRights]::Synchronize" in text
    assert "[int](if (" not in text


def test_forward_final_archive_seal_does_not_rewalk_locked_transactions() -> None:
    text = source()
    final = text[text.index("$finalArchiveAclError = $null") :]
    assert "-Path $ArchiveRoot `" in final
    assert "-RootOnly `" in final


def test_forward_upgrade_is_signed_staged_and_leaves_tasks_stopped() -> None:
    text = source()
    launcher = launcher_source()

    assert "[Parameter(Mandatory = $true)]" in text
    assert '[string]$Archive' in text
    assert '[string]$ExpectedTerminalSha256' in text
    assert '[string]$TargetRunnerIdentity' in text
    assert '[int]$ExpectedMt5Login' in text
    assert "^[A-Fa-f0-9]{64}$" in text
    assert 'Assert-SignedReleaseArchive `' in text
    assert '-ExpectedProfile "forward-shadow"' in text
    assert text.count("Assert-SignedReleaseArchive `") >= 2
    assert "$ReleaseManifest.archive_file" in text
    assert '"app.stage.$RunId"' in text
    assert '"venv311.stage.$RunId"' in text
    assert "Expand-Archive -LiteralPath $VerifiedArchivePath" in text
    assert '"signed-release"' in text
    assert "Copy-FileCreateNew -Source $component -Destination $destination" in text
    assert "[void]$SignedReleaseLocks.Add($copyLock)" in text
    assert "[IO.FileShare]::None" in text
    assert "[IO.FileShare]::Read" in text
    assert "[IO.FileAttributes]::ReadOnly" in text
    assert "New-ForwardPrivateDirectory -Path $TransactionArchive" in text
    assert "$CallerSid" not in text
    assert "Protect-ForwardTree -Path $SignedReleaseRoot" in text
    assert "Verified signed archive changed during extraction." in text
    assert "Install-LockedRelease -Python $StagedPython -App $Staging" in text
    assert "& $StagedPython -I -E -B -m compileall -q $Staging" in text
    assert "Assert-ReleasePythonRuntime" in text
    assert "--output-root $ValidationRoot init" in text
    assert "Staged baseline/runtime campaign hashes are inconsistent." in text
    assert "function Move-ForwardDirectoryExact" in text
    assert '-Source $App `\n        -Destination $PreviousApp `' in text
    assert '-Source $Staging `\n        -Destination $App `' in text
    assert '-Source $Venv `\n        -Destination $PreviousVenv `' in text
    assert '-Source $VenvStaging `\n        -Destination $Venv `' in text
    assert "Assert-CanonicalTaskDefinition" in text
    assert '/skipupdate /config:' in launcher
    assert '[string]$process.Name -ieq "terminal64.exe"' in text
    assert text.count(
        "Resolve-ForwardIdentitySid -Identity ([string]$task.Principal.UserId)"
    ) >= 2
    assert "Set-ScheduledTask `" in text
    assert "Register-ScheduledTask `" not in text
    assert "$Folder.RegisterTaskDefinition(" in text
    assert "ForwardShadowRunnerProof.$RunId" in text
    assert "-Action $OriginalMainActions" in text
    assert "-Action $OriginalWatchdogActions" in text
    assert '"$MainTask.previous.xml"' in text
    assert '"$WatchdogTask.previous.xml"' in text
    assert "Assert-RelativeFileHashes -Base $App" in text
    assert "-Path $App `" in text
    assert "-Path $Venv `" in text
    assert "Start-ScheduledTask" not in text
    assert "$registeredTask.Run($null)" in text
    assert text.count("Stop-ScheduledTask -TaskName $MainTask") >= 1
    assert text.count("Stop-ScheduledTask -TaskName $WatchdogTask") >= 1
    assert "-RestartCount 3" in text
    assert "$definition.Principal.RunLevel = 0" in text
    assert "-notlike \"*$ExpectedScript*\"" not in text
    external_verify = text.index("$ExternalReleaseManifest = Assert-SignedReleaseArchive")
    protect_transaction = text.index(
        "New-ForwardPrivateDirectory -Path $TransactionArchive",
        external_verify,
    )
    protect_empty = text.index(
        "New-ForwardPrivateDirectory -Path $SignedReleaseRoot"
    )
    archive_copy = text.index("Copy-FileCreateNew -Source $component -Destination $destination")
    protect_copy = text.index(
        "Protect-ForwardTree -Path $SignedReleaseRoot -SealOwner", archive_copy
    )
    protected_rehash = text.index("Protected signed release component is unreadable or changed")
    copied_verify = text.index("$ReleaseManifest = Assert-SignedReleaseArchive", external_verify)
    expand_copy = text.index("Expand-Archive -LiteralPath $VerifiedArchivePath")
    post_extract_hash = text.index("Verified signed archive changed during extraction.")
    close_locks = text.index("Close-SignedReleaseLocks -Locks $SignedReleaseLocks", protected_rehash)
    assert (
        external_verify
        < protect_transaction
        < protect_empty
        < archive_copy
        < protect_copy
        < protected_rehash
        < copied_verify
        < expand_copy
        < post_extract_hash
        < close_locks
    )


def test_forward_upgrade_rollback_and_finally_are_fail_closed_on_running_code() -> None:
    text = source()
    main_try = text.index("try {", text.index("$CandidateResults ="))
    preflight_archive = text.index("$ExternalReleaseManifest = Assert-SignedReleaseArchive", main_try)
    first_runtime_stop = text.index("Stop-ForwardRuntime\n", main_try)
    runtime_flag = text.index("$runtimeControlEntered = $true", main_try)
    rights_hardening = text.index("Set-ForwardAccountRightsExact", first_runtime_stop)
    catch = text.index("catch {\n    $UpgradeSucceeded = $false")
    rollback_gate = text.index("if (-not $runtimeControlEntered)", catch)
    rollback_stop = text.index("Stop-ForwardRuntime", catch)
    first_candidate_cleanup = text.index("foreach ($candidate in @($CreatedCandidates))", catch)
    first_app_move = text.index('-Label "Rollback new app removal"', catch)
    finally_block = text.rindex("finally {")

    assert preflight_archive < runtime_flag < first_runtime_stop < rights_hardening
    assert rollback_gate < rollback_stop < first_candidate_cleanup < first_app_move
    assert "filesystem rollback was not attempted" in text
    assert "Stop-ForwardRuntimeEarly" not in text
    assert "if ($runtimeControlEntered)" in text[finally_block:]
    assert "Stop-ForwardRuntime" in text[finally_block:]
    assert "Assert-TaskPairStopped" in text[finally_block:]
    assert "Assert-NoForwardPythonProcesses" in text[finally_block:]
    assert "final stopped-state enforcement failed" in text
    final_lock_error = text.index("$finalLockCleanupError = $null", finally_block)
    final_stop = text.index("Stop-ForwardRuntime", final_lock_error)
    final_lock_throw = text.index("signed release lock cleanup failed", final_stop)
    assert final_lock_error < final_stop < final_lock_throw
    process_filter = text[
        text.index("function Get-ForwardPythonProcesses") : text.index(
            "function Assert-TaskPairStopped"
        )
    ]
    assert "run_capital_forward.py" in process_filter


def test_forward_upgrade_preserves_legacy_and_canonical_state() -> None:
    text = source()

    assert '"legacy-state.hold.$RunId"' in text
    assert "Get-TreeFingerprint -Path $CanonicalState" in text
    assert "Get-TreeFingerprint -Path $LegacyState" in text
    assert "ForwardShadow has duplicate canonical and legacy campaign state." in text
    assert "Canonical ForwardShadow placeholder is non-empty" in text
    assert '-Destination $LegacyStateHold `' in text
    assert '-Destination $LegacyState `' in text
    assert "Reattached legacy campaign state" in text
    protect_app = text.index("-Path $App `")
    reattach_legacy = text.index('-Label "Legacy state reattachment"')
    assert protect_app < reattach_legacy
    assert "Rolled-back legacy campaign state" in text
    assert "Rolled-back canonical campaign state or placeholder" in text
    assert "Remove-Item -LiteralPath $CanonicalState" not in text
    assert "Remove-Item -LiteralPath $LegacyState" not in text


def test_forward_upgrade_acl_reset_preserves_effective_child_access() -> None:
    text = source()
    exact_acl = text[
        text.index("function Set-ForwardExactAcl") : text.index(
            "function Assert-NoUntrustedDeleteChild"
        )
    ]
    protect = text[
        text.index("function Assert-ForwardAclSemantics") : text.index(
            "function Close-SignedReleaseLocks"
        )
    ]
    root_acl_start = protect.index("Set-ForwardExactAcl `")

    assert '$fullControlSids = @("S-1-5-18", "S-1-5-32-544")' in protect
    assert "$CallerSid" not in protect
    assert "New-Object Security.AccessControl.DirectorySecurity" in exact_acl
    assert "New-Object Security.AccessControl.FileSecurity" in exact_acl
    assert "$acl.SetAccessRuleProtection($true, $false)" in exact_acl
    assert "[void]$acl.AddAccessRule($rule)" in exact_acl
    assert "[IO.Directory]::SetAccessControl($resolved, $acl)" in exact_acl
    assert "/grant:r" not in protect
    reset = protect.index("& $IcaclsExe $children /reset /T /C /Q")
    verify = protect.index("& $IcaclsExe $resolved /verify /T /C /Q")
    child_acl = protect.index("-Path $sample.FullName", verify)
    assert root_acl_start < reset < verify < child_acl
    assert "$acl.AreAccessRulesProtected" in protect
    assert "ACL has an unexpected rule" in protect
    assert "FileSystemRights]::ReadAndExecute" in protect
    for mutation_right in (
        "Write",
        "Delete",
        "DeleteSubdirectoriesAndFiles",
        "ChangePermissions",
        "TakeOwnership",
    ):
        assert f"FileSystemRights]::{mutation_right}" in protect

    assert "New-ForwardPrivateDirectory -Path $SignedReleaseRoot" in text
    signed_calls = [
        line for line in text.splitlines() if "Protect-ForwardTree -Path $SignedReleaseRoot" in line
    ]
    assert signed_calls == ["    Protect-ForwardTree -Path $SignedReleaseRoot -SealOwner"]
    assert "Protect-ReleaseApp -App $SignedReleaseRoot" not in text


def test_forward_upgrade_reverification_uses_compatible_read_locks() -> None:
    text = source()
    copy = text[
        text.index("function Copy-FileCreateNew") : text.index(
            "function New-CandidateIfMissing"
        )
    ]
    exclusive_write = copy.index("[IO.FileShare]::None")
    flush = copy.index("$destinationStream.Flush($true)", exclusive_write)
    dispose = copy.index("$destinationStream.Dispose()", flush)
    read_lock = copy.index("return [IO.File]::Open", dispose)
    read_access = copy.index("[IO.FileAccess]::Read", read_lock)
    read_share = copy.index("[IO.FileShare]::Read", read_access)

    assert exclusive_write < flush < dispose < read_lock < read_access < read_share
    assert "$completed" not in copy
    assert text.index("[void]$SignedReleaseLocks.Add($copyLock)") < text.index(
        "$ReleaseManifest = Assert-SignedReleaseArchive"
    )


def test_forward_upgrade_legacy_collision_keeps_real_hold_for_rollback() -> None:
    text = source()
    catch = text.index("catch {", text.index("Signed release unexpectedly contains"))
    hold_branch = text.index(
        "if (Test-Path -LiteralPath $LegacyStateHold -PathType Container)", catch
    )
    authoritative = text.index("Rollback-held authoritative legacy campaign state", hold_branch)
    recover_from_app = text.index(
        "elseif (Test-Path -LiteralPath $LegacyState -PathType Container)", hold_branch
    )
    remove_new_app = text.index('-Label "Rollback new app removal"', catch)

    assert hold_branch < authoritative < recover_from_app < remove_new_app
    assert "Rollback legacy-state hold path unexpectedly exists" not in text


def test_forward_upgrade_candidates_are_atomic_and_fail_on_stale_files() -> None:
    text = source()

    candidates = (
        "run_capital_forward.py.candidate",
        "run_xm_mt5_forward.py.candidate",
        "run_forward_shadow_windows.ps1.candidate",
    )
    for candidate in candidates:
        assert candidate in text
    assert "ForwardShadow candidate preflight failed" in text
    assert "Candidate already exists; refusing to mix it with the signed release" in text
    assert "PRESERVED" not in text
    assert "Candidate private copy hash mismatch" in text
    assert "Candidate final hash mismatch" in text
    assert "Copy-FileCreateNew -Source $Source -Destination $temporary" in text
    assert "-Path $Destination `" in text
    assert "[IO.File]::Move($temporary, $Destination)" in text
    assert "$CreatedCandidates.Add($Destination)" in text
    assert "Remove-Item -LiteralPath $candidate -Force" in text
    assert "Signed release archive must not be stored inside the app or state directory." in text


def test_forward_upgrade_private_build_roots_and_parent_delete_guard_are_ordered() -> None:
    text = source()

    assert '(Join-Path $ArchiveRoot "app.stage.$RunId")' in text
    assert '(Join-Path $ArchiveRoot "venv311.stage.$RunId")' in text
    assert '(Join-Path $ArchiveRoot "upgrade-validation.$RunId")' in text
    assert '(Join-Path $ArchiveRoot "legacy-state.hold.$RunId")' in text
    assert "function Assert-NoUntrustedDeleteChild" in text
    assert "DeleteSubdirectoriesAndFiles" in text
    owner_root = text.index("Set-ForwardTrustedOwner -Path $Root -AllowRoot")
    guard_root = text.index("Assert-NoUntrustedDeleteChild -Path $Root")
    protect_archive = text.index("-Path $ArchiveRoot `", guard_root)
    guard_archive = text.index("-Path $ArchiveRoot `", protect_archive + 1)
    create_transaction = text.index("New-ForwardPrivateDirectory -Path $TransactionArchive")
    assert owner_root < guard_root < protect_archive < guard_archive < create_transaction
    assert "-AllowEmpty" in text[protect_archive:create_transaction]
    assert "-SealOwner" in text[protect_archive:create_transaction]
    assert "private transaction path appeared after preflight" in text

    create_stage = text.index("New-ForwardPrivateDirectory -Path $Staging")
    expand = text.index("Expand-Archive -LiteralPath $VerifiedArchivePath", create_stage)
    protect_populated_stage = text.index(
        "Protect-ForwardTree -Path $Staging -SealOwner", expand
    )
    create_venv = text.index("New-ForwardPrivateDirectory -Path $VenvStaging")
    build_venv = text.index("& $BootstrapPython -I -S -E -B -m venv $VenvStaging")
    install = text.index("Install-LockedRelease -Python $StagedPython -App $Staging")
    assert create_stage < expand < protect_populated_stage
    assert create_venv < build_venv < install

    create_validation = text.index("New-ForwardPrivateDirectory -Path $ValidationRoot")
    init = text.index("--output-root $ValidationRoot init")
    assert create_validation < init


def test_forward_upgrade_private_trees_never_grant_the_caller_or_runner() -> None:
    text = source()
    protect = text[
        text.index("function Protect-ForwardTree") : text.index(
            "function Protect-ForwardPrivateFile"
        )
    ]
    atomic_create = text[
        text.index("function New-ForwardPrivateDirectory") : text.index(
            "function Assert-ForwardAclSemantics"
        )
    ]

    assert "$CallerSid" not in text
    assert '$fullControlSids = @("S-1-5-18", "S-1-5-32-544")' in protect
    assert '$security.SetOwner($systemSid)' in atomic_create
    assert '$directory.Create($security)' in atomic_create
    assert "New-ForwardRestorePrivilegeScope" in atomic_create
    assert 'TrustedSids @("S-1-5-18", "S-1-5-32-544")' in atomic_create

    for private_root in (
        "$TransactionArchive",
        "$SignedReleaseRoot",
        "$Staging",
        "$VenvStaging",
        "$ValidationRoot",
    ):
        assert f"New-ForwardPrivateDirectory -Path {private_root}" in text

    archive_lock = text[
        text.index("$TrustedInfrastructureSids") : text.index(
            "$ArchiveBoundaryHardened = $true"
        )
    ]
    assert '-Path $ArchiveRoot `' in archive_lock
    assert "-AllowEmpty `" in archive_lock
    assert "-RunnerIdentity" not in archive_lock

    private_stage_seal = text[
        text.index("$ExpectedDeployHashes") : text.index(
            "$PreviousApp = Join-Path $TransactionArchive"
        )
    ]
    assert "-RunnerIdentity" not in private_stage_seal
    assert private_stage_seal.index("-Path $LegacyState `") < private_stage_seal.index(
        '-Label "Legacy state hold"'
    )

    final_app = text[
        text.index("$Launcher = Join-Path $App") : text.index(
            "$FinalPythonRuntime = Assert-ReleasePythonRuntime"
        )
    ]
    assert "-Path $App `" in final_app
    assert "-Path $Venv `" in final_app
    assert final_app.count("-RunnerIdentity $RunnerIdentity") >= 3

    candidate = text[
        text.index("function New-CandidateIfMissing") : text.index("$CandidateResults")
    ]
    assert "Protect-ForwardPrivateFile `\n        -Path $Destination `" in candidate
    assert "-RunnerSid $RunnerSid" in candidate


def test_forward_upgrade_parent_guards_and_directory_swaps_are_race_closed() -> None:
    text = source()
    guard = text[
        text.index("function Assert-NoUntrustedDeleteChild") : text.index(
            "function Move-ForwardDirectoryExact"
        )
    ]
    move = text[
        text.index("function Move-ForwardDirectoryExact") : text.index(
            "function New-ForwardRestorePrivilegeScope"
        )
    ]

    for mutation_right in (
        "Write",
        "WriteData",
        "CreateFiles",
        "AppendData",
        "CreateDirectories",
        "WriteExtendedAttributes",
        "WriteAttributes",
        "DeleteSubdirectoriesAndFiles",
        "Delete",
        "ChangePermissions",
        "TakeOwnership",
    ):
        assert f"FileSystemRights]::{mutation_right}" in guard

    assert "if (Test-Path -LiteralPath $resolvedDestination)" in move
    assert "[IO.Directory]::Move($resolvedSource, $resolvedDestination)" in move
    assert "exact directory move postcondition failed" in move
    assert "Assert-NoUntrustedDeleteChild `" in move
    assert "Assert-ForwardTrustedOwner" in move
    assert "Move-Item -LiteralPath" not in text

    for label in (
        "Legacy state hold",
        "Previous app archive",
        "Previous Python environment archive",
        "New app activation",
        "New Python environment activation",
        "Legacy state reattachment",
        "Rollback legacy state hold",
        "Rollback new app removal",
        "Rollback new Python environment removal",
        "Rollback previous app restore",
        "Rollback previous Python environment restore",
        "Rollback legacy state restore",
    ):
        assert f'-Label "{label}"' in text


def test_forward_upgrade_bootstrap_inputs_are_sealed_signed_and_locked() -> None:
    text = source()
    main = text[text.index("$CandidateResults =") :]

    root_guard = main.index("Protect-ForwardRoot -RunnerSid $RunnerSid")
    integrity_seal = main.index("Assert-ForwardAclSemantics -Path $IntegrityScript")
    integrity_hash = main.index("$ExpectedIntegrityScriptSha256")
    integrity_lock = main.index("$integrityLock = [IO.File]::Open")
    integrity_load = main.index(". $IntegrityScript")
    assert integrity_seal < integrity_hash < integrity_lock < integrity_load < root_guard
    assert "release integrity verifier is outside the protected pinned release directory" in main

    setting_seal = main.index("Protect-ForwardPrivateFile -Path $ServerFile")
    transaction = main.index("New-ForwardPrivateDirectory -Path $TransactionArchive")
    setting_copy = main.index("New-ForwardLockedSettingCopy", transaction)
    terminal_evidence = main.index("$TerminalExecutableEvidence = Get-TrustedExecutableEvidence")
    assert setting_seal < transaction < setting_copy < terminal_evidence
    assert '-ExpectedSha256 $ExpectedTerminalSha256' in main
    assert "independently audited pinned SHA-256" in text
    assert 'GetFolderPath([Environment+SpecialFolder]::ProgramFiles)' in main


def test_forward_parent_guard_ignores_unresolvable_read_only_aces() -> None:
    text = source()
    guard = text[text.index("function Assert-NoUntrustedDeleteChild") :]
    no_mutation = guard.index("-band $parentMutation) -eq 0")
    resolve_sid = guard.index("Get-ForwardAclRuleSid -Identity")
    assert no_mutation < resolve_sid


def test_forward_runner_proof_compares_canonical_principal_sid() -> None:
    text = source()
    proof = text[
        text.index("function Invoke-ForwardRunnerBrokerProof") :
        text.index("function Assert-TaskXmlRestored")
    ]
    assert "Resolve-ForwardIdentitySid -Identity ([string]$task.Principal.UserId)" in proof
    assert "[string]$task.Principal.UserId -ine $RunnerIdentity" not in proof
    assert "$terminalCleanupDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)" in proof
    assert "Start-Sleep -Milliseconds 250" in proof


def test_forward_upgrade_bootstrap_runtime_inputs_remain_locked() -> None:
    text = source()
    main = text[text.index("$CandidateResults =") :]
    assert "Get-Content -LiteralPath $ServerFile" not in main
    assert "Get-Content -LiteralPath $TerminalFile" not in main

    venv_seal = main.index("Protect-ForwardTree -Path $Venv -SealOwner")
    pyvenv_read = main.index("Get-Content -LiteralPath $pyvenvConfigPath")
    python_evidence = main.index("$BootstrapPythonEvidence = Get-TrustedExecutableEvidence")
    venv_build = main.index("& $BootstrapPython -I -S -E -B -m venv $VenvStaging")
    validation_init = main.index("--output-root $ValidationRoot init")
    close_locks = main.index("Close-SignedReleaseLocks -Locks $SignedReleaseLocks", validation_init)
    assert venv_seal < pyvenv_read < python_evidence < venv_build < validation_init < close_locks
    assert '-SignerSubjectPattern "Python Software Foundation"' in main
    assert "Bootstrap Python changed while creating the staged environment" in main
    assert "The trusted MetaTrader 5 terminal changed during validation" in main

    privilege = text[
        text.index("function New-ForwardRestorePrivilegeScope") : text.index(
            "function New-ForwardPrivateDirectory"
        )
    ]
    assert "EnableScoped" in privilege
    assert "previousState" in privilege
    assert "public void Dispose()" in privilege
    atomic = text[
        text.index("function New-ForwardPrivateDirectory") : text.index(
            "function Assert-ForwardAclSemantics"
        )
    ]
    assert "$restorePrivilege.Dispose()" in atomic


def test_forward_upgrade_seals_immutable_owners_and_can_recover_legacy_metadata() -> None:
    text = source()
    owner = text[
        text.index("function Assert-ForwardTrustedOwner") : text.index(
            "function Set-ForwardExactAcl"
        )
    ]
    critical = text[
        text.index("function Assert-ForwardCriticalLeafAcl") : text.index(
            "function Close-SignedReleaseLocks"
        )
    ]
    private_snapshot = text[
        text.index("function Assert-ForwardPrivateAclForSnapshot") : text.index(
            "function Assert-RelativeFileHashes"
        )
    ]

    assert '/setowner "*S-1-5-18" /T /C /Q' in owner
    assert 'ownerSid -cne "S-1-5-18"' in owner
    assert "Trusted-owner path contains a reparse point" in owner
    assert "[switch]$RequireTrustedOwner" in critical
    assert "Assert-ForwardTrustedOwner -Path $Path" in critical
    assert "Assert-ForwardTrustedOwner -Path $target" in private_snapshot
    assert text.count("-RequireTrustedOwner") == 5
    assert "-SealOwner" in text
    assert "$accessAcl.SetOwner(" in text
    assert "$accessAcl.SetGroup(" in text
    assert "$legacyStateDetached = $true" in text
    assert "Rollback-detached legacy campaign state" in text
    assert "still permit exact snapshot recovery" in text


def test_forward_upgrade_candidates_are_private_readonly_transaction_copies() -> None:
    text = source()
    candidate = text[
        text.index("function New-CandidateIfMissing") : text.index("$CandidateResults")
    ]
    private_file = text[
        text.index("function Protect-ForwardPrivateFile") : text.index(
            "function Assert-ForwardCriticalLeafAcl"
        )
    ]

    assert "Join-Path $TransactionArchive" in candidate
    assert "Copy-Item -LiteralPath $Source" not in candidate
    copy_function = text[
        text.index("function Copy-FileCreateNew") : text.index(
            "function New-CandidateIfMissing"
        )
    ]
    assert copy_function.index("Protect-ForwardPrivateFile -Path $Destination") < (
        copy_function.index("return [IO.File]::Open")
    )
    copy = candidate.index("Copy-FileCreateNew -Source $Source -Destination $temporary")
    close_lock = candidate.index("$candidateLock.Dispose()", copy)
    move = candidate.index("[IO.File]::Move($temporary, $Destination)", close_lock)
    protect_final = candidate.index("-Path $Destination `", move)
    final_hash = candidate.index("Candidate final hash mismatch", protect_final)
    assert copy < close_lock < move < protect_final < final_hash
    assert "[IO.FileAttributes]::ReadOnly" in private_file
    assert "Set-ForwardExactAcl `" in private_file
    assert "-ReadOnlySid $readOnlyRunnerSid" in private_file
    assert "private file is not read-only" in private_file
    assert "Set-ForwardTrustedOwner -Path $resolved" in private_file


def test_forward_upgrade_seals_runtime_and_keeps_legacy_private_until_rollover() -> None:
    text = source()

    assert "function Assert-ForwardCriticalLeafAcl" in text
    for leaf in (
        "$Python",
        "$Launcher",
        "$WatchdogLauncher",
        'Join-Path $App "scripts\\run_xm_mt5_forward.py"',
        'Join-Path $App "live_forward\\xm_mt5_demo_config.json"',
    ):
        assert leaf in text
    assert "[IO.FileShare]::Read" in text[
        text.index("function Assert-ForwardCriticalLeafAcl") : text.index(
            "function Close-SignedReleaseLocks"
        )
    ]

    assert "Get-ForwardAclSnapshot -Path $LegacyState" in text
    assert '"legacy-state.original-acl.json"' in text
    assert "Protect-ForwardTree `\n            -Path $LegacyState `" in text
    assert "Assert-ForwardPrivateAclForSnapshot" in text
    assert "Restore-ForwardAclSnapshot `" in text
    assert "Assert-ForwardAclSnapshot -Path $Path -Snapshot $Snapshot" in text
    protect_state = text.index("-Path $LegacyState `", text.index("$ExpectedDeployHashes"))
    reattach = text.index('-Label "Legacy state reattachment"')
    candidates = text.index("$candidateSpecs = @(", reattach)
    success_reattach = text[reattach:candidates]
    assert protect_state < reattach
    assert "Assert-ForwardPrivateAclForSnapshot `" in success_reattach
    assert "Restore-ForwardAclSnapshot `" not in success_reattach
    assert 'legacy_state_acl = if ($StateMode -eq "LEGACY")' in text
    catch = text[text.index("catch {") :]
    assert "Restore-ForwardAclSnapshot `" in catch


def test_forward_upgrade_legacy_acl_restore_avoids_sacl_privilege_dependency() -> None:
    text = source()
    snapshot = text[
        text.index("function Get-ForwardAclSnapshot") : text.index(
            "function Get-ForwardAclSnapshotPath"
        )
    ]
    restore = text[
        text.index("function Restore-ForwardAclSnapshot") : text.index(
            "function Assert-ForwardPrivateAclForSnapshot"
        )
    ]

    assert "owner_sid" in snapshot
    assert "group_sid" in snapshot
    assert "access_sddl" in snapshot
    assert "[Security.AccessControl.AccessControlSections]::Access" in snapshot
    assert "Set-Acl" not in restore
    assert "[IO.Directory]::SetAccessControl($target, $accessAcl)" in restore
    assert "[IO.File]::SetAccessControl($target, $accessAcl)" in restore
    assert "$accessAcl.SetOwner(" in restore
    assert "$accessAcl.SetGroup(" in restore
    assert "Assert-ForwardAclSnapshot -Path $Path -Snapshot $Snapshot" in restore


def test_forward_upgrade_rollback_restores_app_before_legacy_state() -> None:
    text = source()

    restore_app = text.index('-Label "Rollback previous app restore"')
    rollback_legacy_label = text.index('Label "Rolled-back legacy campaign state"')
    assert restore_app < rollback_legacy_label
    assert '-Label "Rollback previous Python environment restore"' in text
    assert "-not $OldVenvArchived" in text
    assert "-not $NewVenvActivated" in text
    assert "-not $TaskDefinitionsChanged" in text
    assert "$rollbackComplete" in text
    assert "App rollback completed." in text


def test_forward_upgrade_requires_a_proven_non_admin_runner_before_task_swap() -> None:
    text = source()
    main = text[text.index("$CandidateResults =") :]

    assert "Assert-ForwardNonAdminRunner" in text
    assert "GetAuthorizationGroups()" in text
    assert '"S-1-5-32-544"' in text
    assert "ForwardShadow target runner is transitively privileged" in text
    assert "Reset-ForwardRunnerPasswordInMemory" in text
    assert "RandomNumberGenerator]::Create()" in text
    assert "$user.SetPassword($plainPassword)" in text
    assert "[Array]::Clear($bytes, 0, $bytes.Length)" in text
    assert "Register-ForwardS4UTaskDefinition" in text
    assert "$Folder.RegisterTaskDefinition(" in text
    assert "ZeroFreeBSTR($passwordPointer)" in text
    assert "Set-ForwardMainTaskWithS4UCredential" in text
    param_block = text[: text.index("$ErrorActionPreference")]
    assert "TargetRunnerPassword" not in param_block

    state_create = main.index("New-ForwardPrivateDirectory -Path $CanonicalState")
    state_acl = main.index("Protect-ForwardStateTree -Path $CanonicalState", state_create)
    broker_proof = main.index(
        "$readOnlyBrokerProof = Invoke-ForwardRunnerBrokerProof", state_acl
    )
    production_proof = main.index(
        "$productionBrokerProof = Invoke-ForwardRunnerBrokerProof", broker_proof
    )
    task_swap = main.index("$TaskDefinitionsChanged = $true", production_proof)
    main_registration = main.index("Set-ForwardMainTaskWithS4UCredential", task_swap)
    assert state_create < state_acl < broker_proof < production_proof < task_swap
    assert task_swap < main_registration
    assert "-ExpectedLogonType \"S4U\"" in main[main_registration:]
    assert "-ExpectedRunLevel \"Limited\"" in main[main_registration:]


def test_forward_runner_logon_rights_are_exact_and_transactional() -> None:
    text = source()
    main = text[text.index("$CandidateResults =") :]
    catch = text.index("catch {", text.index('state = "UPGRADED_TASKS_STOPPED"'))

    for right in (
        "SeBatchLogonRight",
        "SeDenyInteractiveLogonRight",
        "SeDenyNetworkLogonRight",
        "SeDenyRemoteInteractiveLogonRight",
        "SeDenyServiceLogonRight",
    ):
        assert f'"{right}"' in text
    assert "LsaEnumerateAccountRights" in text
    assert "LsaRemoveAccountRights(policy, sid, true" in text
    assert "LsaAddAccountRights" in text

    snapshot = main.index("$RunnerOriginalAccountRights = @(")
    harden = main.index("Set-ForwardAccountRightsExact", snapshot)
    verify = main.index("Assert-ForwardRunnerLogonRights", harden)
    password_reset = main.index("Reset-ForwardRunnerPasswordInMemory", verify)
    assert snapshot < harden < verify < password_reset

    rollback = text[catch:]
    assert "-Rights @($RunnerOriginalAccountRights)" in rollback
    assert "runner account rights were not restored exactly" in rollback
    assert "-not $RunnerRightsHardened" in rollback


def test_forward_integrity_helper_pin_matches_packaged_helper() -> None:
    text = source()
    helper = ROOT / "deploy" / "release_integrity.ps1"
    actual = hashlib.sha256(helper.read_bytes()).hexdigest()

    assert f'$ExpectedIntegrityScriptSha256 = "{actual}"' in text


def test_forward_upgrade_migrates_root_and_state_acls_transactionally() -> None:
    text = source()
    main = text[text.index("$CandidateResults =") :]
    catch = text.index("catch {", text.index('state = "UPGRADED_TASKS_STOPPED"'))

    root_snapshot = main.index("$RootAclSnapshot = Get-ForwardRootAclSnapshot")
    root_protect = main.index("Protect-ForwardRoot -RunnerSid $RunnerSid")
    assert root_snapshot < root_protect
    assert '-FullControlSids @("S-1-5-18", "S-1-5-32-544")' in text[
        text.index("function Protect-ForwardRoot") : text.index(
            "function Assert-ForwardNonAdminRunner"
        )
    ]
    state = text[
        text.index("function Protect-ForwardStateTree") : text.index(
            "function Get-ForwardRootAclSnapshot"
        )
    ]
    assert "-ModifySid $RunnerSid" in state
    assert "Set-ForwardTrustedOwner -Path $resolved -Recurse" in state
    assert "Users" not in state
    assert "Restore-ForwardAclSnapshot `\n                    -Path $CanonicalState `" in text[catch:]
    assert "Restore-ForwardRootAclSnapshot -Snapshot $RootAclSnapshot" in text[catch:]
    assert text.index("Restore-ForwardRootAclSnapshot", catch) > text.index(
        "if ($TransactionArchive -and $rollbackComplete)", catch
    )


def test_forward_runner_probe_is_own_profile_no_send_and_cleans_up() -> None:
    text = source()
    launcher = launcher_source()
    proof = text[
        text.index("function Invoke-ForwardRunnerBrokerProof") : text.index(
            "function Assert-TaskXmlRestored"
        )
    ]

    assert "requires every matching MT5 terminal process to be stopped" in proof
    assert "$observedOwnedTerminal" in proof
    assert "GetOwnerSid" in text
    assert "left an MT5 terminal process behind" in proof
    assert '"FORWARD_SHADOW_RUNNER_TOKEN_SENTINEL_V1"' in launcher
    assert "[IO.FileAccess]::Write" in launcher
    assert "[IO.File]::Delete($RunnerTokenSentinel)" in launcher
    assert "[IO.FileAttributes]::ReadOnly" in launcher
    assert "mt5.initialize(path=terminal, timeout=30000)" in launcher
    assert "mt5.account_info()" in launcher
    assert "mt5.orders_get()" in launcher
    assert "mt5.positions_get()" in launcher
    assert "terminal_info.data_path" in launcher
    assert 'os.environ["APPDATA"]' in launcher
    assert "mt5.order_send" not in launcher
    assert "mt5.order_check" not in launcher


def test_forward_launcher_and_upgrade_isolate_python_and_windows_tools() -> None:
    text = source()
    launcher = launcher_source()

    assert '& $Python -I -E -B (Join-Path $Root "app\\scripts\\run_xm_mt5_forward.py")' in launcher
    assert '& $Python -I -E -B (Join-Path $Root "app\\scripts\\check_mt5_flat.py")' in launcher
    assert '--evidence-nonce ([string]$request.nonce)' in launcher
    assert '"broker-probe-request.json"' in launcher
    assert "& $RuntimePython -I -E -B -" in text
    assert "& $StagedPython -I -E -B -m compileall" in text
    assert "& $StagedPython -I -E -B $StagedXmRunnerPath" in text
    assert 'SetEnvironmentVariable("PSModulePath", $TrustedPSModulePath' in text
    assert '"ScheduledTasks\\ScheduledTasks.psd1"' in text
    assert '"Microsoft.PowerShell.Archive\\Microsoft.PowerShell.Archive.psd1"' in text
    assert "$IcaclsExe" in text
    assert "& icacls.exe" not in text
    assert "$WindowsPowerShellExe" in text
    assert '-Execute "powershell.exe"' not in text


def test_forward_upgrader_is_self_pinned_and_locked_from_protected_program_files() -> None:
    text = source()

    assert "[string]$ExpectedSelfSha256" in text
    assert '"C:\\Program Files\\OtoBacktestDeploy"' in text
    assert '"forward-shadow-$ExpectedSelfSha256"' in text
    assert '[IO.FileShare]::Read' in text
    assert "$selfSha.ComputeHash($SelfReadLock)" in text
    assert "$actualSelfSha256 -cne $ExpectedSelfSha256" in text
    assert "$security.AreAccessRulesProtected" in text
    assert 'Value -cne "S-1-5-18"' in text
    assert 'rules.Count -ne 2' in text
    assert "$SelfReadLock.Dispose()" in text


def test_forward_terminal_proofs_are_demo_only_read_only_then_production_restored() -> None:
    text = source()
    launcher = launcher_source()

    assert '"xm-readonly-password.dpapi"' in text
    assert "cannot migrate a user-scoped DPAPI broker password" in text
    assert '"AllowLiveTrading=$expectedSwitch"' in launcher
    assert "contains an undocumented API switch" in launcher
    assert "AllowLiveTrading=1" in launcher
    assert '"mt5-production.ini"' in launcher
    assert 'ACCOUNT_TRADE_MODE_DEMO' in launcher
    assert 'expected_mode == "ReadOnly"' in launcher
    assert 'expected_mode == "Production"' in launcher
    assert 'terminal_info.trade_allowed' in launcher
    assert 'terminal_info.tradeapi_disabled' in launcher
    assert 'bool(terminal_info.trade_allowed) or bool(terminal_info.tradeapi_disabled)' in launcher
    assert 'account_info.trade_allowed' in launcher
    assert 'account_info.trade_expert' in launcher
    assert "MT5 account does not permit automated demo trading" in launcher
    assert "mt5.order_send" not in launcher
    assert '-TerminalMode ReadOnly `' in text
    assert '-TerminalMode Production `' in text
    assert text.index("-TerminalMode ReadOnly `") < text.index("-TerminalMode Production `")
    assert "production_restore = $productionBrokerProof" in text
    assert "Api=0" not in launcher
    assert "Api=1" not in launcher
    assert "Api=0" not in text
    assert "Api=1" not in text


def test_forward_task_settings_trigger_and_sentinel_are_canonical_and_rollbackable() -> None:
    text = source()

    for setting in (
        "$definition.Settings.Enabled = $true",
        "$definition.Settings.AllowDemandStart = $true",
        "$definition.Settings.RunOnlyIfIdle = $false",
        "$definition.Settings.DisallowStartIfOnBatteries = $false",
        "$definition.Settings.StopIfGoingOnBatteries = $false",
        "$definition.Settings.RunOnlyIfNetworkAvailable = $false",
    ):
        assert setting in text
    assert "$definition.Triggers.Clear()" in text
    assert "$definition.Triggers.Create(8)" in text
    assert '"MSFT_TaskBootTrigger"' in text
    assert "$OriginalMainTriggers" in text
    assert "$OriginalWatchdogTriggers" in text
    assert "-Trigger $OriginalMainTriggers" in text
    assert "-Trigger $OriginalWatchdogTriggers" in text
    sentinel = text.rindex("-Path $RunnerTokenSentinel `")
    assert "-Protected `" in text[sentinel : sentinel + 250]


def test_forward_flat_and_rollover_use_sealed_runner_request_contract() -> None:
    launcher = launcher_source()
    check = flat_check_source()
    rollover = rollover_source()

    assert '--read-only-proof' in launcher
    assert '"terminal-readonly.ini"' in launcher
    assert 'terminal_config_sha256' in launcher
    assert '[IO.FileMode]::CreateNew' in check
    assert '[IO.FileShare]::Read' in check
    assert 'Protect-ForwardPrivateTree -Path $ProbeTransaction' in check
    assert '"Running", "Queued"' in check
    assert 'Get-ForwardRootPythonProcesses' in check
    assert 'Get-ForwardRunnerProcesses' in check
    assert '-MethodName GetOwner ' in check
    assert '-MethodName GetOwnerSid' not in check
    assert '-AllowUnresolved' in check
    assert 'return $matches.ToArray()' in check
    assert '[string]$actions[0].Arguments -cne $ExpectedArguments' in check
    assert 'READ_ONLY_PROOF' in check
    assert 'DISABLED_FOR_READ_ONLY_PROOF' in check
    assert "python_trade_api_enabled" in check
    assert "python_trade_api_disabled" not in check

    deploy_new = rollover.index("Copy-ForwardFileCreateNew")
    fresh_probe = rollover.index("$readinessOutput = @(& $WindowsPowerShellExe", deploy_new)
    next_state = rollover.index("[void][IO.Directory]::CreateDirectory($StateNext)", fresh_probe)
    state_swap = rollover.index("[IO.Directory]::Move($StateNext, $State)", next_state)
    assert deploy_new < fresh_probe < next_state < state_swap
    assert "-KeepStopped `" in rollover[fresh_probe:next_state]
    assert "[IO.File]::Move(" in rollover
    assert "[IO.Directory]::Move(" in rollover
    assert "Move-Item -LiteralPath" not in rollover
    assert "& $Python -I -E -B $XmTarget" in rollover
    assert "Assert-ForwardTaskXmlUnchanged" in rollover
    assert '[string]$mainTaskDefinition.Actions[0].Arguments -notlike' not in rollover
    for script in (check, rollover):
        assert "Assert-ForwardCanonicalTaskPair" in script
    for canonical_gate in (
        "RestartCount -ne 3",
        "AllowDemandStart -ne $true",
        "RunOnlyIfIdle -ne $false",
        "DisallowStartIfOnBatteries -ne $false",
        "RunOnlyIfNetworkAvailable -ne $false",
        'CimClassName -cne "MSFT_TaskBootTrigger"',
        'ExpectedLogonType "ServiceAccount"',
        'ExpectedRunLevel "Highest"',
    ):
        assert canonical_gate in check
