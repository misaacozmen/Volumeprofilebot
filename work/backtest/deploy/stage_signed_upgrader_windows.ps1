[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Archive,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedPythonSha256,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedTerminalSha256,
    [string]$UpgraderSource = (Join-Path $PSScriptRoot "upgrade_super1_signed_app_windows.ps1"),
    [string]$IntegrityScript = (Join-Path $PSScriptRoot "release_integrity.ps1")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security

$ExpectedPSHome = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0")
)
$script:PowerShellExe = [IO.Path]::GetFullPath(
    (Join-Path $ExpectedPSHome "powershell.exe")
)
$CurrentPowerShellExe = [IO.Path]::GetFullPath(
    [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
)
if (-not [IO.Path]::GetFullPath($PSHOME).Equals($ExpectedPSHome, [StringComparison]::OrdinalIgnoreCase) -or
    -not $CurrentPowerShellExe.Equals($script:PowerShellExe, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run the Super1 stage helper only with trusted 64-bit Windows PowerShell."
}

if (-not (Test-Path -LiteralPath $IntegrityScript -PathType Leaf)) {
    throw "Integrity script missing: $IntegrityScript"
}
. $IntegrityScript
$manifest = Assert-SignedReleaseArchive -Archive $Archive -ExpectedProfile "super1"

$resolvedUpgraderSource = [IO.Path]::GetFullPath($UpgraderSource)
if (-not (Test-Path -LiteralPath $resolvedUpgraderSource -PathType Leaf)) {
    throw "Super1 upgrader source script missing: $resolvedUpgraderSource"
}
if ((Get-Item -LiteralPath $resolvedUpgraderSource -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Super1 upgrader source is a reparse point: $resolvedUpgraderSource"
}
$upgraderSha256 = (Get-FileHash -LiteralPath $resolvedUpgraderSource -Algorithm SHA256).Hash.ToLowerInvariant()

$ProgramFiles = [IO.Path]::GetFullPath(
    [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
)
$DeployRoot = [IO.Path]::GetFullPath((Join-Path $ProgramFiles "OtoBacktestDeploy"))
$TargetDir = [IO.Path]::GetFullPath((Join-Path $DeployRoot ("super1-" + $upgraderSha256)))
$TargetUpgrader = [IO.Path]::GetFullPath((Join-Path $TargetDir "upgrade_super1_signed_app_windows.ps1"))

if (-not $TargetDir.StartsWith($DeployRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Staging target directory escapes trusted OtoBacktestDeploy path: $TargetDir"
}

if (-not (Test-Path -LiteralPath $DeployRoot)) {
    New-Item -ItemType Directory -Force -Path $DeployRoot | Out-Null
}
if (-not (Test-Path -LiteralPath $TargetDir)) {
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
}

Copy-Item -LiteralPath $resolvedUpgraderSource -Destination $TargetUpgrader -Force

$icaclsExe = [IO.Path]::GetFullPath((Join-Path ([Environment]::SystemDirectory) "icacls.exe"))

$rootAcl = New-Object Security.AccessControl.DirectorySecurity
$rootAcl.SetAccessRuleProtection($true, $false)
$inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [Security.AccessControl.InheritanceFlags]::ObjectInherit

foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) {
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
[IO.Directory]::SetAccessControl($TargetDir, $rootAcl)

& $icaclsExe (Join-Path $TargetDir "*") /reset /T /C /Q | Out-Null
& $icaclsExe $TargetDir /setowner "*S-1-5-18" /T /C /Q | Out-Null
& $icaclsExe $TargetDir /verify /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "ACL verification failed on staged upgrader directory: $TargetDir"
}

$targetAcl = Get-Acl -LiteralPath $TargetDir
if (-not $targetAcl.AreAccessRulesProtected) {
    throw "Staged upgrader directory ACL inheritance is not protected."
}
$allowedSids = @("S-1-5-18", "S-1-5-32-544")
$rules = $targetAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])
foreach ($rule in $rules) {
    $sid = [string]$rule.IdentityReference.Value
    if ($sid -notin $allowedSids) {
        throw "Staged upgrader directory contains untrusted ACL rule: sid=$sid"
    }
    if (([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::FullControl) -ne [int][Security.AccessControl.FileSystemRights]::FullControl) {
        throw "Staged upgrader directory rule is not FullControl: sid=$sid"
    }
}

$copiedHash = (Get-FileHash -LiteralPath $TargetUpgrader -Algorithm SHA256).Hash.ToLowerInvariant()
if ($copiedHash -cne $upgraderSha256) {
    throw "Staged upgrader copy hash mismatch: expected=$upgraderSha256 actual=$copiedHash"
}

& $TargetUpgrader `
    -Archive $Archive `
    -ExpectedPythonSha256 $ExpectedPythonSha256 `
    -ExpectedTerminalSha256 $ExpectedTerminalSha256 `
    -ExpectedSelfSha256 $upgraderSha256
