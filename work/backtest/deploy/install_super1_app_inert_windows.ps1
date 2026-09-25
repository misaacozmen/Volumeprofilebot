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
. $IntegrityHelper

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

foreach ($component in @($Archive, $ManifestPath, $SignaturePath, $BootstrapPython)) {
    if (-not (Test-Path -LiteralPath $component -PathType Leaf)) { throw "Required inert install input is missing: $component" }
    Assert-NotReparsePoint -Path $component
}
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

Set-InertInstallDirectoryAcl -Path $TargetRoot -UserSid $ReadOnlyUserSid
$appRoot = Join-Path $TargetRoot "app"
$appStagingRoot = Join-Path $TargetRoot (".app-stage-" + [string]$manifest.release_id)
if ((Test-Path -LiteralPath $appRoot) -or (Test-Path -LiteralPath $appStagingRoot)) {
    throw "Inert install app or staging path already exists; refusing to replace it."
}
New-Item -ItemType Directory -Path $appStagingRoot -ErrorAction Stop | Out-Null
Set-InertInstallDirectoryAcl -Path $appStagingRoot -UserSid $ReadOnlyUserSid
try {
    Expand-VerifiedArchive -ArchivePath $Archive -Destination $appStagingRoot
    $venvRoot = Join-Path $TargetRoot "venv311"
    & $BootstrapPython -I -E -B -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Inert app venv creation failed." }
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    & $venvPython -I -E -B -m pip install --disable-pip-version-check --no-index --require-hashes -r (Join-Path $appStagingRoot "requirements-windows.lock")
    if ($LASTEXITCODE -ne 0) { throw "Locked offline venv dependency install failed." }
    Push-Location -LiteralPath $appStagingRoot
    try {
        & $venvPython -I -E -B -m pip install --disable-pip-version-check --no-index --no-deps --no-build-isolation "."
        if ($LASTEXITCODE -ne 0) { throw "Inert Super1 application install failed." }
    }
    finally { Pop-Location }
    Move-Item -LiteralPath $appStagingRoot -Destination $appRoot -ErrorAction Stop
    Copy-Item -LiteralPath $Archive -Destination (Join-Path $TargetRoot "super1-forward.zip")
    Copy-Item -LiteralPath $ManifestPath -Destination (Join-Path $TargetRoot "super1-forward.manifest.json")
    Copy-Item -LiteralPath $SignaturePath -Destination (Join-Path $TargetRoot "super1-forward.manifest.sig")
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
    throw "INERT_INSTALL_FAILED_WITH_FILES_PRESERVED: inspect $TargetRoot before any retry. $($_.Exception.Message)"
}
