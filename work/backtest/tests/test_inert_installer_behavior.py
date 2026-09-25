from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from powershell_contract import facts, powershell_harness


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
EXPECTED_ARTIFACT_TESTS = [
    "test_deployment_security.py",
    "test_xm_mt5_forward.py",
    "test_super1_xm_forward.py",
    "test_check_mt5_flat.py",
    "test_v16_deployment_contract.py",
]


def _record_bytes(path: str, payload: bytes) -> tuple[str, str, str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
    return path, f"sha256={digest}", str(len(payload))


def _wheel(path: Path, dist: str, version: str, package_files: dict[str, bytes]) -> bytes:
    dist_info = f"{dist.replace('-', '_')}-{version}.dist-info"
    entries = dict(package_files)
    entries[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n\n".encode()
    )
    entries[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: inert-installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    rows = [_record_bytes(name, payload) for name, payload in entries.items()]
    rows.append((f"{dist_info}/RECORD", "", ""))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    entries[f"{dist_info}/RECORD"] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path.read_bytes()


def _fixture_backend() -> bytes:
    return b'''from pathlib import Path\nimport base64, csv, hashlib, io, zipfile\n\ndef build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n    filename = "super1_install_fixture-0.0.1-py3-none-any.whl"\n    dist = "super1_install_fixture-0.0.1.dist-info"\n    entries = {\n        "super1_install_fixture.py": Path("super1_install_fixture.py").read_bytes(),\n        f"{dist}/METADATA": b"Metadata-Version: 2.1\\nName: super1-install-fixture\\nVersion: 0.0.1\\n\\n",\n        f"{dist}/WHEEL": b"Wheel-Version: 1.0\\nGenerator: inert-installer-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",\n    }\n    rows = []\n    for name, payload in entries.items():\n        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")\n        rows.append((name, "sha256=" + digest, str(len(payload))))\n    rows.append((f"{dist}/RECORD", "", ""))\n    out = io.StringIO(newline="")\n    csv.writer(out, lineterminator="\\n").writerows(rows)\n    entries[f"{dist}/RECORD"] = out.getvalue().encode()\n    destination = Path(wheel_directory) / filename\n    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as wheel:\n        for name, payload in entries.items():\n            wheel.writestr(name, payload)\n    return filename\n'''


def _synthetic_release(tmp_path: Path) -> tuple[Path, Path, str]:
    release = tmp_path / "release"
    release.mkdir()
    wheelhouse = release / "wheelhouse"
    wheelhouse.mkdir()
    wheel_path = wheelhouse / "fixture_dep-1.0-py3-none-any.whl"
    wheel_bytes = _wheel(
        wheel_path,
        "fixture-dep",
        "1.0",
        {"fixture_dep.py": b"VALUE = 'installed from local wheelhouse'\n"},
    )
    wheel_hash = hashlib.sha256(wheel_bytes).hexdigest()
    (release / "requirements-windows.lock").write_text(
        f"fixture-dep==1.0 --hash=sha256:{wheel_hash}\n", encoding="utf-8"
    )
    (release / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "fixture_backend"\nbackend-path = ["."]\n',
        encoding="utf-8",
    )
    (release / "fixture_backend.py").write_bytes(_fixture_backend())
    (release / "super1_install_fixture.py").write_text("INSTALLED = True\n", encoding="utf-8")
    archive_path = release / "super1-forward.zip"
    package_files = [
        release / "requirements-windows.lock",
        release / "pyproject.toml",
        release / "fixture_backend.py",
        release / "super1_install_fixture.py",
        wheel_path,
    ]
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in package_files:
            archive.write(path, path.relative_to(release).as_posix())

    records = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if not info.is_dir():
                payload = archive.read(info.filename)
                records.append({"path": info.filename, "sha256": hashlib.sha256(payload).hexdigest()})

    risk_files = [
        {
            "path": f"data/raw/nq/DUKASCOPY_FIXTURE_{index:03d}.csv",
            "sha256": hashlib.sha256(f"risk-{index}".encode()).hexdigest(),
        }
        for index in range(144)
    ]
    risk_set = "".join(f"{item['path']} {item['sha256'].lower()}\n" for item in sorted(risk_files, key=lambda x: x["path"]))
    engine_files = [
        {"path": "nq/DUKASCOPY_AUDIT.csv", "sha256": hashlib.sha256(b"nq").hexdigest(), "bytes": 2},
        {"path": "spx/DUKASCOPY_AUDIT.csv", "sha256": hashlib.sha256(b"spx").hexdigest(), "bytes": 3},
    ]
    engine_set = "".join(f"{item['path']} {item['sha256'].lower()}\n" for item in sorted(engine_files, key=lambda x: x["path"]))
    python_hash = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "release_id": "synthetic-installer-behavior-test",
        "profile": "super1",
        "archive_file": archive_path.name,
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "created_at_utc": "2026-09-25T00:00:00Z",
        "git_commit": "1" * 40,
        "git_dirty": False,
        "python_version": "3.11.9",
        "python_executable_sha256": python_hash,
        "pytest_passed": True,
        "pytest_collected_count": 1,
        "pytest_pass_count": 1,
        "pytest_skipped_count": 0,
        "pytest_nodeid_sha256": "2" * 64,
        "artifact_pytest_passed": True,
        "artifact_pytest_collected_count": 1,
        "artifact_pytest_pass_count": 1,
        "artifact_pytest_skipped_count": 0,
        "artifact_pytest_nodeid_sha256": "3" * 64,
        "artifact_test_files": EXPECTED_ARTIFACT_TESTS,
        "test_inputs": {
            "risk_manifest_path": "data/provenance/first30_pre2025_inputs.sha256",
            "risk_manifest_sha256": "4" * 64,
            "risk_set_sha256": hashlib.sha256(risk_set.encode()).hexdigest(),
            "risk_file_count": 144,
            "risk_files": risk_files,
            "engine_audit_manifest_sha256": "5" * 64,
            "engine_audit_set_sha256": hashlib.sha256(engine_set.encode()).hexdigest(),
            "engine_audit_csv_count": len(engine_files),
            "engine_audit_files": engine_files,
        },
        "files": records,
    }
    manifest_path = release / "super1-forward.manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")

    private_xml_path = tmp_path / "synthetic-private.xml"
    public_xml_path = tmp_path / "synthetic-public.xml"
    keygen = powershell_harness(
        '$rsa = New-Object Security.Cryptography.RSACryptoServiceProvider -ArgumentList 2048; '
        '[IO.File]::WriteAllText($args[0], $rsa.ToXmlString($true), (New-Object Text.UTF8Encoding($false))); '
        '[IO.File]::WriteAllText($args[1], $rsa.ToXmlString($false), (New-Object Text.UTF8Encoding($false))); '
        '$rsa.Dispose()',
        str(private_xml_path),
        str(public_xml_path),
    )
    assert keygen.returncode == 0, keygen.stderr
    public_xml = public_xml_path.read_text(encoding="utf-8")
    helper_text = (DEPLOY / "release_integrity.ps1").read_text(encoding="utf-8")
    helper_text, xml_replacements = re.subn(
        r"(?m)^\$script:ReleasePublicKeyXml = '.*'$",
        "$script:ReleasePublicKeyXml = '" + public_xml + "'",
        helper_text,
    )
    helper_hash = hashlib.sha256(public_xml.encode("utf-8")).hexdigest()
    helper_text, hash_replacements = re.subn(
        r'(?m)^\$script:ReleasePublicKeySha256 = "[a-f0-9]{64}"$',
        f'$script:ReleasePublicKeySha256 = "{helper_hash}"',
        helper_text,
    )
    assert (xml_replacements, hash_replacements) == (1, 1)
    test_helper = tmp_path / "release_integrity_synthetic_test.ps1"
    test_helper.write_text(helper_text, encoding="utf-8")

    signed = powershell_harness(
        '$rsa = New-Object Security.Cryptography.RSACryptoServiceProvider; '
        '$rsa.FromXmlString([IO.File]::ReadAllText($args[0])); '
        '$bytes = [IO.File]::ReadAllBytes($args[1]); '
        '$sig = $rsa.SignData($bytes, [Security.Cryptography.CryptoConfig]::MapNameToOID("SHA256")); '
        '[IO.File]::WriteAllText($args[2], [Convert]::ToBase64String($sig) + "`n", (New-Object Text.UTF8Encoding($false))); '
        '$rsa.Dispose()',
        str(private_xml_path),
        str(manifest_path),
        str(release / "super1-forward.manifest.sig"),
    )
    assert signed.returncode == 0, signed.stderr
    return release, test_helper, manifest["archive_sha256"]


def _validate(test_helper: Path, archive: Path) -> subprocess.CompletedProcess[str]:
    return powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); '
        + '. $args[0]; $null = Assert-SignedReleaseArchive -Archive $args[1] -ExpectedProfile "super1" -RequireProvenance; "VALID"',
        str(test_helper),
        str(archive),
    )


def test_inert_installer_rejects_bad_signature(tmp_path: Path) -> None:
    release, helper, _ = _synthetic_release(tmp_path)
    signature = release / "super1-forward.manifest.sig"
    signature.write_text("AAAA\n", encoding="ascii")
    result = _validate(helper, release / "super1-forward.zip")
    assert result.returncode != 0
    assert "Release manifest signature validation failed" in result.stderr


def test_inert_installer_rejects_archive_hash_change(tmp_path: Path) -> None:
    release, helper, _ = _synthetic_release(tmp_path)
    archive = release / "super1-forward.zip"
    archive.write_bytes(archive.read_bytes() + b"post-signature mutation")
    result = _validate(helper, archive)
    assert result.returncode != 0
    assert "Release archive SHA-256 validation failed" in result.stderr


def test_inert_installer_blocks_file_mutation_after_validation(tmp_path: Path) -> None:
    release, helper, _ = _synthetic_release(tmp_path)
    installer = DEPLOY / "install_super1_app_inert_windows.ps1"
    lock_function = next(item["extent_text"] for item in facts(installer, "function") if item["name"] == "Open-InertReleaseInputLocks")
    archive = release / "super1-forward.zip"
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); . $args[0];\n'
        + lock_function
        + '\n$locks = Open-InertReleaseInputLocks -Paths @($args[1], $args[2], $args[3], $args[4]); '
        + '$null = Assert-SignedReleaseArchive -Archive $args[1] -ExpectedProfile "super1" -RequireProvenance; '
        + '$blocked = $false; try { $s = [IO.File]::Open($args[1], [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None); $s.Dispose() } '
        + 'catch [System.IO.IOException] { $blocked = $true } catch [System.UnauthorizedAccessException] { $blocked = $true }; '
        + 'foreach ($lock in $locks) { $lock.stream.Dispose() }; if (-not $blocked) { throw "POST_VALIDATION_WRITE_WAS_NOT_BLOCKED" }; "WRITE_BLOCKED"',
        str(helper),
        str(archive),
        str(release / "super1-forward.manifest.json"),
        str(release / "super1-forward.manifest.sig"),
        sys.executable,
    )
    assert result.returncode == 0, result.stderr
    assert "WRITE_BLOCKED" in result.stdout
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before


def test_inert_installer_installs_from_hash_locked_offline_wheelhouse(tmp_path: Path) -> None:
    release, helper, _ = _synthetic_release(tmp_path)
    installer = DEPLOY / "install_super1_app_inert_windows.ps1"
    extracted_check = next(
        item["extent_text"] for item in facts(installer, "function")
        if item["name"] == "Assert-ExtractedReleaseMatchesManifest"
    )
    app = tmp_path / "app"
    app.mkdir()
    with zipfile.ZipFile(release / "super1-forward.zip") as archive:
        archive.extractall(app)
    venv = tmp_path / "venv"
    created = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); . $args[0];\n'
        + extracted_check
        + '\n$manifest = Assert-SignedReleaseArchive -Archive $args[3] -ExpectedProfile "super1" -RequireProvenance; '
        + 'Assert-ExtractedReleaseMatchesManifest -Root $args[2] -Manifest $manifest; '
        + 'Install-LockedRelease -Python $args[1] -App $args[2]; '
        + 'Assert-ExtractedReleaseMatchesManifest -Root $args[2] -Manifest $manifest -AllowBuildArtifacts; '
        + '& $args[1] -I -E -B -c "import fixture_dep, super1_install_fixture; assert fixture_dep.VALUE.startswith(\'installed from local\'); assert super1_install_fixture.INSTALLED"; '
        + 'if ($LASTEXITCODE -ne 0) { throw "OFFLINE_WHEELHOUSE_INSTALL_VERIFY_FAILED" }; "WHEELHOUSE_INSTALL_OK"',
        str(helper),
        str(venv_python),
        str(app),
        str(release / "super1-forward.zip"),
    )
    assert result.returncode == 0, result.stderr
    assert "WHEELHOUSE_INSTALL_OK" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="The rollback behavior asserts Windows ACL restoration.")
def test_inert_installer_failed_install_return_quarantines_only_owned_paths(tmp_path: Path) -> None:
    installer = DEPLOY / "install_super1_app_inert_windows.ps1"
    names = {"Assert-NotReparsePoint", "Restore-InertTargetAcl", "Move-InertInstallFailureArtifacts"}
    functions = [item["extent_text"] for item in facts(installer, "function") if item["name"] in names]
    assert len(functions) == len(names)
    root = tmp_path / "target"
    root.mkdir()
    for name in ("app", ".app-stage-test", "venv311"):
        (root / name).mkdir()
        (root / name / "owned.txt").write_text(name, encoding="utf-8")
    for name in ("super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"):
        (root / name).write_text(name, encoding="utf-8")
    owned = [str(root / name) for name in ("app", ".app-stage-test", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig")]
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); '
        + 'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Security"); '
        + "\n".join(functions)
        + '\n$owned = @(); for ($i = 1; $i -lt $args.Count; $i++) { $owned += $args[$i] }; '
        + '$sddl = (Get-Acl -LiteralPath $args[0]).Sddl; '
        + '$preserved = Move-InertInstallFailureArtifacts -Root $args[0] -OwnedPaths $owned -OriginalSddl $sddl; '
        + 'Write-Output ("QUARANTINE=" + $preserved)',
        str(root),
        *owned,
    )
    assert result.returncode == 0, result.stderr
    match = re.search(r"QUARANTINE=(.+)", result.stdout)
    assert match, result.stdout
    quarantine = Path(match.group(1).strip())
    assert list(root.iterdir()) == []
    assert {item.name for item in quarantine.iterdir()} == {Path(path).name for path in owned}


def test_release_builder_binds_actual_engine_audit_set_and_rejects_later_change(tmp_path: Path) -> None:
    builder = DEPLOY / "build_signed_windows_release.ps1"
    required = {"Get-ByteSha256", "Get-EngineAuditInputSet", "Assert-EngineAuditInputSetUnchanged"}
    functions = [item["extent_text"] for item in facts(builder, "function") if item["name"] in required]
    assert len(functions) == len(required)
    root = tmp_path / "engine-audit"
    (root / "nq").mkdir(parents=True)
    (root / "spx").mkdir()
    records = []
    for relative, payload in (
        ("nq/DUKASCOPY_2025-01-01.csv", b"nq-1"),
        ("nq/DUKASCOPY_2025-01-02.csv", b"nq-2"),
        ("spx/DUKASCOPY_2025-01-01.csv", b"spx-1"),
    ):
        path = root / relative
        path.write_bytes(payload)
        records.append({"path": relative, "sha256": hashlib.sha256(payload).hexdigest()})
    (root / "engine_audit_manifest.json").write_text(
        json.dumps(
            {
                "schema": "engine-audit-inputs-v1",
                "datasets": [
                    {"leg_key": leg, "files": [row for row in records if row["path"].startswith(leg + "/")]}
                    for leg in ("nq", "spx")
                ],
            }
        ),
        encoding="utf-8",
    )
    changed = root / "nq" / "DUKASCOPY_2025-01-01.csv"
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); '
        + "\n".join(functions)
        + '\n$expected = Get-EngineAuditInputSet -SourceRoot $args[0]; '
        + 'if ($expected.count -ne 3) { throw "ENGINE_AUDIT_COUNT_WAS_HARDCODED" }; '
        + '[IO.File]::WriteAllText($args[1], "changed"); '
        + 'try { Assert-EngineAuditInputSetUnchanged -SourceRoot $args[0] -Expected $expected; throw "ENGINE_AUDIT_CHANGE_WAS_NOT_REJECTED" } '
        + 'catch { if ($_.Exception.Message -notmatch "ENGINE_AUDIT_INPUT_(?:HASH_MISMATCH|SET_CHANGED)") { throw }; "ENGINE_AUDIT_CHANGE_REJECTED" }',
        str(root),
        str(changed),
    )
    assert result.returncode == 0, result.stderr
    assert "ENGINE_AUDIT_CHANGE_REJECTED" in result.stdout


def test_release_builder_rejects_changed_pinned_risk_input(tmp_path: Path) -> None:
    builder = DEPLOY / "build_signed_windows_release.ps1"
    required = {"Get-ByteSha256", "Get-PinnedRiskInputSet", "Assert-PinnedRiskInputSetUnchanged"}
    functions = [item["extent_text"] for item in facts(builder, "function") if item["name"] in required]
    assert len(functions) == len(required)
    root = tmp_path / "risk-inputs"
    nq = root / "data" / "raw" / "nq"
    spx = root / "data" / "raw" / "spx"
    nq.mkdir(parents=True)
    spx.mkdir()
    manifest_lines = []
    paths = []
    for index in range(144):
        leg = "nq" if index < 72 else "spx"
        relative = f"data/raw/{leg}/DUKASCOPY_FIXTURE_{index:03d}.csv"
        payload = f"fixture-{index}".encode()
        (root / relative).write_bytes(payload)
        manifest_lines.append(f"{hashlib.sha256(payload).hexdigest()} *{relative}")
        paths.append(root / relative)
    manifest = root / "first30_pre2025_inputs.sha256"
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); '
        + "\n".join(functions)
        + '\n$expected = Get-PinnedRiskInputSet -InputRoot $args[0] -ManifestPath $args[1]; '
        + '[IO.File]::WriteAllText($args[2], "changed after provenance preparation"); '
        + 'try { Assert-PinnedRiskInputSetUnchanged -InputRoot $args[0] -ManifestPath $args[1] -Expected $expected; throw "RISK_INPUT_CHANGE_WAS_NOT_REJECTED" } '
        + 'catch { if ($_.Exception.Message -notmatch "PINNED_RISK_INPUT_HASH_MISMATCH|PINNED_RISK_INPUT_SET_CHANGED") { throw }; "RISK_INPUT_CHANGE_REJECTED" }',
        str(root),
        str(manifest),
        str(paths[0]),
    )
    assert result.returncode == 0, result.stderr
    assert "RISK_INPUT_CHANGE_REJECTED" in result.stdout


def test_release_builder_holds_candidate_and_audit_provenance_files_read_locked(tmp_path: Path) -> None:
    builder = DEPLOY / "build_signed_windows_release.ps1"
    lock_function = next(item["extent_text"] for item in facts(builder, "function") if item["name"] == "Open-ProvenanceInputLocks")
    source = tmp_path / "source.csv"
    source.write_text("pinned provenance bytes", encoding="utf-8")
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); '
        + lock_function
        + '\n$locks = Open-ProvenanceInputLocks -Paths @($args[0]); '
        + '$read = [IO.File]::ReadAllText($args[0]); if ($read -ne "pinned provenance bytes") { throw "READ_LOCK_BLOCKED_READ" }; '
        + '$blocked = $false; try { $writer = [IO.File]::Open($args[0], [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None); $writer.Dispose() } '
        + 'catch [System.IO.IOException] { $blocked = $true } catch [System.UnauthorizedAccessException] { $blocked = $true }; '
        + 'foreach ($lock in $locks) { $lock.stream.Dispose() }; '
        + '$writer = [IO.File]::Open($args[0], [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None); $writer.Dispose(); '
        + 'if (-not $blocked) { throw "PROVENANCE_WRITE_WAS_NOT_BLOCKED" }; "PROVENANCE_READ_LOCK_OK"',
        str(source),
    )
    assert result.returncode == 0, result.stderr
    assert "PROVENANCE_READ_LOCK_OK" in result.stdout


def test_super1_candidate_provenance_is_checked_against_source_and_archive(tmp_path: Path) -> None:
    builder = DEPLOY / "build_signed_windows_release.ps1"
    required = {
        "Get-ByteSha256",
        "Get-Super1CandidateProvenanceSet",
        "Assert-Super1CandidateProvenanceUnchanged",
        "Assert-Super1CandidateProvenanceArchive",
    }
    functions = [item["extent_text"] for item in facts(builder, "function") if item["name"] in required]
    assert len(functions) == len(required)
    source = tmp_path / "repo"
    candidate = source / "research_candidates" / "v20_strategy_loop" / "nq_spx_local_fresh_forward_candidate_v1.json"
    evidence = source / "outputs" / "audit.csv"
    candidate.parent.mkdir(parents=True)
    evidence.parent.mkdir(parents=True)
    evidence_bytes = b"immutable audit evidence\n"
    evidence.write_bytes(evidence_bytes)
    candidate.write_text(
        json.dumps({"provenance": {"inputs": [{
            "path": "outputs/audit.csv",
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "bytes": len(evidence_bytes),
        }]}}),
        encoding="utf-8",
    )
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); '
        + "\n".join(functions)
        + '\n$set = Get-Super1CandidateProvenanceSet -SourceRoot $args[0]; '
        + 'Assert-Super1CandidateProvenanceUnchanged -SourceRoot $args[0] -Expected $set; '
        + '$archiveFiles = @(@{ path = "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json"; sha256 = $set.candidate_sha256 }) + @($set.files); '
        + 'Assert-Super1CandidateProvenanceArchive -ArchiveFiles $archiveFiles -InputSet $set; '
        + '$badArchiveFiles = @(@{ path = "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json"; sha256 = $set.candidate_sha256 }, @{ path = $set.files[0].path; sha256 = ("0" * 64) }); '
        + 'try { Assert-Super1CandidateProvenanceArchive -ArchiveFiles $badArchiveFiles -InputSet $set; throw "ARCHIVE_PROVENANCE_CHANGE_WAS_NOT_REJECTED" } '
        + 'catch { if ($_.Exception.Message -notmatch "SUPER1_CANDIDATE_PROVENANCE_INPUT_NOT_BOUND_TO_ARCHIVE") { throw }; "ARCHIVE_PROVENANCE_CHANGE_REJECTED" }; '
        + '[IO.File]::WriteAllText((Join-Path $args[0] "outputs/audit.csv"), "changed"); '
        + 'try { Assert-Super1CandidateProvenanceUnchanged -SourceRoot $args[0] -Expected $set; throw "PROVENANCE_CHANGE_WAS_NOT_REJECTED" } '
        + 'catch { if ($_.Exception.Message -notmatch "SUPER1_CANDIDATE_PROVENANCE_HASH_OR_SIZE_MISMATCH|SUPER1_CANDIDATE_PROVENANCE_CHANGED_DURING_RELEASE_BUILD") { throw }; "PROVENANCE_CHANGE_REJECTED" }',
        str(source),
    )
    assert result.returncode == 0, result.stderr
    assert "PROVENANCE_CHANGE_REJECTED" in result.stdout


def test_inert_upgrade_rollback_moves_the_previous_signed_bundle_as_one_unit(tmp_path: Path) -> None:
    manager = DEPLOY / "manage_super1_app_inert_windows.ps1"
    names = {"Move-InertBundleTo", "Restore-InertBundleFrom"}
    functions = [item["extent_text"] for item in facts(manager, "function") if item["name"] in names]
    assert len(functions) == len(names)
    root = tmp_path / "active"
    root.mkdir()
    for name in ("app", "venv311"):
        (root / name).mkdir()
        (root / name / "release.txt").write_text(f"previous-{name}", encoding="utf-8")
    for name in ("super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"):
        (root / name).write_text(f"previous-{name}", encoding="utf-8")
    previous = tmp_path / "history" / "txn" / "previous"
    restored = tmp_path / "restored"
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Management"); '
        + "\n".join(functions)
        + '\nMove-InertBundleTo -SourceRoot $args[0] -DestinationRoot $args[1]; '
        + 'New-Item -ItemType Directory -Path $args[2] | Out-Null; '
        + 'Restore-InertBundleFrom -SourceRoot $args[1] -TargetRoot $args[2]; "BUNDLE_RESTORED"',
        str(root),
        str(previous),
        str(restored),
    )
    assert result.returncode == 0, result.stderr
    assert "BUNDLE_RESTORED" in result.stdout
    assert list(root.iterdir()) == []
    assert {item.name for item in restored.iterdir()} == {
        "app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"
    }
    assert (restored / "app" / "release.txt").read_text(encoding="utf-8") == "previous-app"


def test_inert_upgrade_partial_bundle_move_restores_already_moved_components(tmp_path: Path) -> None:
    manager = DEPLOY / "manage_super1_app_inert_windows.ps1"
    names = {"Move-InertBundleTo", "Restore-InertBundleFrom"}
    functions = [item["extent_text"] for item in facts(manager, "function") if item["name"] in names]
    assert len(functions) == len(names)
    source = tmp_path / "active"
    destination = tmp_path / "history"
    source.mkdir()
    destination.mkdir()
    for name in ("app", "venv311"):
        (source / name).mkdir()
        (source / name / "release.txt").write_text(f"current-{name}", encoding="utf-8")
    for name in ("super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"):
        (source / name).write_text(f"current-{name}", encoding="utf-8")
    (destination / "super1-forward.zip").write_text("preexisting destination collision", encoding="utf-8")
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Management"); '
        + "\n".join(functions)
        + '\ntry { Move-InertBundleTo -SourceRoot $args[0] -DestinationRoot $args[1]; throw "PARTIAL_MOVE_FAILURE_NOT_INJECTED" } '
        + 'catch { if ($_.Exception.Message -notmatch "INERT_BUNDLE_DESTINATION_EXISTS") { throw }; "PARTIAL_MOVE_INJECTED" }; '
        + 'Restore-InertBundleFrom -SourceRoot $args[1] -TargetRoot $args[0] -PreserveExistingTargetPaths; "PARTIAL_MOVE_RECOVERED"',
        str(source),
        str(destination),
    )
    assert result.returncode == 0, result.stderr
    assert "PARTIAL_MOVE_RECOVERED" in result.stdout
    assert {item.name for item in source.iterdir()} == {
        "app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"
    }
    assert (source / "app" / "release.txt").read_text(encoding="utf-8") == "current-app"
    assert (destination / "super1-forward.zip").read_text(encoding="utf-8") == "preexisting destination collision"


def test_inert_recovery_resumes_interrupted_previous_bundle_move(tmp_path: Path) -> None:
    manager = DEPLOY / "manage_super1_app_inert_windows.ps1"
    required = {"Assert-PathNotReparse", "Move-InertBundleTo", "Restore-InertBundleFrom", "Write-InertTransactionRecord", "Invoke-InertAppRecovery"}
    functions = [item["extent_text"] for item in facts(manager, "function") if item["name"] in required]
    assert len(functions) == len(required)
    target = tmp_path / "target"
    transaction = target / "history" / ("a" * 32)
    previous = transaction / "previous"
    target.mkdir()
    previous.mkdir(parents=True)
    for name in ("app", "venv311"):
        (target / name).mkdir()
        (target / name / "release.txt").write_text(f"old-{name}", encoding="utf-8")
        (target / name).rename(previous / name)
    for name in ("super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"):
        (target / name).write_text(f"old-{name}", encoding="utf-8")
    (transaction / "transaction.json").write_text(json.dumps({
        "schema": "super1-inert-upgrade-v1",
        "transaction_id": "a" * 32,
        "state": "UPGRADE_MOVING_PREVIOUS",
        "previous_release_id": "old-release",
        "previous_archive_sha256": "a" * 64,
        "release_id": "new-release",
        "archive_sha256": "b" * 64,
        "failure": None,
        "task_or_watchdog_created": False,
        "terminal_or_bot_started": False,
        "deployment_ready": False,
    }), encoding="utf-8")
    harness_assert = '''function Assert-InertBundle {
    param([string]$Root, [string]$ExpectedReleaseId, [string]$ExpectedArchiveSha256)
    foreach ($name in @("app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $name))) { throw "BUNDLE_COMPONENT_MISSING: $name" }
    }
    return [pscustomobject]@{ manifest = [pscustomobject]@{ release_id = $ExpectedReleaseId }; archive_sha256 = $ExpectedArchiveSha256 }
}'''
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Management"); '
        + harness_assert
        + "\n".join(functions)
        + '\n$TargetRoot = $args[0]; $null = Invoke-InertAppRecovery -TransactionId ("a" * 32); '
        + '$record = Get-Content -LiteralPath (Join-Path $args[0] ("history/" + ("a" * 32) + "/transaction.json")) -Raw | ConvertFrom-Json; '
        + 'if ($record.state -cne "FAILED_ROLLED_BACK") { throw "UPGRADE_CRASH_RECOVERY_PHASE_WRONG" }; "UPGRADE_CRASH_RECOVERED"',
        str(target),
    )
    assert result.returncode == 0, result.stderr
    assert "UPGRADE_CRASH_RECOVERED" in result.stdout
    assert {item.name for item in target.iterdir()} == {
        "app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig", "history"
    }
    assert (target / "app" / "release.txt").read_text(encoding="utf-8") == "old-app"


def test_inert_recovery_resumes_interrupted_rollback_and_preserves_both_bundles(tmp_path: Path) -> None:
    manager = DEPLOY / "manage_super1_app_inert_windows.ps1"
    required = {"Assert-PathNotReparse", "Move-InertBundleTo", "Restore-InertBundleFrom", "Write-InertTransactionRecord", "Invoke-InertAppRecovery"}
    functions = [item["extent_text"] for item in facts(manager, "function") if item["name"] in required]
    assert len(functions) == len(required)
    target = tmp_path / "target"
    transaction = target / "history" / ("b" * 32)
    previous = transaction / "previous"
    rolled_back = transaction / "rolled-back-current"
    failed_target = transaction / "failed-rollback-target"
    for root in (target, previous, rolled_back):
        root.mkdir(parents=True)
    for name in ("app", "venv311"):
        for root, label in ((target, "old"), (rolled_back, "new")):
            (root / name).mkdir()
            (root / name / "release.txt").write_text(f"{label}-{name}", encoding="utf-8")
    # This models a crash after the old app and venv were moved back, before sidecars.
    for name in ("super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"):
        (previous / name).write_text(f"old-{name}", encoding="utf-8")
        (rolled_back / name).write_text(f"new-{name}", encoding="utf-8")
    (transaction / "transaction.json").write_text(json.dumps({
        "schema": "super1-inert-upgrade-v1",
        "transaction_id": "b" * 32,
        "state": "ROLLBACK_RESTORING_PREVIOUS",
        "previous_release_id": "old-release",
        "previous_archive_sha256": "a" * 64,
        "release_id": "new-release",
        "archive_sha256": "b" * 64,
        "failure": None,
        "task_or_watchdog_created": False,
        "terminal_or_bot_started": False,
        "deployment_ready": False,
    }), encoding="utf-8")
    harness_assert = '''function Assert-InertBundle {
    param([string]$Root, [string]$ExpectedReleaseId, [string]$ExpectedArchiveSha256)
    foreach ($name in @("app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $name))) { throw "BUNDLE_COMPONENT_MISSING: $name" }
    }
    return [pscustomobject]@{ manifest = [pscustomobject]@{ release_id = $ExpectedReleaseId }; archive_sha256 = $ExpectedArchiveSha256 }
}'''
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Management"); '
        + harness_assert
        + "\n".join(functions)
        + '\n$TargetRoot = $args[0]; $null = Invoke-InertAppRecovery -TransactionId ("b" * 32); '
        + '$record = Get-Content -LiteralPath (Join-Path $args[0] ("history/" + ("b" * 32) + "/transaction.json")) -Raw | ConvertFrom-Json; '
        + 'if ($record.state -cne "UPGRADED") { throw "ROLLBACK_CRASH_RECOVERY_PHASE_WRONG" }; "ROLLBACK_CRASH_RECOVERED"',
        str(target),
    )
    assert result.returncode == 0, result.stderr
    assert "ROLLBACK_CRASH_RECOVERED" in result.stdout
    assert {item.name for item in target.iterdir()} == {
        "app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig", "history"
    }
    assert (target / "app" / "release.txt").read_text(encoding="utf-8") == "new-app"
    assert {item.name for item in previous.iterdir()} == {
        "app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig"
    }
    assert (previous / "app" / "release.txt").read_text(encoding="utf-8") == "old-app"
    assert failed_target.is_dir() and list(failed_target.iterdir()) == []


def test_inert_upgrade_failure_journal_is_atomic_and_does_not_reset_unrelated_acls(tmp_path: Path) -> None:
    manager = DEPLOY / "manage_super1_app_inert_windows.ps1"
    source = manager.read_text(encoding="utf-8")
    assert 'state = "FAILED_ROLLED_BACK"' in source
    assert "Write-InertTransactionRecord -Path $transactionRecordPath -Record $transactionRecord" in source
    assert "Get-ChildItem -LiteralPath $TargetRoot -Directory -Recurse" not in source
    assert 'state = "UPGRADE_MOVING_PREVIOUS"' in source
    assert '$transactionRecord.state = "UPGRADE_PROMOTING"' in source
    assert '$record.state = "ROLLBACK_MOVING_CURRENT"' in source
    assert '$record.state = "ROLLBACK_RESTORING_PREVIOUS"' in source
    assert "function Invoke-InertAppRecovery" in source
    assert "-PreserveExistingTargetPaths" in source
    assert "$existingRollbackItems.Count -ne 0" in source
    assert "-ExpectedReleaseId ([string]$record.previous_release_id)" in source
    assert "-ExpectedReleaseId ([string]$record.release_id)" in source
    write_function = next(item["extent_text"] for item in facts(manager, "function") if item["name"] == "Write-InertTransactionRecord")
    record = tmp_path / "transaction.json"
    result = powershell_harness(
        'Import-Module (Join-Path $PSHOME "Modules/Microsoft.PowerShell.Utility"); '
        + write_function
        + '\nWrite-InertTransactionRecord -Path $args[0] -Record ([ordered]@{ state = "UPGRADED" }); '
        + 'Write-InertTransactionRecord -Path $args[0] -Record ([ordered]@{ state = "FAILED_ROLLED_BACK" }); '
        + '$record = Get-Content -LiteralPath $args[0] -Raw | ConvertFrom-Json; '
        + 'if ($record.state -cne "FAILED_ROLLED_BACK") { throw "TRANSACTION_JOURNAL_STALE" }; '
        + 'if (@(Get-ChildItem -LiteralPath ((Split-Path -Parent $args[0])) -Filter "transaction.json.tmp-*" -Force).Count) { throw "TRANSACTION_TEMP_LEFT_BEHIND" }; "JOURNAL_ATOMIC_UPDATE_OK"',
        str(record),
    )
    assert result.returncode == 0, result.stderr
    assert "JOURNAL_ATOMIC_UPDATE_OK" in result.stdout
