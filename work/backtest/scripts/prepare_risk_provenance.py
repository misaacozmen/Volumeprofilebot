from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from frozen_first30_thresholds import load_thresholds


INPUT_MANIFEST = ROOT / "data" / "provenance" / "first30_pre2025_inputs.sha256"
RAW_TARGET = ROOT / "data" / "raw"
BASELINE_LOCK = ROOT / "forward_shadow" / "baseline_lock.json"
BASELINE_MANIFEST = ROOT / "forward_shadow" / "engine_reliability_audit_2025_feb_mar_manifest.json"
THRESHOLD_ARTIFACT = ROOT / "research_candidates" / "calibration" / "first30_thresholds_pre2025_v1.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_inputs() -> dict[Path, str]:
    result: dict[Path, str] = {}
    for raw_line in INPUT_MANIFEST.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        expected, relative = line.split(None, 1)
        relative_path = Path(relative.lstrip("*"))
        if relative_path.parts[:2] != ("data", "raw"):
            raise RuntimeError(f"input manifest path is outside data/raw: {relative}")
        result[relative_path] = expected.lower()
    if len(result) != 144:
        raise RuntimeError(f"expected 144 pinned raw inputs, found {len(result)}")
    return result


def source_path(source_root: Path, relative: Path) -> Path:
    candidate = source_root / relative
    if candidate.is_file():
        return candidate
    return source_root / Path(*relative.parts[2:])


def stage_and_verify(source_root: Path, expected: dict[Path, str]) -> None:
    source_root = source_root.resolve()
    for relative, expected_hash in expected.items():
        source = source_path(source_root, relative)
        if not source.is_file():
            raise RuntimeError(f"missing pinned input: {relative}")
        actual = sha256(source)
        if actual != expected_hash:
            raise RuntimeError(f"pinned input hash mismatch: {relative}")
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)

    actual_files = {
        path.relative_to(ROOT).as_posix()
        for directory in (ROOT / "data" / "raw" / "nq", ROOT / "data" / "raw" / "spx")
        for path in directory.glob("DUKASCOPY_*.csv")
    }
    if actual_files != {path.as_posix() for path in expected}:
        raise RuntimeError("data/raw contains a different DUKASCOPY input set")


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare and verify pinned risk provenance inputs.")
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="prepared input tree containing data/raw/nq and data/raw/spx, or those two directories directly",
    )
    parser.add_argument(
        "--engine-audit-source-root",
        type=Path,
        required=True,
        help="separate provider-sourced 2025 audit inputs plus engine_audit_manifest.json",
    )
    parser.add_argument(
        "--rebuild-threshold-artifact",
        action="store_true",
        help="recreate the artifact after verification; use only when intentionally refreshing its sealed bytes",
    )
    args = parser.parse_args()
    expected = expected_inputs()
    stage_and_verify(args.source_root, expected)
    if args.rebuild_threshold_artifact:
        run([sys.executable, str(ROOT / "scripts" / "run_main_candidate_filter_tests.py"), "--rebuild-threshold-artifact"])
    load_thresholds(verify_sources=True)
    with tempfile.TemporaryDirectory(prefix="otobt-engine-audit-") as temporary_stage:
        run(
            [
                sys.executable,
                str(ROOT / "scripts" / "prepare_engine_audit_inputs.py"),
                "--source-root",
                str(args.engine_audit_source_root.resolve()),
                "--stage-root",
                temporary_stage,
            ]
        )
        run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_engine_reliability_audit.py"),
                "--market-data-root",
                temporary_stage,
            ]
        )
    if not BASELINE_MANIFEST.is_file():
        raise RuntimeError("baseline audit did not produce its tracked manifest")
    print(f"verified raw inputs: {len(expected)}")
    print(f"threshold artifact: {sha256(THRESHOLD_ARTIFACT)}")
    print(f"baseline manifest: {sha256(BASELINE_MANIFEST)}")


if __name__ == "__main__":
    main()
