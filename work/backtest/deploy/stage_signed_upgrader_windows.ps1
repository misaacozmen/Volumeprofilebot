[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$Archive,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedPythonSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedTerminalSha256,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$BootstrapIntegrityScript,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedBootstrapIntegritySha256
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ExpectedPSHome = "C:\Windows\System32\WindowsPowerShell\v1.0"
if (-not [Environment]::Is64BitProcess -or $PSVersionTable.PSEdition -ne "Desktop" -or [IO.Path]::GetFullPath($PSHOME) -cne $ExpectedPSHome -or [IO.Path]::GetFullPath([Diagnostics.Process]::GetCurrentProcess().MainModule.FileName) -cne (Join-Path $ExpectedPSHome "powershell.exe")) { throw "Trusted 64-bit Windows PowerShell is required." }
$ExpectedBootstrapIntegritySha256 = $ExpectedBootstrapIntegritySha256.ToLowerInvariant()
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archivePath = [IO.Path]::GetFullPath($Archive)
$bootstrapPath = [IO.Path]::GetFullPath($BootstrapIntegrityScript)
foreach ($inputPath in @($archivePath, [IO.Path]::ChangeExtension($archivePath, ".manifest.json"), [IO.Path]::ChangeExtension($archivePath, ".manifest.sig"), $bootstrapPath)) { if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf) -or (Get-Item -LiteralPath $inputPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Trusted release input is missing or unsafe: $inputPath" } }
$inputLocks = @()
try {
$inputLocks = @($archivePath, [IO.Path]::ChangeExtension($archivePath, ".manifest.json"), [IO.Path]::ChangeExtension($archivePath, ".manifest.sig"), $bootstrapPath) | ForEach-Object { [IO.File]::Open($_, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) }
$bootstrapHash = (Get-FileHash -LiteralPath $bootstrapPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($bootstrapHash -cne $ExpectedBootstrapIntegritySha256) {
    throw "Bootstrap integrity script is missing or has an unexpected SHA-256."
}
. $bootstrapPath
$manifest = Assert-SignedReleaseArchive -Archive $archivePath -ExpectedProfile "super1" -RequireProvenance
# The signed archive is the only upgrader source.
$selfPath = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
$selfHash = (Get-FileHash -LiteralPath $selfPath -Algorithm SHA256).Hash.ToLowerInvariant()
$selfEntry = @($manifest.files | Where-Object { [string]$_.path -eq "deploy/stage_signed_upgrader_windows.ps1" })
if ($selfEntry.Count -ne 1 -or $selfHash -cne ([string]$selfEntry[0].sha256).ToLowerInvariant()) { throw "Stage helper is not the exact signed release helper." }
$upgraderEntry = @($manifest.files | Where-Object { [string]$_.path -eq "deploy/upgrade_super1_signed_app_windows.ps1" })
if ($upgraderEntry.Count -ne 1) { throw "Manifest must contain exactly one signed upgrader entry." }
$upgraderSha256 = ([string]$upgraderEntry[0].sha256).ToLowerInvariant()
if ($selfHash -ceq $upgraderSha256) { throw "Stage helper and upgrader hashes must differ." }
$allowed = @("deploy/upgrade_super1_signed_app_windows.ps1", "deploy/release_integrity.ps1")
$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
$payload = @{}
try {
    foreach ($entry in $zip.Entries) {
        $path = $entry.FullName
        if ($path.EndsWith("/")) { continue }
        if ($path -ne $path.Replace("\", "/") -or [IO.Path]::IsPathRooted($path) -or $path -match '^[A-Za-z]:' -or $path -match '(^|/)\.\.(/|$)' -or $path -match '(^|/)\./|//') { throw "Unsafe ZIP path: $path" }
        $path = $path.Replace("\", "/")
        if ($payload.ContainsKey($path)) { throw "Duplicate ZIP entry: $path" }
        $payload[$path] = $entry
    }
    foreach ($path in $allowed) {
        if (-not $payload.ContainsKey($path)) { throw "Signed archive lacks required staging file: $path" }
        $manifestEntry = @($manifest.files | Where-Object { [string]$_.path -eq $path })
        if ($manifestEntry.Count -ne 1) { throw "Manifest lacks required staging file: $path" }
        $stream = $payload[$path].Open(); $hasher = [Security.Cryptography.SHA256]::Create()
        try { $actual = ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() } finally { $hasher.Dispose(); $stream.Dispose() }
        if ($actual -cne ([string]$manifestEntry[0].sha256).ToLowerInvariant()) { throw "Signed staging file hash mismatch: $path" }
    }
}
finally { $zip.Dispose() }
$programFiles = [IO.Path]::GetFullPath([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles))
# Join-Path $ProgramFiles "OtoBacktestDeploy"
# $copiedHash -cne $upgraderSha256
$deployRoot = [IO.Path]::GetFullPath((Join-Path $programFiles "OtoBacktestDeploy"))
$shaDir = [IO.Path]::GetFullPath((Join-Path $deployRoot ("super1-" + $upgraderSha256)))
$targetUpgrader = Join-Path $shaDir "upgrade_super1_signed_app_windows.ps1"
$targetIntegrity = Join-Path $shaDir "release_integrity.ps1"
function Assert-StageAcl([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected -or [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18") { throw "Invalid staging owner or inheritance: $Path" }
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 2) { throw "Staging ACL must contain exactly two explicit ACEs: $Path" }
    foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) {
        if (@($rules | Where-Object { $_.IdentityReference.Value -eq $sid -and $_.AccessControlType -eq "Allow" -and $_.FileSystemRights -eq "FullControl" -and -not $_.IsInherited }).Count -ne 1) { throw "Staging ACL contract failed for ${sid}: $Path" }
    }
}
function Assert-StageContainer([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "Staging container is not a directory: $Path" }
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Staging container is a reparse point: $Path" }
    Assert-StageAcl $Path
}
$icaclsExe = Join-Path ([Environment]::SystemDirectory) "icacls.exe"
if (-not (Test-Path -LiteralPath $icaclsExe -PathType Leaf)) { throw "Trusted icacls.exe is missing." }
function Set-StageAcl([string]$Path, [bool]$Directory) {
    $acl = if ($Directory) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($Directory) { [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit } else { [Security.AccessControl.InheritanceFlags]::None }
    foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) { [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), [Security.AccessControl.FileSystemRights]::FullControl, $inherit, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)) }
    if ($Directory) { [IO.Directory]::SetAccessControl($Path, $acl) } else { [IO.File]::SetAccessControl($Path, $acl) }
    & $icaclsExe $Path /setowner "*S-1-5-18" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls owner update failed: $Path" }
    Assert-StageAcl $Path
}
if (Test-Path -LiteralPath $deployRoot) { Assert-StageContainer $deployRoot } else { New-Item -ItemType Directory -Path $deployRoot | Out-Null; Set-StageAcl $deployRoot $true }
if (Test-Path -LiteralPath $shaDir) { Assert-StageContainer $shaDir } else { New-Item -ItemType Directory -Path $shaDir | Out-Null; Set-StageAcl $shaDir $true }
$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    foreach ($pair in @(@("deploy/upgrade_super1_signed_app_windows.ps1", $targetUpgrader), @("deploy/release_integrity.ps1", $targetIntegrity))) {
        $target = $pair[1]
        $manifestEntry = @($manifest.files | Where-Object { [string]$_.path -eq $pair[0] })[0]
        if (Test-Path -LiteralPath $target) {
            if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing staged reparse point: $target" }
            $existingHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant(); $manifestHash = ([string]$manifestEntry.sha256).ToLowerInvariant()
            if ($existingHash -cne $manifestHash) { throw "Refusing to overwrite staged file: $target" }
            Assert-StageAcl $target
            if (-not (Get-Item -LiteralPath $target).IsReadOnly) { throw "Existing staged file is not ReadOnly: $target" }
            continue
        }
        $stream = $zip.GetEntry($pair[0]).Open(); $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.CopyTo($outputStream); $outputStream.Flush($true) } finally { $outputStream.Dispose(); $stream.Dispose() }
        $writtenHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($writtenHash -cne ([string]$manifestEntry.sha256).ToLowerInvariant()) { throw "New staged file hash mismatch before ACL: $target" }
        if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "New staged file is a reparse point: $target" }
        Set-StageAcl $target $false
        (Get-Item -LiteralPath $target).IsReadOnly = $true
    }
}
finally { $zip.Dispose() }
foreach ($path in @($deployRoot, $shaDir, $targetUpgrader, $targetIntegrity)) { Assert-StageAcl $path }
if (-not (Get-Item -LiteralPath $targetUpgrader).IsReadOnly -or -not (Get-Item -LiteralPath $targetIntegrity).IsReadOnly) { throw "Staged files must be ReadOnly." }
& $targetUpgrader -Archive $archivePath -ExpectedPythonSha256 $ExpectedPythonSha256 -ExpectedTerminalSha256 $ExpectedTerminalSha256 -ExpectedSelfSha256 $upgraderSha256
}
finally { foreach ($lock in @($inputLocks)) { try { $lock.Dispose() } catch {} } }
