[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("forward-shadow", "super1")]
    [string]$Profile,
    [Parameter(Mandatory = $true)]
    [string]$OutputArchive,
    [string]$Python = "python",
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SymlinkFixtureRoot,
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
$Archive = [IO.Path]::GetFullPath($OutputArchive)
$OutputRoot = Split-Path -Parent $Archive
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("otobt-release-" + [Guid]::NewGuid().ToString("N"))
$Stage = Join-Path $TempRoot "payload"
$Wheelhouse = Join-Path $Stage "wheelhouse"
$LinuxWheelhouse = Join-Path $Stage "wheelhouse-linux"
$Super1ProvenanceFiles = @()

function Assert-OwnerSymlinkFixture {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not [IO.Path]::IsPathRooted($Root)) {
        throw "LINK_FIXTURE_ROOT_REQUIRED: fixture root must be an absolute path."
    }
    $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).ProviderPath
    $requiredFiles = @(
        (Join-Path $resolvedRoot "input\allowed.txt"),
        (Join-Path $resolvedRoot "outside\outside.txt")
    )
    foreach ($path in $requiredFiles) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "LINK_FIXTURE_ROOT_INVALID: required owner fixture file is missing: $path"
        }
    }

    $linkPath = Join-Path $resolvedRoot "output\escape-symlink.txt"
    if (-not (Test-Path -LiteralPath $linkPath -PathType Leaf)) {
        throw "LINK_FIXTURE_REQUIRED: owner symlink is missing: $linkPath"
    }
    $link = Get-Item -LiteralPath $linkPath -Force -ErrorAction Stop
    if ([string]$link.LinkType -cne "SymbolicLink") {
        throw "LINK_FIXTURE_REQUIRED: expected an owner-created SymbolicLink: $linkPath"
    }
    $targetValue = [string]$link.Target
    if ([string]::IsNullOrWhiteSpace($targetValue)) {
        throw "LINK_FIXTURE_INVALID: owner symlink target could not be read."
    }
    $actualTargetPath = if ([IO.Path]::IsPathRooted($targetValue)) {
        $targetValue
    }
    else {
        Join-Path (Split-Path -Parent $linkPath) $targetValue
    }
    $actualTarget = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $actualTargetPath -ErrorAction Stop).ProviderPath)
    $expectedTarget = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath (Join-Path $resolvedRoot "outside\outside.txt") -ErrorAction Stop).ProviderPath)
    if (-not [string]::Equals($actualTarget, $expectedTarget, [StringComparison]::OrdinalIgnoreCase)) {
        throw "LINK_FIXTURE_INVALID: owner symlink target does not match the prepared outside file."
    }
    return $resolvedRoot
}

function Get-CollectionNodeIds {
    param([Parameter(Mandatory = $true)][object[]]$Output)
    $nodeIds = @()
    foreach ($line in $Output) {
        $trimmed = ([string]$line).Trim()
        if ($trimmed -match '^((?:tests|artifact_tests)/\S+\.py::.+)$') {
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

$SymlinkFixtureRoot = Assert-OwnerSymlinkFixture -Root $SymlinkFixtureRoot
if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("PYTEST_ADDOPTS"))) {
    throw "Release build refuses hidden PYTEST_ADDOPTS; pass all test options explicitly."
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

function Get-ByteSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally { $hasher.Dispose() }
}

function Assert-ReleaseIntegrityPayload {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][object]$Contract,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $bom = $Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and
        $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF
    $crCount = @($Bytes | Where-Object { $_ -eq 13 }).Count
    $lfCount = @($Bytes | Where-Object { $_ -eq 10 }).Count
    $sha256 = Get-ByteSha256 -Bytes $Bytes
    if (
        $Bytes.Length -ne [int]$Contract.byte_length -or
        $bom -ne [bool]$Contract.bom -or
        $crCount -ne [int]$Contract.cr_count -or
        $lfCount -ne [int]$Contract.lf_count -or
        $sha256 -cne ([string]$Contract.sha256_lf).ToLowerInvariant()
    ) {
        throw "$Label does not match the release-integrity byte contract."
    }
}

function Assert-ReleaseIntegrityFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Contract,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
    Assert-ReleaseIntegrityPayload `
        -Bytes ([IO.File]::ReadAllBytes($Path)) `
        -Contract $Contract `
        -Label $Label
}

function Assert-ReleaseIntegrityContract {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )
    $contractPath = Join-Path $SourceRoot "deploy\release_integrity_contract.json"
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        throw "Release-integrity contract is missing: $contractPath"
    }
    $contract = [IO.File]::ReadAllText($contractPath) | ConvertFrom-Json
    if ([int]$contract.schema_version -ne 1 -or
        [string]$contract.helper_path -cne "deploy/release_integrity.ps1" -or
        [string]$contract.encoding -cne "UTF-8" -or
        [bool]$contract.bom -ne $false -or
        [string]$contract.eol -cne "LF" -or
        [int]$contract.cr_count -ne 0 -or
        [int]$contract.lf_count -lt 0 -or
        [string]$contract.source_commit -notmatch '^[A-Fa-f0-9]{40}$' -or
        [string]$contract.source_tree -notmatch '^[A-Fa-f0-9]{40}$' -or
        [string]$contract.source_blob_sha1 -notmatch '^[A-Fa-f0-9]{40}$' -or
        [string]$contract.sha256_lf -notmatch '^[A-Fa-f0-9]{64}$'
    ) {
        throw "Release-integrity contract metadata is invalid."
    }
    $helperPath = Join-Path $SourceRoot ($contract.helper_path.Replace("/", "\"))
    $sourceTree = (& git -C $RepoRoot rev-parse "$($contract.source_commit)^{tree}").Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceTree -cne [string]$contract.source_tree) {
        throw "Release-integrity source tree does not match the contract."
    }
    $repoRelativeHelperPath = $helperPath.Substring($RepoRoot.Length).TrimStart('\').Replace('\', '/')
    $sourceBlob = (& git -C $RepoRoot rev-parse "$($contract.source_commit):$repoRelativeHelperPath").Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceBlob -cne [string]$contract.source_blob_sha1) {
        throw "Release-integrity source blob does not match the contract."
    }
    Assert-ReleaseIntegrityFile -Path $helperPath -Contract $contract -Label "Release-integrity checkout helper"
    $checkoutBlob = (& git -C $RepoRoot hash-object -- $helperPath).Trim()
    if ($LASTEXITCODE -ne 0 -or $checkoutBlob -cne [string]$contract.source_blob_sha1) {
        throw "Release-integrity checkout helper is not the contracted Git blob."
    }
    $super1Text = [IO.File]::ReadAllText((Join-Path $SourceRoot "deploy\upgrade_super1_signed_app_windows.ps1"))
    $forwardText = [IO.File]::ReadAllText((Join-Path $SourceRoot "deploy\upgrade_forward_shadow_windows.ps1"))
    $expectedPin = ([string]$contract.sha256_lf).ToLowerInvariant()
    foreach ($entry in @(
        [pscustomobject]@{ Name = "Super1"; Text = $super1Text },
        [pscustomobject]@{ Name = "ForwardShadow"; Text = $forwardText }
    )) {
        $matches = [regex]::Matches(
            $entry.Text,
            '\$ExpectedIntegrityScriptSha256\s*=\s*"([A-Fa-f0-9]{64})"'
        )
        if ($matches.Count -ne 1 -or [string]$matches[0].Groups[1].Value -cne $expectedPin) {
            throw "$($entry.Name) integrity helper pin does not match the contracted source."
        }
    }
    return $contract
}

function Assert-ReleaseIntegrityZipEntry {
    param(
        [Parameter(Mandatory = $true)][IO.Compression.ZipArchive]$Zip,
        [Parameter(Mandatory = $true)][object]$Contract
    )
    $path = [string]$Contract.helper_path
    $entries = @($Zip.Entries | Where-Object { $_.FullName.Replace("\", "/") -ceq $path })
    if ($entries.Count -ne 1) {
        throw "Release archive must contain exactly one integrity helper entry: $path"
    }
    $stream = $entries[0].Open()
    $memory = New-Object IO.MemoryStream
    try {
        $stream.CopyTo($memory)
        Assert-ReleaseIntegrityPayload `
            -Bytes $memory.ToArray() `
            -Contract $Contract `
            -Label "Release archive integrity helper"
    }
    finally {
        $memory.Dispose()
        $stream.Dispose()
    }
}

$ReleaseIntegrityContract = Assert-ReleaseIntegrityContract -SourceRoot $SourceRoot -RepoRoot $RepoRoot
if (-not (Test-Path -LiteralPath $PrivateKeyPath -PathType Leaf)) {
    throw "DPAPI release signing key is missing: $PrivateKeyPath"
}

# 1. Git dirty check
$gitStatus = (& git -C $RepoRoot status --porcelain -- $SourceRoot)
$repoGitStatus = (& git -C $RepoRoot status --porcelain)
if ($gitStatus -or $repoGitStatus) {
    $dirtyDetails = @($gitStatus) + @($repoGitStatus | Where-Object { $_ -notin $gitStatus })
    throw "Git working tree is dirty; refusing release build: $($dirtyDetails -join '; ')"
}
$gitCommit = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $gitCommit) {
    throw "Could not determine git commit."
}
$gitDirty = $false

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
$fullCollectPath = Join-Path $TempRoot "full.collect.txt"
$fullJunitPath = Join-Path $TempRoot "full.junit.xml"
$pytestCollectCmd = "$Python -m pytest --collect-only -q -p no:cacheprovider tests --symlink-fixture-root '$SymlinkFixtureRoot'"
$pytestCmd = "$Python -m pytest -q -p no:cacheprovider tests --symlink-fixture-root '$SymlinkFixtureRoot' --junitxml=<full-suite>"
Push-Location -LiteralPath $SourceRoot
try {
    $fullCollectOutput = & $Python -m pytest --collect-only -q -p no:cacheprovider tests --symlink-fixture-root $SymlinkFixtureRoot
    $fullCollectExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($fullCollectExitCode -ne 0) {
    throw "Release build aborted: full collect-only inventory failed."
}
$fullNodeIds = @(Get-CollectionNodeIds -Output $fullCollectOutput)
$fullInventory = Write-NodeIdInventory -NodeIds $fullNodeIds -Path $fullCollectPath
Push-Location -LiteralPath $SourceRoot
try {
    $pytestOutput = & $Python -m pytest -q -p no:cacheprovider tests --symlink-fixture-root $SymlinkFixtureRoot "--junitxml=$fullJunitPath"
    $fullSuiteExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($fullSuiteExitCode -ne 0) {
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
if ($postTestGitStatus) {
    throw "Tests modified the source tree; refusing release build: $($postTestGitStatus -join '; ')"
}

$createdAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
if (-not $ReleaseId) {
    $utcFormatted = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $shortCommit = if ($gitCommit.Length -ge 12) { $gitCommit.Substring(0, 12) } else { $gitCommit }
    $ReleaseId = "$Profile-$utcFormatted-$shortCommit-v16"
}

New-Item -ItemType Directory -Force -Path $Stage,$Wheelhouse,$OutputRoot | Out-Null

try {
    foreach ($directory in @("backtest", "deploy", "forward_shadow", "live_forward", "scripts")) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $directory) -Destination (Join-Path $Stage $directory) -Recurse
    }
    if ($Profile -eq "super1") {
        $canonicalSuper1Config = Join-Path $Stage "live_forward\super1_xm_mt5_demo_config_v4.json"
        $deployedSuper1Config = Join-Path $Stage "live_forward\super1_xm_mt5_demo_config.json"
        if (-not (Test-Path -LiteralPath $canonicalSuper1Config -PathType Leaf)) {
            throw "Versioned Super1 V4 runtime config is missing from release staging."
        }
        Copy-Item -LiteralPath $canonicalSuper1Config -Destination $deployedSuper1Config -Force
        if ((Get-FileHash -LiteralPath $canonicalSuper1Config -Algorithm SHA256).Hash -cne
            (Get-FileHash -LiteralPath $deployedSuper1Config -Algorithm SHA256).Hash) {
            throw "Generated deployed Super1 runtime config is not byte-identical to V4."
        }
    }
    foreach ($file in @("pyproject.toml", "README.md")) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $file) -Destination (Join-Path $Stage $file)
    }
    Assert-ReleaseIntegrityFile `
        -Path (Join-Path $Stage "deploy\release_integrity.ps1") `
        -Contract $ReleaseIntegrityContract `
        -Label "Release-integrity staging helper"
    $artifactTestFiles = @("test_deployment_security.py", "test_xm_mt5_forward.py", "test_super1_xm_forward.py", "test_check_mt5_flat.py", "test_v16_deployment_contract.py")
    $artifactTestRoot = Join-Path $Stage "artifact_tests"
    New-Item -ItemType Directory -Force -Path $artifactTestRoot | Out-Null
    foreach ($testFile in $artifactTestFiles) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot (Join-Path "tests" $testFile)) -Destination (Join-Path $artifactTestRoot $testFile)
    }
    Copy-Item -LiteralPath (Join-Path $SourceRoot "tests\powershell_contract.py") -Destination (Join-Path $artifactTestRoot "powershell_contract.py")
    $baselineSource = Join-Path $SourceRoot "outputs\reports\engine_reliability_audit_2025_feb_mar\run_manifest.json"
    $baselineTarget = Join-Path $Stage "outputs\reports\engine_reliability_audit_2025_feb_mar"
    New-Item -ItemType Directory -Force -Path $baselineTarget | Out-Null
    Copy-Item -LiteralPath $baselineSource -Destination (Join-Path $baselineTarget "run_manifest.json")
    if ($Profile -eq "super1") {
        foreach ($relative in @(
            "research_candidates\super1",
            "research_candidates\v20_strategy_loop"
        )) {
            $target = Join-Path $Stage $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath (Join-Path $SourceRoot $relative) -Destination $target -Recurse
        }
        $candidateSource = Join-Path $SourceRoot "research_candidates\v20_strategy_loop\nq_spx_local_fresh_forward_candidate_v1.json"
        $candidatePayload = Get-Content -LiteralPath $candidateSource -Raw | ConvertFrom-Json
        $Super1ProvenanceFiles = @(
            $candidatePayload.provenance.inputs | ForEach-Object { [string]$_.path }
        )
        $sourcePrefix = $SourceRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        $stagePrefix = $Stage.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        foreach ($relative in $Super1ProvenanceFiles) {
            if (-not $relative -or [IO.Path]::IsPathRooted($relative)) {
                throw "Super1 provenance path is not repository-relative: $relative"
            }
            $nativeRelative = $relative.Replace("/", [IO.Path]::DirectorySeparatorChar)
            $provenanceSource = [IO.Path]::GetFullPath((Join-Path $SourceRoot $nativeRelative))
            $provenanceTarget = [IO.Path]::GetFullPath((Join-Path $Stage $nativeRelative))
            if (-not $provenanceSource.StartsWith($sourcePrefix) -or -not $provenanceTarget.StartsWith($stagePrefix)) {
                throw "Super1 provenance path escapes release roots: $relative"
            }
            if (-not (Test-Path -LiteralPath $provenanceSource -PathType Leaf)) {
                throw "Super1 provenance input is missing: $relative"
            }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $provenanceTarget) | Out-Null
            Copy-Item -LiteralPath $provenanceSource -Destination $provenanceTarget -Force
        }
    }

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
            "pandas==3.0.3" "MetaTrader5==5.0.6090" "setuptools==81.0.0" "wheel==0.48.0" "pytest==8.4.1"
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
    $artifactPytestCommand = "$artifactPython -m pytest -q artifact_tests/test_deployment_security.py artifact_tests/test_xm_mt5_forward.py artifact_tests/test_super1_xm_forward.py artifact_tests/test_check_mt5_flat.py artifact_tests/test_v16_deployment_contract.py --junitxml=<artifact-suite>"
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

    $requiredPayloadFiles = @(
        "backtest/__init__.py",
        "deploy/release_integrity.ps1",
        "deploy/release_integrity_contract.json",
        "deploy/watchdog_windows.ps1",
        "deploy/run_forward_shadow_windows.ps1",
        "forward_shadow/baseline_lock.json",
        "forward_shadow/frozen_config.json",
        "live_forward/capital_demo_config.json",
        "live_forward/xm_mt5_demo_config.json",
        "scripts/check_mt5_flat.py",
        "scripts/run_capital_forward.py",
        "scripts/run_forward_shadow.py",
        "scripts/run_xm_mt5_forward.py",
        "outputs/reports/engine_reliability_audit_2025_feb_mar/run_manifest.json",
        "pyproject.toml",
        "README.md",
        "requirements-windows.lock"
    )
    if ($Profile -eq "forward-shadow") {
        $requiredPayloadFiles += @(
            "deploy/check_forward_flat_windows.ps1",
            "deploy/forward-shadow.service",
            "deploy/rollover_forward_shadow_campaign_windows.ps1",
            "deploy/upgrade_forward_shadow_windows.ps1",
            "requirements-linux.lock"
        )
    }
    else {
        $requiredPayloadFiles += @(
            "deploy/check_super1_flat_windows.ps1",
            "deploy/rollover_super1_campaign_windows.ps1",
            "deploy/run_super1_windows.ps1",
            "deploy/run_super1_demo_smoke_windows.ps1",
            "deploy/stage_signed_upgrader_windows.ps1",
            "deploy/super1_secure_task.ps1",
            "deploy/upgrade_super1_signed_app_windows.ps1",
            "live_forward/super1_xm_mt5_demo_config.json",
            "live_forward/super1_xm_mt5_demo_config_v4.json",
            "research_candidates/super1/super1_manifest.json",
            "research_candidates/super1/super1_signal_contract.json",
            "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json",
            "scripts/run_super1_xm_mt5_forward.py"
        )
        $requiredPayloadFiles += $Super1ProvenanceFiles
    }
    foreach ($relative in $requiredPayloadFiles) {
        $nativeRelative = $relative.Replace("/", [IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath (Join-Path $Stage $nativeRelative) -PathType Leaf)) {
            throw "Release staging is incomplete; required file is missing: $relative"
        }
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
        Assert-ReleaseIntegrityZipEntry -Zip $zip -Contract $ReleaseIntegrityContract
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
        created_at_utc = $createdAtUtc
        built_at_utc = $createdAtUtc
        git_commit = $gitCommit
        git_dirty = $gitDirty
        python_version = $pythonVersion
        python_executable_sha256 = $pythonExeSha256
        pytest_command = $pytestCmd
        pytest_collect_command = $pytestCollectCmd
        pytest_symlink_fixture_root = $SymlinkFixtureRoot
        pytest_passed = $pytestPassed
        pytest_collected_count = $pytestCollectedCount
        pytest_pass_count = $pytestPassedCount
        pytest_skipped_count = $pytestSkippedCount
        pytest_nodeid_sha256 = $pytestNodeIdSha256
        pytest_passed_count = $pytestPassedCount
        dependencies = [ordered]@{
            pandas = "3.0.3"
            MetaTrader5 = "5.0.6090"
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
        release_integrity = [ordered]@{
            helper_path = [string]$ReleaseIntegrityContract.helper_path
            helper_sha256 = [string]$ReleaseIntegrityContract.sha256_lf
            contract_path = "deploy/release_integrity_contract.json"
            contract_sha256 = (Get-FileHash -LiteralPath (Join-Path $Stage "deploy\release_integrity_contract.json") -Algorithm SHA256).Hash.ToLowerInvariant()
        }
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
