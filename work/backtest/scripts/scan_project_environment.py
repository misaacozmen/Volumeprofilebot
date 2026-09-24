"""Fail-closed project environment schema and source-read scanner."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "super1_environment_v1.schema.json"
PYTHON_ROOTS = (ROOT / "backtest", ROOT / "scripts")
WORKFLOW_ROOT = ROOT / ".github" / "workflows"


class EnvironmentSchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnvironmentFinding:
    path: str
    line: int
    key: str
    kind: str


def load_schema(path: str | Path = SCHEMA_PATH) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"schema_version", "project_prefixes", "project_variables", "platform_variables", "ci_variables", "worker_allowed_variables"}
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema_version") != 1:
        raise EnvironmentSchemaError("environment schema is not the closed v1 schema")
    for key in required - {"schema_version"}:
        values = payload[key]
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values) or len(set(values)) != len(values):
            raise EnvironmentSchemaError(f"environment schema field {key} is invalid")
    project_prefixes = payload["project_prefixes"]
    if not all(isinstance(item, str) and item.endswith("_") for item in project_prefixes):
        raise EnvironmentSchemaError("environment schema project prefixes are invalid")
    all_keys = set(payload["project_variables"]) | set(payload["platform_variables"]) | set(payload["ci_variables"])
    if not set(payload["worker_allowed_variables"]).issubset(all_keys):
        raise EnvironmentSchemaError("worker environment contains a key outside the central schema")
    return payload


def validate_runtime_environment(environment: Mapping[str, str], schema: Mapping[str, object] | None = None) -> None:
    schema = schema or load_schema()
    prefixes = tuple(str(item) for item in schema["project_prefixes"])
    supported = set(schema["project_variables"])
    unknown = sorted(str(name) for name in environment if any(str(name).startswith(prefix) for prefix in prefixes) and str(name) not in supported)
    if unknown:
        raise EnvironmentSchemaError(f"unknown project environment variable(s): {', '.join(unknown)}")


def _literal_key(node: ast.AST) -> str | None:
    return str(node.value) if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_os_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"


def _python_findings(path: Path) -> list[EnvironmentFinding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise EnvironmentSchemaError(f"cannot parse environment-scanned file {path}") from exc
    findings: list[EnvironmentFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            key = _literal_key(node.slice)
            findings.append(EnvironmentFinding(path.as_posix(), node.lineno, key or "<dynamic>", "os.environ[]"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "getenv"}:
            receiver = node.func.value
            if _is_os_environ(receiver) or (isinstance(receiver, ast.Name) and receiver.id == "os" and node.func.attr == "getenv"):
                key = _literal_key(node.args[0]) if node.args else None
                findings.append(EnvironmentFinding(path.as_posix(), node.lineno, key or "<dynamic>", "os.environ/getenv"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "environment_value":
            key = _literal_key(node.args[0]) if node.args else None
            if key is not None:
                findings.append(EnvironmentFinding(path.as_posix(), node.lineno, key, "environment_value"))
    return findings


_WORKFLOW_KEY = re.compile(r"^\s{0,8}([A-Z][A-Z0-9_]*)\s*:")


def _workflow_findings(path: Path) -> list[EnvironmentFinding]:
    findings: list[EnvironmentFinding] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _WORKFLOW_KEY.match(line)
        if match:
            findings.append(EnvironmentFinding(path.as_posix(), line_number, match.group(1), "workflow-env-or-key"))
    return findings


def scan_files(paths: Iterable[str | Path], *, schema: Mapping[str, object] | None = None) -> list[EnvironmentFinding]:
    schema = schema or load_schema()
    allowed = set(schema["project_variables"]) | set(schema["platform_variables"]) | set(schema["ci_variables"])
    findings: list[EnvironmentFinding] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files = sorted(file for file in path.rglob("*") if file.is_file() and file.suffix in {".py", ".yml", ".yaml"})
        elif path.is_file() and path.suffix in {".py", ".yml", ".yaml"}:
            files = [path]
        else:
            continue
        for file in files:
            findings.extend(_python_findings(file) if file.suffix == ".py" else _workflow_findings(file))
    return [item for item in findings if item.key not in allowed]


def _workflow_roots(root: Path) -> list[Path]:
    roots = [root / ".github" / "workflows"]
    for repository_root in root.parents:
        candidate = repository_root / ".github" / "workflows"
        if (repository_root / ".git").exists():
            roots.append(candidate)
            break
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in roots:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def scan_repository(root: str | Path = ROOT, *, schema_path: str | Path = SCHEMA_PATH, environment: Mapping[str, str] | None = None) -> dict[str, object]:
    root = Path(root).resolve()
    schema = load_schema(schema_path)
    validate_runtime_environment(os.environ if environment is None else environment, schema)
    paths = [root / "backtest", root / "scripts", *_workflow_roots(root)]
    findings = scan_files(paths, schema=schema)
    scanned_roots = []
    for path in paths:
        if not path.exists():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = Path(os.path.relpath(path, root))
        scanned_roots.append(relative.as_posix())
    return {
        "schema_version": 1,
        "status": "CLEAN" if not findings else "BLOCKED_UNKNOWN_ENV_READ",
        "finding_count": len(findings),
        "findings": [item.__dict__ for item in findings],
        "scanned_roots": scanned_roots,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    args = parser.parse_args(argv)
    try:
        result = scan_repository(args.root, schema_path=args.schema)
    except (EnvironmentSchemaError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": 1, "status": "BLOCKED_ENV_SCHEMA", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "CLEAN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
