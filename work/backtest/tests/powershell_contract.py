from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


POWERSHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def powershell_ast(path: Path) -> dict[str, Any]:
    script = r'''
$tokens = $null
$errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($env:OTOBT_AST_PATH, [ref]$tokens, [ref]$errors)
$facts = @()
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "assignment"; left = $_.Left.Extent.Text; right_type = $_.Right.GetType().Name; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.TryStatementAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "try"; has_finally = ($null -ne $_.Finally); has_catch = ($null -ne $_.CatchClauses); start = $_.Extent.StartOffset; end = $_.Extent.EndOffset }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "function"; name = $_.Name; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object {
    $name = if ($_.CommandElements.Count) { [string]$_.CommandElements[0].Value } else { "" }
    $facts += [ordered]@{ kind = "command"; name = $name; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.MemberExpressionAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "member"; member = [string]$_.Member.Value; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset }
}
[ordered]@{ errors = @($errors | ForEach-Object Message); facts = $facts } | ConvertTo-Json -Depth 8 -Compress
'''
    environment = dict(__import__("os").environ)
    environment["OTOBT_AST_PATH"] = str(path.resolve())
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout.strip())


def powershell_harness(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="otobt-ps-contract-") as directory:
        path = Path(directory) / "harness.ps1"
        path.write_text(script, encoding="utf-8")
        environment = dict(__import__("os").environ)
        for index, argument in enumerate(args):
            environment[f"OTOBT_HARNESS_ARG{index}"] = argument
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path), *args],
            capture_output=True,
            text=True,
            env=environment,
        )


def facts(path: Path, kind: str) -> list[dict[str, Any]]:
    return [item for item in powershell_ast(path)["facts"] if item["kind"] == kind]
