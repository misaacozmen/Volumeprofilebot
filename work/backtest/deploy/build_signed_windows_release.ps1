[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("forward-shadow", "super1")]
    [string]$Profile,
    [string]$OutputArchive,
    [switch]$ValidateOnly,
    [string]$Python = "python",
    [string]$PrivateKeyPath = (Join-Path $env:LOCALAPPDATA "OtoBacktest\release-private-key.dpapi"),
    [string]$ReleaseId
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression

$SourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RepoRoot = (& git -C $SourceRoot rev-parse --show-toplevel).Trim()
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("otobt-release-" + [Guid]::NewGuid().ToString("N"))
$effectiveArchive = if ($OutputArchive) { $OutputArchive } elseif ($ValidateOnly) { Join-Path $TempRoot "validate-only.zip" } else { throw "OutputArchive is required unless ValidateOnly is set." }
$Archive = [IO.Path]::GetFullPath($effectiveArchive)
$OutputRoot = Split-Path -Parent $Archive
$Stage = Join-Path $TempRoot "payload"
$Wheelhouse = Join-Path $Stage "wheelhouse"
$LinuxWheelhouse = Join-Path $Stage "wheelhouse-linux"
$Super1ProvenanceFiles = @()

function Get-CanonicalArtifactTestFiles {
    param([Parameter(Mandatory = $true)][string]$Root)
    $manifestPath = Join-Path $Root "deploy\artifact_test_files.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Canonical artifact-test manifest is missing: $manifestPath"
    }
    $payload = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ([int]$payload.schema_version -ne 1) {
        throw "Unsupported artifact-test manifest schema."
    }
    $files = @($payload.artifact_test_files | ForEach-Object { [string]$_ })
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($file in $files) {
        if ($file -notmatch '^test_[A-Za-z0-9_]+\.py$' -or -not $seen.Add($file)) {
            throw "Artifact-test list contains an invalid or duplicate entry: $file"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $Root (Join-Path "tests" $file)) -PathType Leaf)) {
            throw "Artifact-test file is missing: $file"
        }
    }
    $sorted = @($files | Sort-Object -CaseSensitive -Culture en-US)
    if ($files.Count -eq 0 -or (($files -join "`n") -cne ($sorted -join "`n"))) {
        throw "Artifact-test list must be canonical case-sensitive sorted order."
    }
    return $files
}

function Get-ReleasePayloadAllowlist {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$SelectedProfile)
    $path = Join-Path $Root "deploy\release_payload_allowlist.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Release payload allowlist is missing: $path" }
    $payload = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ([int]$payload.schema_version -ne 1 -or $null -eq $payload.profiles.$SelectedProfile) { throw "Release payload allowlist profile is missing: $SelectedProfile" }
    $files = @($payload.profiles.$SelectedProfile.files | ForEach-Object { [string]$_ })
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($file in $files) {
        if ([IO.Path]::IsPathRooted($file) -or $file.Contains("..") -or -not $seen.Add($file)) { throw "Release payload allowlist contains an unsafe or duplicate path: $file" }
    }
    if ($files.Count -eq 0) { throw "Release payload allowlist cannot be empty." }
    return @($files | Sort-Object -CaseSensitive -Culture en-US)
}

function Get-Utf8Sha256 {
    param([Parameter(Mandatory = $true)][string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
        )).Replace("-", "").ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Test-TrueValue {
    param([Parameter(Mandatory = $true)][object]$Value)
    return [string]$Value -eq "True"
}

function Get-CollectionNodeIds {
    param([Parameter(Mandatory = $true)][object[]]$Output)
    $nodeIds = @()
    foreach ($line in $Output) {
        $trimmed = ([string]$line).Trim()
        if ($trimmed -match '^(\S+::\S+)(?:\s+.*)?$') {
            $nodeIds += $Matches[1]
        }
    }
    return $nodeIds
}

function Get-JunitNodeIds {
    param([Parameter(Mandatory = $true)][xml]$Document)
    $nodeIds = @()
    foreach ($testcase in @($Document.SelectNodes("//testcase"))) {
        $modulePath = ([string]$testcase.classname).Replace('.', '/')
        $nodeIds += "$modulePath.py::$([string]$testcase.name)"
    }
    return $nodeIds
}

function Write-NodeIdInventory {
    param(
        [Parameter(Mandatory = $true)][object[]]$NodeIds,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $sorted = @($NodeIds | Sort-Object -CaseSensitive -Culture en-US)
    [IO.File]::WriteAllLines(
        $Path,
        $sorted,
        (New-Object Text.UTF8Encoding($false))
    )
    return [ordered]@{
        count = $sorted.Count
        sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Assert-JunitMatchesInventory {
    param(
        [Parameter(Mandatory = $true)][string]$InventoryPath,
        [Parameter(Mandatory = $true)][xml]$Junit,
        [Parameter(Mandatory = $true)][string]$SuiteName
    )
    $expected = @(Get-Content -LiteralPath $InventoryPath | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $actual = @(Get-JunitNodeIds -Document $Junit)
    $badCases = @(
        $Junit.SelectNodes("//testcase") | Where-Object {
            $null -ne $_.SelectSingleNode("failure") -or
            $null -ne $_.SelectSingleNode("error") -or
            $null -ne $_.SelectSingleNode("skipped")
        }
    )
    if ($actual.Count -eq 0 -or $badCases.Count -ne 0) {
        throw "$SuiteName JUnit suite must contain only passing, non-skipped testcases. actual=$($actual.Count) bad=$($badCases.Count)"
    }
    $expectedSorted = @($expected | Sort-Object -CaseSensitive -Culture en-US)
    $actualSorted = @($actual | Sort-Object -CaseSensitive -Culture en-US)
    if ($expectedSorted.Count -ne $actualSorted.Count -or
        (($expectedSorted -join "`n") -cne ($actualSorted -join "`n"))) {
        throw "$SuiteName JUnit nodeid multiset differs from collect-only inventory. collected=$($expectedSorted.Count) junit=$($actualSorted.Count)"
    }
    return [ordered]@{
        collected_count = $expectedSorted.Count
        pass_count = $actualSorted.Count
        skipped_count = 0
        nodeid_sha256 = (Get-FileHash -LiteralPath $InventoryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

# 1. Git dirty check
$gitStatus = (& git -C $RepoRoot status --porcelain -- $SourceRoot)
$repoGitStatus = (& git -C $RepoRoot status --porcelain)
if (($gitStatus -or $repoGitStatus) -and -not $ValidateOnly) {
    $dirtyDetails = @($gitStatus) + @($repoGitStatus | Where-Object { $_ -notin $gitStatus })
    throw "Git working tree is dirty; refusing release build: $($dirtyDetails -join '; ')"
}
$initialRepoGitStatus = @($repoGitStatus)
$gitCommit = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $gitCommit) {
    throw "Could not determine git commit."
}
$gitDirty = [bool]($gitStatus -or $repoGitStatus)

# Promotion is a prerequisite for every Super1 release mode, including
# ValidateOnly. Keep this before environment checks, staging, and signing.
if ($Profile -eq "super1") {
    $promotionValidationCommand = "from pathlib import Path; from backtest.candidate_validation import validate_super1_v4_candidate; validate_super1_v4_candidate(Path(r'$SourceRoot') / 'research_candidates' / 'super1' / 'super1_unsigned_candidate_v4.json', Path(r'$SourceRoot'), True)"
    $promotionValidationOutput = & $Python -E -B -c $promotionValidationCommand 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Super1 promotion gate failed: $($promotionValidationOutput -join ' ')"
    }
}

# 2. CPython 3.11 check
$pyCheck = (& $Python -c "import sys, platform; print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.python_implementation()}|{sys.version}')")
if ($LASTEXITCODE -ne 0 -or -not $pyCheck) {
    throw "Failed to query Python environment: $Python"
}
$pyParts = $pyCheck.Split("|")
if ($pyParts[0] -ne "3.11" -or $pyParts[1] -ne "CPython") {
    throw "Release build requires CPython 3.11. Found: $pyCheck"
}
$pythonVersion = $pyParts[2].Trim()
$pythonExe = (& $Python -c "import sys; print(sys.executable)").Trim()
$pythonExeSha256 = (Get-FileHash -LiteralPath $pythonExe -Algorithm SHA256).Hash.ToLowerInvariant()

# 3. Pre-build test suite execution
[void][IO.Directory]::CreateDirectory($TempRoot)
$compileRoot = Join-Path $TempRoot "compile-root"
New-Item -ItemType Directory -Force -Path $compileRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceRoot "backtest") -Destination (Join-Path $compileRoot "backtest") -Recurse
Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts") -Destination (Join-Path $compileRoot "scripts") -Recurse
$compileOutput = & $Python -B -m compileall -q $compileRoot 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Release build aborted: compileall failed: $($compileOutput -join ' ')"
}
$psAstCommand = "from pathlib import Path; import sys; sys.path.insert(0, r'$SourceRoot\\tests'); from powershell_contract import powershell_ast; paths=sorted(Path(r'$SourceRoot\\deploy').glob('*.ps1')); results=[powershell_ast(path) for path in paths]; bad=[(str(path), result.get('errors', [])) for path, result in zip(paths, results) if result.get('errors')]; assert not bad, bad"
$psAstOutput = & $Python -E -B -c $psAstCommand 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Release build aborted: PowerShell AST validation failed: $($psAstOutput -join ' ')"
}
$fullCollectPath = Join-Path $TempRoot "full.collect.txt"
$fullJunitPath = Join-Path $TempRoot "full.junit.xml"
$pytestCmd = "$Python -m pytest -q $SourceRoot --junitxml=<full-suite>"
$fullCollectOutput = & $Python -m pytest --collect-only -q $SourceRoot
if ($LASTEXITCODE -ne 0) {
    throw "Release build aborted: full collect-only inventory failed."
}
$fullNodeIds = @(Get-CollectionNodeIds -Output $fullCollectOutput)
$fullInventory = Write-NodeIdInventory -NodeIds $fullNodeIds -Path $fullCollectPath
$pytestOutput = & $Python -m pytest -q $SourceRoot "--junitxml=$fullJunitPath"
if ($LASTEXITCODE -ne 0) {
    throw "Release build aborted: pytest test suite failed."
}
if (-not (Test-Path -LiteralPath $fullJunitPath -PathType Leaf)) {
    throw "Release build aborted: full JUnit report is missing."
}
[xml]$fullJunit = Get-Content -LiteralPath $fullJunitPath -Raw
$fullGate = Assert-JunitMatchesInventory -InventoryPath $fullCollectPath -Junit $fullJunit -SuiteName "Full"
$passedCount = [int]$fullGate.pass_count
$pytestPassed = $true
$pytestCollectedCount = [int]$fullGate.collected_count
$pytestPassedCount = [int]$fullGate.pass_count
$pytestSkippedCount = [int]$fullGate.skipped_count
$pytestNodeIdSha256 = [string]$fullGate.nodeid_sha256
$postTestGitStatus = (& git -C $RepoRoot status --porcelain)
if ((@($postTestGitStatus | Sort-Object) -join "`n") -cne (@($initialRepoGitStatus | Sort-Object) -join "`n")) {
    throw "Tests modified the source tree; refusing release build: $($postTestGitStatus -join '; ')"
}
# 4. Fresh reliability attestation. A release must never reuse the checked-in
# report: run the audit in the build temp root and require every gate.
$FreshAuditRoot = Join-Path $TempRoot "fresh-reliability-audit"
$freshAuditOutput = & $Python -E -B (Join-Path $SourceRoot "scripts\run_engine_reliability_audit.py") --report-dir $FreshAuditRoot --frozen-inventory (Join-Path $SourceRoot "data\provenance\dukascopy_v4\frozen_invalid_leg_days_v4.csv") --reacquisition-manifest (Join-Path $SourceRoot "data\provenance\dukascopy_v4\acquisition_v5\reacquisition_manifest_v5.json") 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Fresh engine reliability audit failed: $($freshAuditOutput -join ' ')"
}
$freshAuditManifestPath = Join-Path $FreshAuditRoot "run_manifest.json"
$freshAuditSummaryPath = Join-Path $FreshAuditRoot "summary.csv"
if (-not (Test-Path -LiteralPath $freshAuditManifestPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $freshAuditSummaryPath -PathType Leaf)) {
    throw "Fresh engine reliability audit did not publish its manifest and summary."
}
$freshAuditManifest = Get-Content -LiteralPath $freshAuditManifestPath -Raw | ConvertFrom-Json
$freshAuditSummary = @(Import-Csv -LiteralPath $freshAuditSummaryPath)
if ($freshAuditSummary.Count -ne 1) {
    throw "Fresh engine reliability audit summary is not exactly one row."
}
$freshRow = $freshAuditSummary[0]
$freshHashFields = @([string]$freshAuditManifest.code_hash, [string]$freshAuditManifest.config_hash, [string]$freshAuditManifest.result_hash)
if ($freshHashFields | Where-Object { $_ -notmatch '^[A-Fa-f0-9]{64}$' }) {
    throw "Fresh engine reliability audit manifest hashes are incomplete."
}
if ($null -eq $freshAuditManifest.data_hashes -or @($freshAuditManifest.data_hashes.PSObject.Properties).Count -eq 0) {
    throw "Fresh engine reliability audit has no input dataset hashes."
}
$expectedCanonicalResultHash = "b55a980e89bf31f31047558b804a183a95d359a1865dd095388eed93cc0682f2"
if ([string]$freshAuditManifest.result_hash -ne $expectedCanonicalResultHash) {
    throw "Fresh engine reliability result hash differs from the canonical expected result."
}
$coverage = $freshAuditManifest.coverage
$coverageConsistent = $null -ne $coverage -and
    [int]$coverage.missing_sessions -eq 0 -and
    [int]$coverage.invalid_sessions -eq 0 -and
    [int]$coverage.valid_sessions -eq [int]$coverage.evaluated_sessions
if (-not (Test-TrueValue $freshRow.core_tests_passed) -or
    -not (Test-TrueValue $freshRow.deterministic_rerun) -or
    [int]$freshRow.prefix_violation_count -ne 0 -or
    [int]$freshRow.invalid_data_days_blocked -ne 0 -or
    [int]$freshRow.canonical_decisions -ne 56 -or
    [int]$freshRow.filled_post_pair_cap -ne 7 -or
    [Math]::Abs([double]$freshRow.net_r - 8.5) -gt 0.000000001 -or
    -not $coverageConsistent -or
    [bool]$freshAuditManifest.forward_shadow_ready -ne (Test-TrueValue $freshRow.forward_shadow_ready) -or
    -not (Test-TrueValue $freshRow.forward_shadow_ready)) {
    throw "Fresh engine reliability audit did not pass the 56/7/8.5R, determinism, prefix, core-test, or coverage gates."
}
$FreshHistoryRoot = Join-Path $TempRoot "fresh-canonical-full-history"
$freshHistoryOutput = & $Python -E -B (Join-Path $SourceRoot "scripts\run_canonical_production_full_history.py") --report-dir $FreshHistoryRoot --gap-inventory (Join-Path $SourceRoot "data\provenance\dukascopy_v4\frozen_invalid_leg_days_v4.csv") --reacquired-root (Join-Path $SourceRoot "data\provenance\dukascopy_v4\acquisition_v5\bundles") --reacquisition-manifest (Join-Path $SourceRoot "data\provenance\dukascopy_v4\acquisition_v5\reacquisition_manifest_v5.json") 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Fresh canonical full-history gate failed closed: $($freshHistoryOutput -join ' ')"
}
$freshHistoryManifestPath = Join-Path $FreshHistoryRoot "run_manifest.json"
if (-not (Test-Path -LiteralPath $freshHistoryManifestPath -PathType Leaf)) {
    throw "Fresh canonical full-history gate did not publish a manifest."
}
$freshHistoryManifest = Get-Content -LiteralPath $freshHistoryManifestPath -Raw | ConvertFrom-Json
if ([bool]$freshHistoryManifest.promotable -ne $true -or
    [int]$freshHistoryManifest.coverage.missing_sessions -ne 0 -or
    [int]$freshHistoryManifest.coverage.invalid_sessions -ne 0) {
    throw "Fresh canonical full-history data coverage is incomplete; no release is permitted."
}
$inputDatasetSha256 = Get-Utf8Sha256 (($freshAuditManifest.data_hashes | ConvertTo-Json -Compress -Depth 20))
$freshAuditManifestSha256 = (Get-FileHash -LiteralPath $freshAuditManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()

$createdAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
if (-not $ReleaseId) {
    $utcFormatted = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $shortCommit = if ($gitCommit.Length -ge 12) { $gitCommit.Substring(0, 12) } else { $gitCommit }
    $ReleaseId = "$Profile-$utcFormatted-$shortCommit-v16"
}

New-Item -ItemType Directory -Force -Path $Stage,$Wheelhouse,$OutputRoot | Out-Null
$allowlistFiles = @(Get-ReleasePayloadAllowlist -Root $SourceRoot -SelectedProfile $Profile)

try {
    foreach ($relative in $allowlistFiles) {
        $nativeRelative = $relative.Replace("/", [IO.Path]::DirectorySeparatorChar)
        $source = Join-Path $SourceRoot $nativeRelative
        $target = Join-Path $Stage $nativeRelative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            if ($relative -in @(
                "requirements-windows.lock",
                "requirements-linux.lock",
                "outputs/reports/engine_reliability_audit_fresh_20260908_final5/run_manifest.json"
            )) { continue }
            throw "Release allowlist file is missing: $relative"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
    $artifactTestFiles = @(Get-CanonicalArtifactTestFiles -Root $SourceRoot)
    $artifactTestRoot = Join-Path $Stage "artifact_tests"
    New-Item -ItemType Directory -Force -Path $artifactTestRoot | Out-Null
    foreach ($testFile in $artifactTestFiles) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot (Join-Path "tests" $testFile)) -Destination (Join-Path $artifactTestRoot $testFile)
    }
    Copy-Item -LiteralPath (Join-Path $SourceRoot "tests\powershell_contract.py") -Destination (Join-Path $artifactTestRoot "powershell_contract.py")
    Copy-Item -LiteralPath (Join-Path $SourceRoot "tests\v08_helpers.py") -Destination (Join-Path $artifactTestRoot "v08_helpers.py")
    $freshReportRelative = if ($Profile -eq "super1") {
        "outputs/reports/engine_reliability_audit_fresh_20260908_final5"
    } else {
        "outputs/reports/engine_reliability_audit_2025_feb_mar"
    }
    $freshReportTarget = Join-Path $Stage ($freshReportRelative.Replace("/", [IO.Path]::DirectorySeparatorChar))
    New-Item -ItemType Directory -Force -Path $freshReportTarget | Out-Null
    Copy-Item -LiteralPath $freshAuditManifestPath -Destination (Join-Path $freshReportTarget "run_manifest.json") -Force
    foreach ($cache in @(Get-ChildItem -LiteralPath $Stage -Recurse -Directory -Filter "__pycache__")) {
        $resolved = [IO.Path]::GetFullPath($cache.FullName)
        if (-not $resolved.StartsWith(([IO.Path]::GetFullPath($Stage) + [IO.Path]::DirectorySeparatorChar))) {
            throw "Unsafe cache cleanup target: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    foreach ($compiled in @(
        Get-ChildItem -LiteralPath $Stage -Recurse -File |
            Where-Object { $_.Extension -in @(".pyc", ".pyo") }
    )) {
        $resolved = [IO.Path]::GetFullPath($compiled.FullName)
        if (-not $resolved.StartsWith(([IO.Path]::GetFullPath($Stage) + [IO.Path]::DirectorySeparatorChar))) {
            throw "Unsafe compiled-file cleanup target: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Force
    }
    foreach ($secret in @(
        Get-ChildItem -LiteralPath $Stage -Recurse -File |
            Where-Object {
                $_.Extension -in @(".pem", ".key", ".dpapi", ".pfx", ".cer", ".crt") -or
                $_.Name -match '(credential|password|secret|\.env)'
            }
    )) {
        $resolved = [IO.Path]::GetFullPath($secret.FullName)
        if (-not $resolved.StartsWith(([IO.Path]::GetFullPath($Stage) + [IO.Path]::DirectorySeparatorChar))) {
            throw "Unsafe secret cleanup target: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Force
    }

    & $Python -m pip download --disable-pip-version-check --only-binary=:all: `
        --platform win_amd64 --python-version 311 --implementation cp --abi cp311 `
        --dest $Wheelhouse `
            "pandas==3.0.3" "MetaTrader5==5.0.6162" "setuptools==81.0.0" "wheel==0.48.0" "pytest==8.4.1"
    if ($LASTEXITCODE -ne 0) { throw "Windows wheelhouse build failed." }

    $lockLines = foreach ($wheel in Get-ChildItem -LiteralPath $Wheelhouse -File | Sort-Object Name) {
        $hash = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "wheelhouse/$($wheel.Name) --hash=sha256:$hash"
    }
    [IO.File]::WriteAllLines(
        (Join-Path $Stage "requirements-windows.lock"),
        $lockLines,
        (New-Object Text.UTF8Encoding($false))
    )
    $artifactVenv = Join-Path $TempRoot "artifact-venv"
    & $Python -m venv $artifactVenv
    if ($LASTEXITCODE -ne 0) { throw "Artifact venv creation failed." }
    $artifactPython = Join-Path $artifactVenv "Scripts\python.exe"
    $artifactTestPaths = @($artifactTestFiles | ForEach-Object { Join-Path "artifact_tests" $_ })
    $artifactCollectPath = Join-Path $TempRoot "artifact.collect.txt"
    $artifactJunitPath = Join-Path $TempRoot "artifact.junit.xml"
    Push-Location -LiteralPath $Stage
    try {
        & $artifactPython -m pip install --disable-pip-version-check --no-index --require-hashes -r requirements-windows.lock
        if ($LASTEXITCODE -ne 0) { throw "Locked artifact dependency installation failed." }
        $artifactCollectOutput = & $artifactPython -m pytest --collect-only -q $artifactTestPaths
        if ($LASTEXITCODE -ne 0) { throw "Locked artifact collect-only inventory failed." }
        $artifactOutput = & $artifactPython -m pytest -q $artifactTestPaths "--junitxml=$artifactJunitPath"
        if ($LASTEXITCODE -ne 0) { throw "Locked artifact tests failed." }
    }
    finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $artifactJunitPath -PathType Leaf)) {
        throw "Locked artifact JUnit report is missing."
    }
    $artifactNodeIds = @(Get-CollectionNodeIds -Output $artifactCollectOutput)
    $artifactInventory = Write-NodeIdInventory -NodeIds $artifactNodeIds -Path $artifactCollectPath
    [xml]$artifactJunit = Get-Content -LiteralPath $artifactJunitPath -Raw
    $artifactGate = Assert-JunitMatchesInventory -InventoryPath $artifactCollectPath -Junit $artifactJunit -SuiteName "Artifact"
    $artifactPassedCount = [int]$artifactGate.pass_count
    $artifactPytestCollectedCount = [int]$artifactGate.collected_count
    $artifactPytestPassCount = [int]$artifactGate.pass_count
    $artifactPytestSkippedCount = [int]$artifactGate.skipped_count
    $artifactPytestNodeIdSha256 = [string]$artifactGate.nodeid_sha256
    $artifactPytestCommand = "$artifactPython -m pytest -q $($artifactTestPaths -join ' ') --junitxml=$artifactJunitPath"
    $artifactPytestPassed = ($artifactPytestSkippedCount -eq 0 -and $artifactPytestPassCount -eq $artifactPytestCollectedCount)
    $lockedDependencies = @($lockLines)
    Remove-Item -LiteralPath $artifactTestRoot -Recurse -Force
    Get-ChildItem -LiteralPath $Stage -Recurse -Directory -Force | Where-Object { $_.Name -eq ".pytest_cache" } | Remove-Item -Recurse -Force
    foreach ($cache in @(Get-ChildItem -LiteralPath $Stage -Recurse -Directory -Filter "__pycache__")) { Remove-Item -LiteralPath $cache.FullName -Recurse -Force }
    Get-ChildItem -LiteralPath $Stage -Recurse -File -Force | Where-Object { $_.Extension -in @(".pyc", ".pyo") } | Remove-Item -Force
    if ($Profile -eq "forward-shadow") {
        New-Item -ItemType Directory -Force -Path $LinuxWheelhouse | Out-Null
        & $Python -m pip download --disable-pip-version-check --only-binary=:all: `
            --platform manylinux_2_28_x86_64 --python-version 311 --implementation cp --abi cp311 `
            --dest $LinuxWheelhouse `
            "pandas==3.0.3" "setuptools==81.0.0" "wheel==0.48.0"
        if ($LASTEXITCODE -ne 0) { throw "Linux wheelhouse build failed." }
        $linuxLockLines = foreach ($wheel in Get-ChildItem -LiteralPath $LinuxWheelhouse -File | Sort-Object Name) {
            $hash = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "wheelhouse-linux/$($wheel.Name) --hash=sha256:$hash"
        }
        [IO.File]::WriteAllLines(
            (Join-Path $Stage "requirements-linux.lock"),
            $linuxLockLines,
            (New-Object Text.UTF8Encoding($false))
        )
    }

    $requiredPayloadFiles = @($allowlistFiles)
    $stageFiles = @(Get-ChildItem -LiteralPath $Stage -Recurse -File | ForEach-Object {
        $_.FullName.Substring($Stage.Length).TrimStart('\\', '/') -replace '\\', '/'
    })
    $unexpectedStageFiles = @($stageFiles | Where-Object { $_ -notin $requiredPayloadFiles -and $_ -notlike 'wheelhouse/*' -and $_ -notlike 'wheelhouse-linux/*' })
    if ($unexpectedStageFiles.Count -ne 0) { throw "Release staging contains files outside the signed allowlist: $($unexpectedStageFiles -join ', ')" }
    if ($Profile -eq "super1") {
        $legacySuper1Files = @(
            "live_forward/super1_xm_mt5_demo_config.json",
            "live_forward/xm_mt5_demo_config.json",
            "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json",
            "scripts/download_dukascopy.py",
            "scripts/discover_xm_mt5_server.py"
        )
        $legacyPresent = @($stageFiles | Where-Object { $_ -in $legacySuper1Files })
        if ($legacyPresent.Count -ne 0) { throw "Super1 release contains legacy order/data/broker files: $($legacyPresent -join ', ')" }
    }
    foreach ($relative in $requiredPayloadFiles) {
        $nativeRelative = $relative.Replace("/", [IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath (Join-Path $Stage $nativeRelative) -PathType Leaf)) {
            throw "Release staging is incomplete; required file is missing: $relative"
        }
    }
    if ($Profile -eq "super1") {
        $candidatePath = Join-Path $Stage "research_candidates\super1\super1_unsigned_candidate_v4.json"
        $requirePromotable = "True"
        $candidateValidationOutput = & $Python -E -B -c "from backtest.candidate_validation import validate_super1_v4_candidate; validate_super1_v4_candidate(r'$candidatePath', root=r'$Stage', require_promotable=$requirePromotable)" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Unsigned Super1 candidate validation failed: $($candidateValidationOutput -join ' ')"
        }
    }

    if ($Profile -eq "super1") {
        $isolatedCheck = @"
import sys
sys.path.insert(0, r'$Stage')
import scripts.run_super1_xm_mt5_forward as runner
runner.validate_super1_candidate(runner.core.read_json(runner.RUNTIME_CONFIG))
runner.configure_core()
try:
    runner.core.runtime_config()
except Exception as exc:
    assert 'no-send' in str(exc).lower()
else:
    raise AssertionError('dry-start reached a bound runtime without private binding')
assert 'MetaTrader5' not in sys.modules
"@
        $isolatedOutput = & $Python -I -E -B -c $isolatedCheck 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Isolated Super1 staged-tree import/no-send failed: $($isolatedOutput -join ' ')" }
        $v2Active = @(Get-ChildItem -LiteralPath $Stage -Recurse -File | Where-Object { $_.Name -match '(?i)super1.*v2|instrument_registry_v2|2022_2026_v2' })
        if ($v2Active.Count -ne 0) { throw "Super1 staging contains an active V2 artifact." }
    }
    if ($ValidateOnly) {
        [ordered]@{
            validation = "PASSED"
            profile = $Profile
            signed = $false
            archive_created = $false
            staged_tree_import = "PASSED"
            private_binding_no_send = "PASSED"
            full_test_count = $pytestPassedCount
            full_test_nodeid_sha256 = $pytestNodeIdSha256
            fresh_audit_manifest_sha256 = (Get-FileHash -LiteralPath $freshAuditManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
            fresh_history_manifest_sha256 = (Get-FileHash -LiteralPath $freshHistoryManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        } | ConvertTo-Json
        return
    }
    if (Test-Path -LiteralPath $Archive) {
        throw "Output archive already exists; refusing to overwrite: $Archive"
    }
    $zipCreate = [IO.Compression.ZipFile]::Open($Archive, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in Get-ChildItem -LiteralPath $Stage -Recurse -File) {
            $relative = $file.FullName.Substring($Stage.Length).TrimStart('\', '/') -replace '\\', '/'
            $entry = $zipCreate.CreateEntry($relative, [IO.Compression.CompressionLevel]::Optimal)
            $input = [IO.File]::OpenRead($file.FullName); $output = $entry.Open()
            try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose() }
        }
    }
    finally { $zipCreate.Dispose() }
    
    $manifestFiles = @()
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $archiveEntries = @($zip.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        foreach ($relative in $requiredPayloadFiles) {
            if ($relative -notin $archiveEntries) {
                throw "Release archive is incomplete; required file is missing: $relative"
            }
        }
        foreach ($entry in ($zip.Entries | Sort-Object FullName)) {
            $entryPath = $entry.FullName.Replace("\", "/")
            if ($entryPath.EndsWith("/")) { continue }
            $entryStream = $entry.Open()
            $hasher = [Security.Cryptography.SHA256]::Create()
            try {
                $entryHash = ([BitConverter]::ToString(
                    $hasher.ComputeHash($entryStream)
                )).Replace("-", "").ToLowerInvariant()
            }
            finally {
                $hasher.Dispose()
                $entryStream.Dispose()
            }
            $manifestFiles += [ordered]@{
                path = $entryPath
                sha256 = $entryHash
            }
        }
    }
    finally {
        $zip.Dispose()
    }
    $archiveHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $enginePackageDescriptor = @(
        $manifestFiles |
            Where-Object { [string]$_.path -match '^(backtest|scripts)/' } |
            Sort-Object -Property path -CaseSensitive -Culture en-US |
            ForEach-Object { "$($_.path)=$($_.sha256)" }
    ) -join "`n"
    $enginePackageHash = Get-Utf8Sha256 $enginePackageDescriptor
    $calendarPath = Join-Path $Stage "live_forward\calendars\us_equity_rth_2022_2026_v4.json"
    $calendarArtifactHash = (Get-FileHash -LiteralPath $calendarPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifestPath = [IO.Path]::ChangeExtension($Archive, ".manifest.json")
    $signaturePath = [IO.Path]::ChangeExtension($Archive, ".manifest.sig")
    if ((Test-Path -LiteralPath $manifestPath) -or (Test-Path -LiteralPath $signaturePath)) {
        throw "Manifest/signature output already exists; refusing to overwrite."
    }
    $manifest = [ordered]@{
        schema_version = 1
        release_id = $ReleaseId
        profile = $Profile
        archive_file = [IO.Path]::GetFileName($Archive)
        archive_sha256 = $archiveHash
        release_archive_sha256 = $archiveHash
        engine_package_sha256 = $enginePackageHash
        calendar_artifact_sha256 = $calendarArtifactHash
        input_dataset_sha256 = $inputDatasetSha256
        reliability_audit_manifest_sha256 = $freshAuditManifestSha256
        reliability_audit_ready = $true
        payload_allowlist_sha256 = (Get-FileHash -LiteralPath (Join-Path $SourceRoot "deploy\release_payload_allowlist.json") -Algorithm SHA256).Hash.ToLowerInvariant()
        payload_allowlist = $allowlistFiles
        reliability_audit_summary = [ordered]@{
            canonical_decisions = [int]$freshRow.canonical_decisions
            filled_post_pair_cap = [int]$freshRow.filled_post_pair_cap
            net_r = [double]$freshRow.net_r
            deterministic_rerun = [bool]$freshRow.deterministic_rerun
            prefix_violation_count = [int]$freshRow.prefix_violation_count
            core_tests_passed = [bool]$freshRow.core_tests_passed
            coverage_missing_sessions = [int]$coverage.missing_sessions
            coverage_invalid_sessions = [int]$coverage.invalid_sessions
            coverage_valid_sessions = [int]$coverage.valid_sessions
            coverage_evaluated_sessions = [int]$coverage.evaluated_sessions
        }
        created_at_utc = $createdAtUtc
        built_at_utc = $createdAtUtc
        git_commit = $gitCommit
        git_dirty = $gitDirty
        python_version = $pythonVersion
        python_executable_sha256 = $pythonExeSha256
        pytest_command = $pytestCmd
        pytest_passed = $pytestPassed
        pytest_collected_count = $pytestCollectedCount
        pytest_pass_count = $pytestPassedCount
        pytest_skipped_count = $pytestSkippedCount
        pytest_nodeid_sha256 = $pytestNodeIdSha256
        pytest_passed_count = $pytestPassedCount
        dependencies = [ordered]@{
            pandas = "3.0.3"
            MetaTrader5 = "5.0.6162"
            setuptools = "81.0.0"
            wheel = "0.48.0"
            pytest = "8.4.1"
        }
        artifact_pytest_passed = $artifactPytestPassed
        artifact_pytest_collected_count = $artifactPytestCollectedCount
        artifact_pytest_pass_count = $artifactPytestPassCount
        artifact_pytest_skipped_count = $artifactPytestSkippedCount
        artifact_pytest_nodeid_sha256 = $artifactPytestNodeIdSha256
        artifact_pytest_count = $artifactPassedCount
        artifact_pytest_command = $artifactPytestCommand
        artifact_test_files = $artifactTestFiles
        locked_dependencies = $lockedDependencies
        wheelhouse = @(
            Get-ChildItem -LiteralPath $Wheelhouse -File | Sort-Object Name | ForEach-Object {
                [ordered]@{
                    file = $_.Name
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
        )
        linux_wheelhouse = @(
            if (Test-Path -LiteralPath $LinuxWheelhouse) {
                Get-ChildItem -LiteralPath $LinuxWheelhouse -File | Sort-Object Name | ForEach-Object {
                    [ordered]@{
                        file = $_.Name
                        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                    }
                }
            }
        )
        files = $manifestFiles
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($manifestPath, $manifestJson + "`n", (New-Object Text.UTF8Encoding($false)))

    if (-not (Test-Path -LiteralPath $PrivateKeyPath -PathType Leaf)) {
        throw "DPAPI release signing key is missing after software/data gates: $PrivateKeyPath"
    }
    $protected = [Convert]::FromBase64String((Get-Content -LiteralPath $PrivateKeyPath -Raw).Trim())
    $privateBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $protected,
        [Text.Encoding]::UTF8.GetBytes("OtoBacktestReleaseSigningV1"),
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    try {
        $rsa.FromXmlString([Text.Encoding]::UTF8.GetString($privateBytes))
        $signature = $rsa.SignData(
            [IO.File]::ReadAllBytes($manifestPath),
            [Security.Cryptography.CryptoConfig]::MapNameToOID("SHA256")
        )
        [IO.File]::WriteAllText(
            $signaturePath,
            [Convert]::ToBase64String($signature) + "`n",
            (New-Object Text.UTF8Encoding($false))
        )
    }
    finally {
        if ($privateBytes) { [Array]::Clear($privateBytes, 0, $privateBytes.Length) }
        $rsa.Dispose()
    }

    [ordered]@{
        release_id = $ReleaseId
        archive = $Archive
        manifest = $manifestPath
        signature = $signaturePath
        archive_sha256 = $archiveHash
        release_archive_sha256 = $archiveHash
    } | ConvertTo-Json
}
finally {
    if (Test-Path -LiteralPath $TempRoot) {
        $resolvedTemp = [IO.Path]::GetFullPath($TempRoot)
        $expectedPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if (-not $resolvedTemp.StartsWith($expectedPrefix) -or $resolvedTemp -notmatch "otobt-release-[0-9a-f]{32}$") {
            throw "Unsafe release temp cleanup target: $resolvedTemp"
        }
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
