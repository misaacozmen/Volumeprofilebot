from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import subprocess
import tempfile
import shutil
from uuid import uuid4
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 2


class ArtifactValidationError(ValueError):
    """Raised when a frozen research artifact is malformed or no longer reproducible."""


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_sha256(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    return sha256(canonical_json_bytes(unsigned)).hexdigest()


def repo_relative(path: Path, root: Path) -> str:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ArtifactValidationError(f"Input is outside repository root: {resolved_path}") from exc


def file_record(path: Path, root: Path, *, role: str) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "path": repo_relative(path, root),
        "role": role,
        "bytes": len(content),
        "sha256": sha256(content).hexdigest(),
    }


def dependency_versions(names: Iterable[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in sorted(set(names)):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if len(value) == 40 else None


def build_provenance(
    root: Path,
    inputs: Iterable[tuple[Path, str]],
    *,
    dependencies: Iterable[str] = ("pandas",),
) -> dict[str, Any]:
    records = [file_record(path, root, role=role) for path, role in inputs]
    records.sort(key=lambda row: (row["path"], row["role"]))
    paths = [row["path"] for row in records]
    if len(paths) != len(set(paths)):
        raise ArtifactValidationError("Every provenance input path must appear exactly once")
    return {
        "inputs": records,
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "dependencies": dependency_versions(dependencies),
            "git_commit": git_commit(root),
        },
    }


def seal_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    sealed = dict(payload)
    sealed["schema_version"] = SCHEMA_VERSION
    sealed["artifact_sha256"] = payload_sha256(sealed)
    return sealed


def validate_provenance(provenance: Any, root: Path, *, verify_inputs: bool) -> None:
    if not isinstance(provenance, dict):
        raise ArtifactValidationError("Missing provenance object")
    environment = provenance.get("environment")
    if not isinstance(environment, dict):
        raise ArtifactValidationError("Missing provenance.environment")
    required_environment = {
        "python_version",
        "python_implementation",
        "dependencies",
        "git_commit",
    }
    missing_environment = required_environment - set(environment)
    if missing_environment:
        raise ArtifactValidationError(
            f"Missing provenance environment fields: {sorted(missing_environment)}"
        )
    if environment["git_commit"] is not None and not isinstance(environment["git_commit"], str):
        raise ArtifactValidationError("provenance.environment.git_commit must be a string or null")
    records = provenance.get("inputs")
    if not isinstance(records, list) or not records:
        raise ArtifactValidationError("provenance.inputs must be a non-empty list")
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ArtifactValidationError("Invalid provenance input record")
        path_value = record.get("path")
        if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute():
            raise ArtifactValidationError("Provenance paths must be non-empty repository-relative paths")
        normalized = Path(path_value).as_posix()
        if normalized != path_value or ".." in Path(path_value).parts:
            raise ArtifactValidationError(f"Non-canonical provenance path: {path_value}")
        if path_value in seen:
            raise ArtifactValidationError(f"Duplicate provenance path: {path_value}")
        seen.add(path_value)
        digest = record.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ArtifactValidationError(f"Invalid SHA-256 for {path_value}")
        if not isinstance(record.get("bytes"), int) or record["bytes"] < 0:
            raise ArtifactValidationError(f"Invalid byte count for {path_value}")
        if not isinstance(record.get("role"), str) or not record["role"]:
            raise ArtifactValidationError(f"Missing role for {path_value}")
        if verify_inputs:
            absolute = (root / path_value).resolve()
            try:
                absolute.relative_to(root.resolve())
            except ValueError as exc:
                raise ArtifactValidationError(f"Provenance path escapes repository: {path_value}") from exc
            try:
                content = absolute.read_bytes()
            except OSError as exc:
                raise ArtifactValidationError(f"Missing provenance input: {path_value}") from exc
            if len(content) != record["bytes"] or sha256(content).hexdigest() != digest:
                raise ArtifactValidationError(f"Provenance input changed: {path_value}")


def validate_artifact(
    payload: Any,
    root: Path,
    *,
    artifact_type: str | None = None,
    verify_inputs: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ArtifactValidationError("Artifact must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactValidationError(
            f"Unsupported artifact schema: {payload.get('schema_version')!r}"
        )
    if artifact_type is not None and payload.get("artifact_type") != artifact_type:
        raise ArtifactValidationError(
            f"Expected {artifact_type!r}, got {payload.get('artifact_type')!r}"
        )
    expected = payload_sha256(payload)
    if payload.get("artifact_sha256") != expected:
        raise ArtifactValidationError("Artifact payload SHA-256 mismatch")
    validate_provenance(payload.get("provenance"), root, verify_inputs=verify_inputs)
    return payload


def load_artifact(
    path: Path,
    root: Path,
    *,
    artifact_type: str | None = None,
    verify_inputs: bool = True,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"Cannot read artifact {path}: {exc}") from exc
    return validate_artifact(
        payload,
        root,
        artifact_type=artifact_type,
        verify_inputs=verify_inputs,
    )


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def promote_directory_atomic(staging: Path, destination: Path) -> None:
    """Promote a complete staging tree without exposing partially written output."""
    if not staging.is_dir():
        raise ArtifactValidationError(f"Staging directory does not exist: {staging}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except BaseException:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def promote_directories_transactional(pairs: Iterable[tuple[Path, Path]]) -> None:
    """Promote related directories as one rollback-safe publication transaction."""
    items = [(staging.resolve(), destination.resolve()) for staging, destination in pairs]
    if not items or len({destination for _, destination in items}) != len(items):
        raise ArtifactValidationError("Publication destinations must be non-empty and unique")
    for staging, destination in items:
        if not staging.is_dir():
            raise ArtifactValidationError(f"Staging directory does not exist: {staging}")
        destination.parent.mkdir(parents=True, exist_ok=True)

    transaction = uuid4().hex
    backups = {
        destination: destination.with_name(f".{destination.name}.previous.{transaction}")
        for _, destination in items
    }
    promoted: set[Path] = set()
    try:
        for _, destination in items:
            backup = backups[destination]
            if backup.exists():
                raise ArtifactValidationError(f"Publication backup already exists: {backup}")
            if destination.exists():
                os.replace(destination, backup)
        for staging, destination in items:
            os.replace(staging, destination)
            promoted.add(destination)
    except BaseException:
        for _, destination in reversed(items):
            backup = backups[destination]
            if destination in promoted and destination.exists():
                shutil.rmtree(destination)
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
        raise
    else:
        for backup in backups.values():
            if backup.exists():
                shutil.rmtree(backup)
