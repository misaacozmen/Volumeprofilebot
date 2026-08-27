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
$ExpectedPSHome = [IO.Path]::GetFullPath((Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0"))
$ExpectedPowerShell = [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "powershell.exe"))
$CurrentPowerShell = [IO.Path]::GetFullPath([Diagnostics.Process]::GetCurrentProcess().MainModule.FileName)
if (-not [Environment]::Is64BitProcess -or $PSEdition -cne "Desktop" -or
    -not [IO.Path]::GetFullPath($PSHOME).Equals($ExpectedPSHome, [StringComparison]::OrdinalIgnoreCase) -or
    -not $CurrentPowerShell.Equals($ExpectedPowerShell, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Trusted 64-bit Windows PowerShell is required."
}
$ExpectedBootstrapIntegritySha256 = $ExpectedBootstrapIntegritySha256.ToLowerInvariant()
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archivePath = [IO.Path]::GetFullPath($Archive)
$bootstrapPath = [IO.Path]::GetFullPath($BootstrapIntegrityScript)
$manifestPath = [IO.Path]::ChangeExtension($archivePath, ".manifest.json")
$signaturePath = [IO.Path]::ChangeExtension($archivePath, ".manifest.sig")
foreach ($inputPath in @($archivePath, $manifestPath, $signaturePath, $bootstrapPath)) {
    if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf) -or
        (Get-Item -LiteralPath $inputPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Trusted release input is missing or unsafe: $inputPath"
    }
}

$inputLocks = [Collections.Generic.List[IO.FileStream]]::new()
$targetLocks = [Collections.Generic.List[IO.FileStream]]::new()
$lockErrors = [Collections.Generic.List[string]]::new()
$primaryError = $null
$upgraderOutput = @()
try {
    foreach ($inputPath in @($archivePath, $manifestPath, $signaturePath, $bootstrapPath)) {
        try {
            $handle = [IO.File]::Open($inputPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            [void]$inputLocks.Add($handle)
        }
        catch {
            throw "Trusted release input lock failed: $inputPath; $($_.Exception.Message)"
        }
    }
    $bootstrapStream = $inputLocks[3]
    $bootstrapHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $bootstrapStream.Position = 0
        $bootstrapBytes = $bootstrapHasher.ComputeHash($bootstrapStream)
        $bootstrapStream.Position = 0
    }
    finally { $bootstrapHasher.Dispose() }
    $bootstrapHash = ([BitConverter]::ToString($bootstrapBytes)).Replace("-", "").ToLowerInvariant()
    if ($bootstrapHash -cne $ExpectedBootstrapIntegritySha256) { throw "Bootstrap integrity script hash mismatch." }
    . $bootstrapPath
    $manifest = Assert-SignedReleaseArchive -Archive $archivePath -ExpectedProfile "super1" -RequireProvenance
    $selfPath = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
    $selfHash = (Get-FileHash -LiteralPath $selfPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $selfEntry = @($manifest.files | Where-Object { [string]$_.path -eq "deploy/stage_signed_upgrader_windows.ps1" })
    if ($selfEntry.Count -ne 1 -or $selfHash -cne ([string]$selfEntry[0].sha256).ToLowerInvariant()) { throw "Stage helper is not the exact signed release helper." }
    $upgraderEntry = @($manifest.files | Where-Object { [string]$_.path -eq "deploy/upgrade_super1_signed_app_windows.ps1" })
    if ($upgraderEntry.Count -ne 1) { throw "Manifest must contain exactly one signed upgrader entry." }
    $upgraderSha256 = ([string]$upgraderEntry[0].sha256).ToLowerInvariant()
    if ($selfHash -ceq $upgraderSha256) { throw "Stage helper and upgrader hashes must differ." }

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
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Staging container is a reparse point: $Path" }
        Assert-StageAcl $Path
    }
    function Set-StageAcl([string]$Path, [bool]$Directory) {
        $acl = if ($Directory) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
        $acl.SetAccessRuleProtection($true, $false)
        $inherit = if ($Directory) { [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit } else { [Security.AccessControl.InheritanceFlags]::None }
        foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) { [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), [Security.AccessControl.FileSystemRights]::FullControl, $inherit, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)) }
        if ($Directory) { [IO.Directory]::SetAccessControl($Path, $acl) } else { [IO.File]::SetAccessControl($Path, $acl) }
        $icacls = Join-Path ([Environment]::SystemDirectory) "icacls.exe"
        & $icacls $Path /setowner "*S-1-5-18" /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls owner update failed: $Path" }
        Assert-StageAcl $Path
    }
    function Get-StageStreamSha256([IO.Stream]$Stream) {
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $Stream.Position = 0; $bytes = $hasher.ComputeHash($Stream); $Stream.Position = 0; return ([BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant() }
        finally { $hasher.Dispose() }
    }
    function Assert-StageTarget([string]$Path, [IO.FileStream]$Stream, [string]$ManifestHash) {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Staged target is missing or unsafe: $Path" }
        if ((Get-StageStreamSha256 $Stream) -cne $ManifestHash.ToLowerInvariant()) { throw "Staged target hash mismatch: $Path" }
        Assert-StageAcl $Path
        if (-not $item.IsReadOnly) { throw "Staged target is not ReadOnly: $Path" }
    }

    $programFiles = [IO.Path]::GetFullPath([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles))
    $deployRoot = [IO.Path]::GetFullPath((Join-Path $programFiles "OtoBacktestDeploy"))
    $shaDir = [IO.Path]::GetFullPath((Join-Path $deployRoot ("super1-" + $upgraderSha256)))
    $targetUpgrader = Join-Path $shaDir "upgrade_super1_signed_app_windows.ps1"
    $targetIntegrity = Join-Path $shaDir "release_integrity.ps1"
    if (Test-Path -LiteralPath $deployRoot) { Assert-StageContainer $deployRoot } else { New-Item -ItemType Directory -Path $deployRoot | Out-Null; Set-StageAcl $deployRoot $true }
    if (Test-Path -LiteralPath $shaDir) { Assert-StageContainer $shaDir } else { Assert-StageContainer $deployRoot; New-Item -ItemType Directory -Path $shaDir | Out-Null; Set-StageAcl $shaDir $true }

    $allowed = @("deploy/upgrade_super1_signed_app_windows.ps1", "deploy/release_integrity.ps1")
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $payload = @{}
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
        foreach ($pair in @(@("deploy/upgrade_super1_signed_app_windows.ps1", $targetUpgrader), @("deploy/release_integrity.ps1", $targetIntegrity))) {
            Assert-StageContainer $deployRoot
            Assert-StageContainer $shaDir
            $target = $pair[1]
            $manifestEntry = @($manifest.files | Where-Object { [string]$_.path -eq $pair[0] })[0]
            if (Test-Path -LiteralPath $target) {
                $targetHandle = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                [void]$targetLocks.Add($targetHandle)
                $targetItem = Get-Item -LiteralPath $target -Force
                if ($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing staged reparse point: $target" }
                Assert-StageTarget $target $targetHandle ([string]$manifestEntry.sha256)
                continue
            }
            Assert-StageContainer $deployRoot
            Assert-StageContainer $shaDir
            $stream = $zip.GetEntry($pair[0]).Open()
            try {
                $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $stream.CopyTo($outputStream); $outputStream.Flush($true) } finally { $outputStream.Dispose() }
            }
            finally { $stream.Dispose() }
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -cne ([string]$manifestEntry.sha256).ToLowerInvariant()) { throw "New staged file hash mismatch before ACL: $target" }
            if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "New staged file is a reparse point: $target" }
            Assert-StageContainer $deployRoot
            Assert-StageContainer $shaDir
            Set-StageAcl $target $false
            Assert-StageContainer $deployRoot
            Assert-StageContainer $shaDir
            (Get-Item -LiteralPath $target -Force).IsReadOnly = $true
            $targetHandle = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            [void]$targetLocks.Add($targetHandle)
            Assert-StageTarget $target $targetHandle ([string]$manifestEntry.sha256)
        }
    }
    finally { $zip.Dispose() }
    Assert-StageContainer $deployRoot
    Assert-StageContainer $shaDir
    foreach ($path in @($targetUpgrader, $targetIntegrity)) { Assert-StageContainer (Split-Path -Parent $path); Assert-StageAcl $path }
    if (-not (Get-Item -LiteralPath $targetUpgrader -Force).IsReadOnly -or -not (Get-Item -LiteralPath $targetIntegrity -Force).IsReadOnly) { throw "Staged files must be ReadOnly." }
    Assert-StageAcl $targetUpgrader
    Assert-StageAcl $targetIntegrity
    $upgraderOutput = @(& $targetUpgrader -Archive $archivePath -ExpectedPythonSha256 $ExpectedPythonSha256 -ExpectedTerminalSha256 $ExpectedTerminalSha256 -ExpectedSelfSha256 $upgraderSha256 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Staged upgrader failed: $($upgraderOutput -join ' | ')" }
}
catch {
    $primaryError = $_
}
finally {
    foreach ($lock in $targetLocks) {
        try { $lock.Dispose() } catch { $lockErrors.Add("target lock: $($_.Exception.Message)") }
    }
    foreach ($lock in $inputLocks) {
        try { $lock.Dispose() } catch { $lockErrors.Add("input lock: $($_.Exception.Message)") }
    }
}
if ($primaryError) {
    if ($lockErrors.Count -gt 0) { throw [Exception]::new("Signed staging failed; lock cleanup incomplete: $($lockErrors -join '; ')", $primaryError.Exception) }
    throw $primaryError
}
if ($lockErrors.Count -gt 0) { throw "Signed staging lock cleanup incomplete: $($lockErrors -join '; ')" }
$upgraderOutput
