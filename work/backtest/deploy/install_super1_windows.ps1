[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^super1-local-demo-20260907-r8$')][string]$ExpectedReleaseId,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedArchiveSha256,
    [string]$PythonExe = "C:\Program Files\Python311\python.exe",
    [switch]$PlanOnly,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
throw "LEGACY_DISABLED_USE_SIGNED_V16_RUNBOOK: Signed R8 triple / signed v16 runbook required."
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract
. (Join-Path $PSScriptRoot "super1_install_transaction.ps1")

function Assert-Super1Administrator {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run this script from an elevated PowerShell." }
}
function Get-Super1Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Write-Super1Pin([string]$Path, [object]$Value) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try { [IO.File]::WriteAllText($temporary, (ConvertTo-Json $Value -Depth 8) + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false))); Move-Item -LiteralPath $temporary -Destination $Path -Force }
    finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}

function Copy-Super1ImmutableFile([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "Release input is missing: $Source" }
    if (Test-Path -LiteralPath $Destination) { throw "Immutable staging destination already exists: $Destination" }
    $sourceStream = [IO.File]::Open($Source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $destinationStream = $null
    try {
        $destinationStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $sourceStream.CopyTo($destinationStream)
        $destinationStream.Flush($true)
    }
    finally {
        if ($destinationStream) { $destinationStream.Dispose() }
        if ($sourceStream) { $sourceStream.Dispose() }
    }
}

function Get-Super1TreeSnapshot([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved)) { return [ordered]@{ exists = $false; type = "missing" } }
    $rootItem = Get-Item -LiteralPath $resolved -Force
    $items = @($rootItem)
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Cannot snapshot a reparse-backed path: $resolved"
    }
    if ($rootItem.PSIsContainer) {
        $items += @(Get-ChildItem -LiteralPath $resolved -Recurse -Force -ErrorAction Stop)
    }
    $entries = New-Object Collections.Generic.List[object]
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Cannot snapshot reparse-backed path: $($item.FullName)"
        }
        $relative = if ($item.FullName -ceq $resolved) { "." } else {
            $item.FullName.Substring($resolved.Length).TrimStart('\', '/') -replace '\\', '/'
        }
        $acl = Get-Acl -LiteralPath $item.FullName -ErrorAction Stop
        $entry = [ordered]@{
            relative_path = $relative
            type = if ($item.PSIsContainer) { "directory" } else { "file" }
            attributes = [string]$item.Attributes
            owner = [string]$acl.Owner
            sddl = [string]$acl.Sddl
        }
        if (-not $item.PSIsContainer) { $entry.sha256 = Get-Super1Hash $item.FullName }
        $entries.Add([pscustomobject]$entry)
    }
    $canonical = @($entries | Sort-Object relative_path | ForEach-Object {
        $_ | ConvertTo-Json -Depth 5 -Compress
    }) -join "`n"
    $treeHash = [Security.Cryptography.SHA256]::Create()
    try {
        $treeSha256 = [BitConverter]::ToString($treeHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))).Replace("-", "").ToLowerInvariant()
    }
    finally { $treeHash.Dispose() }
    return [ordered]@{
        exists = $true
        type = if ($rootItem.PSIsContainer) { "directory" } else { "file" }
        file_count = @($entries | Where-Object type -eq "file").Count
        directory_count = @($entries | Where-Object type -eq "directory").Count
        tree_sha256 = $treeSha256
        owner = [string](Get-Acl -LiteralPath $resolved).Owner
        sddl = [string](Get-Acl -LiteralPath $resolved).Sddl
        entries = @($entries | Sort-Object relative_path)
    }
}

function Get-Super1TaskSnapshot([string]$TaskName, [string]$Destination) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { return [ordered]@{ task = $TaskName; exists = $false } }
    if ([string]$task.State -in @("Running", "Queued")) {
        throw "Super1 task is active; refusing to replace it during a fresh transaction: $TaskName"
    }
    $xml = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    [IO.File]::WriteAllText($Destination, $xml, (New-Object Text.UTF8Encoding($false)))
    return [ordered]@{
        task = $TaskName
        exists = $true
        xml = $Destination
        xml_sha256 = Get-Super1Hash $Destination
        state = [string]$task.State
        last_task_result = [int64](Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop).LastTaskResult
        principal = [string]$task.Principal.UserId
        logon_type = [string]$task.Principal.LogonType
        run_level = [string]$task.Principal.RunLevel
        settings = [ordered]@{
            restart_count = [int]$task.Settings.RestartCount
            wake_to_run = [bool]$task.Settings.WakeToRun
            disallow_start_on_batteries = [bool]$task.Settings.DisallowStartIfOnBatteries
            stop_on_batteries = [bool]$task.Settings.StopIfGoingOnBatteries
        }
        trigger_count = @($task.Triggers).Count
    }
}

$integrity = Join-Path $PSScriptRoot "release_integrity.ps1"
if (-not (Test-Path -LiteralPath $integrity -PathType Leaf)) { throw "Missing release integrity verifier." }
. $integrity

function Invoke-Super1ReleasePreflight {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$ReleaseId,
        [Parameter(Mandatory = $true)][string]$ArchiveSha256
    )
    $directoryPath = [IO.Path]::GetFullPath($Directory)
    if (-not (Test-Path -LiteralPath $directoryPath -PathType Container)) { throw "R8 release directory is missing: $directoryPath" }
    $archivePath = Join-Path $directoryPath "super1-forward.zip"
    $manifestPath = Join-Path $directoryPath "super1-forward.manifest.json"
    $signaturePath = Join-Path $directoryPath "super1-forward.manifest.sig"
    foreach ($path in @($archivePath, $manifestPath, $signaturePath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Signed R8 triple is incomplete: $path" }
    }
    $topLevel = @(Get-ChildItem -LiteralPath $directoryPath -Force)
    if ($topLevel.Count -ne 3 -or @($topLevel | Where-Object { -not $_.PSIsContainer -and $_.Name -notin @("super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig") }).Count -ne 0) {
        throw "R8 release directory must contain exactly the signed triple."
    }
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -cne $ArchiveSha256.ToLowerInvariant()) { throw "R8 archive hash does not match -ExpectedArchiveSha256." }
    $sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $signed = Assert-SignedReleaseArchive -Archive $archivePath -ExpectedProfile "super1" -SourceRoot $sourceRoot -RequireProvenance
    if ([string]$signed.archive_file -cne "super1-forward.zip" -or
        [string]$signed.release_id -cne $ReleaseId -or
        [string]$signed.archive_sha256 -cne $actualHash) { throw "R8 signed manifest identity/archive binding is invalid." }
    if ([int]$signed.pytest_collected_count -lt 566 -or
        [int]$signed.artifact_pytest_collected_count -lt 259 -or
        [int]$signed.pytest_pass_count -ne [int]$signed.pytest_collected_count -or
        [int]$signed.artifact_pytest_pass_count -ne [int]$signed.artifact_pytest_collected_count -or
        [int]$signed.pytest_skipped_count -ne 0 -or [int]$signed.artifact_pytest_skipped_count -ne 0) {
        throw "R8 signed test inventory is below 566/259 or contains skipped tests."
    }
    $gitRoot = (& git -C $sourceRoot rev-parse --show-toplevel 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gitRoot)) {
        $gitRoot = (& git rev-parse --show-toplevel 2>$null).Trim()
    }
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gitRoot)) { throw "Could not resolve the repository root for R8 provenance." }
    $dirty = @(& git -C $gitRoot status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) { throw "R8 source tree is not clean before install." }
    $head = (& git -C $gitRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -cne [string]$signed.git_commit) { throw "R8 signed commit does not match clean source HEAD." }
    foreach ($critical in @(
        @{ path = "deploy/install_super1_windows.ps1"; actual = $PSCommandPath },
        @{ path = "deploy/super1_install_transaction.ps1"; actual = (Join-Path $PSScriptRoot "super1_install_transaction.ps1") },
        @{ path = "deploy/release_integrity.ps1"; actual = $integrity }
    )) {
        $entry = @($signed.files | Where-Object { [string]$_.path -ceq $critical.path })
        $actual = (Get-FileHash -LiteralPath $critical.actual -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($entry.Count -ne 1 -or $actual -cne [string]$entry[0].sha256) { throw "Installer integrity does not match signed archive: $($critical.path)" }
    }
    return [pscustomobject]@{
        release_id = [string]$signed.release_id
        archive = $archivePath
        manifest = $manifestPath
        signature = $signaturePath
        archive_sha256 = $actualHash
        git_commit = [string]$signed.git_commit
        pytest_collected = [int]$signed.pytest_collected_count
        artifact_pytest_collected = [int]$signed.artifact_pytest_collected_count
    }
}

$preflight = Invoke-Super1ReleasePreflight -Directory $ReleaseDirectory -ReleaseId $ExpectedReleaseId -ArchiveSha256 $ExpectedArchiveSha256
if ($PlanOnly -or $WhatIf) {
    [ordered]@{
        state = "PREINSTALL_SIGNED_RELEASE_PREFLIGHT_PASS"
        release_id = $preflight.release_id
        archive = $preflight.archive
        manifest = $preflight.manifest
        signature = $preflight.signature
        archive_sha256 = $preflight.archive_sha256
        git_commit = $preflight.git_commit
        pytest_collected = $preflight.pytest_collected
        artifact_pytest_collected = $preflight.artifact_pytest_collected
        transaction_simulated = $false
        admin_install_executed = $false
        readiness_executed = $false
        mutation = $false
    } | ConvertTo-Json -Depth 6
    exit 0
}

Assert-Super1Administrator
$root = [string]$Contract.root
$archive = [string]$preflight.archive
$releaseManifest = [string]$preflight.manifest
$releaseSignature = [string]$preflight.signature
$app = [string]$Contract.app
$appNext = "$app.next"
$transactionId = [Guid]::NewGuid().ToString("N")
$transactionRoot = Join-Path (
    Join-Path ([Environment]::GetFolderPath("CommonApplicationData")) "Super1\install-transactions"
) ("fresh-install-" + $transactionId)
$pythonPath = [IO.Path]::GetFullPath($PythonExe)
$terminal = [string]$Contract.terminal
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw "Missing signed Super1 release archive: $archive" }
if (Test-Path -LiteralPath $appNext) { throw "Stale app.next exists; recover the previous transaction before retrying." }
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "Python 3.11 bootstrap runtime is missing: $pythonPath" }
if (-not (Test-Path -LiteralPath $terminal -PathType Leaf)) { throw "Dedicated XM terminal is missing at the contract path: $terminal" }

$oldState = [string]$Contract.state
$oldControl = [string]$Contract.control
$oldTrust = [string]$Contract.runtime_trust
$oldVenv = Join-Path $root "venv311"
$runnerName = [string]$Contract.runner_account
$runnerCreated = $false
$taskNames = @([string]$Contract.main_task, [string]$Contract.watchdog_task)
$movedEntries = @()
$taskBackups = @()
$newTargets = @()
$runnerCredentialBackup = $null
$runnerCredentialPath = $null
$runnerCredentialExistedBefore = $false
$tasksWereRemoved = $false
$newTasksRegistered = $false
$rootAclBackupPath = $null
$aclBackupEntries = @()
$preState = $null
$preRootSnapshot = $null
$oldTasksRemoved = $false
$stagedReleaseRoot = $null
$readinessExecuted = $false
$readinessState = $null
$readinessNonce = [Guid]::NewGuid().ToString()
$runnerSid = $null
$securitySnapshotList = @()
$rootExistedBefore = Test-Path -LiteralPath $root -PathType Container
$preRootSnapshot = Get-Super1TreeSnapshot $root
$transactionJournal = Join-Path $transactionRoot "transaction.json"
$releaseMetadata = [ordered]@{
    release_id = [string]$preflight.release_id
    archive = [string]$preflight.archive
    manifest = [string]$preflight.manifest
    signature = [string]$preflight.signature
    archive_sha256 = [string]$preflight.archive_sha256
    promoted_archive = [string]$Contract.release_archive
    promoted_manifest = [string]$Contract.release_manifest
    promoted_signature = [string]$Contract.release_signature
}

function Write-Super1TransactionJournal([string]$Status, [string]$ErrorMessage = "") {
    $payload = [ordered]@{
        schema_version = 1
        transaction_id = $transactionId
        install_transaction_id = $transactionId
        readiness_nonce = $readinessNonce
        root_existed_before = $rootExistedBefore
        status = $Status
        error = $ErrorMessage
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        pre_state = $preState
        signed_release_triple = $releaseMetadata
        moved = @($movedEntries)
        task_backups = @($taskBackups)
        readiness_executed = $readinessExecuted
        readiness_state = $readinessState
    }
    [IO.File]::WriteAllText(
        $transactionJournal,
        (($payload | ConvertTo-Json -Depth 8 -Compress) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
}

function Get-Super1PreStateSnapshot([string]$TaskBackupRoot) {
    $taskStates = @()
    foreach ($taskName in $taskNames) {
        $taskBackupPath = Join-Path $TaskBackupRoot ($taskName + ".xml")
        $taskStates += Get-Super1TaskSnapshot -TaskName $taskName -Destination $taskBackupPath
    }
    $credentialHash = $null
    $credentialExists = $false
    $existingRunner = Get-LocalUser -Name $runnerName -ErrorAction SilentlyContinue
    if ($null -ne $existingRunner) {
        $runnerIdentity = "$env:COMPUTERNAME\$runnerName"
        try {
            $runnerSid = ([Security.Principal.NTAccount]::new($runnerIdentity)).Translate([Security.Principal.SecurityIdentifier]).Value
            $runnerProfile = Get-CimInstance Win32_UserProfile -ErrorAction Stop | Where-Object { [string]$_.SID -ceq $runnerSid } | Select-Object -First 1
            if ($runnerProfile -and $runnerProfile.LocalPath) {
                $candidateCredential = Join-Path ([string]$runnerProfile.LocalPath) "AppData\Local\Super1\xm-password.dpapi"
                $credentialExists = Test-Path -LiteralPath $candidateCredential -PathType Leaf
                if ($credentialExists) { $credentialHash = Get-Super1Hash $candidateCredential }
            }
        }
        catch { $credentialExists = $false; $credentialHash = $null }
    }
    return [ordered]@{
        root = $preRootSnapshot
        app = Get-Super1TreeSnapshot $app
        venv = Get-Super1TreeSnapshot $oldVenv
        state = Get-Super1TreeSnapshot $oldState
        control = Get-Super1TreeSnapshot $oldControl
        trust = Get-Super1TreeSnapshot $oldTrust
        terminal = Get-Super1TreeSnapshot $terminalRoot
        portable_marker = Get-Super1TreeSnapshot $portableMarker
        release_archive = Get-Super1TreeSnapshot ([string]$Contract.release_archive)
        release_manifest = Get-Super1TreeSnapshot ([string]$Contract.release_manifest)
        release_signature = Get-Super1TreeSnapshot ([string]$Contract.release_signature)
        runner = [ordered]@{
            exists = $null -ne $existingRunner
            sid = if ($null -eq $existingRunner) { $null } else { [string]$existingRunner.SID }
            enabled = if ($null -eq $existingRunner) { $null } else { [bool]$existingRunner.Enabled }
        }
        credential = [ordered]@{ exists = $credentialExists; sha256 = $credentialHash }
        tasks = @($taskStates)
    }
}

function Move-Super1ExistingToArchive([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source)) { return }
    if (Test-Path -LiteralPath $Destination) { throw "Recovery destination already exists: $Destination" }
    Move-Item -LiteralPath $Source -Destination $Destination
    $script:movedEntries += [pscustomobject]@{ source = $Source; archive = $Destination }
}

function Export-Super1ExistingTask([string]$TaskName, [string]$Destination) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { return $false }
    if ([string]$task.State -in @("Running", "Queued")) {
        throw "Super1 task is active; refusing to replace it during a fresh transaction: $TaskName"
    }
    Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop |
        Set-Content -LiteralPath $Destination -Encoding UTF8
    $script:taskBackups += [pscustomobject]@{ task = $TaskName; xml = $Destination }
    return $true
}

function Protect-Super1RecoveryRoot([string]$Path) {
    $icacls = Join-Path ([Environment]::SystemDirectory) "icacls.exe"
    & $icacls $Path /inheritance:r /grant:r "SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect the Super1 recovery transaction archive." }
}

function Assert-Super1ExtractedPayload([string]$Path, [object]$Manifest) {
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Extracted R8 payload contains a reparse point: $($entry.FullName)"
        }
    }
    $expected = @{}
    foreach ($file in @($Manifest.files)) {
        $relative = ([string]$file.path) -replace '\\', '/'
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.StartsWith('/') -or $relative.Contains('../')) {
            throw "Signed R8 manifest contains an unsafe extracted path: $relative"
        }
        $expected[$relative] = [string]$file.sha256
        $target = Join-Path $Path ($relative -replace '/', '\\')
        if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or
            (Get-Super1Hash $target) -cne [string]$file.sha256) {
            throw "Extracted R8 payload hash mismatch: $relative"
        }
    }
    $actual = @(
        Get-ChildItem -LiteralPath $Path -Recurse -File -Force |
            ForEach-Object { $_.FullName.Substring($Path.Length).TrimStart('\\', '/') -replace '\\', '/' }
    )
    $unexpected = @($actual | Where-Object { -not $expected.ContainsKey($_) })
    if ($unexpected.Count -gt 0 -or $actual.Count -ne $expected.Count) {
        throw "Extracted R8 payload contains unexpected or missing files."
    }
}

$runner = Get-LocalUser -Name $runnerName -ErrorAction SilentlyContinue
$runnerPassword = $null

$state = [string]$Contract.state
$control = [string]$Contract.control
$trust = [string]$Contract.runtime_trust
$icacls = Join-Path ([Environment]::SystemDirectory) "icacls.exe"
$terminalRoot = Split-Path -Parent $terminal
$portableMarker = Join-Path $terminalRoot "portable.txt"
$portableModeDetected = Test-Path -LiteralPath $portableMarker -PathType Leaf
try {
    # The root existence and security snapshot above are intentionally before
    # the first New-Item mutation.
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    New-Item -ItemType Directory -Force -Path $transactionRoot | Out-Null
    Protect-Super1RecoveryRoot -Path $transactionRoot
    $taskBackupRoot = Join-Path $transactionRoot "task-backup"
    New-Item -ItemType Directory -Force -Path $taskBackupRoot | Out-Null
    $preState = Get-Super1PreStateSnapshot -TaskBackupRoot $taskBackupRoot
    $securitySnapshotList = @(
        [pscustomobject]@{ path = $root; snapshot = $preState.root }
        [pscustomobject]@{ path = $app; snapshot = $preState.app }
        [pscustomobject]@{ path = $oldVenv; snapshot = $preState.venv }
        [pscustomobject]@{ path = $oldState; snapshot = $preState.state }
        [pscustomobject]@{ path = $oldControl; snapshot = $preState.control }
        [pscustomobject]@{ path = $oldTrust; snapshot = $preState.trust }
        [pscustomobject]@{ path = $terminalRoot; snapshot = $preState.terminal }
        [pscustomobject]@{ path = [string]$Contract.release_archive; snapshot = $preState.release_archive }
        [pscustomobject]@{ path = [string]$Contract.release_manifest; snapshot = $preState.release_manifest }
        [pscustomobject]@{ path = [string]$Contract.release_signature; snapshot = $preState.release_signature }
    )
    $taskBackups = @($preState.tasks | Where-Object { [bool]$_.exists } | ForEach-Object {
        [pscustomobject]@{ task = [string]$_.task; xml = [string]$_.xml }
    })
    Write-Super1TransactionJournal -Status "PREPARED"

    if ($null -eq $runner) {
        $runnerPassword = Read-Host "Super1Runner Windows parolasi" -AsSecureString
        New-LocalUser -Name $runnerName -Password $runnerPassword -Description "Super1 local non-administrator runner" -AccountNeverExpires -PasswordNeverExpires | Out-Null
        $runnerCreated = $true
        $runner = Get-LocalUser -Name $runnerName -ErrorAction Stop
    }

    $stagedReleaseRoot = Join-Path $transactionRoot "release-staging"
    New-Item -ItemType Directory -Force -Path $stagedReleaseRoot | Out-Null
    $externalArchive = $archive
    $externalManifest = $releaseManifest
    $externalSignature = $releaseSignature
    $stagedArchive = Join-Path $stagedReleaseRoot "super1-forward.zip"
    $stagedManifest = Join-Path $stagedReleaseRoot "super1-forward.manifest.json"
    $stagedSignature = Join-Path $stagedReleaseRoot "super1-forward.manifest.sig"
    Copy-Super1ImmutableFile -Source $externalArchive -Destination $stagedArchive
    Copy-Super1ImmutableFile -Source $externalManifest -Destination $stagedManifest
    Copy-Super1ImmutableFile -Source $externalSignature -Destination $stagedSignature
    $stagedSigned = Assert-SignedReleaseArchive -Archive $stagedArchive -ExpectedProfile "super1" -SourceRoot ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))) -RequireProvenance
    if ([string]$stagedSigned.release_id -cne [string]$preflight.release_id -or
        [string]$stagedSigned.archive_sha256 -cne [string]$preflight.archive_sha256) {
        throw "Staged R8 release triple changed during immutable copy."
    }
    $archive = $stagedArchive
    $releaseManifest = $stagedManifest
    $releaseSignature = $stagedSignature
    Write-Super1TransactionJournal -Status "STAGED_RELEASE_VERIFIED"

    $rootAclBackupPath = Join-Path $transactionRoot "root.acl"
    & $icacls $root /save $rootAclBackupPath /c /q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not back up the existing Super1 root ACL." }
    & $icacls $root /inheritance:r /grant:r "SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove inherited Authenticated Users access from Super1 root." }

    foreach ($item in @(
        @{ source = $archive; destination = [string]$Contract.release_archive; name = "release-archive.previous" },
        @{ source = $releaseManifest; destination = [string]$Contract.release_manifest; name = "release-manifest.previous" },
        @{ source = $releaseSignature; destination = [string]$Contract.release_signature; name = "release-signature.previous" }
    )) {
        Move-Super1ExistingToArchive -Source $item.destination -Destination (Join-Path $transactionRoot $item.name)
        Copy-Super1ImmutableFile -Source $item.source -Destination $item.destination
        $newTargets += $item.destination
    }
    Write-Super1TransactionJournal -Status "RELEASE_TRIPLE_PROMOTED"

    foreach ($taskName in $taskNames) {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        }
    }
    $oldTasksRemoved = $true
    Write-Super1TransactionJournal -Status "TASKS_ARCHIVED"

    $runnerIdentity = "$env:COMPUTERNAME\$runnerName"
    $runnerSid = if ($null -ne $runner) {
        ([Security.Principal.NTAccount]::new($runnerIdentity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } else { $null }
    if ($runnerSid) {
        $runnerCredentialExistedBefore = [bool]$preState.credential.exists
        $runnerProfile = Get-CimInstance Win32_UserProfile -ErrorAction Stop | Where-Object {
            [string]$_.SID -ceq $runnerSid
        } | Select-Object -First 1
        if ($null -ne $runnerProfile -and -not [string]::IsNullOrWhiteSpace([string]$runnerProfile.LocalPath)) {
            $runnerCredentialPath = Join-Path ([string]$runnerProfile.LocalPath) "AppData\Local\Super1\xm-password.dpapi"
            if (Test-Path -LiteralPath $runnerCredentialPath -PathType Leaf) {
                $runnerCredentialBackup = Join-Path $transactionRoot "runner-xm-password.dpapi"
                Copy-Item -LiteralPath $runnerCredentialPath -Destination $runnerCredentialBackup
            }
        }
    }

    $aclBackupRoot = Join-Path $transactionRoot "acl-backup"
    New-Item -ItemType Directory -Force -Path $aclBackupRoot | Out-Null
    foreach ($item in @(
        @{ name = "app"; path = $app },
        @{ name = "venv311"; path = $oldVenv },
        @{ name = "state"; path = $oldState },
        @{ name = "control"; path = $oldControl },
        @{ name = "runtime-trust"; path = $oldTrust },
        @{ name = "terminal"; path = $terminalRoot }
    )) {
        if (Test-Path -LiteralPath $item.path) {
            & $icacls $item.path /save (Join-Path $aclBackupRoot ($item.name + ".acl")) /t /c /q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not back up ACLs for existing Super1 $($item.name)." }
            $aclBackupEntries += [pscustomobject]@{
                path = [string]$item.path
                backup = Join-Path $aclBackupRoot ($item.name + ".acl")
            }
        }
    }
    Move-Super1ExistingToArchive -Source $app -Destination (Join-Path $transactionRoot "app.previous")
    Move-Super1ExistingToArchive -Source $oldVenv -Destination (Join-Path $transactionRoot "venv311.previous")
    Move-Super1ExistingToArchive -Source $oldState -Destination (Join-Path $transactionRoot "state.previous")
    Move-Super1ExistingToArchive -Source $oldControl -Destination (Join-Path $transactionRoot "control.previous")
    Move-Super1ExistingToArchive -Source $oldTrust -Destination (Join-Path $transactionRoot "runtime-trust.previous")
    Move-Super1ExistingToArchive -Source $portableMarker -Destination (Join-Path $transactionRoot "portable.txt.previous")
    if ($portableModeDetected) {
        $portableStateArchive = Join-Path $transactionRoot "portable-state.previous"
        New-Item -ItemType Directory -Force -Path $portableStateArchive | Out-Null
        foreach ($portableName in @("bases", "config", "MQL5", "profiles", "logs", "templates", "tester")) {
            Move-Super1ExistingToArchive `
                -Source (Join-Path $terminalRoot $portableName) `
                -Destination (Join-Path $portableStateArchive $portableName)
        }
    }
    Write-Super1TransactionJournal -Status "OLD_STATE_ARCHIVED"

    $newTargets += $appNext
    Expand-Archive -LiteralPath $archive -DestinationPath $appNext
    Assert-Super1ExtractedPayload -Path $appNext -Manifest $stagedSigned
    $python = Join-Path $root "venv311\Scripts\python.exe"
    $newTargets += $oldVenv
    & $pythonPath -m venv (Join-Path $root "venv311")
    if ($LASTEXITCODE -ne 0) {
        throw "Super1 Python venv creation failed."
    }
    Install-LockedRelease -Python $python -App $appNext
    Move-Item -LiteralPath $appNext -Destination $app
    $newTargets += $app
    Write-Super1TransactionJournal -Status "APP_PROMOTED"
    $helper = Join-Path $app "deploy\super1_secure_task.ps1"
    . $helper
    $runnerSid = Get-Super1SecurePrincipalSid -Identity $runnerIdentity
    & $icacls $root /grant:r "${runnerSid}:(RX)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not grant the Runner root traverse/read access." }
    foreach ($immutableRootFile in @(
        [string]$Contract.release_archive,
        [string]$Contract.release_manifest,
        [string]$Contract.release_signature
    )) {
        & $icacls $immutableRootFile /inheritance:r /grant:r "SYSTEM:(F)" "BUILTIN\Administrators:(F)" "${runnerSid}:(RX)" /setowner "*S-1-5-18" /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not seal Runner read-only access to $immutableRootFile." }
    }
    $newTargets += $state
    New-Super1SecureDirectory -Path $state -RunnerSid $runnerSid -RunnerRights ([Security.AccessControl.FileSystemRights]::Modify) | Out-Null
    $newTargets += $control
    New-Super1SecureDirectory -Path $control -RunnerSid $runnerSid | Out-Null
    $newTargets += $trust
    New-Super1SecureDirectory -Path $trust -RunnerSid $runnerSid | Out-Null
    & $icacls $oldVenv /inheritance:r /grant:r "SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)" "${runnerSid}:(OI)(CI)(RX)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not grant the Runner venv read/execute access." }
    if (Test-Path -LiteralPath $terminalRoot -PathType Container) {
        & $icacls $terminalRoot /inheritance:r /grant:r "SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)" "${runnerSid}:(OI)(CI)(RX)" /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not harden the dedicated XM terminal program tree." }
    }
    Protect-ReleaseApp -App $app -RunnerIdentity "$env:COMPUTERNAME\$runnerName"

    $terminalSignature = Get-AuthenticodeSignature -LiteralPath $terminal
    if ($terminalSignature.Status -ne "Valid" -or [string]$terminalSignature.SignerCertificate.Subject -notmatch "MetaQuotes") { throw "XM terminal Authenticode publisher is not trusted." }
    $terminalPin = [ordered]@{
        schema_version = 1; terminal_path = $terminal; terminal_sha256 = Get-Super1Hash $terminal
        byte_size = (Get-Item -LiteralPath $terminal).Length; version = [Diagnostics.FileVersionInfo]::GetVersionInfo($terminal).FileVersion
        signer_subject = [string]$terminalSignature.SignerCertificate.Subject; signer_thumbprint = [string]$terminalSignature.SignerCertificate.Thumbprint
    }
    Write-Super1Pin -Path (Join-Path $trust "terminal_runtime_pin.json") -Value $terminalPin
    $powershell = Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe"
    $powershellSignature = Get-AuthenticodeSignature -LiteralPath $powershell
    if ($powershellSignature.Status -ne "Valid") { throw "Windows PowerShell Authenticode validation failed." }
    Write-Super1Pin -Path (Join-Path $trust "powershell_runtime_pin.json") -Value ([ordered]@{
        schema_version = 1; powershell_path = $powershell; powershell_sha256 = Get-Super1Hash $powershell
        byte_size = (Get-Item -LiteralPath $powershell).Length; version = [Diagnostics.FileVersionInfo]::GetVersionInfo($powershell).FileVersion
        signer_subject = [string]$powershellSignature.SignerCertificate.Subject; signer_thumbprint = [string]$powershellSignature.SignerCertificate.Thumbprint
    })

    & $python -I -E -B (Join-Path $app "scripts\run_super1_xm_mt5_forward.py") --output-root $state init
    if ($LASTEXITCODE -ne 0) { throw "Fresh Super1 campaign initialization failed." }
    & (Join-Path $app "deploy\finalize_super1_fresh_windows.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Fresh Super1 task finalization failed." }
    $newTasksRegistered = $true
    $readinessStdout = Join-Path $transactionRoot "readiness.stdout.log"
    $readinessStderr = Join-Path $transactionRoot "readiness.stderr.log"
    $readinessScript = Join-Path $app "deploy\test_super1_local_readiness.ps1"
    $readinessProcess = Start-Process -FilePath $powershell -ArgumentList @(
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $readinessScript,
        "-InstallTransactionId", $transactionId, "-ReadinessNonce", $readinessNonce
    ) -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $readinessStdout -RedirectStandardError $readinessStderr
    $readinessExecuted = $true
    $readinessRaw = if (Test-Path -LiteralPath $readinessStdout) { [IO.File]::ReadAllText($readinessStdout) } else { "" }
    $readinessError = if (Test-Path -LiteralPath $readinessStderr) { [IO.File]::ReadAllText($readinessStderr) } else { "" }
    $readinessLines = @($readinessRaw -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($readinessProcess.ExitCode -ne 0 -or $readinessLines.Count -ne 1) {
        throw "Fresh Super1 readiness child failed (exit=$($readinessProcess.ExitCode)): $readinessError"
    }
    try { $readinessEvidence = $readinessLines[0] | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "Fresh Super1 readiness child did not emit one JSON object: $($_.Exception.Message)" }
    if ([string]$readinessEvidence.state -cne "READY_FOR_DEMO_SMOKE" -or
        [string]$readinessEvidence.install_transaction_id -cne $transactionId -or
        [string]$readinessEvidence.transaction_id -cne $transactionId -or
        [string]$readinessEvidence.readiness_nonce -cne $readinessNonce -or
        [string]$readinessEvidence.nonce -cne $readinessNonce) {
        throw "Fresh Super1 readiness evidence is not bound to this transaction and nonce."
    }
    $readinessState = [string]$readinessEvidence.state
    Write-Super1TransactionJournal -Status "READINESS_COMPLETE"
    Write-Super1TransactionJournal -Status "COMPLETE"
    Seal-Super1TransactionTree -Path $transactionRoot
}
catch {
    $failure = [string]$_.Exception.Message
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()
    Invoke-Super1TransactionRollback `
        -RootPath $root `
        -TransactionPath $transactionRoot `
        -TaskNameList $taskNames `
        -OldTasksRemoved $oldTasksRemoved `
        -NewTargetList $newTargets `
        -MovedEntryList $movedEntries `
        -TaskBackupList $taskBackups `
        -CredentialBackup $runnerCredentialBackup `
        -CredentialPath $runnerCredentialPath `
        -RunnerWasCreated $runnerCreated `
        -RunnerName $runnerName `
        -AclBackupList $aclBackupEntries `
        -RootAclBackup $rootAclBackupPath `
        -IcaclsPath $icacls `
        -Errors $rollbackErrors `
        -RootExistedBefore $rootExistedBefore `
        -SecuritySnapshotList $securitySnapshotList `
        -RunnerProcessPath $pythonPath `
        -RunnerHarnessPath (Join-Path $app "scripts\run_super1_xm_mt5_forward.py") `
        -RunnerSid $runnerSid
    if ($runnerCredentialPath -and -not $runnerCredentialExistedBefore -and
        (Test-Path -LiteralPath $runnerCredentialPath -PathType Leaf)) {
        try { Remove-Item -LiteralPath $runnerCredentialPath -Force }
        catch { $rollbackErrors.Add("remove newly created Runner credential metadata: $($_.Exception.Message)") }
    }
    try { Write-Super1TransactionJournal -Status "ROLLED_BACK" -ErrorMessage $failure } catch { $rollbackErrors.Add("journal: $($_.Exception.Message)") }
    try { Seal-Super1TransactionTree -Path $transactionRoot } catch { $rollbackErrors.Add("seal transaction evidence: $($_.Exception.Message)") }
    if ($rollbackErrors.Count -gt 0) {
        throw "Super1 fresh-install transaction failed and rollback was incomplete: $($rollbackErrors -join '; ')"
    }
    throw "Super1 fresh-install transaction rolled back: $failure"
}
finally {
    if ($runnerPassword) { $runnerPassword.Dispose() }
}
