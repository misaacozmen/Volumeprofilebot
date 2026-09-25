[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ReleaseDirectory,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$TargetRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ExpectedReleaseId,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedArchiveSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^S-1-5-(?:\d+-)*\d+$')][string]$ReadOnlyUserSid,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$BootstrapPython,
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.IO.Compression.FileSystem

$ReleaseDirectory = [IO.Path]::GetFullPath($ReleaseDirectory)
$TargetRoot = [IO.Path]::GetFullPath($TargetRoot)
$BootstrapPython = [IO.Path]::GetFullPath($BootstrapPython)
$Archive = Join-Path $ReleaseDirectory "super1-forward.zip"
$ManifestPath = Join-Path $ReleaseDirectory "super1-forward.manifest.json"
$SignaturePath = Join-Path $ReleaseDirectory "super1-forward.manifest.sig"
$IntegrityHelper = Join-Path $PSScriptRoot "release_integrity.ps1"
if (-not (Test-Path -LiteralPath $IntegrityHelper -PathType Leaf)) {
    throw "Signed release integrity helper is missing from the inspected deploy source."
}
$ExpectedIntegrityScriptSha256 = "6e7dae16e7238fb75cbe81d9d614647d3530cd83a67cc9b42c80c580d7c98b27"
$integrityBytes = [IO.File]::ReadAllBytes($IntegrityHelper)
$integrityHasher = [Security.Cryptography.SHA256]::Create()
try { $integritySha256 = ([BitConverter]::ToString($integrityHasher.ComputeHash($integrityBytes))).Replace("-", "").ToLowerInvariant() }
finally { $integrityHasher.Dispose() }
if ($integritySha256 -cne $ExpectedIntegrityScriptSha256) {
    throw "Inspected signed-release helper does not match this installer's reviewed byte pin."
}
. ([scriptblock]::Create([Text.Encoding]::UTF8.GetString($integrityBytes)))

function Assert-NotReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Inert install path may not be a reparse point: $Path"
    }
}

function Assert-TrustedInstallParent {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$UserSid)
    $parent = Split-Path -Parent $Path
    Assert-NotReparsePoint -Path $parent
    $trustedSids = @("S-1-5-18", "S-1-5-32-544", $UserSid)
    $acl = Get-Acl -LiteralPath $parent
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -notin $trustedSids) { throw "Inert install parent owner is not trusted: $ownerSid" }
    $deleteChild = [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
    foreach ($rule in @($acl.Access)) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $trustedSids -and
            ($rule.FileSystemRights -band $deleteChild) -eq $deleteChild) {
            throw "Untrusted SID can delete the inert install root through its parent: $sid"
        }
    }
}

function Set-InertInstallDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$UserSid)
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new("S-1-5-18"))
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($grant in @(
        @{ sid = "S-1-5-18"; rights = [Security.AccessControl.FileSystemRights]::FullControl },
        @{ sid = "S-1-5-32-544"; rights = [Security.AccessControl.FileSystemRights]::FullControl },
        @{ sid = $UserSid; rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute }
    )) {
        [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new([string]$grant.sid),
            [Security.AccessControl.FileSystemRights]$grant.rights,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Expand-VerifiedArchive {
    param([Parameter(Mandatory = $true)][string]$ArchivePath, [Parameter(Mandatory = $true)][string]$Destination)
    $destinationRoot = [IO.Path]::GetFullPath($Destination).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $zip = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        foreach ($entry in $zip.Entries) {
            $relative = [string]$entry.FullName
            if (-not $relative -or $relative -ne $relative.Replace("\", "/") -or
                [IO.Path]::IsPathRooted($relative) -or $relative -match '^[A-Za-z]:' -or
                $relative -match '(^|/)\.\.?(/|$)' -or $relative.Contains(":") -or
                $relative -match '(^|/)//') {
                throw "Signed archive contains a non-normalized or unsafe app entry: $relative"
            }
            $target = [IO.Path]::GetFullPath((Join-Path $Destination ($relative.Replace("/", [IO.Path]::DirectorySeparatorChar))))
            if (-not $target.StartsWith($destinationRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Signed archive entry escapes the inert app destination: $relative"
            }
            if ($relative.EndsWith("/")) {
                New-Item -ItemType Directory -Force -Path $target | Out-Null
                continue
            }
            $parent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
                New-Item -ItemType Directory -Force -Path $parent | Out-Null
            }
            $input = $entry.Open()
            $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $input.CopyTo($output) }
            finally { $output.Dispose(); $input.Dispose() }
        }
    }
    finally { $zip.Dispose() }
}

function Assert-ExtractedReleaseMatchesManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object]$Manifest,
        [switch]$AllowBuildArtifacts
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $expected = @{}
    foreach ($entry in @($Manifest.files)) { $expected[[string]$entry.path] = ([string]$entry.sha256).ToLowerInvariant() }
    $actual = @{}
    foreach ($item in @(Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File -Force -ErrorAction Stop)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Extracted release contains a reparse point: $($item.FullName)" }
        $relative = $item.FullName.Substring($resolvedRoot.Length).TrimStart('\', '/').Replace('\', '/')
        $actual[$relative] = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $expectedPaths = @($expected.Keys | Sort-Object -CaseSensitive -Culture en-US)
    $actualPaths = @($actual.Keys | Sort-Object -CaseSensitive -Culture en-US)
    $unexpectedPaths = @($actualPaths | Where-Object {
        -not $expected.ContainsKey([string]$_) -and
        (-not $AllowBuildArtifacts -or [string]$_ -notmatch '(^|/)([^/]+\.egg-info|__pycache__|build|dist)(/|$)')
    })
    $missingExpected = @($expectedPaths | Where-Object { -not $actual.ContainsKey([string]$_) })
    if ($missingExpected.Count -gt 0 -or $unexpectedPaths.Count -gt 0) {
        throw "Extracted release file set differs from its signed archive manifest."
    }
    foreach ($path in $expectedPaths) {
        if ($actual[$path] -cne $expected[$path]) { throw "Extracted/installed release bytes differ from signed manifest: $path" }
    }
}

function Open-InertReleaseInputLocks {
    param([Parameter(Mandatory = $true)][string[]]$Paths)
    $locks = [Collections.Generic.List[object]]::new()
    try {
        foreach ($path in $Paths) {
            $stream = [IO.File]::Open(
                $path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::Read
            )
            $locks.Add([pscustomobject]@{ path = $path; stream = $stream })
        }
        return ,$locks
    }
    catch {
        foreach ($lock in $locks) { $lock.stream.Dispose() }
        throw
    }
}

function Restore-InertTargetAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Sddl)
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $acl.SetSecurityDescriptorSddlForm($Sddl)
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Move-InertInstallFailureArtifacts {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$OwnedPaths,
        [Parameter(Mandatory = $true)][string]$OriginalSddl
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $parent = Split-Path -Parent $resolvedRoot
    Assert-NotReparsePoint -Path $resolvedRoot
    Assert-NotReparsePoint -Path $parent
    $failureRoot = Join-Path $parent (".super1-inert-failed-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $failureRoot -ErrorAction Stop | Out-Null
    foreach ($ownedPath in $OwnedPaths) {
        $resolvedOwned = [IO.Path]::GetFullPath($ownedPath)
        if (-not $resolvedOwned.StartsWith($resolvedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Rollback source is outside the inert install root: $resolvedOwned"
        }
        if (-not (Test-Path -LiteralPath $resolvedOwned)) { continue }
        Assert-NotReparsePoint -Path $resolvedOwned
        Move-Item -LiteralPath $resolvedOwned -Destination (Join-Path $failureRoot ([IO.Path]::GetFileName($resolvedOwned))) -ErrorAction Stop
    }
    $remaining = @(Get-ChildItem -LiteralPath $resolvedRoot -Force -ErrorAction Stop)
    if ($remaining.Count -ne 0) { throw "INERT_INSTALL_ROLLBACK_UNEXPECTED_REMAINDER: $($remaining.Name -join ',')" }
    Restore-InertTargetAcl -Path $resolvedRoot -Sddl $OriginalSddl
    return $failureRoot
}

$inputLocks = $null
$installMutationStarted = $false
$originalTargetSddl = $null
$appRoot = Join-Path $TargetRoot "app"
$appStagingRoot = Join-Path $TargetRoot ".app-stage-pending"
$venvRoot = Join-Path $TargetRoot "venv311"
$installedSidecars = @(
    (Join-Path $TargetRoot "super1-forward.zip"),
    (Join-Path $TargetRoot "super1-forward.manifest.json"),
    (Join-Path $TargetRoot "super1-forward.manifest.sig")
)
try {
    foreach ($component in @($Archive, $ManifestPath, $SignaturePath, $BootstrapPython)) {
        if (-not (Test-Path -LiteralPath $component -PathType Leaf)) { throw "Required inert install input is missing: $component" }
        Assert-NotReparsePoint -Path $component
    }
    $inputLocks = Open-InertReleaseInputLocks -Paths @($Archive, $ManifestPath, $SignaturePath, $BootstrapPython)

$manifest = Assert-SignedReleaseArchive -Archive $Archive -ExpectedProfile "super1" -RequireProvenance
if ([string]$manifest.release_id -cne $ExpectedReleaseId) { throw "Signed release ID does not match the reviewed install plan." }
if ([string]$manifest.release_id -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
    throw "Signed release ID is not a safe staging-directory name."
}
$actualArchiveSha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualArchiveSha256 -cne $ExpectedArchiveSha256.ToLowerInvariant()) { throw "Signed release archive hash does not match the reviewed install plan." }
if ((Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -notmatch '^[a-f0-9]{64}$') {
    throw "Signed manifest hash could not be computed."
}
$pythonSha256 = (Get-FileHash -LiteralPath $BootstrapPython -Algorithm SHA256).Hash.ToLowerInvariant()
if ($pythonSha256 -cne ([string]$manifest.python_executable_sha256).ToLowerInvariant()) {
    throw "Bootstrap CPython does not match the signer-bound release build interpreter."
}
& $BootstrapPython -c "import sys; assert sys.version_info[:2] == (3, 11) and sys.implementation.name == 'cpython'"
if ($LASTEXITCODE -ne 0) { throw "Bootstrap interpreter is not CPython 3.11." }

if (-not (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
    throw "Target root must already exist and be empty; this installer does not create or replace its parent."
}
Assert-NotReparsePoint -Path $TargetRoot
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($ReadOnlyUserSid -cne $currentSid) { throw "ReadOnlyUserSid must match the interactive installer account." }
Assert-TrustedInstallParent -Path $TargetRoot -UserSid $ReadOnlyUserSid
$targetChildren = @(Get-ChildItem -LiteralPath $TargetRoot -Force)
if ($targetChildren.Count -ne 0) { throw "Target root is not empty; refusing to overwrite or migrate existing content." }
$admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $PlanOnly -and -not $admin) { throw "Elevation is required only for the explicit inert app install." }

if ($PlanOnly) {
    [ordered]@{
        status = "PLAN_ONLY_NO_CHANGES"
        release_id = [string]$manifest.release_id
        profile = [string]$manifest.profile
        archive_sha256 = $actualArchiveSha256
        manifest_sha256 = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        target_root = $TargetRoot
        app_path = Join-Path $TargetRoot "app"
        venv_path = Join-Path $TargetRoot "venv311"
        read_only_user_sid = $ReadOnlyUserSid
        invoking_sid = $currentSid
        scheduled_tasks_created = $false
        watchdog_started = $false
        terminal_installed_or_started = $false
        existing_install_migrated = $false
    } | ConvertTo-Json -Depth 4
    return
}

$originalTargetSddl = (Get-Acl -LiteralPath $TargetRoot -ErrorAction Stop).Sddl
$installMutationStarted = $true
Set-InertInstallDirectoryAcl -Path $TargetRoot -UserSid $ReadOnlyUserSid
$appStagingRoot = Join-Path $TargetRoot (".app-stage-" + [string]$manifest.release_id)
if ((Test-Path -LiteralPath $appRoot) -or (Test-Path -LiteralPath $appStagingRoot)) {
    throw "Inert install app or staging path already exists; refusing to replace it."
}
New-Item -ItemType Directory -Path $appStagingRoot -ErrorAction Stop | Out-Null
Set-InertInstallDirectoryAcl -Path $appStagingRoot -UserSid $ReadOnlyUserSid
try {
    Expand-VerifiedArchive -ArchivePath $Archive -Destination $appStagingRoot
    Assert-ExtractedReleaseMatchesManifest -Root $appStagingRoot -Manifest $manifest
    & $BootstrapPython -I -E -B -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Inert app venv creation failed." }
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    Install-LockedRelease -Python $venvPython -App $appStagingRoot
    Assert-ExtractedReleaseMatchesManifest -Root $appStagingRoot -Manifest $manifest -AllowBuildArtifacts
    Move-Item -LiteralPath $appStagingRoot -Destination $appRoot -ErrorAction Stop
    Copy-Item -LiteralPath $Archive -Destination $installedSidecars[0]
    Copy-Item -LiteralPath $ManifestPath -Destination $installedSidecars[1]
    Copy-Item -LiteralPath $SignaturePath -Destination $installedSidecars[2]
    foreach ($pair in @(
        @{ source = $Archive; target = $installedSidecars[0] },
        @{ source = $ManifestPath; target = $installedSidecars[1] },
        @{ source = $SignaturePath; target = $installedSidecars[2] }
    )) {
        if ((Get-FileHash -LiteralPath ([string]$pair.source) -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            (Get-FileHash -LiteralPath ([string]$pair.target) -Algorithm SHA256).Hash.ToLowerInvariant()) {
            throw "Installed signed release sidecar differs from its locked verified input: $($pair.target)"
        }
    }
    foreach ($directory in @(Get-ChildItem -LiteralPath $TargetRoot -Directory -Recurse -Force)) {
        Assert-NotReparsePoint -Path $directory.FullName
        Set-InertInstallDirectoryAcl -Path $directory.FullName -UserSid $ReadOnlyUserSid
    }
    [ordered]@{
        status = "INERT_APP_INSTALLED"
        release_id = [string]$manifest.release_id
        archive_sha256 = $actualArchiveSha256
        root = $TargetRoot
        app = $appRoot
        python = $venvPython
        tasks_created = $false
        watchdog_started = $false
        terminal_installed_or_started = $false
        credentials_created = $false
        existing_install_migrated = $false
        deployment_ready = $false
    } | ConvertTo-Json -Depth 4
}
catch {
    if (-not $installMutationStarted) { throw }
    $failureMessage = $_.Exception.Message
    $rollbackErrors = [Collections.Generic.List[string]]::new()
    $failureRoot = ""
    try {
        $ownedPaths = @($appRoot, $appStagingRoot, $venvRoot) + $installedSidecars
        $failureRoot = Move-InertInstallFailureArtifacts -Root $TargetRoot -OwnedPaths $ownedPaths -OriginalSddl $originalTargetSddl
    }
    catch { $rollbackErrors.Add($_.Exception.Message) }
    if ($rollbackErrors.Count -gt 0) {
        throw "INERT_INSTALL_ROLLBACK_INCOMPLETE: original=$failureMessage rollback=$($rollbackErrors -join '; ') preserved=$failureRoot"
    }
    throw "INERT_INSTALL_FAILED_ROLLED_BACK: $failureMessage preserved=$failureRoot"
}
}
finally {
    if ($null -ne $inputLocks) { foreach ($lock in $inputLocks) { $lock.stream.Dispose() } }
}
