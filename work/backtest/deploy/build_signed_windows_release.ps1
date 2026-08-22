[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("forward-shadow", "super1")]
    [string]$Profile,
    [Parameter(Mandatory = $true)]
    [string]$OutputArchive,
    [string]$Python = "python",
    [string]$PrivateKeyPath = (Join-Path $env:LOCALAPPDATA "OtoBacktest\release-private-key.dpapi")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.IO.Compression.FileSystem

$SourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
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
New-Item -ItemType Directory -Force -Path $Stage,$Wheelhouse,$OutputRoot | Out-Null

try {
    foreach ($directory in @("backtest", "deploy", "forward_shadow", "live_forward", "scripts")) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $directory) -Destination (Join-Path $Stage $directory) -Recurse
    }
    foreach ($file in @("pyproject.toml", "README.md")) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $file) -Destination (Join-Path $Stage $file)
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

    & $Python -m pip download --disable-pip-version-check --only-binary=:all: `
        --platform win_amd64 --python-version 311 --implementation cp --abi cp311 `
        --dest $Wheelhouse `
        "pandas==3.0.3" "MetaTrader5==5.0.6090" "setuptools==81.0.0" "wheel==0.48.0"
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
    Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Archive -CompressionLevel Optimal
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $archiveEntries = @($zip.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        foreach ($relative in $requiredPayloadFiles) {
            if ($relative -notin $archiveEntries) {
                throw "Release archive is incomplete; required file is missing: $relative"
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
        profile = $Profile
        archive_file = [IO.Path]::GetFileName($Archive)
        archive_sha256 = $archiveHash
        built_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        python = "3.11"
        dependencies = [ordered]@{
            pandas = "3.0.3"
            MetaTrader5 = "5.0.6090"
            setuptools = "81.0.0"
            wheel = "0.48.0"
        }
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
