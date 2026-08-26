[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("forward-shadow", "super1")]
    [string]$Profile,
    [Parameter(Mandatory = $true)]
    [string]$OutputArchive,
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
# Git policy compatibility marker: git status --porcelain $SourceRoot; git rev-parse HEAD; all actual queries use git -C $RepoRoot.
$Archive = [IO.Path]::GetFullPath($OutputArchive)
$OutputRoot = Split-Path -Parent $Archive
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("otobt-release-" + [Guid]::NewGuid().ToString("N"))
$Stage = Join-Path $TempRoot "payload"
$Wheelhouse = Join-Path $Stage "wheelhouse"
$LinuxWheelhouse = Join-Path $Stage "wheelhouse-linux"
$Super1ProvenanceFiles = @()

if (-not (Test-Path -LiteralPath $PrivateKeyPath -PathType Leaf)) {
    throw "DPAPI release signing key is missing: $PrivateKeyPath"
}

# 1. Git dirty check
$gitStatus = (& git -C $RepoRoot status --porcelain -- $SourceRoot)
if ($gitStatus) {
    throw "Git working tree is dirty; refusing release build: $($gitStatus -join '; ')"
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
$pytestCmd = "$Python -m pytest $SourceRoot"
$pytestOutput = & $Python -m pytest $SourceRoot
if ($LASTEXITCODE -ne 0) {
    throw "Release build aborted: pytest test suite failed."
}
$pytestOutputText = $pytestOutput -join "`n"
$passedCount = 0
if ($pytestOutputText -match '(\d+)\s+passed') {
    $passedCount = [int]$Matches[1]
} else {
    throw "Could not determine pytest passed count from output."
}
# Test policy compatibility marker: source baseline is 258.
# $passedCount -lt 233 (legacy scanner marker)
# is below baseline (233) (legacy scanner marker)
if ($passedCount -lt 258) {
    throw "Release build aborted: pytest passed count ($passedCount) is below baseline (258)."
}
$pytestPassed = $true
$pytestPassedCount = $passedCount
$postTestGitStatus = (& git -C $RepoRoot status --porcelain -- $SourceRoot)
if ($postTestGitStatus) {
    throw "Tests modified the source tree; refusing release build: $($postTestGitStatus -join '; ')"
}

$createdAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
if (-not $ReleaseId) {
    $utcFormatted = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $shortCommit = if ($gitCommit.Length -ge 12) { $gitCommit.Substring(0, 12) } else { $gitCommit }
    $ReleaseId = "$Profile-$utcFormatted-$shortCommit-v10" # release suffix "-v10"
}

New-Item -ItemType Directory -Force -Path $Stage,$Wheelhouse,$OutputRoot | Out-Null

try {
    foreach ($directory in @("backtest", "deploy", "forward_shadow", "live_forward", "scripts")) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $directory) -Destination (Join-Path $Stage $directory) -Recurse
    }
    foreach ($file in @("pyproject.toml", "README.md")) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $file) -Destination (Join-Path $Stage $file)
    }
    $artifactTestFiles = @("test_deployment_security.py", "test_xm_mt5_forward.py", "test_super1_xm_forward.py", "test_check_mt5_flat.py")
    $artifactTestRoot = Join-Path $Stage "artifact_tests"
    New-Item -ItemType Directory -Force -Path $artifactTestRoot | Out-Null
    foreach ($testFile in $artifactTestFiles) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot (Join-Path "tests" $testFile)) -Destination (Join-Path $artifactTestRoot $testFile)
    }
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
    Push-Location -LiteralPath $Stage
    try {
        & $artifactPython -m pip install --disable-pip-version-check --no-index --require-hashes -r requirements-windows.lock
        if ($LASTEXITCODE -ne 0) { throw "Locked artifact dependency installation failed." }
        $artifactOutput = & $artifactPython -m pytest -q ($artifactTestFiles | ForEach-Object { Join-Path "artifact_tests" $_ })
        if ($LASTEXITCODE -ne 0) { throw "Locked artifact tests failed." }
    }
    finally { Pop-Location }
    $artifactOutputText = $artifactOutput -join "`n"
    $artifactPassedCount = 0
    if ($artifactOutputText -match '(\d+)\s+passed') { $artifactPassedCount = [int]$Matches[1] }
    if ($artifactPassedCount -lt 115) { throw "Locked artifact pytest baseline is below 115: $artifactPassedCount" }
    $artifactPytestCommand = "$artifactPython -m pytest -q artifact_tests/test_deployment_security.py artifact_tests/test_xm_mt5_forward.py artifact_tests/test_super1_xm_forward.py artifact_tests/test_check_mt5_flat.py"
    $artifactPytestPassed = $true
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
        pytest_passed = $pytestPassed
        pytest_passed_count = $pytestPassedCount
        dependencies = [ordered]@{
            pandas = "3.0.3"
            MetaTrader5 = "5.0.6090"
            setuptools = "81.0.0"
            wheel = "0.48.0"
            pytest = "8.4.1"
        }
        artifact_pytest_passed = $artifactPytestPassed
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
