[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^super1-local-demo-20260905-r6$')][string]$ExpectedReleaseId,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedArchiveSha256,
    [string]$PythonExe = "C:\Program Files\Python311\python.exe",
    [switch]$PlanOnly,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract

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
    if (-not (Test-Path -LiteralPath $directoryPath -PathType Container)) { throw "R6 release directory is missing: $directoryPath" }
    $archivePath = Join-Path $directoryPath "super1-forward.zip"
    $manifestPath = Join-Path $directoryPath "super1-forward.manifest.json"
    $signaturePath = Join-Path $directoryPath "super1-forward.manifest.sig"
    foreach ($path in @($archivePath, $manifestPath, $signaturePath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Signed R6 triple is incomplete: $path" }
    }
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -cne $ArchiveSha256.ToLowerInvariant()) { throw "R6 archive hash does not match -ExpectedArchiveSha256." }
    $sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $signed = Assert-SignedReleaseArchive -Archive $archivePath -ExpectedProfile "super1" -SourceRoot $sourceRoot -RequireProvenance
    if ([string]$signed.archive_file -cne "super1-forward.zip" -or
        [string]$signed.release_id -cne $ReleaseId -or
        [string]$signed.archive_sha256 -cne $actualHash) { throw "R6 signed manifest identity/archive binding is invalid." }
    if ([int]$signed.pytest_collected_count -lt 566 -or
        [int]$signed.artifact_pytest_collected_count -lt 259 -or
        [int]$signed.pytest_pass_count -ne [int]$signed.pytest_collected_count -or
        [int]$signed.artifact_pytest_pass_count -ne [int]$signed.artifact_pytest_collected_count -or
        [int]$signed.pytest_skipped_count -ne 0 -or [int]$signed.artifact_pytest_skipped_count -ne 0) {
        throw "R6 signed test inventory is below 566/259 or contains skipped tests."
    }
    if (Test-Path -LiteralPath (Join-Path $sourceRoot ".git") -PathType Container) {
        $dirty = @(& git -C $sourceRoot status --porcelain)
        if ($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) { throw "R6 source tree is not clean before install." }
        $head = (& git -C $sourceRoot rev-parse HEAD).Trim()
        if ($head -cne [string]$signed.git_commit) { throw "R6 signed commit does not match clean source HEAD." }
    }
    foreach ($critical in @(
        @{ path = "deploy/install_super1_windows.ps1"; actual = $PSCommandPath },
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
        state = "PLAN_ONLY_PRECHECK_PASS"
        mutation = $false
        release_id = $preflight.release_id
        archive = $preflight.archive
        manifest = $preflight.manifest
        signature = $preflight.signature
        archive_sha256 = $preflight.archive_sha256
        git_commit = $preflight.git_commit
        pytest_collected = $preflight.pytest_collected
        artifact_pytest_collected = $preflight.artifact_pytest_collected
        transaction_triple = "same-journal-release-triple-with-rollback"
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
$archiveRoot = Join-Path $root "archive"
$transactionId = [Guid]::NewGuid().ToString("N")
$transactionRoot = Join-Path $archiveRoot ("fresh-install-" + [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" + $transactionId)
$pythonPath = [IO.Path]::GetFullPath($PythonExe)
$terminal = [string]$Contract.terminal
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw "Missing signed Super1 release archive: $archive" }
if (Test-Path -LiteralPath $appNext) { throw "Stale app.next exists; recover the previous transaction before retrying." }
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "Python 3.11 bootstrap runtime is missing: $pythonPath" }
if (-not (Test-Path -LiteralPath $terminal -PathType Leaf)) { throw "Dedicated XM terminal is missing at the contract path: $terminal" }

New-Item -ItemType Directory -Force -Path $root | Out-Null
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
$tasksWereRemoved = $false
$newTasksRegistered = $false
$rootAclBackupPath = $null
$aclBackupEntries = @()
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
        status = $Status
        error = $ErrorMessage
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        signed_release_triple = $releaseMetadata
        moved = @($movedEntries)
        task_backups = @($taskBackups)
    }
    [IO.File]::WriteAllText(
        $transactionJournal,
        (($payload | ConvertTo-Json -Depth 8 -Compress) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
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

$runner = Get-LocalUser -Name $runnerName -ErrorAction SilentlyContinue
$runnerPassword = $null
if ($null -eq $runner) {
    $runnerPassword = Read-Host "Super1Runner Windows parolasi" -AsSecureString
    New-LocalUser -Name $runnerName -Password $runnerPassword -Description "Super1 local non-administrator runner" -AccountNeverExpires -PasswordNeverExpires | Out-Null
    $runnerCreated = $true
}

$state = [string]$Contract.state
$control = [string]$Contract.control
$trust = [string]$Contract.runtime_trust
try {
    New-Item -ItemType Directory -Force -Path $transactionRoot | Out-Null
    Protect-Super1RecoveryRoot -Path $transactionRoot
    Write-Super1TransactionJournal -Status "PREPARED"

    $rootAclBackupPath = Join-Path $transactionRoot "root.acl"
    $icacls = Join-Path ([Environment]::SystemDirectory) "icacls.exe"
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
        Copy-Item -LiteralPath $item.source -Destination $item.destination -Force
        $newTargets += $item.destination
    }
    Write-Super1TransactionJournal -Status "RELEASE_TRIPLE_PROMOTED"

    $taskBackupRoot = Join-Path $transactionRoot "task-backup"
    New-Item -ItemType Directory -Force -Path $taskBackupRoot | Out-Null
    foreach ($taskName in $taskNames) {
        $taskBackupPath = Join-Path $taskBackupRoot ($taskName + ".xml")
        if (Export-Super1ExistingTask -TaskName $taskName -Destination $taskBackupPath) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            $tasksWereRemoved = $true
        }
    }

    $runnerIdentity = "$env:COMPUTERNAME\$runnerName"
    $runnerSid = if ($null -ne $runner) {
        ([Security.Principal.NTAccount]::new($runnerIdentity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } else { $null }
    if ($runnerSid) {
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
        @{ name = "runtime-trust"; path = $oldTrust }
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
    Write-Super1TransactionJournal -Status "OLD_STATE_ARCHIVED"

    Expand-Archive -LiteralPath $archive -DestinationPath $appNext
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
    $newTargets += $state
    New-Super1SecureDirectory -Path $state -RunnerSid $runnerSid -RunnerRights ([Security.AccessControl.FileSystemRights]::Modify) | Out-Null
    $newTargets += $control
    New-Super1SecureDirectory -Path $control -RunnerSid $runnerSid | Out-Null
    $newTargets += $trust
    New-Super1SecureDirectory -Path $trust -RunnerSid $runnerSid | Out-Null
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
    $newTasksRegistered = $true
    Write-Super1TransactionJournal -Status "COMPLETE"
}
catch {
    if (Test-Path -LiteralPath $appNext) { Remove-Item -LiteralPath $appNext -Recurse -Force }
    $failure = [string]$_.Exception.Message
    $rollbackErrors = New-Object Collections.Generic.List[string]
    try {
        foreach ($taskName in $taskNames) {
            if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            }
        }
    } catch { $rollbackErrors.Add("task removal: $($_.Exception.Message)") }
    foreach ($target in $newTargets) {
        if (Test-Path -LiteralPath $target) {
            try {
                $failedTarget = Join-Path $transactionRoot (([IO.Path]::GetFileName($target)) + ".failed")
                if (-not (Test-Path -LiteralPath $failedTarget)) {
                    Move-Item -LiteralPath $target -Destination $failedTarget -Force
                }
            } catch { $rollbackErrors.Add("new target ${target}: $($_.Exception.Message)") }
        }
    }
    foreach ($entry in @($movedEntries | Select-Object -Reverse)) {
        if (Test-Path -LiteralPath $entry.archive -PathType Container -and -not (Test-Path -LiteralPath $entry.source)) {
            try { Move-Item -LiteralPath $entry.archive -Destination $entry.source } catch { $rollbackErrors.Add("restore $($entry.source): $($_.Exception.Message)") }
        }
    }
    foreach ($backup in $taskBackups) {
        try {
            Register-ScheduledTask -TaskName ([string]$backup.task) -Xml (Get-Content -Raw -LiteralPath ([string]$backup.xml)) -Force | Out-Null
        } catch { $rollbackErrors.Add("restore task $($backup.task): $($_.Exception.Message)") }
    }
    if ($runnerCredentialBackup -and $runnerCredentialPath -and (Test-Path -LiteralPath $runnerCredentialBackup)) {
        try { Copy-Item -LiteralPath $runnerCredentialBackup -Destination $runnerCredentialPath -Force } catch { $rollbackErrors.Add("restore Runner credential metadata: $($_.Exception.Message)") }
    }
    if ($runnerCreated) {
        try { Remove-LocalUser -Name $runnerName -ErrorAction Stop } catch { $rollbackErrors.Add("remove newly created Runner: $($_.Exception.Message)") }
    }
    foreach ($entry in @($aclBackupEntries | Select-Object -Reverse)) {
        if (Test-Path -LiteralPath ([string]$entry.backup) -PathType Leaf -and Test-Path -LiteralPath ([string]$entry.path)) {
            try {
                & $icacls ([string]$entry.path) /restore ([string]$entry.backup) /c /q | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "icacls exit code $LASTEXITCODE" }
            }
            catch { $rollbackErrors.Add("restore ACL $($entry.path): $($_.Exception.Message)") }
        }
    }
    if ($rootAclBackupPath -and (Test-Path -LiteralPath $rootAclBackupPath -PathType Leaf)) {
        try {
            & $icacls $root /restore $rootAclBackupPath /c /q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls exit code $LASTEXITCODE" }
        }
        catch { $rollbackErrors.Add("restore ACL ${root}: $($_.Exception.Message)") }
    }
    try { Write-Super1TransactionJournal -Status "ROLLED_BACK" -ErrorMessage $failure } catch { $rollbackErrors.Add("journal: $($_.Exception.Message)") }
    if ($rollbackErrors.Count -gt 0) {
        throw "Super1 fresh-install transaction failed and rollback was incomplete: $($rollbackErrors -join '; ')"
    }
    throw "Super1 fresh-install transaction rolled back: $failure"
}
finally {
    if ($runnerPassword) { $runnerPassword.Dispose() }
}
