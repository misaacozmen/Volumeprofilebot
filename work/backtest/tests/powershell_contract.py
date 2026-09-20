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
$paramNodes = if ($null -ne $ast.ParamBlock) { @($ast.ParamBlock.Parameters) } else { @() }
$params = @($paramNodes | ForEach-Object { $_.Name.VariablePath.UserPath })
$paramDetails = @($paramNodes | ForEach-Object {
    $parameterAttribute = @($_.Attributes | Where-Object { $_ -is [System.Management.Automation.Language.AttributeAst] -and $_.TypeName.Name -eq "Parameter" }) | Select-Object -First 1
    $mandatoryArgument = @($parameterAttribute.NamedArguments | Where-Object { $_.ArgumentName -eq "Mandatory" }) | Select-Object -First 1
    [ordered]@{ name = $_.Name.VariablePath.UserPath; mandatory = ($null -ne $mandatoryArgument -and [string]$mandatoryArgument.Argument.Extent.Text -eq "`$true") }
})
$paramStart = if ($null -ne $ast.ParamBlock) { $ast.ParamBlock.Extent.StartOffset } else { 0 }
$paramEnd = if ($null -ne $ast.ParamBlock) { $ast.ParamBlock.Extent.EndOffset } else { 0 }
$facts += [ordered]@{ kind = "param_block"; params = $params; param_details = $paramDetails; start = $paramStart; end = $paramEnd; scope = "top-level" }
function Get-Scope($node) {
    $parent = $node.Parent
    while ($null -ne $parent) {
        if ($parent -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return "function:$($parent.Name)" }
        $parent = $parent.Parent
    }
    return "top-level"
}
function Get-Ancestors($node) {
    $result = @()
    $parent = $node.Parent
    while ($null -ne $parent) { $result += $parent.GetType().Name; $parent = $parent.Parent }
    return $result
}
function Get-IfBranches($node) {
    $result = @()
    $parent = $node.Parent
    while ($null -ne $parent) {
        if ($parent -is [System.Management.Automation.Language.IfStatementAst]) {
            $branch = $null; $condition = $null
            foreach ($clause in $parent.Clauses) {
                if ($node.Extent.StartOffset -ge $clause.Item2.Extent.StartOffset -and $node.Extent.EndOffset -le $clause.Item2.Extent.EndOffset) { $branch = "true"; $condition = [string]$clause.Item1.Extent.Text; break }
            }
            if ($null -eq $branch -and $null -ne $parent.ElseClause -and $node.Extent.StartOffset -ge $parent.ElseClause.Extent.StartOffset -and $node.Extent.EndOffset -le $parent.ElseClause.Extent.EndOffset) { $branch = "else" }
            if ($null -ne $branch) { $result += [ordered]@{ condition = $condition; branch = $branch; start = $parent.Extent.StartOffset; end = $parent.Extent.EndOffset } }
        }
        $parent = $parent.Parent
    }
    return $result
}
function Get-TryRegions($node) {
    $result = @(); $parent = $node.Parent
    while ($null -ne $parent) {
        if ($parent -is [System.Management.Automation.Language.TryStatementAst]) { $result += [ordered]@{ start = $parent.Extent.StartOffset; end = $parent.Extent.EndOffset; has_catch = ($null -ne $parent.CatchClauses); has_finally = ($null -ne $parent.Finally) } }
        $parent = $parent.Parent
    }
    return $result
}
function Test-Unreachable($node) {
    $parent = $node.Parent
    while ($null -ne $parent) {
        if ($parent -is [System.Management.Automation.Language.IfStatementAst]) {
            $condition = ([string]$parent.Clauses[0].Item1.Extent.Text).Trim()
            if ($condition -match '^\$(?i:false)$' -or $condition -match '^0$' -or $condition -match '^\$(?i:false)\s*-eq\s*\$(?i:true)$') { return $true }
        }
        $parent = $parent.Parent
    }
    return $false
}
    $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "assignment"; left = $_.Left.Extent.Text; right_type = $_.Right.GetType().Name; right_text = $_.Right.Extent.Text; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = (Get-Scope $_) }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.TryStatementAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "try"; has_finally = ($null -ne $_.Finally); has_catch = ($null -ne $_.CatchClauses); catch_text = if ($null -ne $_.CatchClauses) { (@($_.CatchClauses)[0]).Extent.Text } else { $null }; finally_text = if ($null -ne $_.Finally) { $_.Finally.Extent.Text } else { $null }; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = (Get-Scope $_) }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] }, $true) | ForEach-Object {
    $clauses = @($_.Clauses | ForEach-Object { [ordered]@{ condition = [string]$_.Item1.Extent.Text; branch = "true"; start = $_.Item2.Extent.StartOffset; end = $_.Item2.Extent.EndOffset } })
    if ($null -ne $_.ElseClause) { $clauses += [ordered]@{ condition = $null; branch = "else"; start = $_.ElseClause.Extent.StartOffset; end = $_.ElseClause.Extent.EndOffset } }
    $facts += [ordered]@{ kind = "if"; clauses = $clauses; condition_text = [string]$_.Clauses[0].Item1.Extent.Text; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = (Get-Scope $_) }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object {
$facts += [ordered]@{ kind = "function"; name = $_.Name; extent_text = $_.Extent.Text; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = $_.Name }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object {
    $name = if ($_.CommandElements.Count) { [string]$_.CommandElements[0].Value } else { "" }
    $args = @($_.CommandElements | Select-Object -Skip 1 | ForEach-Object { $_.Extent.Text })
    $facts += [ordered]@{ kind = "command"; name = $name; args = $args; text = $_.Extent.Text; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = (Get-Scope $_); ancestors = @(Get-Ancestors $_); if_branches = @(Get-IfBranches $_); try_regions = @(Get-TryRegions $_); unreachable = (Test-Unreachable $_) }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.MemberExpressionAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "member"; member = [string]$_.Member.Value; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = (Get-Scope $_) }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "variable"; variable = $_.Extent.Text; user_path = $_.VariablePath.UserPath; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = (Get-Scope $_) }
}
$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ReturnStatementAst] }, $true) | ForEach-Object {
    $facts += [ordered]@{ kind = "return"; text = $_.Extent.Text; start = $_.Extent.StartOffset; end = $_.Extent.EndOffset; scope = (Get-Scope $_) }
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
        path.write_text("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest\n" + script, encoding="utf-8")
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
