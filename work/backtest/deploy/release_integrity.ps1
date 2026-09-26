$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security
$script:ReleaseIcaclsExe = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "icacls.exe")
)

$script:ReleasePublicKeyXml = '<RSAKeyValue><Modulus>7EwU71ALEK4mImTv6VuFypiTi6BIDlMs45LCz4ma1u5jTPgDafWv7FKeZSRGiIZk4W/oXhgzyOP+8MMmR0dYT5FwIZmEI7c+AbvPMjlKh7dceXbjpFJjTIow4TqxP2nCStol9EcoBYiLaJF3jpXr9ukaT0fjjAvu/SbAi2TkrdWdHPEhqcnagCknF15G5+cCy27NVH9hFxXkgVnTKM0nbnGqcR4efRFAhCgtSW8Oc1KJFP31WlL463GXvOWUdh80UDOGJN8GxrxCf7yXeqWoOFsvdPNH+hULWVJCXSr7NPYTaQ+wzi7kLH+uFj5dYYkLAqrExfc69SGpsL0jTPEjb0bD4oGwc13NaPoGaMt384zGIr2Ang186TotV5cWHSaBeE21Gng5QoB+G1tRIaS/V5D2Qy0SSyyWRkFcYV1Uk4uZZxH5oZX4FVgbLqN7YGTn8x0TMoNNps0DqzNGKvwSHjexMXRUaHdoHEpgS1f1e4+pVpg30kMNnWyvU639uISF</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>'
$script:ReleasePublicKeySha256 = "dd3522ee11d3543f9868e929986e1b1759814fef13ea86b9eeda1b3c808d89b9"

function Get-ReleaseSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ReleaseBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-ReleaseZipEntryBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$EntryPath
    )
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $entry = $zip.GetEntry($EntryPath)
        if ($null -eq $entry -or $entry.Length -le 0 -or $entry.Length -gt 16777216) {
            throw "Release ZIP entry is missing or exceeds the candidate metadata size limit: $EntryPath"
        }
        $source = $entry.Open()
        $buffer = [IO.MemoryStream]::new()
        try {
            $source.CopyTo($buffer)
            return ,$buffer.ToArray()
        }
        finally {
            $buffer.Dispose()
            $source.Dispose()
        }
    }
    finally { $zip.Dispose() }
}

function Assert-ReleaseArtifactTestFiles {
    param([Parameter(Mandatory = $true)][object]$Manifest)
    $expected = @(
        "test_deployment_security.py",
        "test_xm_mt5_forward.py",
        "test_super1_xm_forward.py",
        "test_check_mt5_flat.py",
        "test_v16_deployment_contract.py"
    )
    $propertyNames = @($Manifest.PSObject.Properties | ForEach-Object Name)
    if ($propertyNames -notcontains "artifact_test_files") {
        throw "Release manifest artifact_test_files is missing."
    }
    $actual = @($Manifest.artifact_test_files | ForEach-Object { [string]$_ })
    if ($actual.Count -ne $expected.Count -or (($actual -join "`n") -cne ($expected -join "`n"))) {
        throw "Release manifest artifact_test_files does not match the exact required list."
    }
}

function Assert-ManifestTestGate {
    param(
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][string]$Prefix
    )
    $required = @(
        "${Prefix}_collected_count",
        "${Prefix}_pass_count",
        "${Prefix}_skipped_count",
        "${Prefix}_nodeid_sha256"
    )
    $properties = @($Manifest.PSObject.Properties.Name)
    foreach ($name in $required) {
        if ($properties -notcontains $name) {
            throw "Release manifest test inventory field is missing: $name"
        }
    }
    $collected = [int]$Manifest.PSObject.Properties["${Prefix}_collected_count"].Value
    $passed = [int]$Manifest.PSObject.Properties["${Prefix}_pass_count"].Value
    $skipped = [int]$Manifest.PSObject.Properties["${Prefix}_skipped_count"].Value
    $nodeIdSha256 = [string]$Manifest.PSObject.Properties["${Prefix}_nodeid_sha256"].Value
    if ($collected -le 0 -or $passed -le 0 -or $passed -ne $collected -or
        $skipped -ne 0 -or $nodeIdSha256 -notmatch '^[A-Fa-f0-9]{64}$') {
        throw "Release manifest test inventory is incomplete or not all passing: $Prefix"
    }
}

function Assert-SignedReleaseArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [string]$ExpectedProfile,
        [string]$SourceRoot,
        [switch]$RequireProvenance
    )

    $archivePath = [IO.Path]::GetFullPath($Archive)
    $manifestPath = [IO.Path]::ChangeExtension($archivePath, ".manifest.json")
    $signaturePath = [IO.Path]::ChangeExtension($archivePath, ".manifest.sig")
    foreach ($required in @($archivePath, $manifestPath, $signaturePath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Missing signed release component: $required"
        }
    }

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $publicHash = ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:ReleasePublicKeyXml))
        )).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    if ($publicHash -ne $script:ReleasePublicKeySha256) {
        throw "Embedded release trust root is corrupt."
    }

    $manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
    $signature = [Convert]::FromBase64String((Get-Content -LiteralPath $signaturePath -Raw).Trim())
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    try {
        $rsa.FromXmlString($script:ReleasePublicKeyXml)
        $validSignature = $rsa.VerifyData(
            $manifestBytes,
            [Security.Cryptography.CryptoConfig]::MapNameToOID("SHA256"),
            $signature
        )
    }
    finally {
        $rsa.Dispose()
    }
    if (-not $validSignature) {
        throw "Release manifest signature validation failed."
    }

    $manifest = [Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json
    if ([int]$manifest.schema_version -ne 1) {
        throw "Unsupported release manifest schema: $($manifest.schema_version)"
    }
    if ([string]$manifest.archive_file -ne [IO.Path]::GetFileName($archivePath)) {
        throw "Release manifest names a different archive."
    }
    if ($ExpectedProfile -and [string]$manifest.profile -ne $ExpectedProfile) {
        throw "Release profile mismatch: expected=$ExpectedProfile actual=$($manifest.profile)"
    }
    $actualArchiveHash = Get-ReleaseSha256 -Path $archivePath
    if ($actualArchiveHash -ne ([string]$manifest.archive_sha256).ToLowerInvariant()) {
        throw "Release archive SHA-256 validation failed."
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    $archiveFilesMap = @{}
    $archiveFileSizesMap = @{}
    try {
        foreach ($entry in $zip.Entries) {
            $entryPath = $entry.FullName
            if ($entryPath -ne $entryPath.Replace("\", "/") -or
                [IO.Path]::IsPathRooted($entryPath) -or $entryPath -match '^[A-Za-z]:' -or
                $entryPath -match '(^|/)\.\.(/|$)' -or $entryPath -match '(^|/)\./|//') {
                throw "Archive contains a rooted, traversal, reversed, or non-normalized path: $entryPath"
            }
            $entryPath = $entryPath.Replace("\", "/")
            if ($entryPath.EndsWith("/")) { continue }
            if ($archiveFilesMap.ContainsKey($entryPath)) { throw "Archive contains duplicate ZIP entry: $entryPath" }
            $fileName = [IO.Path]::GetFileName($entryPath).ToLowerInvariant()
            if ($fileName -match '\.(key|pem|dpapi|pfx|cer|crt)$' -or
                $fileName -match '(credential|password|secret|\.env)') {
                throw "Forbidden credential or signing key file in archive: $entryPath"
            }
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
            $archiveFilesMap[$entryPath] = $entryHash
            $archiveFileSizesMap[$entryPath] = [long]$entry.Length
        }
    }
    finally {
        $zip.Dispose()
    }

    if ($RequireProvenance) {
        if ([string]::IsNullOrWhiteSpace([string]$manifest.release_id) -or
            [string]$manifest.git_commit -notmatch '^[A-Fa-f0-9]{40}$' -or
            [bool]$manifest.git_dirty -ne $false -or
            [string]$manifest.python_version -notmatch '^3\.11' -or
            [bool]$manifest.pytest_passed -ne $true -or
            $null -eq $manifest.files -or @($manifest.files).Count -eq 0) {
            throw "Release manifest provenance is incomplete or below the required test baseline."
        }
        if ([bool]$manifest.artifact_pytest_passed -ne $true) {
            throw "Release manifest artifact test gate is not passing."
        }
        Assert-ManifestTestGate -Manifest $manifest -Prefix "pytest"
        Assert-ManifestTestGate -Manifest $manifest -Prefix "artifact_pytest"
        Assert-ReleaseArtifactTestFiles -Manifest $manifest
        if ([string]$manifest.profile -ceq "super1") {
            $testInputs = $manifest.test_inputs
            $testInputPropertyNames = @()
            if ($null -ne $testInputs) { $testInputPropertyNames = @($testInputs.PSObject.Properties.Name) }
            if ($null -eq $testInputs -or
                [string]$testInputs.risk_manifest_path -cne "data/provenance/first30_pre2025_inputs.sha256" -or
                [int]$testInputs.risk_file_count -ne 144 -or
                [string]$testInputs.risk_manifest_sha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
                [string]$testInputs.risk_set_sha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
                [string]$testInputs.engine_audit_manifest_sha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
                [int]$testInputs.engine_audit_csv_count -le 0 -or
                $testInputPropertyNames -notcontains "super1_candidate_manifest_sha256" -or
                $testInputPropertyNames -notcontains "super1_candidate_provenance_files") {
                throw "Release manifest hash-pinned test input evidence is incomplete."
            }
            $riskFiles = @($testInputs.risk_files)
            if ($riskFiles.Count -ne 144) { throw "Release manifest must bind all 144 hash-pinned risk test inputs." }
            $seenRiskPaths = @{}
            foreach ($riskFile in $riskFiles) {
                $riskPath = [string]$riskFile.path
                if ($riskPath -notmatch '^data/raw/(nq|spx)/DUKASCOPY_[^\r\n]+\.csv$' -or
                    [string]$riskFile.sha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
                    $seenRiskPaths.ContainsKey($riskPath)) {
                    throw "Release manifest contains an invalid or duplicate pinned risk input: $riskPath"
                }
                $seenRiskPaths[$riskPath] = [string]$riskFile.sha256
            }
            $canonicalRiskSet = @(
                $riskFiles | Sort-Object -Property path -CaseSensitive -Culture en-US | ForEach-Object {
                    "$([string]$_.path) $(([string]$_.sha256).ToLowerInvariant())"
                }
            ) -join "`n"
            $riskSetBytes = (New-Object Text.UTF8Encoding($false)).GetBytes($canonicalRiskSet + "`n")
            if ((Get-ReleaseBytesSha256 -Bytes $riskSetBytes) -cne ([string]$testInputs.risk_set_sha256).ToLowerInvariant()) {
                throw "Release manifest pinned risk input set hash is invalid."
            }
            $engineAuditFiles = @($testInputs.engine_audit_files)
            if ([int]$testInputs.engine_audit_csv_count -le 0 -or
                $engineAuditFiles.Count -ne [int]$testInputs.engine_audit_csv_count -or
                [string]$testInputs.engine_audit_set_sha256 -notmatch '^[A-Fa-f0-9]{64}$') {
                throw "Release manifest engine-audit input evidence is incomplete."
            }
            $seenEngineAuditPaths = @{}
            foreach ($auditFile in $engineAuditFiles) {
                $auditPath = [string]$auditFile.path
                if ($auditPath -notmatch '^(nq|spx)/DUKASCOPY_[^\\/:]+\.csv$' -or
                    [string]$auditFile.sha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
                    [long]$auditFile.bytes -le 0 -or
                    $seenEngineAuditPaths.ContainsKey($auditPath)) {
                    throw "Release manifest contains an invalid or duplicate engine-audit input: $auditPath"
                }
                $seenEngineAuditPaths[$auditPath] = [string]$auditFile.sha256
            }
            $canonicalEngineAuditSet = @(
                $engineAuditFiles | Sort-Object -Property path -CaseSensitive -Culture en-US | ForEach-Object {
                    "$([string]$_.path) $(([string]$_.sha256).ToLowerInvariant())"
                }
            ) -join "`n"
            $engineAuditSetBytes = (New-Object Text.UTF8Encoding($false)).GetBytes($canonicalEngineAuditSet + "`n")
            if ((Get-ReleaseBytesSha256 -Bytes $engineAuditSetBytes) -cne ([string]$testInputs.engine_audit_set_sha256).ToLowerInvariant()) {
                throw "Release manifest engine-audit input set hash is invalid."
            }

            $candidateRelative = "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json"
            $candidateSha256 = [string]$testInputs.super1_candidate_manifest_sha256
            $candidateFiles = @($testInputs.super1_candidate_provenance_files)
            if ($candidateSha256 -notmatch '^[A-Fa-f0-9]{64}$' -or $candidateFiles.Count -eq 0) {
                throw "Release manifest Super1 candidate provenance evidence is incomplete."
            }
            if (-not $archiveFilesMap.ContainsKey($candidateRelative) -or
                $archiveFilesMap[$candidateRelative] -cne $candidateSha256.ToLowerInvariant()) {
                throw "Release manifest candidate manifest is not bound to the signed archive."
            }
            $candidateBytes = Get-ReleaseZipEntryBytes -Archive $archivePath -EntryPath $candidateRelative
            if ((Get-ReleaseBytesSha256 -Bytes $candidateBytes) -cne $candidateSha256.ToLowerInvariant()) {
                throw "Release manifest candidate manifest hash does not match archived bytes."
            }
            $candidatePayload = [Text.Encoding]::UTF8.GetString($candidateBytes) | ConvertFrom-Json
            $declaredCandidateFiles = @($candidatePayload.provenance.inputs)
            if ($declaredCandidateFiles.Count -eq 0 -or $declaredCandidateFiles.Count -ne $candidateFiles.Count) {
                throw "Release manifest candidate provenance list does not match the archived candidate."
            }
            $candidatePathSeen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
            $declaredCandidateRecords = @()
            foreach ($candidateFile in $declaredCandidateFiles) {
                $candidatePath = [string]$candidateFile.path
                $candidateFileSha256 = ([string]$candidateFile.sha256).ToLowerInvariant()
                $candidateFileBytes = [long]$candidateFile.bytes
                if ([string]::IsNullOrWhiteSpace($candidatePath) -or
                    $candidatePath -ne $candidatePath.Replace("\", "/") -or
                    [IO.Path]::IsPathRooted($candidatePath) -or $candidatePath -match '^[A-Za-z]:' -or
                    $candidatePath -match '(^|/)\.\.(/|$)|(^|/)\./|//' -or
                    $candidateFileSha256 -notmatch '^[a-f0-9]{64}$' -or $candidateFileBytes -le 0 -or
                    -not $candidatePathSeen.Add($candidatePath)) {
                    throw "Archived candidate contains an invalid or duplicate provenance input: $candidatePath"
                }
                if (-not $archiveFilesMap.ContainsKey($candidatePath) -or
                    $archiveFilesMap[$candidatePath] -cne $candidateFileSha256 -or
                    -not $archiveFileSizesMap.ContainsKey($candidatePath) -or
                    [long]$archiveFileSizesMap[$candidatePath] -ne $candidateFileBytes) {
                    throw "Archived candidate provenance input is not bound to its exact archive bytes: $candidatePath"
                }
                $declaredCandidateRecords += [pscustomobject]@{
                    path = $candidatePath
                    sha256 = $candidateFileSha256
                    bytes = $candidateFileBytes
                }
            }
            $testCandidatePathSeen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
            $testCandidateRecords = @()
            foreach ($candidateFile in $candidateFiles) {
                $candidatePath = [string]$candidateFile.path
                $candidateFileSha256 = ([string]$candidateFile.sha256).ToLowerInvariant()
                $candidateFileBytes = [long]$candidateFile.bytes
                if ([string]::IsNullOrWhiteSpace($candidatePath) -or
                    $candidateFileSha256 -notmatch '^[a-f0-9]{64}$' -or $candidateFileBytes -le 0 -or
                    -not $testCandidatePathSeen.Add($candidatePath)) {
                    throw "Release manifest contains an invalid or duplicate candidate provenance input: $candidatePath"
                }
                $testCandidateRecords += [pscustomobject]@{
                    path = $candidatePath
                    sha256 = $candidateFileSha256
                    bytes = $candidateFileBytes
                }
            }
            $canonicalDeclaredCandidateRecords = @(
                $declaredCandidateRecords | Sort-Object -Property path -CaseSensitive -Culture en-US | ForEach-Object {
                    "$([string]$_.path) $([string]$_.sha256) $([long]$_.bytes)"
                }
            ) -join "`n"
            $canonicalTestCandidateRecords = @(
                $testCandidateRecords | Sort-Object -Property path -CaseSensitive -Culture en-US | ForEach-Object {
                    "$([string]$_.path) $([string]$_.sha256) $([long]$_.bytes)"
                }
            ) -join "`n"
            if ($canonicalDeclaredCandidateRecords -cne $canonicalTestCandidateRecords) {
                throw "Release manifest candidate provenance records do not match the archived candidate input declaration."
            }
        }
        $seenManifestPaths = @{}
        foreach ($f in @($manifest.files)) {
            $path = [string]$f.path
            if ([string]::IsNullOrWhiteSpace($path) -or $path -ne $path.Replace("\", "/") -or
                [IO.Path]::IsPathRooted($path) -or $path -match '^[A-Za-z]:' -or $path -match '(^|/)\.\.(/|$)' -or
                $path -match '(^|/)\./|//' -or $seenManifestPaths.ContainsKey($path)) {
                throw "Release manifest contains a duplicate or non-normalized file path: $path"
            }
            $seenManifestPaths[$path] = $true
        }
    }

    if ($manifest.files) {
        $manifestFilesMap = @{}
        foreach ($f in $manifest.files) {
            $manifestFilesMap[[string]$f.path] = ([string]$f.sha256).ToLowerInvariant()
        }
        foreach ($path in $manifestFilesMap.Keys) {
            if (-not $archiveFilesMap.ContainsKey($path)) {
                throw "Archive is missing manifest file entry: $path"
            }
            if ($archiveFilesMap[$path] -ne $manifestFilesMap[$path]) {
                throw "Archive file entry SHA-256 mismatch for $path"
            }
        }
        foreach ($path in $archiveFilesMap.Keys) {
            if (-not $manifestFilesMap.ContainsKey($path)) {
                throw "Archive contains unexpected entry not in manifest: $path"
            }
        }
    }

    if ($SourceRoot) {
        Assert-ReleaseSourceIntegrity `
            -ArchiveFilesMap $archiveFilesMap `
            -SourceRoot $SourceRoot `
            -Profile ([string]$manifest.profile) `
            -TestInputs $manifest.test_inputs
    }

    return $manifest
}

function Assert-ReleaseSourceIntegrity {
    param(
        [Parameter(Mandatory = $true)][hashtable]$ArchiveFilesMap,
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$Profile,
        [object]$TestInputs
    )
    $resolvedSource = [IO.Path]::GetFullPath($SourceRoot)
    if (-not (Test-Path -LiteralPath $resolvedSource -PathType Container)) {
        throw "SourceRoot directory does not exist: $resolvedSource"
    }

    $sourceDirs = @("backtest", "deploy", "forward_shadow", "live_forward", "scripts")
    $sourceFiles = @("pyproject.toml", "README.md")
    $sourceFiles += "outputs/reports/engine_reliability_audit_2025_feb_mar/run_manifest.json"
    $sourceProdFiles = @{}

    if ($Profile -eq "super1") {
        $riskManifestPath = Join-Path $resolvedSource "data\provenance\first30_pre2025_inputs.sha256"
        if ($null -eq $TestInputs -or -not (Test-Path -LiteralPath $riskManifestPath -PathType Leaf) -or
            (Get-ReleaseSha256 -Path $riskManifestPath) -cne ([string]$TestInputs.risk_manifest_sha256).ToLowerInvariant()) {
            throw "Signed release risk input manifest does not match the inspected source contract."
        }
        $expectedPinnedPaths = @($TestInputs.risk_files | ForEach-Object { [string]$_.path } | Sort-Object -CaseSensitive -Culture en-US)
        foreach ($riskFile in @($TestInputs.risk_files)) {
            $rawInput = Join-Path $resolvedSource ([string]$riskFile.path.Replace("/", [IO.Path]::DirectorySeparatorChar))
            if (Test-Path -LiteralPath $rawInput -PathType Leaf) {
                if ((Get-Item -LiteralPath $rawInput -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw "Pinned risk test input is reparse-backed: $($riskFile.path)"
                }
                if ((Get-ReleaseSha256 -Path $rawInput) -cne ([string]$riskFile.sha256).ToLowerInvariant()) {
                    throw "Pinned risk test input differs from its signed manifest record: $($riskFile.path)"
                }
            }
        }
        $sourceRawRoot = Join-Path $resolvedSource "data\raw"
        if (Test-Path -LiteralPath $sourceRawRoot -PathType Container) {
            $actualPinnedPaths = @(
                foreach ($leg in @("nq", "spx")) {
                    $legRoot = Join-Path $sourceRawRoot $leg
                    if (Test-Path -LiteralPath $legRoot -PathType Container) {
                        Get-ChildItem -LiteralPath $legRoot -File -Filter "DUKASCOPY_*.csv" |
                            ForEach-Object { "data/raw/$leg/$($_.Name)" }
                    }
                }
            ) | Sort-Object -CaseSensitive -Culture en-US
            if ($actualPinnedPaths.Count -ne 144 -or
                ($actualPinnedPaths -join "`n") -cne ($expectedPinnedPaths -join "`n")) {
                throw "SourceRoot data/raw does not contain the exact 144 signed risk test inputs."
            }
        }
        $versionedRuntimeConfig = Join-Path $resolvedSource "live_forward\super1_xm_mt5_demo_config_v4.json"
        if (Test-Path -LiteralPath $versionedRuntimeConfig -PathType Leaf) {
            $sourceProdFiles["live_forward/super1_xm_mt5_demo_config.json"] = Get-ReleaseSha256 -Path $versionedRuntimeConfig
        }
        $sourceDirs += @(
            "research_candidates/super1",
            "research_candidates/v20_strategy_loop"
        )
        $candJson = Join-Path $resolvedSource "research_candidates\v20_strategy_loop\nq_spx_local_fresh_forward_candidate_v1.json"
        if (Test-Path -LiteralPath $candJson) {
            $payload = Get-Content -LiteralPath $candJson -Raw | ConvertFrom-Json
            if ($payload.provenance -and $payload.provenance.inputs) {
                foreach ($inp in $payload.provenance.inputs) {
                    $sourceFiles += [string]$inp.path
                }
            }
        }
    }

    foreach ($dir in $sourceDirs) {
        $fullDir = Join-Path $resolvedSource ($dir.Replace("/", [IO.Path]::DirectorySeparatorChar))
        if (Test-Path -LiteralPath $fullDir) {
            foreach ($file in (Get-ChildItem -LiteralPath $fullDir -Recurse -File)) {
                if ($file.Extension -in @(".pyc", ".pyo", ".tmp", ".bak", ".pem", ".key", ".dpapi", ".pfx", ".cer", ".crt") -or
                    $file.Name -match '(credential|password|secret|\.env)' -or
                    $file.FullName -match '[\\/](__pycache__|\.pytest_cache|\.git|\.venv)[\\/]') {
                    continue
                }
                $rel = $file.FullName.Substring($resolvedSource.Length).TrimStart('\', '/').Replace("\", "/")
                $sourceProdFiles[$rel] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    }
    foreach ($f in $sourceFiles) {
        $fullFile = Join-Path $resolvedSource ($f.Replace("/", [IO.Path]::DirectorySeparatorChar))
        if (Test-Path -LiteralPath $fullFile) {
            $rel = $f.Replace("\", "/")
            $sourceProdFiles[$rel] = (Get-FileHash -LiteralPath $fullFile -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }

    foreach ($rel in $sourceProdFiles.Keys) {
        if (-not $ArchiveFilesMap.ContainsKey($rel)) {
            throw "Production source file missing from release archive: $rel"
        }
        if ($ArchiveFilesMap[$rel] -ne $sourceProdFiles[$rel]) {
            throw "Production file content mismatch between source and release archive: $rel"
        }
    }

    foreach ($rel in $ArchiveFilesMap.Keys) {
        if ($rel -match '^wheelhouse' -or $rel -match '^requirements-.*\.lock$') {
            continue
        }
        if (-not $sourceProdFiles.ContainsKey($rel)) {
            throw "Release archive contains unexpected production entry not present in source tree: $rel"
        }
        if ($ArchiveFilesMap[$rel] -ne $sourceProdFiles[$rel]) {
            throw "Release archive entry mismatch with source tree: $rel"
        }
    }
}

function Install-LockedRelease {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$App
    )

    $lock = Join-Path $App "requirements-windows.lock"
    $wheelhouse = Join-Path $App "wheelhouse"
    if (-not (Test-Path -LiteralPath $lock -PathType Leaf) -or
        -not (Test-Path -LiteralPath $wheelhouse -PathType Container)) {
        throw "Signed release has no locked offline wheelhouse."
    }
    Push-Location -LiteralPath $App
    $previousPythonHome = $env:PYTHONHOME
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONHOME = $null
        $env:PYTHONPATH = $null
        & $Python -I -E -B -m pip install --disable-pip-version-check --no-index --require-hashes `
            --find-links $wheelhouse -r "requirements-windows.lock"
        if ($LASTEXITCODE -ne 0) { throw "Locked dependency installation failed." }
        & $Python -I -E -B -m pip install --disable-pip-version-check --no-index --no-deps `
            --no-build-isolation "."
        if ($LASTEXITCODE -ne 0) { throw "Application installation failed." }
    }
    finally {
        $env:PYTHONHOME = $previousPythonHome
        $env:PYTHONPATH = $previousPythonPath
        Pop-Location
    }
}

function Protect-ReleaseApp {
    param(
        [Parameter(Mandatory = $true)][string]$App,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity
    )
    $resolved = [IO.Path]::GetFullPath($App)
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Application ACL target is not a directory: $resolved"
    }
    if ((Get-Item -LiteralPath $resolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Application ACL root must not be a reparse point: $resolved"
    }
    $reparsePoint = Get-ChildItem -LiteralPath $resolved -Recurse -Force |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
        Select-Object -First 1
    if ($reparsePoint) {
        throw "Application tree contains a reparse point: $($reparsePoint.FullName)"
    }

    $callerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($RunnerIdentity -match '^S-\d-(?:\d+-)+\d+$') {
        $runnerSid = [Security.Principal.SecurityIdentifier]::new($RunnerIdentity).Value
    }
    else {
        $runnerSid = ([Security.Principal.NTAccount]$RunnerIdentity).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    $fullControlSids = @("S-1-5-18", "S-1-5-32-544", $callerSid) |
        Select-Object -Unique
    $rootAcl = New-Object Security.AccessControl.DirectorySecurity
    $rootAcl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($sid in $fullControlSids) {
        [void]$rootAcl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new($sid),
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    if ($runnerSid -notin $fullControlSids) {
        [void]$rootAcl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new($runnerSid),
                [Security.AccessControl.FileSystemRights]::ReadAndExecute,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    [IO.Directory]::SetAccessControl($resolved, $rootAcl)

    if (@(Get-ChildItem -LiteralPath $resolved -Force).Count -ne 0) {
        & $script:ReleaseIcaclsExe (Join-Path $resolved "*") /reset /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not inherit protected application ACLs to descendants."
        }
    }
    & $script:ReleaseIcaclsExe $resolved /setowner "*S-1-5-18" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set trusted application ownership." }
    & $script:ReleaseIcaclsExe $resolved /verify /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Application ACL verification failed." }

    $ownerSamples = @((Get-Item -LiteralPath $resolved -Force))
    $ownerSamples += @(
        Get-ChildItem -LiteralPath $resolved -Recurse -Force |
            Select-Object -First 2
    )
    foreach ($sample in $ownerSamples) {
        $owner = (Get-Acl -LiteralPath $sample.FullName).Owner
        $ownerSid = if ($owner -match '^S-\d-(?:\d+-)+\d+$') {
            [Security.Principal.SecurityIdentifier]::new($owner).Value
        }
        else {
            ([Security.Principal.NTAccount]$owner).Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        if ($ownerSid -ne "S-1-5-18") {
            throw "Application tree ownership verification failed: $($sample.FullName)"
        }
    }

    # Some installers intentionally run the service as the installing account.
    # Use caller FullControl only while sealing the tree, then leave that runtime
    # SID with read/execute so the installed app remains immutable.
    if (
        $callerSid -eq $runnerSid -and
        $callerSid -notin @("S-1-5-18", "S-1-5-32-544")
    ) {
        & $script:ReleaseIcaclsExe $resolved /grant:r "*$($callerSid):(OI)(CI)RX" /Q | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not downgrade the runtime caller to read/execute."
        }
        & $script:ReleaseIcaclsExe $resolved /verify /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Runtime read/execute ACL verification failed."
        }
    }

    $expectedFullSids = @("S-1-5-18", "S-1-5-32-544")
    if ($callerSid -ne $runnerSid) { $expectedFullSids += $callerSid }
    $expectedFullSids = @($expectedFullSids | Select-Object -Unique)
    $expectedReadSids = @()
    if ($runnerSid -notin $expectedFullSids) { $expectedReadSids += $runnerSid }
    $expectedSids = @($expectedFullSids + $expectedReadSids | Select-Object -Unique)
    $rightsBySid = @{}
    $requiredInheritance = [int](
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    $finalRootAcl = Get-Acl -LiteralPath $resolved
    if (-not $finalRootAcl.AreAccessRulesProtected) {
        throw "Application root ACL inheritance is not protected."
    }
    foreach ($rule in $finalRootAcl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )) {
        $sid = [string]$rule.IdentityReference.Value
        if (
            $rule.IsInherited -or
            [int]$rule.InheritanceFlags -ne $requiredInheritance -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $sid -notin $expectedSids
        ) {
            throw "Application root ACL contains an unexpected rule: sid=$sid"
        }
        $currentRights = if ($rightsBySid.ContainsKey($sid)) {
            [int]$rightsBySid[$sid]
        } else { 0 }
        $rightsBySid[$sid] = $currentRights -bor [int]$rule.FileSystemRights
    }
    $full = [int][Security.AccessControl.FileSystemRights]::FullControl
    foreach ($sid in $expectedFullSids) {
        if (([int]$rightsBySid[$sid] -band $full) -ne $full) {
            throw "Application root ACL is missing trusted FullControl: sid=$sid"
        }
    }
    $readExecute = [int][Security.AccessControl.FileSystemRights]::ReadAndExecute
    $mutation = [int](
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    foreach ($sid in $expectedReadSids) {
        $rights = [int]$rightsBySid[$sid]
        if (
            ($rights -band $readExecute) -ne $readExecute -or
            ($rights -band $mutation) -ne 0
        ) {
            throw "Application root runtime ACL is not read/execute-only: sid=$sid"
        }
    }
    if (@($rightsBySid.Keys).Count -ne $expectedSids.Count) {
        throw "Application root ACL contains duplicate or missing trusted rules."
    }
}
