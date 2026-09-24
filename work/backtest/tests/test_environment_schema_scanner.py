from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest.live.settings import SUPPORTED_ENV, SettingsError, validate_environment_keys
from scripts.scan_project_environment import EnvironmentSchemaError, load_schema, scan_files, scan_repository, validate_runtime_environment


ROOT = Path(__file__).resolve().parents[1]


def test_central_schema_matches_runtime_project_allowlist() -> None:
    schema = load_schema()
    assert set(schema["project_variables"]) == set(SUPPORTED_ENV)
    assert scan_repository(ROOT)["status"] == "CLEAN"


def test_nested_project_scan_includes_repository_workflows() -> None:
    result = scan_repository(ROOT)
    assert any(path.endswith(".github/workflows") for path in result["scanned_roots"])
    workflow_root = ROOT.parents[1] / ".github" / "workflows"
    assert scan_files([workflow_root / "super1-risk-gates.yml"], schema=load_schema()) == []


def test_nested_scan_finds_unknown_key_in_repository_root_workflow(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    project = repository / "work" / "backtest"
    project.mkdir(parents=True)
    (repository / ".git").mkdir()
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "fixture.yml").write_text("env:\n  CI_ONLY_UNDECLARED: fixture\n", encoding="utf-8")

    result = scan_repository(project, schema_path=ROOT / "schemas" / "super1_environment_v1.schema.json", environment={})

    assert result["status"] == "BLOCKED_UNKNOWN_ENV_READ"
    assert "../../.github/workflows" in result["scanned_roots"]
    assert [(item["key"], item["kind"]) for item in result["findings"]] == [
        ("CI_ONLY_UNDECLARED", "workflow-env-or-key")
    ]


def test_unknown_project_runtime_environment_is_fail_closed() -> None:
    with pytest.raises((SettingsError, EnvironmentSchemaError), match="unknown project environment"):
        validate_environment_keys({"SUPER1_NOT_IN_SCHEMA": "fixture"})
    with pytest.raises(EnvironmentSchemaError, match="unknown project environment"):
        validate_runtime_environment({"CAPITAL_NOT_IN_SCHEMA": "fixture"})


def test_source_and_ci_reads_outside_schema_are_reported(tmp_path: Path) -> None:
    source = tmp_path / "bad.py"
    source.write_text(
        "import os\nvalue = os.environ['PROJECT_NOT_IN_SCHEMA']\nother = os.getenv(dynamic_name)\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "bad.yml"
    workflow.write_text("env:\n  PROJECT_NOT_IN_SCHEMA: fixture\n", encoding="utf-8")
    findings = scan_files([source, workflow])
    assert {(item.key, item.kind) for item in findings} == {
        ("PROJECT_NOT_IN_SCHEMA", "os.environ[]"),
        ("<dynamic>", "os.environ/getenv"),
        ("PROJECT_NOT_IN_SCHEMA", "workflow-env-or-key"),
    }


def test_ci_workflow_runs_fail_closed_scanner() -> None:
    workflow = ROOT.parents[1] / ".github" / "workflows" / "super1-risk-gates.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "python scripts/scan_project_environment.py" in text
    assert "test_environment_schema_scanner.py" in text
