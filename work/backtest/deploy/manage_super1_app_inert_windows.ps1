[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet("Upgrade", "Rollback")][string]$Action,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$TargetRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^S-1-5-(?:\d+-)*\d+$')][string]$ReadOnlyUserSid,
    [string]$ReleaseDirectory,
    [string]$ExpectedReleaseId,
    [string]$ExpectedArchiveSha256,
    [string]$BootstrapPython,
    [string]$TransactionId
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.IO.Compression.FileSystem
$TargetRoot = [IO.Path]::GetFullPath($TargetRoot)
$IntegrityHelper = Join-Path $PSScriptRoot "release_integrity.ps1"
$ExpectedIntegrityScriptSha256 = "6e7dae16e7238fb75cbe81d9d614647d3530cd83a67cc9b42c80c580d7c98b27"
$integrityBytes = [IO.File]::ReadAllBytes($IntegrityHelper)
$integrityHasher = [Security.Cryptography.SHA256]::Create()
try { $integritySha256 = ([BitConverter]::ToString($integrityHasher.ComputeHash($integrityBytes))).Replace("-", "").ToLowerInvariant() }
finally { $integrityHasher.Dispose() }
if ($integritySha256 -cne $ExpectedIntegrityScriptSha256) { throw "Inert upgrade helper byte pin mismatch." }
. ([scriptblock]::Create([Text.Encoding]::UTF8.GetString($integrityBytes)))

function Assert-PathNotReparse {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Inert app path is a reparse point: $Path" }
}

function Assert-InertTreeNoReparse {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-PathNotReparse -Path $Path
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Inert app tree contains a reparse point: $($item.FullName)" }
    }
}

function Set-InertAppAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$UserSid)
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new("S-1-5-18"))
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
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
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Assert-InertTargetAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$UserSid)
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) { throw "Inert target DACL must be protected from parent inheritance." }
    $trusted = @("S-1-5-18", "S-1-5-32-544", $UserSid)
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -notin $trusted) { throw "Inert target owner is not trusted: $ownerSid" }
    $requiredFull = @("S-1-5-18", "S-1-5-32-544")
    foreach ($sid in $requiredFull) {
        $grant = @($acl.Access | Where-Object {
            $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ceq $sid -and
            ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl
        })
        if ($grant.Count -eq 0) { throw "Inert target is missing required administrative ACL grant: $sid" }
    }
    $userGrant = @($acl.Access | Where-Object {
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ceq $UserSid -and
        ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq [Security.AccessControl.FileSystemRights]::ReadAndExecute
    })
    if ($userGrant.Count -eq 0) { throw "Inert target does not grant its read-only user access." }
    $writeRights = [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach ($rule in @($acl.Access)) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        $canWrite = ($rule.FileSystemRights -band $writeRights) -ne 0
        if (($sid -notin @("S-1-5-18", "S-1-5-32-544") -and $canWrite) -or ($sid -ceq $UserSid -and $canWrite)) {
            throw "Read-only or untrusted SID can modify the inert target: $sid"
        }
    }
}

function Assert-InertTargetParent {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$UserSid)
    $parent = Split-Path -Parent $Path
    Assert-PathNotReparse -Path $parent
    $trusted = @("S-1-5-18", "S-1-5-32-544", $UserSid)
    $acl = Get-Acl -LiteralPath $parent -ErrorAction Stop
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -notin $trusted) { throw "Inert target parent owner is not trusted: $ownerSid" }
    $deleteChild = [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
    foreach ($rule in @($acl.Access)) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $trusted -and ($rule.FileSystemRights -band $deleteChild) -eq $deleteChild) {
            throw "Untrusted SID can replace the inert target through its parent: $sid"
        }
    }
}

function Open-InertInputLocks {
    param([Parameter(Mandatory = $true)][string[]]$Paths)
    $locks = [Collections.Generic.List[object]]::new()
    try {
        foreach ($path in $Paths) {
            Assert-PathNotReparse -Path $path
            $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            $locks.Add([pscustomobject]@{ path = $path; stream = $stream })
        }
        return ,$locks
    }
    catch { foreach ($lock in $locks) { $lock.stream.Dispose() }; throw }
}

function Expand-InertVerifiedArchive {
    param([Parameter(Mandatory = $true)][string]$Archive, [Parameter(Mandatory = $true)][string]$Destination)
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $relative = [string]$entry.FullName
            if (-not $relative -or $relative -ne $relative.Replace("\", "/") -or
                [IO.Path]::IsPathRooted($relative) -or $relative -match '^[A-Za-z]:' -or
                $relative -match '(^|/)\.\.?(/|$)' -or $relative.Contains(":") -or
                $relative -match '(^|/)//') { throw "Unsafe signed archive path: $relative" }
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $relative.Replace("/", [IO.Path]::DirectorySeparatorChar)))
            $prefix = [IO.Path]::GetFullPath($Destination).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Archive path escapes app staging: $relative" }
            if ($relative.EndsWith("/")) { New-Item -ItemType Directory -Force -Path $target | Out-Null; continue }
            $parent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $parent -PathType Container)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            $input = $entry.Open()
            $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose() }
        }
    }
    finally { $zip.Dispose() }
}

function Assert-InertExtractedFilesMatchManifest {
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
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Upgrade stage contains a reparse point: $($item.FullName)" }
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
    if ($missingExpected.Count -gt 0 -or $unexpectedPaths.Count -gt 0) { throw "Upgrade stage file set differs from signed archive manifest." }
    foreach ($path in $expectedPaths) { if ($actual[$path] -cne $expected[$path]) { throw "Upgrade staged app bytes differ from signed archive: $path" } }
}

function Get-InertBundlePaths {
    param([Parameter(Mandatory = $true)][string]$Root)
    return @(
        (Join-Path $Root "app"),
        (Join-Path $Root "venv311"),
        (Join-Path $Root "super1-forward.zip"),
        (Join-Path $Root "super1-forward.manifest.json"),
        (Join-Path $Root "super1-forward.manifest.sig")
    )
}

function Assert-InertBundle {
    param([Parameter(Mandatory = $true)][string]$Root, [string]$ExpectedReleaseId, [string]$ExpectedArchiveSha256)
    $paths = Get-InertBundlePaths -Root $Root
    foreach ($path in $paths) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Inert app bundle component is missing: $path" }
        Assert-PathNotReparse -Path $path
    }
    Assert-InertTreeNoReparse -Path (Join-Path $Root "app")
    Assert-InertTreeNoReparse -Path (Join-Path $Root "venv311")
    $archive = Join-Path $Root "super1-forward.zip"
    $manifest = Assert-SignedReleaseArchive -Archive $archive -ExpectedProfile "super1" -RequireProvenance
    Assert-InertExtractedFilesMatchManifest -Root (Join-Path $Root "app") -Manifest $manifest -AllowBuildArtifacts
    if ($ExpectedReleaseId -and [string]$manifest.release_id -cne $ExpectedReleaseId) { throw "Inert bundle release ID mismatch." }
    $archiveSha = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ExpectedArchiveSha256 -and $archiveSha -cne $ExpectedArchiveSha256.ToLowerInvariant()) { throw "Inert bundle archive hash mismatch." }
    return [pscustomobject]@{ manifest = $manifest; archive_sha256 = $archiveSha }
}

function Copy-InertReleaseSidecars {
    param([string[]]$SourcePaths, [string[]]$TargetPaths)
    for ($i = 0; $i -lt $SourcePaths.Count; $i++) {
        Copy-Item -LiteralPath $SourcePaths[$i] -Destination $TargetPaths[$i] -ErrorAction Stop
        $sourceHash = (Get-FileHash -LiteralPath $SourcePaths[$i] -Algorithm SHA256).Hash.ToLowerInvariant()
        $targetHash = (Get-FileHash -LiteralPath $TargetPaths[$i] -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sourceHash -cne $targetHash) { throw "Staged signed sidecar differs from locked input: $($TargetPaths[$i])" }
    }
}

function Move-InertBundleTo {
    param([string]$SourceRoot, [string]$DestinationRoot)
    if (-not (Test-Path -LiteralPath $DestinationRoot)) { New-Item -ItemType Directory -Path $DestinationRoot -ErrorAction Stop | Out-Null }
    $names = @("app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig")
    foreach ($name in $names) {
        $source = Join-Path $SourceRoot $name
        if (Test-Path -LiteralPath $source) {
            $destination = Join-Path $DestinationRoot $name
            if (Test-Path -LiteralPath $destination) { throw "INERT_BUNDLE_DESTINATION_EXISTS: $destination" }
            Move-Item -LiteralPath $source -Destination $destination -ErrorAction Stop
        }
    }
}

function Restore-InertBundleFrom {
    param([string]$SourceRoot, [string]$TargetRoot, [switch]$PreserveExistingTargetPaths)
    $names = @("app", "venv311", "super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig")
    foreach ($name in $names) {
        $source = Join-Path $SourceRoot $name
        if (Test-Path -LiteralPath $source) {
            $destination = Join-Path $TargetRoot $name
            if (Test-Path -LiteralPath $destination) {
                if ($PreserveExistingTargetPaths) { continue }
                throw "INERT_BUNDLE_RESTORE_COLLISION: $destination"
            }
            Move-Item -LiteralPath $source -Destination $destination -ErrorAction Stop
        }
    }
}

function Write-InertTransactionRecord {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][object]$Record)
    $target = [IO.Path]::GetFullPath($Path)
    $temporary = $target + ".tmp-" + [Guid]::NewGuid().ToString("N")
    $backup = $target + ".bak-" + [Guid]::NewGuid().ToString("N")
    $payload = ($Record | ConvertTo-Json -Depth 4) + "`n"
    try {
        [IO.File]::WriteAllText($temporary, $payload, (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            [IO.File]::Replace($temporary, $target, $backup)
            [IO.File]::Delete($backup)
        } else {
            [IO.File]::Move($temporary, $target)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) { [IO.File]::Delete($temporary) }
        if (Test-Path -LiteralPath $backup -PathType Leaf) { [IO.File]::Delete($backup) }
    }
}

function Invoke-InertAppUpgrade {
    param()
    foreach ($required in @($ReleaseDirectory, $ExpectedReleaseId, $ExpectedArchiveSha256, $BootstrapPython)) {
        if ([string]::IsNullOrWhiteSpace([string]$required)) { throw "Upgrade requires release directory, release ID, expected archive hash and bootstrap Python." }
    }
    if ($ExpectedArchiveSha256 -notmatch '^[A-Fa-f0-9]{64}$') { throw "Expected archive SHA-256 must be 64 hex characters." }
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($ReadOnlyUserSid -cne $currentSid) { throw "ReadOnlyUserSid must match the interactive installer account." }
    if (-not (Test-Path -LiteralPath $TargetRoot -PathType Container)) { throw "Inert target root is missing." }
    Assert-PathNotReparse -Path $TargetRoot
    Assert-InertTargetAcl -Path $TargetRoot -UserSid $ReadOnlyUserSid
    $admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "Elevation is required only for inert app upgrade." }
    $current = Assert-InertBundle -Root $TargetRoot

    $releaseRoot = [IO.Path]::GetFullPath($ReleaseDirectory)
    $archive = Join-Path $releaseRoot "super1-forward.zip"
    $manifestPath = Join-Path $releaseRoot "super1-forward.manifest.json"
    $signaturePath = Join-Path $releaseRoot "super1-forward.manifest.sig"
    $python = [IO.Path]::GetFullPath($BootstrapPython)
    $locks = Open-InertInputLocks -Paths @($archive, $manifestPath, $signaturePath, $python)
    $transaction = $null
    $stage = Join-Path $TargetRoot (".upgrade-stage-" + [Guid]::NewGuid().ToString("N"))
    $oldMoveStarted = $false
    $oldMoveComplete = $false
    try {
        $newManifest = Assert-SignedReleaseArchive -Archive $archive -ExpectedProfile "super1" -RequireProvenance
        if ([string]$newManifest.release_id -cne $ExpectedReleaseId) { throw "Signed release ID does not match the upgrade plan." }
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedArchiveSha256.ToLowerInvariant()) { throw "Signed release archive hash does not match the upgrade plan." }
        if ((Get-FileHash -LiteralPath $python -Algorithm SHA256).Hash.ToLowerInvariant() -cne ([string]$newManifest.python_executable_sha256).ToLowerInvariant()) { throw "Bootstrap CPython hash differs from signed manifest." }
        & $python -c "import sys; assert sys.version_info[:2] == (3, 11) and sys.implementation.name == 'cpython'"
        if ($LASTEXITCODE -ne 0) { throw "Bootstrap interpreter is not CPython 3.11." }

        $history = Join-Path $TargetRoot "history"
        if (-not (Test-Path -LiteralPath $history)) { New-Item -ItemType Directory -Path $history | Out-Null; Set-InertAppAcl -Path $history -UserSid $ReadOnlyUserSid }
        Assert-PathNotReparse -Path $history
        $transaction = Join-Path $history ([Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $transaction -ErrorAction Stop | Out-Null
        Set-InertAppAcl -Path $transaction -UserSid $ReadOnlyUserSid
        $previous = Join-Path $transaction "previous"
        New-Item -ItemType Directory -Path $previous -ErrorAction Stop | Out-Null
        New-Item -ItemType Directory -Path $stage -ErrorAction Stop | Out-Null
        Set-InertAppAcl -Path $stage -UserSid $ReadOnlyUserSid
        $stageApp = Join-Path $stage "app"
        New-Item -ItemType Directory -Path $stageApp | Out-Null
        Expand-InertVerifiedArchive -Archive $archive -Destination $stageApp
        Assert-InertExtractedFilesMatchManifest -Root $stageApp -Manifest $newManifest
        $stageVenv = Join-Path $stage "venv311"
        & $python -I -E -B -m venv $stageVenv
        if ($LASTEXITCODE -ne 0) { throw "Upgrade virtual environment creation failed." }
        $stagePython = Join-Path $stageVenv "Scripts\python.exe"
        Install-LockedRelease -Python $stagePython -App $stageApp
        Assert-InertExtractedFilesMatchManifest -Root $stageApp -Manifest $newManifest -AllowBuildArtifacts
        $incoming = Join-Path $stage "release"
        New-Item -ItemType Directory -Path $incoming | Out-Null
        Copy-InertReleaseSidecars -SourcePaths @($archive, $manifestPath, $signaturePath) -TargetPaths @(
            (Join-Path $incoming "super1-forward.zip"),
            (Join-Path $incoming "super1-forward.manifest.json"),
            (Join-Path $incoming "super1-forward.manifest.sig")
        )
        $previousApp = Assert-InertBundle -Root $TargetRoot
        $oldMoveStarted = $true
        Move-InertBundleTo -SourceRoot $TargetRoot -DestinationRoot $previous
        $oldMoveComplete = $true
        Move-Item -LiteralPath $stageApp -Destination (Join-Path $TargetRoot "app") -ErrorAction Stop
        Move-Item -LiteralPath $stageVenv -Destination (Join-Path $TargetRoot "venv311") -ErrorAction Stop
        Restore-InertBundleFrom -SourceRoot $incoming -TargetRoot $TargetRoot
        $verified = Assert-InertBundle -Root $TargetRoot -ExpectedReleaseId $ExpectedReleaseId -ExpectedArchiveSha256 $ExpectedArchiveSha256
        $record = [ordered]@{
            schema = "super1-inert-upgrade-v1"
            transaction_id = [IO.Path]::GetFileName($transaction)
            state = "UPGRADED"
            previous_release_id = [string]$previousApp.manifest.release_id
            previous_archive_sha256 = [string]$previousApp.archive_sha256
            release_id = [string]$verified.manifest.release_id
            archive_sha256 = [string]$verified.archive_sha256
            task_or_watchdog_created = $false
            terminal_or_bot_started = $false
            deployment_ready = $false
        }
        Write-InertTransactionRecord -Path (Join-Path $transaction "transaction.json") -Record $record
        return $record | ConvertTo-Json -Depth 4
    }
    catch {
        $failure = $_.Exception.Message
        $rollbackErrors = [Collections.Generic.List[string]]::new()
        if ($oldMoveStarted -and $transaction) {
            try {
                if ($oldMoveComplete) {
                    $failed = Join-Path $transaction "failed-current"
                    if (-not (Test-Path -LiteralPath $failed)) { Move-InertBundleTo -SourceRoot $TargetRoot -DestinationRoot $failed }
                    Restore-InertBundleFrom -SourceRoot $previous -TargetRoot $TargetRoot
                }
                else {
                    Restore-InertBundleFrom -SourceRoot $previous -TargetRoot $TargetRoot -PreserveExistingTargetPaths
                }
                $null = Assert-InertBundle -Root $TargetRoot `
                    -ExpectedReleaseId ([string]$previousApp.manifest.release_id) `
                    -ExpectedArchiveSha256 ([string]$previousApp.archive_sha256)
                $failedRecord = [ordered]@{
                    schema = "super1-inert-upgrade-v1"
                    transaction_id = [IO.Path]::GetFileName($transaction)
                    state = "FAILED_ROLLED_BACK"
                    previous_release_id = [string]$previousApp.manifest.release_id
                    previous_archive_sha256 = [string]$previousApp.archive_sha256
                    attempted_release_id = [string]$newManifest.release_id
                    attempted_archive_sha256 = $ExpectedArchiveSha256.ToLowerInvariant()
                    failure = $failure
                    task_or_watchdog_created = $false
                    terminal_or_bot_started = $false
                    deployment_ready = $false
                }
                Write-InertTransactionRecord -Path (Join-Path $transaction "transaction.json") -Record $failedRecord
            }
            catch { $rollbackErrors.Add($_.Exception.Message) }
        }
        if ((Test-Path -LiteralPath $stage) -and $transaction) {
            try { Move-Item -LiteralPath $stage -Destination (Join-Path $transaction "failed-stage") -ErrorAction Stop }
            catch { $rollbackErrors.Add($_.Exception.Message) }
        }
        if ($rollbackErrors.Count) { throw "INERT_UPGRADE_ROLLBACK_INCOMPLETE: original=$failure rollback=$($rollbackErrors -join '; ') transaction=$transaction" }
        throw "INERT_UPGRADE_FAILED_ROLLED_BACK: $failure transaction=$transaction"
    }
    finally { foreach ($lock in $locks) { $lock.stream.Dispose() } }
}

function Invoke-InertAppRollback {
    if ($TransactionId -notmatch '^[a-fA-F0-9]{32}$') { throw "Rollback requires the 32-character transaction ID returned by an inert upgrade." }
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($ReadOnlyUserSid -cne $currentSid) { throw "ReadOnlyUserSid must match the interactive installer account." }
    Assert-InertTargetAcl -Path $TargetRoot -UserSid $ReadOnlyUserSid
    $admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "Elevation is required only for inert app rollback." }
    $transaction = Join-Path (Join-Path $TargetRoot "history") $TransactionId
    $recordPath = Join-Path $transaction "transaction.json"
    if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) { throw "Inert rollback transaction record is missing." }
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    if ([string]$record.schema -cne "super1-inert-upgrade-v1" -or [string]$record.transaction_id -cne $TransactionId -or [string]$record.state -cne "UPGRADED") { throw "Inert transaction is not in UPGRADED state." }
    $previous = Join-Path $transaction "previous"
    $previousBundle = Assert-InertBundle -Root $previous -ExpectedReleaseId ([string]$record.previous_release_id) -ExpectedArchiveSha256 ([string]$record.previous_archive_sha256)
    $current = Assert-InertBundle -Root $TargetRoot -ExpectedReleaseId ([string]$record.release_id) -ExpectedArchiveSha256 ([string]$record.archive_sha256)
    $rolledBack = Join-Path $transaction "rolled-back-current"
    if (Test-Path -LiteralPath $rolledBack) {
        Assert-PathNotReparse -Path $rolledBack
        $existingRollbackItems = @(Get-ChildItem -LiteralPath $rolledBack -Force -ErrorAction Stop)
        if ($existingRollbackItems.Count -ne 0) { throw "Rollback has already preserved a current bundle for this transaction." }
    }
    $currentMoveStarted = $false
    $currentMoveComplete = $false
    try {
        $currentMoveStarted = $true
        Move-InertBundleTo -SourceRoot $TargetRoot -DestinationRoot $rolledBack
        $currentMoveComplete = $true
        Restore-InertBundleFrom -SourceRoot $previous -TargetRoot $TargetRoot
        $restored = Assert-InertBundle -Root $TargetRoot -ExpectedReleaseId ([string]$record.previous_release_id) -ExpectedArchiveSha256 ([string]$record.previous_archive_sha256)
        $record.state = "ROLLED_BACK"
        $record.rollback_release_id = [string]$restored.manifest.release_id
        $record.rollback_archive_sha256 = [string]$restored.archive_sha256
        $record.task_or_watchdog_created = $false
        $record.terminal_or_bot_started = $false
        $record.deployment_ready = $false
        Write-InertTransactionRecord -Path $recordPath -Record $record
        return $record | ConvertTo-Json -Depth 4
    }
    catch {
        $failure = $_.Exception.Message
        if ($currentMoveStarted) {
            try {
                if ($currentMoveComplete) {
                    $failedRestored = Join-Path $transaction "failed-rollback-target"
                    if (-not (Test-Path -LiteralPath $failedRestored)) { Move-InertBundleTo -SourceRoot $TargetRoot -DestinationRoot $failedRestored }
                    Restore-InertBundleFrom -SourceRoot $rolledBack -TargetRoot $TargetRoot
                }
                else {
                    Restore-InertBundleFrom -SourceRoot $rolledBack -TargetRoot $TargetRoot -PreserveExistingTargetPaths
                }
                $null = Assert-InertBundle -Root $TargetRoot `
                    -ExpectedReleaseId ([string]$current.manifest.release_id) `
                    -ExpectedArchiveSha256 ([string]$current.archive_sha256)
            }
            catch { throw "INERT_ROLLBACK_RECOVERY_INCOMPLETE: original=$failure recovery=$($_.Exception.Message) transaction=$transaction" }
        }
        throw "INERT_ROLLBACK_FAILED_RESTORED_CURRENT: $failure transaction=$transaction"
    }
}

if (-not (Test-Path -LiteralPath $TargetRoot -PathType Container)) { throw "Inert target root must already exist." }
Assert-PathNotReparse -Path $TargetRoot
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($ReadOnlyUserSid -cne $currentSid) { throw "ReadOnlyUserSid must match the interactive account." }
Assert-InertTargetParent -Path $TargetRoot -UserSid $ReadOnlyUserSid
if ($Action -eq "Upgrade") { Invoke-InertAppUpgrade }
else { Invoke-InertAppRollback }
