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
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedSelfSha256
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$OriginalPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$OriginalPythonHome = $env:PYTHONHOME
$OriginalPythonPath = $env:PYTHONPATH
$SelfScriptLock = $null
$PowerShellHostLock = $null
$TerminalLock = $null
$BootstrapPythonLock = $null
$IntegrityScriptLock = $null
$ReleaseInputLocks = New-Object Collections.Generic.List[IDisposable]
$RuntimeConfigEvidence = @()
$Result = $null
$primaryError = $null
$cleanupErrors = New-Object Collections.Generic.List[string]
$runtimeHelpersReady = $false
$runtimeControlEntered = $false
$ExpectedPSHome = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0")
)
$script:Super1PowerShellExe = [IO.Path]::GetFullPath(
    (Join-Path $ExpectedPSHome "powershell.exe")
)
$CurrentPowerShellExe = [IO.Path]::GetFullPath(
    [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
)
try {
if (-not [IO.Path]::GetFullPath($PSHOME).Equals(
        $ExpectedPSHome,
        [StringComparison]::OrdinalIgnoreCase
    ) -or
    -not $CurrentPowerShellExe.Equals(
        $script:Super1PowerShellExe,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Run the Super1 upgrader only with trusted 64-bit Windows PowerShell."
}
$ExpectedSelfSha256 = $ExpectedSelfSha256.ToLowerInvariant()
$ProgramFiles = [IO.Path]::GetFullPath(
    [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
)
$ProtectedDeployRoot = [IO.Path]::GetFullPath(
    (Join-Path $ProgramFiles "OtoBacktestDeploy")
)
$ProtectedSelfDirectory = [IO.Path]::GetFullPath(
    (Join-Path $ProtectedDeployRoot ("super1-" + $ExpectedSelfSha256))
)
$ProtectedSelfPath = [IO.Path]::GetFullPath(
    (Join-Path $ProtectedSelfDirectory "upgrade_super1_signed_app_windows.ps1")
)
$CurrentSelfPath = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
if (-not $CurrentSelfPath.Equals(
        $ProtectedSelfPath,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Run the Super1 upgrader only from its SHA-addressed protected deploy path."
}
foreach ($protectedPath in @(
    $ProgramFiles,
    $ProtectedDeployRoot,
    $ProtectedSelfDirectory,
    $ProtectedSelfPath
)) {
    if (-not (Test-Path -LiteralPath $protectedPath) -or
        (Get-Item -LiteralPath $protectedPath -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) {
        throw "Protected Super1 upgrader path is missing or is a reparse point: $protectedPath"
    }
}
$SelfScriptLock = [IO.File]::Open(
    $ProtectedSelfPath,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
$SelfScriptHasher = [Security.Cryptography.SHA256]::Create()
try {
    $SelfScriptSha256 = [BitConverter]::ToString(
        $SelfScriptHasher.ComputeHash($SelfScriptLock)
    ).Replace("-", "").ToLowerInvariant()
    $SelfScriptLock.Position = 0
}
finally { $SelfScriptHasher.Dispose() }
if ($SelfScriptSha256 -cne $ExpectedSelfSha256) {
    throw "Protected Super1 upgrader does not match its mandatory self SHA-256 pin."
}
foreach ($protectedPath in @(
    $ProtectedDeployRoot,
    $ProtectedSelfDirectory,
    $ProtectedSelfPath
)) {
    $protectedItem = Get-Item -LiteralPath $protectedPath -Force
    $protectedSecurity = if ($protectedItem.PSIsContainer) {
        [IO.Directory]::GetAccessControl(
            $protectedPath,
            [Security.AccessControl.AccessControlSections]::Access -bor
                [Security.AccessControl.AccessControlSections]::Owner
        )
    }
    else {
        [IO.File]::GetAccessControl(
            $protectedPath,
            [Security.AccessControl.AccessControlSections]::Access -bor
                [Security.AccessControl.AccessControlSections]::Owner
        )
    }
    if (-not $protectedSecurity.AreAccessRulesProtected -or
        [string]$protectedSecurity.GetOwner(
            [Security.Principal.SecurityIdentifier]
        ).Value -cne "S-1-5-18") {
        throw "Protected Super1 upgrader path is not SYSTEM-owned with protected ACL: $protectedPath"
    }
    $protectedRules = @($protectedSecurity.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    ))
    if ($protectedRules.Count -ne 2) {
        throw "Protected Super1 upgrader path must have exactly SYSTEM/BA ACL entries: $protectedPath"
    }
    $protectedSeen = @{}
    foreach ($rule in $protectedRules) {
        $sid = [string]$rule.IdentityReference.Value
        if ($rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $sid -notin @("S-1-5-18", "S-1-5-32-544") -or
            [int]$rule.FileSystemRights -ne
                [int][Security.AccessControl.FileSystemRights]::FullControl -or
            $protectedSeen.ContainsKey($sid)) {
            throw "Protected Super1 upgrader DACL is not exact SYSTEM/BA FullControl: $protectedPath"
        }
        $protectedSeen[$sid] = $true
    }
    if (-not $protectedSeen.ContainsKey("S-1-5-18") -or
        -not $protectedSeen.ContainsKey("S-1-5-32-544")) {
        throw "Protected Super1 upgrader path lacks exact trusted principals: $protectedPath"
    }
}
if (-not ((Get-Item -LiteralPath $ProtectedSelfPath -Force).Attributes -band
    [IO.FileAttributes]::ReadOnly)) {
    throw "Protected Super1 upgrader is not read-only."
}
$TrustedPSModulePath = [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "Modules"))
$env:PSModulePath = $TrustedPSModulePath
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$ScheduledTasksModule = [IO.Path]::GetFullPath(
    (Join-Path $TrustedPSModulePath "ScheduledTasks\ScheduledTasks.psd1")
)
$SecurityModule = [IO.Path]::GetFullPath(
    (Join-Path $TrustedPSModulePath "Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1")
)
$script:Super1IcaclsExe = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "icacls.exe")
)
foreach ($trustedDependency in @(
    $ScheduledTasksModule,
    $SecurityModule,
    $script:Super1IcaclsExe,
    $script:Super1PowerShellExe
)) {
    if (-not (Test-Path -LiteralPath $trustedDependency -PathType Leaf)) {
        throw "Trusted Windows dependency is missing: $trustedDependency"
    }
    if ((Get-Item -LiteralPath $trustedDependency -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Trusted Windows dependency is a reparse point: $trustedDependency"
    }
}
if ((Get-Item -LiteralPath $script:Super1PowerShellExe -Force).Attributes -band
    [IO.FileAttributes]::ReparsePoint) {
    throw "Trusted Windows PowerShell executable is a reparse point."
}
$loadedSecurityModule = @(Import-Module -Name $SecurityModule -Force -PassThru -ErrorAction Stop)
if ($loadedSecurityModule.Count -ne 1 -or -not [IO.Path]::GetFullPath([string]$loadedSecurityModule[0].Path).Equals($SecurityModule, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected Microsoft.PowerShell.Security module path."
}
$PowerShellHostLock = [IO.File]::Open(
    $script:Super1PowerShellExe,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
$PowerShellSignature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature `
    -LiteralPath $script:Super1PowerShellExe
if ([string]$PowerShellSignature.Status -cne "Valid" -or
    -not $PowerShellSignature.SignerCertificate -or
    [string]$PowerShellSignature.SignerCertificate.Subject -notmatch
        '^CN=Microsoft Windows, O=Microsoft Corporation,') {
    throw "Trusted Windows PowerShell signature/publisher validation failed."
}
$PowerShellHasher = [Security.Cryptography.SHA256]::Create()
try {
    $PowerShellHostSha256 = [BitConverter]::ToString(
        $PowerShellHasher.ComputeHash($PowerShellHostLock)
    ).Replace("-", "").ToLowerInvariant()
    $PowerShellHostLock.Position = 0
}
finally { $PowerShellHasher.Dispose() }
$PowerShellHostEvidence = [pscustomobject]@{
    path = $script:Super1PowerShellExe
    sha256 = $PowerShellHostSha256
    signer_subject = [string]$PowerShellSignature.SignerCertificate.Subject
    signer_thumbprint = [string]$PowerShellSignature.SignerCertificate.Thumbprint
    product_version = [Diagnostics.FileVersionInfo]::GetVersionInfo(
        $script:Super1PowerShellExe
    ).ProductVersion
}
$loadedScheduledTasksModule = @(Import-Module -Name $ScheduledTasksModule -Force -PassThru -ErrorAction Stop)
if ($loadedScheduledTasksModule.Count -ne 1 -or -not [IO.Path]::GetFullPath([string]$loadedScheduledTasksModule[0].Path).Equals($ScheduledTasksModule, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected ScheduledTasks module path."
}
$Root = [IO.Path]::GetFullPath("C:\Super1")
$MainTask = "Super1XM"
$WatchdogTask = "Super1Watchdog"
$ExpectedIntegrityScriptSha256 = "6e7dae16e7238fb75cbe81d9d614647d3530cd83a67cc9b42c80c580d7c98b27"

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $resolvedChild = [IO.Path]::GetFullPath($Child)
    $resolvedParent = [IO.Path]::GetFullPath($Parent)
    return (
        $resolvedChild.Equals($resolvedParent, [StringComparison]::OrdinalIgnoreCase) -or
        $resolvedChild.StartsWith(
            ($resolvedParent + [IO.Path]::DirectorySeparatorChar),
            [StringComparison]::OrdinalIgnoreCase
        )
    )
}

function Assert-Super1ChildPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-PathWithin -Child $Path -Parent $Root) -or
        [IO.Path]::GetFullPath($Path).Equals($Root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe Super1 transaction path: $Path"
    }
}

function Get-AclIdentitySid {
    param([Parameter(Mandatory = $true)][Security.Principal.IdentityReference]$Identity)
    try {
        return $Identity.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        throw "Could not resolve ACL identity to a SID: $Identity"
    }
}

function Assert-NoUntrustedDeleteChild {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$TrustedSids
    )
    $deleteChild = [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
    $parentAcl = Get-Acl -LiteralPath $Path
    $ownerSid = [string]$parentAcl.GetOwner(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($ownerSid -notin $TrustedSids) {
        throw "Untrusted SID owns protected transaction parent $Path`: $ownerSid"
    }
    foreach ($rule in @($parentAcl.Access)) {
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)
        ) {
            continue
        }
        $sid = Get-AclIdentitySid -Identity $rule.IdentityReference
        if (
            $sid -notin $TrustedSids -and
            ($rule.FileSystemRights -band $deleteChild) -eq $deleteChild
        ) {
            throw "Untrusted SID can delete protected transaction children through parent $Path`: $sid"
        }
    }
}

function Set-ExactSuper1DirectoryAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Collections.IDictionary]$RightsBySid
    )
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($entry in $RightsBySid.GetEnumerator()) {
        [void]$acl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new([string]$entry.Key),
                [Security.AccessControl.FileSystemRights]$entry.Value,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    [IO.Directory]::SetAccessControl($Path, $acl)
}

function Set-ExactSuper1FileAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Collections.IDictionary]$RightsBySid
    )
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in $RightsBySid.GetEnumerator()) {
        [void]$acl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new([string]$entry.Key),
                [Security.AccessControl.FileSystemRights]$entry.Value,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    [IO.File]::SetAccessControl($Path, $acl)
}

function Assert-ExactSuper1DirectoryAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Collections.IDictionary]$RightsBySid
    )
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected -or
        [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18") {
        throw "Super1 directory is not protected and SYSTEM-owned: $Path"
    }
    $actual = @{}
    $inheritance = [int](
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    foreach ($rule in $acl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )) {
        $sid = [string]$rule.IdentityReference.Value
        if ($rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            [int]$rule.InheritanceFlags -ne $inheritance -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
            -not $RightsBySid.Contains($sid)) {
            throw "Super1 directory has an unexpected ACL rule $Path`: $sid"
        }
        $current = if ($actual.ContainsKey($sid)) { [int]$actual[$sid] } else { 0 }
        $actual[$sid] = $current -bor [int]$rule.FileSystemRights
    }
    foreach ($entry in $RightsBySid.GetEnumerator()) {
        $expectedRights = [int]$entry.Value -bor
            [int][Security.AccessControl.FileSystemRights]::Synchronize
        if ([int]$actual[[string]$entry.Key] -ne $expectedRights) {
            throw "Super1 directory rights mismatch $Path`: $($entry.Key)"
        }
    }
    if ($actual.Keys.Count -ne $RightsBySid.Keys.Count) {
        throw "Super1 directory ACL is not exact: $Path"
    }
}

function Assert-ExactSuper1FileAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Collections.IDictionary]$RightsBySid
    )
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected -or
        [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18") {
        throw "Super1 file is not protected and SYSTEM-owned: $Path"
    }
    $actual = @{}
    foreach ($rule in $acl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )) {
        $sid = [string]$rule.IdentityReference.Value
        if ($rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
            -not $RightsBySid.Contains($sid)) {
            throw "Super1 file has an unexpected ACL rule $Path`: $sid"
        }
        $current = if ($actual.ContainsKey($sid)) { [int]$actual[$sid] } else { 0 }
        $actual[$sid] = $current -bor [int]$rule.FileSystemRights
    }
    foreach ($entry in $RightsBySid.GetEnumerator()) {
        $expectedRights = [int]$entry.Value -bor
            [int][Security.AccessControl.FileSystemRights]::Synchronize
        if ([int]$actual[[string]$entry.Key] -ne $expectedRights) {
            throw "Super1 file rights mismatch $Path`: $($entry.Key)"
        }
    }
    if ($actual.Keys.Count -ne $RightsBySid.Keys.Count) {
        throw "Super1 file ACL is not exact: $Path"
    }
}

function Assert-PrivateDirectoryAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CallerSid,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Private transaction directory still inherits ACLs: $Path"
    }
    if (
        [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne
            "S-1-5-18"
    ) {
        throw "Private transaction directory is not owned by SYSTEM: $Path"
    }
    $trustedSids = @("S-1-5-18", "S-1-5-32-544")
    $rightsBySid = @{}
    $requiredInheritance = (
        [int][Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [int][Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    foreach ($rule in @($acl.Access)) {
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.IsInherited -or
            [int]$rule.InheritanceFlags -ne $requiredInheritance -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None
        ) {
            throw "Private transaction directory has an unexpected ACL rule: $Path"
        }
        $sid = Get-AclIdentitySid -Identity $rule.IdentityReference
        if ($sid -eq $RunnerSid) {
            throw "Super1 runner unexpectedly has an ACE on private transaction storage: $Path"
        }
        if ($sid -eq $CallerSid -and $CallerSid -notin $trustedSids) {
            throw "Super1 upgrade caller unexpectedly has a direct ACE on private transaction storage: $Path"
        }
        if ($sid -notin $trustedSids) {
            throw "Untrusted SID has an ACE on private transaction storage $Path`: $sid"
        }
        $current = if ($rightsBySid.ContainsKey($sid)) { [int]$rightsBySid[$sid] } else { 0 }
        $rightsBySid[$sid] = $current -bor [int]$rule.FileSystemRights
    }
    $full = [int][Security.AccessControl.FileSystemRights]::FullControl
    foreach ($sid in $trustedSids) {
        if ([int]$rightsBySid[$sid] -ne $full) {
            throw "Private transaction storage lacks trusted FullControl $Path`: $sid"
        }
    }
    if (@($rightsBySid.Keys).Count -ne $trustedSids.Count) {
        throw "Private transaction storage lacks an exact trusted FullControl ACL: $Path"
    }
}

function Protect-TransactionArchiveRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CallerSid,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Transaction archive root cannot be a reparse point: $Path"
    }
    Set-ExactSuper1DirectoryAcl -Path $Path -RightsBySid @{
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
    }
    & $script:Super1IcaclsExe $Path /setowner "*S-1-5-18" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not set trusted ownership on the Super1 transaction archive root."
    }
    Assert-PrivateDirectoryAcl -Path $Path -CallerSid $CallerSid -RunnerSid $RunnerSid
}

function Protect-PrivateFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CallerSid,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    Set-ExactSuper1FileAcl -Path $Path -RightsBySid @{
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
    }
    & $script:Super1IcaclsExe $Path /setowner "*S-1-5-18" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not set trusted ownership on private Super1 transaction file: $Path"
    }
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Private Super1 transaction file still inherits ACLs: $Path"
    }
    if (
        [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne
            "S-1-5-18"
    ) {
        throw "Private Super1 transaction file is not owned by SYSTEM: $Path"
    }
    $trustedSids = @("S-1-5-18", "S-1-5-32-544")
    $rightsBySid = @{}
    foreach ($rule in @($acl.Access)) {
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.IsInherited
        ) {
            throw "Private Super1 transaction file has an unexpected ACL rule: $Path"
        }
        $sid = Get-AclIdentitySid -Identity $rule.IdentityReference
        if ($sid -eq $RunnerSid) {
            throw "Super1 runner unexpectedly has an ACE on private transaction file: $Path"
        }
        if ($sid -eq $CallerSid -and $CallerSid -notin $trustedSids) {
            throw "Super1 upgrade caller unexpectedly has a direct ACE on private transaction file: $Path"
        }
        if ($sid -notin $trustedSids) {
            throw "Untrusted SID has an ACE on private Super1 transaction file $Path`: $sid"
        }
        $current = if ($rightsBySid.ContainsKey($sid)) { [int]$rightsBySid[$sid] } else { 0 }
        $rightsBySid[$sid] = $current -bor [int]$rule.FileSystemRights
    }
    $full = [int][Security.AccessControl.FileSystemRights]::FullControl
    foreach ($sid in $trustedSids) {
        if ([int]$rightsBySid[$sid] -ne $full) {
            throw "Private Super1 transaction file lacks trusted FullControl $Path`: $sid"
        }
    }
    if (@($rightsBySid.Keys).Count -ne $trustedSids.Count) {
        throw "Private Super1 transaction file lacks an exact trusted FullControl ACL: $Path"
    }
}

function Stop-Super1Tasks {
    Stop-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $MainTask -ErrorAction SilentlyContinue
}

function Get-Super1PythonProcesses {
    $venvRoot = [IO.Path]::GetFullPath((Join-Path $Root "venv311"))
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $name = [string]$_.Name
                $commandLine = [string]$_.CommandLine
                $executable = [string]$_.ExecutablePath
                $isPython = $name -ieq "python.exe" -or $name -ieq "pythonw.exe"
                $belongsToSuper1 = (
                    (-not [string]::IsNullOrWhiteSpace($commandLine) -and
                        $commandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
                    (-not [string]::IsNullOrWhiteSpace($executable) -and
                        (Test-PathWithin -Child $executable -Parent $venvRoot))
                )
                $isPython -and $belongsToSuper1
            }
    )
}

function Get-Super1TerminalProcesses {
    $terminal = [IO.Path]::GetFullPath((Join-Path $Root "mt5-clean5833\terminal64.exe"))
    $matching = @(
        Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction Stop |
            Where-Object {
                -not [string]::IsNullOrWhiteSpace([string]$_.ExecutablePath) -and
                [IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
                    $terminal,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    return @($matching | ForEach-Object {
        $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction Stop
        if ([uint32]$owner.ReturnValue -ne 0 -or [string]::IsNullOrWhiteSpace([string]$owner.User)) {
            throw "Could not resolve the owner of Super1 terminal process $($_.ProcessId)."
        }
        $account = if ([string]::IsNullOrWhiteSpace([string]$owner.Domain)) {
            [string]$owner.User
        } else { "$($owner.Domain)\$($owner.User)" }
        $ownerSid = (New-Object Security.Principal.NTAccount($account)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        [pscustomobject]@{
            ProcessId = [int]$_.ProcessId
            ExecutablePath = [string]$_.ExecutablePath
            OwnerSid = [string]$ownerSid
        }
    })
}

function Get-Super1TaskRunnerSid {
    $identity = [string](Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop).Principal.UserId
    if ($identity -match '^S-\d-(?:\d+-)+\d+$') {
        return [Security.Principal.SecurityIdentifier]::new($identity).Value
    }
    return (New-Object Security.Principal.NTAccount($identity)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
}

function Get-Super1ProcessOwnerSid {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [switch]$AllowUnresolved
    )
    $owner = $null
    try {
        $owner = Invoke-CimMethod `
            -InputObject $Process `
            -MethodName GetOwner `
            -ErrorAction Stop
    }
    catch {
        if ($AllowUnresolved) { return $null }
        throw "Could not prove the owner of process $($Process.ProcessId)."
    }
    if ([uint32]$owner.ReturnValue -ne 0 -or
        [string]::IsNullOrWhiteSpace([string]$owner.User)) {
        if ($AllowUnresolved) { return $null }
        throw "Could not prove the owner of process $($Process.ProcessId)."
    }
    $identity = if ([string]::IsNullOrWhiteSpace([string]$owner.Domain)) {
        [string]$owner.User
    } else { "$($owner.Domain)\$($owner.User)" }
    try {
        return (New-Object Security.Principal.NTAccount($identity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch {
        if ($AllowUnresolved) { return $null }
        throw "Could not translate the owner of process $($Process.ProcessId)."
    }
}

function Get-Super1RunnerProcesses {
    $expectedRunnerSid = Get-Super1TaskRunnerSid
    $matches = New-Object Collections.Generic.List[object]
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ((Get-Super1ProcessOwnerSid `
                -Process $process `
                -AllowUnresolved) -ceq $expectedRunnerSid) {
            $matches.Add([pscustomobject]@{
                ProcessId = [int]$process.ProcessId
                Name = [string]$process.Name
                ExecutablePath = [string]$process.ExecutablePath
            })
        }
    }
    return $matches.ToArray()
}

function Get-UnexpectedSuper1RunnerProcesses {
    param([switch]$PreserveTerminal)
    $terminal = [IO.Path]::GetFullPath((Join-Path $Root "mt5-clean5833\terminal64.exe"))
    return @(Get-Super1RunnerProcesses | Where-Object {
        if (-not $PreserveTerminal) { return $true }
        if ([string]::IsNullOrWhiteSpace([string]$_.ExecutablePath)) { return $true }
        return -not [IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
            $terminal,
            [StringComparison]::OrdinalIgnoreCase
        )
    })
}

function Wait-Super1Stopped {
    param(
        [int]$TimeoutSeconds = 30,
        [switch]$PreserveTerminal
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $mainState = [string](Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop).State
        $watchdogState = [string](Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop).State
        $processes = @(Get-Super1PythonProcesses)
        $terminalProcesses = @(Get-Super1TerminalProcesses)
        $expectedRunnerSid = Get-Super1TaskRunnerSid
        $terminalOwnerMismatch = @($terminalProcesses | Where-Object {
            [string]$_.OwnerSid -cne $expectedRunnerSid
        })
        $unexpectedRunnerProcesses = @(
            Get-UnexpectedSuper1RunnerProcesses -PreserveTerminal:$PreserveTerminal
        )
        $taskIsActive = (
            $mainState -in @("Running", "Queued") -or
            $watchdogState -in @("Running", "Queued")
        )
        if (-not $taskIsActive -and $processes.Count -eq 0 -and
            ((-not $PreserveTerminal -and $terminalProcesses.Count -eq 0) -or
                ($PreserveTerminal -and $terminalProcesses.Count -le 1)) -and
            $terminalOwnerMismatch.Count -eq 0 -and
            $unexpectedRunnerProcesses.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    $processes = @(Get-Super1PythonProcesses)
    $terminalProcesses = @(Get-Super1TerminalProcesses)
    $processIds = @($processes | ForEach-Object { [string]$_.ProcessId }) -join ","
    $terminalPids = @($terminalProcesses | ForEach-Object { [string]$_.ProcessId }) -join ","
    $runnerPids = @($unexpectedRunnerProcesses | ForEach-Object {
        "$($_.ProcessId):$($_.Name)"
    }) -join ","
    throw "Super1 tasks/processes did not stop: main=$mainState watchdog=$watchdogState python_pids=$processIds terminal_pids=$terminalPids unexpected_runner_pids=$runnerPids"
}

function Stop-Super1RuntimeForRollback {
    param([switch]$PreserveTerminal)
    Stop-Super1Tasks
    try {
        Wait-Super1Stopped -TimeoutSeconds 5 -PreserveTerminal:$PreserveTerminal
        return
    }
    catch {
        $gracefulFailure = $_
    }
    if ($PreserveTerminal -and @(Get-Super1TerminalProcesses).Count -gt 1) {
        throw "Refusing to choose among multiple canonical Super1 terminal processes."
    }

    Stop-Super1Tasks
    foreach ($process in @(Get-Super1PythonProcesses)) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if (-not $PreserveTerminal) {
        foreach ($process in @(Get-Super1TerminalProcesses)) {
            $expectedRunnerSid = Get-Super1TaskRunnerSid
            if ([string]$process.OwnerSid -cne $expectedRunnerSid) {
                throw "Refusing to stop canonical Super1 terminal owned by unexpected SID $($process.OwnerSid)."
            }
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
    for ($pass = 0; $pass -lt 3; $pass++) {
        $unexpectedRunnerProcesses = @(
            Get-UnexpectedSuper1RunnerProcesses -PreserveTerminal:$PreserveTerminal
        )
        foreach ($process in $unexpectedRunnerProcesses) {
            Stop-Process `
                -Id ([int]$process.ProcessId) `
                -Force `
                -ErrorAction SilentlyContinue
        }
        if ($unexpectedRunnerProcesses.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    }
    Stop-Super1Tasks
    try {
        Wait-Super1Stopped -TimeoutSeconds 30 -PreserveTerminal:$PreserveTerminal
    }
    catch {
        throw "Super1 rollback stop gate failed after forced process shutdown; graceful=$($gracefulFailure.Exception.Message); forced=$($_.Exception.Message)"
    }
}

function Copy-FileCreateNew {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target
    )
    $sourceStream = [IO.File]::Open(
        $Source,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $targetStream = $null
    try {
        $targetStream = [IO.File]::Open(
            $Target,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $sourceStream.CopyTo($targetStream)
        $targetStream.Flush($true)
    }
    finally {
        if ($targetStream) { $targetStream.Dispose() }
        $sourceStream.Dispose()
    }
}

function Get-TrustedBootstrapPythonEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$PyvenvConfig,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$CallerSid
    )
    if (-not (Test-Path -LiteralPath $PyvenvConfig -PathType Leaf)) {
        throw "Existing Super1 environment has no pyvenv.cfg bootstrap provenance."
    }
    if ((Get-Item -LiteralPath $PyvenvConfig -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 pyvenv.cfg bootstrap provenance cannot be a reparse point."
    }
    $config = @{}
    foreach ($line in [IO.File]::ReadAllLines($PyvenvConfig)) {
        if ($line -notmatch '^\s*([^=]+?)\s*=\s*(.*?)\s*$') { continue }
        $key = $Matches[1].Trim().ToLowerInvariant()
        if ($config.ContainsKey($key)) {
            throw "Super1 pyvenv.cfg contains a duplicate bootstrap key: $key"
        }
        $config[$key] = $Matches[2].Trim()
    }
    $python = if ($config.ContainsKey("executable")) {
        [IO.Path]::GetFullPath([string]$config["executable"])
    }
    elseif ($config.ContainsKey("home")) {
        [IO.Path]::GetFullPath((Join-Path ([string]$config["home"]) "python.exe"))
    }
    else {
        throw "Super1 pyvenv.cfg has no base Python executable provenance."
    }
    if ([IO.Path]::GetFileName($python) -cne "python.exe") {
        throw "Super1 bootstrap provenance must resolve to python.exe."
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Trusted Super1 bootstrap Python is missing: $python"
    }

    $programFilesRoots = @(
        [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object {
        [IO.Path]::GetFullPath($_).TrimEnd([IO.Path]::DirectorySeparatorChar)
    } | Select-Object -Unique
    $matchedRoot = $null
    foreach ($root in $programFilesRoots) {
        if ($python.StartsWith(
            ($root + [IO.Path]::DirectorySeparatorChar),
            [StringComparison]::OrdinalIgnoreCase
        )) {
            $matchedRoot = $root
            break
        }
    }
    if (-not $matchedRoot) {
        throw "Super1 bootstrap Python is outside Program Files: $python"
    }

    $trustedMutationSids = @(
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    )
    if ($RunnerSid -in $trustedMutationSids -or $CallerSid -in @("S-1-5-18", "S-1-5-32-544")) {
        throw "Super1 bootstrap trust identities are not separated as expected."
    }
    $mutationMask = [int64](
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    ) -bor 0x10000000L -bor 0x40000000L
    $pathsToCheck = New-Object Collections.Generic.List[string]
    $pathsToCheck.Add($python)
    $current = [IO.Path]::GetFullPath((Split-Path -Parent $python))
    while ($true) {
        $pathsToCheck.Add($current)
        if ($current.Equals($matchedRoot, [StringComparison]::OrdinalIgnoreCase)) { break }
        $next = [IO.Path]::GetFullPath((Split-Path -Parent $current))
        if ($next.Equals($current, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Super1 bootstrap Python trust walk escaped Program Files."
        }
        $current = $next
    }
    foreach ($checkedPath in $pathsToCheck) {
        $item = Get-Item -LiteralPath $checkedPath -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Super1 bootstrap Python path contains a reparse point: $checkedPath"
        }
        $acl = Get-Acl -LiteralPath $checkedPath
        $ownerSid = [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($ownerSid -notin $trustedMutationSids) {
            throw "Super1 bootstrap Python path has an untrusted owner: $checkedPath owner=$ownerSid"
        }
        foreach ($rule in $acl.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        )) {
            if (
                $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)
            ) {
                continue
            }
            $sid = [string]$rule.IdentityReference.Value
            $rights = [int64][int]$rule.FileSystemRights
            if ($rights -lt 0) { $rights += 0x100000000L }
            if ($sid -notin $trustedMutationSids -and ($rights -band $mutationMask) -ne 0) {
                throw "Untrusted SID can mutate the Super1 bootstrap Python path $checkedPath`: $sid"
            }
        }
    }

    $lock = [IO.File]::Open(
        $python,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $hash = Get-ReleaseSha256 -Path $python
        if ($hash -cne $ExpectedSha256.ToLowerInvariant()) {
            throw "Super1 bootstrap Python does not match the pinned SHA-256."
        }
        $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature `
            -LiteralPath $python
        if (
            [string]$signature.Status -cne "Valid" -or
            -not $signature.SignerCertificate -or
            [string]$signature.SignerCertificate.Subject -notmatch "Python Software Foundation"
        ) {
            throw "Super1 bootstrap Python has no valid Python Software Foundation signature."
        }
        $version = [string](Get-Item -LiteralPath $python).VersionInfo.ProductVersion
        if ($version -notmatch '^3\.11\.') {
            throw "Super1 bootstrap Python is not an approved Python 3.11 runtime."
        }
        if ((Get-ReleaseSha256 -Path $python) -cne $hash) {
            throw "Super1 bootstrap Python changed while its read lock was acquired."
        }
        return [pscustomobject]@{
            path = $python
            sha256 = $hash
            signature_status = [string]$signature.Status
            signer_subject = [string]$signature.SignerCertificate.Subject
            product_version = $version
            lock = $lock
        }
    }
    catch {
        $lock.Dispose()
        throw
    }
}

function Get-Super1TerminalPinEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$PointerPath,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    $terminalRoot = [IO.Path]::GetFullPath($ExpectedRoot)
    $terminal = [IO.Path]::GetFullPath((Join-Path $terminalRoot "terminal64.exe"))
    foreach ($required in @($PointerPath, $terminalRoot, $terminal)) {
        if (-not (Test-Path -LiteralPath $required) -or
            (Get-Item -LiteralPath $required -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Super1 terminal pin dependency is missing or is a reparse point: $required"
        }
    }
    $pointerLock = [IO.File]::Open(
        $PointerPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $terminalLock = $null
    try {
        $reader = New-Object IO.StreamReader($pointerLock, [Text.Encoding]::UTF8, $true, 1024, $true)
        try { $pointerValue = [IO.Path]::GetFullPath($reader.ReadToEnd().Trim()) }
        finally { $reader.Dispose() }
        if (-not $pointerValue.Equals($terminal, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Super1 terminal pointer does not name the canonical pinned terminal."
        }
        $terminalLock = [IO.File]::Open(
            $terminal,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $hash = Get-ReleaseSha256 -Path $terminal
        if ($hash -cne $ExpectedSha256.ToLowerInvariant()) {
            throw "Super1 terminal64.exe does not match the mandatory SHA-256 pin."
        }
        $allowedFiles = @(
            "terminal64.exe", "MetaEditor64.exe", "metatester64.exe",
            "Terminal.ico", "uninstall.exe"
        )
        $allowedDirectories = @(
            "bases", "config", "llm-agent", "logs", "MQL5", "Profiles",
            "Sounds", "Tester"
        )
        foreach ($item in @(Get-ChildItem -LiteralPath $terminalRoot -Force)) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint -or
                ($item.PSIsContainer -and $item.Name -notin $allowedDirectories) -or
                (-not $item.PSIsContainer -and $item.Name -notin $allowedFiles)) {
                throw "Super1 terminal tree has an unapproved top-level item: $($item.FullName)"
            }
        }
        $nestedReparse = Get-ChildItem -LiteralPath $terminalRoot -Recurse -Force |
            Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
            Select-Object -First 1
        if ($nestedReparse) {
            throw "Super1 terminal tree contains a nested reparse point: $($nestedReparse.FullName)"
        }
        if ((Get-ReleaseSha256 -Path $terminal) -cne $hash) {
            throw "Super1 terminal changed while acquiring its deployment pin."
        }
        $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature `
            -LiteralPath $terminal
        return [pscustomobject]@{
            path = $terminal
            root = $terminalRoot
            sha256 = $hash
            signature_status = [string]$signature.Status
            pointer = $PointerPath
            lock = $terminalLock
        }
    }
    catch {
        if ($terminalLock) { $terminalLock.Dispose() }
        throw
    }
    finally { $pointerLock.Dispose() }
}

function Protect-Super1TerminalRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$PointerPath,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$CallerSid
    )
    $expectedTerminalRoot = [IO.Path]::GetFullPath($ExpectedRoot)
    $expectedTerminal = [IO.Path]::GetFullPath((Join-Path $expectedTerminalRoot "terminal64.exe"))
    foreach ($required in @($PointerPath, $expectedTerminalRoot, $expectedTerminal)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Super1 terminal dependency is missing: $required"
        }
        if ((Get-Item -LiteralPath $required -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Super1 terminal trust path contains a reparse point: $required"
        }
    }

    $pointerLock = [IO.File]::Open(
        $PointerPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $terminalLock = $null
    try {
        $reader = New-Object IO.StreamReader($pointerLock, [Text.Encoding]::UTF8, $true, 1024, $true)
        try { $terminal = [IO.Path]::GetFullPath($reader.ReadToEnd().Trim()) }
        finally { $reader.Dispose() }
        if (-not $terminal.Equals($expectedTerminal, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Super1 terminal pointer does not name the canonical pinned terminal."
        }
        $terminalLock = [IO.File]::Open(
            $terminal,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $terminalHash = Get-ReleaseSha256 -Path $terminal
        if ($terminalHash -cne $ExpectedSha256.ToLowerInvariant()) {
            throw "Super1 terminal64.exe does not match the mandatory SHA-256 pin."
        }

        $allowedRootFiles = @(
            "terminal64.exe", "MetaEditor64.exe", "metatester64.exe",
            "Terminal.ico", "uninstall.exe"
        )
        $allowedRootDirectories = @(
            "bases", "config", "llm-agent", "logs", "MQL5", "Profiles",
            "Sounds", "Tester"
        )
        $assertTerminalLayout = {
            foreach ($item in @(Get-ChildItem -LiteralPath $expectedTerminalRoot -Force)) {
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw "Super1 terminal tree contains a reparse point: $($item.FullName)"
                }
                if ($item.PSIsContainer) {
                    if ($item.Name -notin $allowedRootDirectories) {
                        throw "Super1 terminal root contains an unapproved directory: $($item.Name)"
                    }
                }
                elseif ($item.Name -notin $allowedRootFiles) {
                    throw "Super1 terminal root contains an unapproved file: $($item.Name)"
                }
            }
            $nestedReparse = Get-ChildItem -LiteralPath $expectedTerminalRoot -Recurse -Force |
                Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
                Select-Object -First 1
            if ($nestedReparse) {
                throw "Super1 terminal tree contains a nested reparse point: $($nestedReparse.FullName)"
            }
        }
        & $assertTerminalLayout

        $terminalRootRights = @{
            "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
            "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
            $RunnerSid = [Security.AccessControl.FileSystemRights]::ReadAndExecute
        }
        Set-ExactSuper1DirectoryAcl -Path $expectedTerminalRoot -RightsBySid $terminalRootRights
        & $script:Super1IcaclsExe (Join-Path $expectedTerminalRoot "*") /reset /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not seal the Super1 terminal executable tree." }
        & $script:Super1IcaclsExe $expectedTerminalRoot /setowner "*S-1-5-18" /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not set SYSTEM ownership on the Super1 terminal tree." }

        $writableDataDirectories = @(
            "bases",
            "config",
            "llm-agent",
            "logs",
            "Tester",
            "MQL5\Files",
            "MQL5\Images",
            "MQL5\logs",
            "MQL5\Profiles"
        )
        $dataRights = @{
            "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
            "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
            $RunnerSid = [Security.AccessControl.FileSystemRights]::Modify
        }
        foreach ($relativeDataPath in $writableDataDirectories) {
            $dataPath = [IO.Path]::GetFullPath((Join-Path $expectedTerminalRoot $relativeDataPath))
            if (-not (Test-Path -LiteralPath $dataPath -PathType Container)) { continue }
            Set-ExactSuper1DirectoryAcl -Path $dataPath -RightsBySid $dataRights
            if (@(Get-ChildItem -LiteralPath $dataPath -Force).Count -ne 0) {
                & $script:Super1IcaclsExe (Join-Path $dataPath "*") /reset /T /C /Q | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "Could not apply the Super1 terminal data ACL: $dataPath" }
            }
            & $script:Super1IcaclsExe $dataPath /setowner "*S-1-5-18" /T /C /Q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not set terminal data ownership: $dataPath" }
            Assert-ExactSuper1DirectoryAcl -Path $dataPath -RightsBySid $dataRights
        }

        $pointerRights = @{
            "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
            "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
            $RunnerSid = [Security.AccessControl.FileSystemRights]::ReadAndExecute
        }
        Set-ExactSuper1FileAcl -Path $PointerPath -RightsBySid $pointerRights
        & $script:Super1IcaclsExe $PointerPath /setowner "*S-1-5-18" /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not protect the Super1 terminal pointer." }
        [IO.File]::SetAttributes(
            $PointerPath,
            [IO.File]::GetAttributes($PointerPath) -bor [IO.FileAttributes]::ReadOnly
        )

        & $assertTerminalLayout
        Assert-ExactSuper1DirectoryAcl -Path $expectedTerminalRoot -RightsBySid $terminalRootRights
        Assert-ExactSuper1FileAcl -Path $PointerPath -RightsBySid $pointerRights
        Assert-EffectiveReleaseFileAcl `
            -Path $terminal `
            -RunnerIdentity $RunnerSid `
            -CallerSid $CallerSid
        if ((Get-ReleaseSha256 -Path $terminal) -cne $terminalHash) {
            throw "Super1 terminal changed while the executable read lock was held."
        }
        $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature `
            -LiteralPath $terminal
        return [pscustomobject]@{
            path = $terminal
            root = $expectedTerminalRoot
            sha256 = $terminalHash
            signature_status = [string]$signature.Status
            pointer = $PointerPath
            writable_data_directories = $writableDataDirectories
            lock = $terminalLock
        }
    }
    catch {
        if ($terminalLock) { $terminalLock.Dispose() }
        throw
    }
    finally {
        $pointerLock.Dispose()
    }
}

function Initialize-Super1ProbeControl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $rights = @{
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
        $RunnerSid = [Security.AccessControl.FileSystemRights]::ReadAndExecute
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        foreach ($entry in $rights.GetEnumerator()) {
            [void]$acl.AddAccessRule(
                [Security.AccessControl.FileSystemAccessRule]::new(
                    [Security.Principal.SecurityIdentifier]::new([string]$entry.Key),
                    [Security.AccessControl.FileSystemRights]$entry.Value,
                    $inheritance,
                    [Security.AccessControl.PropagationFlags]::None,
                    [Security.AccessControl.AccessControlType]::Allow
                )
            )
        }
        [void][IO.Directory]::CreateDirectory($Path, $acl)
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container) -or
        (Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 probe-control path is not a real directory."
    }
    Set-ExactSuper1DirectoryAcl -Path $Path -RightsBySid $rights
    & $script:Super1IcaclsExe $Path /setowner "*S-1-5-18" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect the Super1 probe-control directory." }
    Assert-ExactSuper1DirectoryAcl -Path $Path -RightsBySid $rights
    if (@(Get-ChildItem -LiteralPath $Path -Force).Count -ne 0) {
        throw "Super1 probe-control contains a stale or unexpected request."
    }
}

function Protect-Super1RuntimeConfigFiles {
    param(
        [Parameter(Mandatory = $true)][string[]]$Paths,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $rights = @{
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
        $RunnerSid = [Security.AccessControl.FileSystemRights]::ReadAndExecute
    }
    $results = New-Object Collections.Generic.List[object]
    try {
        foreach ($path in $Paths) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                (Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Super1 runtime config is missing or is a reparse point: $path"
            }
            $lock = [IO.File]::Open(
                $path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::Read
            )
            try {
                $hash = Get-ReleaseSha256 -Path $path
                Set-ExactSuper1FileAcl -Path $path -RightsBySid $rights
                & $script:Super1IcaclsExe $path /setowner "*S-1-5-18" /Q | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "Could not protect Super1 runtime config: $path" }
                [IO.File]::SetAttributes(
                    $path,
                    [IO.File]::GetAttributes($path) -bor [IO.FileAttributes]::ReadOnly
                )
                Assert-ExactSuper1FileAcl -Path $path -RightsBySid $rights
                if ((Get-ReleaseSha256 -Path $path) -cne $hash) {
                    throw "Super1 runtime config changed while read-locked: $path"
                }
                $results.Add([pscustomobject]@{ path = $path; sha256 = $hash; lock = $lock })
                $lock = $null
            }
            finally {
                if ($lock) { $lock.Dispose() }
            }
        }
        return $results.ToArray()
    }
    catch {
        foreach ($result in $results.ToArray()) { $result.lock.Dispose() }
        throw
    }
}

function Protect-SignedReleaseCopy {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CallerSid,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    if ($CallerSid -notmatch '^S-\d-(?:\d+-)+\d+$') {
        throw "Invalid signed-release caller SID: $CallerSid"
    }
    foreach ($file in Get-ChildItem -LiteralPath $Path -File) {
        Set-ItemProperty -LiteralPath $file.FullName -Name IsReadOnly -Value $true
    }
    Set-ExactSuper1DirectoryAcl -Path $Path -RightsBySid @{
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
    }
    & $script:Super1IcaclsExe (Join-Path $Path "*") /reset /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not reset verified signed release descendants to the protected root ACL."
    }
    & $script:Super1IcaclsExe $Path /setowner "*S-1-5-18" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not set trusted ownership on the verified signed release."
    }
    foreach ($lockedPath in @($Path) + @(Get-ChildItem -LiteralPath $Path -Force | ForEach-Object FullName)) {
        $lockedAcl = Get-Acl -LiteralPath $lockedPath
        if (
            [string]$lockedAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne
                "S-1-5-18"
        ) {
            throw "Verified signed release item is not owned by SYSTEM: $lockedPath"
        }
        $rightsBySid = @{}
        $expectInherited = -not $lockedPath.Equals($Path, [StringComparison]::OrdinalIgnoreCase)
        $trustedSids = @("S-1-5-18", "S-1-5-32-544")
        foreach ($rule in @($lockedAcl.Access)) {
            $sid = Get-AclIdentitySid -Identity $rule.IdentityReference
            if ($sid -eq $RunnerSid -and $RunnerSid -notin $trustedSids) {
                throw "Super1 runner unexpectedly has an ACE on the verified signed release: $lockedPath"
            }
            if ($sid -eq $CallerSid -and $CallerSid -notin $trustedSids) {
                throw "Super1 upgrade caller unexpectedly has a direct ACE on the verified signed release: $lockedPath"
            }
            if (
                $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                [bool]$rule.IsInherited -ne $expectInherited -or
                $sid -notin $trustedSids
            ) {
                throw "Verified signed release has an unexpected ACL rule $lockedPath`: $sid"
            }
            $current = if ($rightsBySid.ContainsKey($sid)) { [int]$rightsBySid[$sid] } else { 0 }
            $rightsBySid[$sid] = $current -bor [int]$rule.FileSystemRights
        }
        $full = [int][Security.AccessControl.FileSystemRights]::FullControl
        foreach ($sid in $trustedSids) {
            if ([int]$rightsBySid[$sid] -ne $full) {
                throw "Verified signed release lacks trusted FullControl $lockedPath`: $sid"
            }
        }
        if (@($rightsBySid.Keys).Count -ne $trustedSids.Count) {
            throw "Verified signed release ACL is not exactly SYSTEM/Administrators: $lockedPath"
        }
        if (
            (Test-Path -LiteralPath $lockedPath -PathType Leaf) -and
            -not [IO.File]::GetAttributes($lockedPath).HasFlag([IO.FileAttributes]::ReadOnly)
        ) {
            throw "Verified signed release file is not read-only: $lockedPath"
        }
    }
}

function Invoke-Super1OfflineValidation {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ReleaseApp
    )
    $validator = @'
import json
import sys
from pathlib import Path

app = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(app / "scripts"))
import run_super1_xm_mt5_forward as super1

runtime_path = app / "live_forward" / "super1_xm_mt5_demo_config.json"
runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
candidate = super1.validate_super1_candidate(runtime)
print(json.dumps({
    "state": "VALID",
    "candidate_artifact_sha256": candidate["artifact_sha256"],
    "candidate_file_sha256": super1.core.file_hash(app / runtime["candidate_path"]),
    "contract_sha256": super1.core.file_hash(app / runtime["signal_contract_path"]),
    "engine_source_sha256": super1.core.source_code_hash(),
}, sort_keys=True))
'@
    $previousBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE
    $env:PYTHONDONTWRITEBYTECODE = "1"
    try {
        $output = @($validator | & $Python -I -E -B - $ReleaseApp)
        if ($LASTEXITCODE -ne 0 -or $output.Count -eq 0) {
            throw "Super1 offline contract validation failed for $ReleaseApp"
        }
        $result = $output[-1] | ConvertFrom-Json
        if ([string]$result.state -ne "VALID") {
            throw "Super1 offline validator returned an unexpected state."
        }
        return $result
    }
    finally {
        $env:PYTHONDONTWRITEBYTECODE = $previousBytecodeSetting
    }
}

function Assert-ReleasePythonDependencies {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][object]$ExpectedDependencies
    )
    $expectedJson = $ExpectedDependencies | ConvertTo-Json -Compress
    $expectedPayload = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($expectedJson)
    )
    $validator = @'
import base64
import importlib
import importlib.metadata
import json
import sys

expected = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
actual = {name: importlib.metadata.version(name) for name in expected}
for distribution, module in (("pandas", "pandas"), ("MetaTrader5", "MetaTrader5")):
    if distribution in expected:
        importlib.import_module(module)
if actual != expected:
    raise SystemExit(f"dependency version mismatch: expected={expected!r} actual={actual!r}")
print(json.dumps(actual, sort_keys=True))
'@
    $output = @($validator | & $Python -I -E -B - $expectedPayload)
    if ($LASTEXITCODE -ne 0 -or $output.Count -eq 0) {
        throw "Release Python dependency import/version validation failed: $Python"
    }
    return ($output[-1] | ConvertFrom-Json)
}

function Get-Super1PrincipalSid {
    param([Parameter(Mandatory = $true)][string]$Identity)
    if ($Identity -match '^S-\d-(?:\d+-)+\d+$') {
        return [Security.Principal.SecurityIdentifier]::new($Identity).Value
    }
    return (New-Object Security.Principal.NTAccount($Identity)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
}

function ConvertFrom-ScheduledTaskDuration {
    param([Parameter(Mandatory = $true)][object]$Value)
    if ($Value -is [TimeSpan]) { return [TimeSpan]$Value }
    try { return [Xml.XmlConvert]::ToTimeSpan([string]$Value) }
    catch { return [TimeSpan]::Parse([string]$Value) }
}

function Assert-CanonicalSuper1Task {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments
    )
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $actions = @($task.Actions)
    $triggers = @($task.Triggers)
    $settings = $task.Settings
    $restartInterval = ConvertFrom-ScheduledTaskDuration -Value $settings.RestartInterval
    $executionLimit = ConvertFrom-ScheduledTaskDuration -Value $settings.ExecutionTimeLimit
    if (
        [string]$task.TaskPath -cne "\" -or
        $actions.Count -ne 1 -or
        -not [IO.Path]::GetFullPath([string]$actions[0].Execute).Equals(
            $script:Super1PowerShellExe,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$actions[0].Arguments -cne $ExpectedArguments -or
        -not [string]::IsNullOrEmpty([string]$actions[0].WorkingDirectory) -or
        (Get-Super1PrincipalSid -Identity ([string]$task.Principal.UserId)) -cne
            "S-1-5-18" -or
        [string]$task.Principal.LogonType -cne "ServiceAccount" -or
        [string]$task.Principal.RunLevel -cne "Highest" -or
        -not [bool]$settings.Enabled -or
        [int]$settings.RestartCount -ne 3 -or
        $restartInterval -ne [TimeSpan]::FromMinutes(1) -or
        $executionLimit -ne [TimeSpan]::Zero -or
        -not [bool]$settings.StartWhenAvailable -or
        [string]$settings.MultipleInstances -ne "IgnoreNew"
    ) {
        throw "$TaskName action/settings are not the canonical stopped Super1 definition."
    }
    if ($triggers.Count -ne 1 -or
        [string]$triggers[0].CimClass.CimClassName -cne "MSFT_TaskBootTrigger" -or
        -not [bool]$triggers[0].Enabled -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].StartBoundary) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].EndBoundary) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].ExecutionTimeLimit) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Id) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Delay) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Repetition.Interval) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Repetition.Duration) -or
        [bool]$triggers[0].Repetition.StopAtDurationEnd) {
        throw "$TaskName boot trigger is not the canonical Super1 definition."
    }
}

function Assert-FrozenSuper1PasswordTask {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedXml,
        [Parameter(Mandatory = $true)][string]$ExpectedRunnerSid,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments
    )
    $task = Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $actions = @($task.Actions)
    $triggers = @($task.Triggers)
    $principalSid = if ([string]$task.Principal.UserId -match '^S-\d-(?:\d+-)+\d+$') {
        [Security.Principal.SecurityIdentifier]::new(
            [string]$task.Principal.UserId
        ).Value
    }
    else {
        (New-Object Security.Principal.NTAccount(
            [string]$task.Principal.UserId
        )).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if (
        [string](Export-ScheduledTask -TaskName $MainTask -ErrorAction Stop) -cne
            $ExpectedXml -or
        [string]$task.TaskPath -cne "\" -or
        $actions.Count -ne 1 -or
        [string]$actions[0].Execute -cne "powershell.exe" -or
        [string]$actions[0].Arguments -cne $ExpectedArguments -or
        -not [string]::IsNullOrEmpty([string]$actions[0].WorkingDirectory) -or
        $principalSid -cne $ExpectedRunnerSid -or
        [string]$task.Principal.LogonType -cne "Password" -or
        [string]$task.Principal.RunLevel -cne "Limited" -or
        -not [bool]$task.Settings.Enabled -or
        [int]$task.Settings.RestartCount -ne 999 -or
        (ConvertFrom-ScheduledTaskDuration -Value $task.Settings.RestartInterval) -ne
            [TimeSpan]::FromMinutes(1) -or
        (ConvertFrom-ScheduledTaskDuration -Value $task.Settings.ExecutionTimeLimit) -ne
            [TimeSpan]::Zero -or
        -not [bool]$task.Settings.StartWhenAvailable -or
        [string]$task.Settings.MultipleInstances -cne "IgnoreNew"
    ) {
        throw "Super1 Password task changed from the exact live password-preserving definition."
    }
    if ($triggers.Count -ne 1 -or
        [string]$triggers[0].CimClass.CimClassName -cne "MSFT_TaskBootTrigger" -or
        -not [bool]$triggers[0].Enabled -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].StartBoundary) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].EndBoundary) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].ExecutionTimeLimit) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Id) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Delay) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Repetition.Interval) -or
        -not [string]::IsNullOrEmpty([string]$triggers[0].Repetition.Duration) -or
        [bool]$triggers[0].Repetition.StopAtDurationEnd) {
        throw "Super1 Password task boot trigger differs from the exact live definition."
    }
}

function Assert-RunnerReadExecuteAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity
    )
    $runnerSid = if ($RunnerIdentity -match '^S-\d-(?:\d+-)+\d+$') {
        [Security.Principal.SecurityIdentifier]::new($RunnerIdentity).Value
    }
    else {
        (New-Object Security.Principal.NTAccount($RunnerIdentity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    $pathAcl = Get-Acl -LiteralPath $Path
    if (
        [string]$pathAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne
            "S-1-5-18"
    ) {
        throw "Release tree root is not owned by SYSTEM: $Path"
    }
    if (-not $pathAcl.AreAccessRulesProtected) {
        throw "Release tree root still inherits ACLs: $Path"
    }
    $callerSid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $trustedSids = @("S-1-5-18", "S-1-5-32-544", $callerSid, $runnerSid)
    $rightsBySid = @{}
    $requiredInheritance = (
        [int][Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [int][Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    foreach ($rule in $pathAcl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )) {
        $sid = [string]$rule.IdentityReference.Value
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.IsInherited -or
            $sid -notin $trustedSids -or
            ([int]$rule.InheritanceFlags -band $requiredInheritance) -ne $requiredInheritance
        ) {
            throw "Release tree root has an unexpected ACL entry $Path`: $sid"
        }
        $current = if ($rightsBySid.ContainsKey($sid)) { [int]$rightsBySid[$sid] } else { 0 }
        $rightsBySid[$sid] = $current -bor [int]$rule.FileSystemRights
    }
    $full = [int][Security.AccessControl.FileSystemRights]::FullControl
    $readExecute = [int][Security.AccessControl.FileSystemRights]::ReadAndExecute
    $mutation = (
        [int][Security.AccessControl.FileSystemRights]::Write -bor
        [int][Security.AccessControl.FileSystemRights]::Delete -bor
        [int][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [int][Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    foreach ($fullControlSid in @("S-1-5-18", "S-1-5-32-544", $callerSid)) {
        if (([int]$rightsBySid[$fullControlSid] -band $full) -ne $full) {
            throw "Trusted identity lacks full control on release tree root $Path`: $fullControlSid"
        }
    }
    if (
        ([int]$rightsBySid[$runnerSid] -band $readExecute) -ne $readExecute -or
        ([int]$rightsBySid[$runnerSid] -band $mutation) -ne 0
    ) {
        throw "Runner read/execute ACL validation failed: $Path"
    }
}

function Assert-EffectiveReleaseFileAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity,
        [Parameter(Mandatory = $true)][string]$CallerSid
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Release ACL target is not a file: $Path"
    }
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Release ACL target cannot be a reparse point: $Path"
    }
    $runnerSid = if ($RunnerIdentity -match '^S-\d-(?:\d+-)+\d+$') {
        [Security.Principal.SecurityIdentifier]::new($RunnerIdentity).Value
    }
    else {
        (New-Object Security.Principal.NTAccount($RunnerIdentity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    $trustedSids = @("S-1-5-18", "S-1-5-32-544", $CallerSid, $runnerSid)
    $runnerHasReadExecute = $false
    $systemHasFullControl = $false
    $administratorsHaveFullControl = $false
    $writeMask = [Security.AccessControl.FileSystemRights](
        [int][Security.AccessControl.FileSystemRights]::WriteData -bor
        [int][Security.AccessControl.FileSystemRights]::AppendData -bor
        [int][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [int][Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [int][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [int][Security.AccessControl.FileSystemRights]::Delete -bor
        [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [int][Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    $fileAcl = Get-Acl -LiteralPath $Path
    if (
        [string]$fileAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne
            "S-1-5-18"
    ) {
        throw "Release file is not owned by SYSTEM: $Path"
    }
    foreach ($rule in @($fileAcl.Access)) {
        $sid = Get-AclIdentitySid -Identity $rule.IdentityReference
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            -not $rule.IsInherited -or
            $sid -notin $trustedSids
        ) {
            throw "Release file has an unexpected effective ACL entry $Path`: $sid"
        }
        if ($sid -eq $runnerSid) {
            if (($rule.FileSystemRights -band $writeMask) -ne 0) {
                throw "Runner has write-capable access to release file: $Path"
            }
            if (
                ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq
                    [Security.AccessControl.FileSystemRights]::ReadAndExecute
            ) {
                $runnerHasReadExecute = $true
            }
        }
        if (
            $sid -eq "S-1-5-18" -and
            ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
                [Security.AccessControl.FileSystemRights]::FullControl
        ) {
            $systemHasFullControl = $true
        }
        if (
            $sid -eq "S-1-5-32-544" -and
            ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
                [Security.AccessControl.FileSystemRights]::FullControl
        ) {
            $administratorsHaveFullControl = $true
        }
    }
    if (-not ($runnerHasReadExecute -and $systemHasFullControl -and $administratorsHaveFullControl)) {
        throw "Release file effective ACL validation failed: $Path"
    }
    $share = [IO.FileShare]([int][IO.FileShare]::ReadWrite -bor [int][IO.FileShare]::Delete)
    $handle = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    try { $null = $handle.ReadByte() }
    finally { $handle.Dispose() }
}

function New-AtomicSuper1Candidate {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$TransactionId,
        [Parameter(Mandatory = $true)][string]$TransactionRoot,
        [Parameter(Mandatory = $true)][string]$CallerSid,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $temporary = [IO.Path]::GetFullPath((Join-Path $TransactionRoot (
        [IO.Path]::GetFileName($Target) + ".tmp-$TransactionId"
    )))
    Assert-Super1ChildPath -Path $Source
    Assert-Super1ChildPath -Path $Target
    Assert-Super1ChildPath -Path $temporary
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Promoted Super1 candidate source is missing: $Source"
    }
    if (Test-Path -LiteralPath $Target) {
        throw "Refusing to overwrite an existing Super1 rollover candidate: $Target"
    }
    if (Test-Path -LiteralPath $temporary) {
        throw "Refusing to reuse a Super1 candidate temporary path: $temporary"
    }
    $CandidateTempPaths.Add($temporary)
    Copy-FileCreateNew -Source $Source -Target $temporary
    $sourceHash = Get-ReleaseSha256 -Path $Source
    if ((Get-ReleaseSha256 -Path $temporary) -ne $sourceHash) {
        throw "Super1 candidate temporary copy hash mismatch: $Target"
    }
    Protect-PrivateFile -Path $temporary -CallerSid $CallerSid -RunnerSid $RunnerSid
    [IO.File]::Move($temporary, $Target)
    $null = $CandidateTempPaths.Remove($temporary)
    $CreatedCandidates.Add($Target)
    Protect-PrivateFile -Path $Target -CallerSid $CallerSid -RunnerSid $RunnerSid
    if ((Get-ReleaseSha256 -Path $Target) -ne $sourceHash) {
        throw "Super1 candidate final hash mismatch: $Target"
    }
    return [pscustomobject]@{
        path = $Target
        state = "CREATED_FROM_SIGNED_APP"
        sha256 = $sourceHash
    }
}

$App = $null
$State = $null
$ArchiveRoot = $null
$Venv = $null
$Python = $null
$TerminalPointer = $null
$TerminalRoot = $null
$TerminalEvidence = $null
$UpgradeReadiness = $null
$ProbeControl = $null
$ArchivePath = $null
$UpgradeArchive = $null
$ArchivedApp = $null
$ArchivedVenv = $null
$FailedApp = $null
$FailedVenv = $null
$FailedStaging = $null
$FailedStagedVenv = $null
$Staging = $null
$StagedVenv = $null
$StagedPython = $null
$SignedReleaseRoot = $null
$VerifiedArchive = $null
$BootstrapPython = $null
$BootstrapPythonEvidence = $null
$ReleaseManifest = $null
$OfflineValidation = $null
$FinalOfflineValidation = $null
$DependencyValidation = $null
$originalAppArchived = $false
$originalVenvArchived = $false
$appPromoted = $false
$venvPromoted = $false
$watchdogActionChanged = $false
$originalMainTaskXml = $null
$expectedMainArguments = $null
$runnerSid = $null
$originalWatchdogActions = $null
$originalWatchdogSettings = $null
$CandidateTempPaths = New-Object Collections.Generic.List[string]
$CreatedCandidates = New-Object Collections.Generic.List[string]
$CandidateResults = New-Object Collections.Generic.List[object]
$transactionEntered = $false

$runtimeHelpersReady = $true

try {
    $Stamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $Transaction = [Guid]::NewGuid().ToString("N")
    $App = [IO.Path]::GetFullPath((Join-Path $Root "app"))
    $State = [IO.Path]::GetFullPath((Join-Path $Root "state"))
    $ArchiveRoot = [IO.Path]::GetFullPath((Join-Path $Root "archive"))
    $Venv = [IO.Path]::GetFullPath((Join-Path $Root "venv311"))
    $Python = [IO.Path]::GetFullPath((Join-Path $Venv "Scripts\python.exe"))
    $TerminalPointer = [IO.Path]::GetFullPath((Join-Path $Root "mt5-terminal.txt"))
    $TerminalRoot = [IO.Path]::GetFullPath((Join-Path $Root "mt5-clean5833"))
    $ProbeControl = [IO.Path]::GetFullPath((Join-Path $Root "probe-control"))
    $ServerConfigPath = [IO.Path]::GetFullPath((Join-Path $Root "xm-server.txt"))
    $PasswordConfigPath = [IO.Path]::GetFullPath((Join-Path $Root "xm-password.dpapi"))
    $ArchivePath = [IO.Path]::GetFullPath($Archive)
    $UpgradeArchive = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot "app-upgrade-$Stamp-$Transaction"))
    $ArchivedApp = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "app.previous"))
    $ArchivedVenv = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "venv311.previous"))
    $FailedApp = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "app.failed"))
    $FailedVenv = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "venv311.failed"))
    $FailedStaging = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "app.staging.failed"))
    $FailedStagedVenv = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "venv311.staging.failed"))
    $Staging = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "app.staging"))
    $StagedVenv = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "venv311.staging"))
    $StagedPython = [IO.Path]::GetFullPath((Join-Path $StagedVenv "Scripts\python.exe"))
    $SignedReleaseRoot = [IO.Path]::GetFullPath((Join-Path $UpgradeArchive "signed-release"))
    $VerifiedArchive = [IO.Path]::GetFullPath(
        (Join-Path $SignedReleaseRoot ([IO.Path]::GetFileName($ArchivePath)))
    )

    foreach ($path in @(
        $App, $State, $ArchiveRoot, $Venv, $Python, $TerminalPointer, $TerminalRoot,
        $ServerConfigPath, $PasswordConfigPath,
        $ProbeControl, $UpgradeArchive, $ArchivedApp,
        $ArchivedVenv, $FailedApp, $FailedVenv, $FailedStaging, $FailedStagedVenv,
        $Staging, $StagedVenv, $StagedPython, $SignedReleaseRoot, $VerifiedArchive
    )) {
        Assert-Super1ChildPath -Path $path
    }
    if ((Test-PathWithin -Child $ArchivePath -Parent $App) -or
        (Test-PathWithin -Child $ArchivePath -Parent $State) -or
        (Test-PathWithin -Child $ArchivePath -Parent $Venv)) {
        throw "The signed release archive cannot be stored inside the active app, state, or venv tree."
    }
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw "Run this script from an elevated PowerShell."
    }
    $callerSid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($callerSid -notmatch '^S-\d-(?:\d+-)+\d+$') {
        throw "Could not resolve the elevated upgrade caller SID."
    }
    foreach ($required in @(
        $Root, $App, $State, $ArchiveRoot, $Venv, $ArchivePath, $TerminalPointer,
        $TerminalRoot, $ServerConfigPath, $PasswordConfigPath
    )) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Missing required Super1 path: $required"
        }
    }
    foreach ($unused in @(
        $UpgradeArchive, $ArchivedApp, $ArchivedVenv, $FailedApp, $FailedVenv,
        $FailedStaging, $FailedStagedVenv, $Staging, $StagedVenv, $SignedReleaseRoot
    )) {
        if (Test-Path -LiteralPath $unused) {
            throw "Refusing to reuse a transaction path: $unused"
        }
    }

    $integrityCandidates = @(
        (Join-Path $PSScriptRoot "release_integrity.ps1"),
        (Join-Path $App "deploy\release_integrity.ps1")
    )
    $IntegrityScript = @($integrityCandidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1)
    if ($IntegrityScript.Count -ne 1) {
        throw "Missing release integrity verifier."
    }
    $IntegrityScript = [IO.Path]::GetFullPath([string]$IntegrityScript[0])
    if ((Get-Item -LiteralPath $IntegrityScript -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 release integrity verifier cannot be a reparse point."
    }
    $integrityHash = (Get-FileHash `
        -LiteralPath $IntegrityScript `
        -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($integrityHash -cne $ExpectedIntegrityScriptSha256) {
        throw "Super1 release integrity verifier hash mismatch."
    }
    $IntegrityScriptLock = [IO.File]::Open(
        $IntegrityScript,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    if ((Get-FileHash `
            -LiteralPath $IntegrityScript `
            -Algorithm SHA256).Hash.ToLowerInvariant() -cne
        $ExpectedIntegrityScriptSha256) {
        throw "Super1 release integrity verifier changed while acquiring its read lock."
    }
    . $IntegrityScript
    if ((Get-FileHash `
            -LiteralPath $IntegrityScript `
            -Algorithm SHA256).Hash.ToLowerInvariant() -cne
        $ExpectedIntegrityScriptSha256) {
        throw "Super1 release integrity verifier changed while it was dot-sourced."
    }

    $mainTaskDefinition = Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $watchdogTaskDefinition = Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
    if (@($mainTaskDefinition.Actions).Count -ne 1 -or @($watchdogTaskDefinition.Actions).Count -ne 1) {
        throw "Each Super1 scheduled task must have exactly one action."
    }
    $runnerIdentity = [string]$mainTaskDefinition.Principal.UserId
    if ([string]::IsNullOrWhiteSpace($runnerIdentity)) {
        throw "Super1 main task runner identity is empty."
    }
    $runnerSid = (New-Object Security.Principal.NTAccount($runnerIdentity)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($runnerSid -eq $callerSid) {
        throw "The elevated upgrade caller cannot be the untrusted Super1 runtime identity."
    }
    $expectedMainArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$App\deploy\run_super1_windows.ps1`""
    $originalMainTaskXml = [string](
        Export-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    )
    Assert-FrozenSuper1PasswordTask `
        -ExpectedXml $originalMainTaskXml `
        -ExpectedRunnerSid $runnerSid `
        -ExpectedArguments $expectedMainArguments
    $originalWatchdogActions = @($watchdogTaskDefinition.Actions)
    $originalWatchdogSettings = $watchdogTaskDefinition.Settings
    $protectedDeployRights = @{
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [Security.AccessControl.FileSystemRights]::FullControl
    }
    foreach ($protectedDirectory in @($ProtectedDeployRoot, $ProtectedSelfDirectory)) {
        Assert-ExactSuper1DirectoryAcl `
            -Path $protectedDirectory `
            -RightsBySid $protectedDeployRights
    }
    Assert-ExactSuper1FileAcl `
        -Path $ProtectedSelfPath `
        -RightsBySid $protectedDeployRights
    if (-not ((Get-Item -LiteralPath $ProtectedSelfPath -Force).Attributes -band
        [IO.FileAttributes]::ReadOnly)) {
        throw "Protected Super1 upgrader is not read-only."
    }
    $SelfScriptLock.Position = 0
    $SelfScriptRecheckHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $SelfScriptRecheckSha256 = [BitConverter]::ToString(
            $SelfScriptRecheckHasher.ComputeHash($SelfScriptLock)
        ).Replace("-", "").ToLowerInvariant()
        $SelfScriptLock.Position = 0
    }
    finally { $SelfScriptRecheckHasher.Dispose() }
    if ($SelfScriptRecheckSha256 -cne $ExpectedSelfSha256) {
        throw "Protected Super1 upgrader changed while its self read lock was held."
    }

    $sourceManifest = [IO.Path]::ChangeExtension($ArchivePath, ".manifest.json")
    $sourceSignature = [IO.Path]::ChangeExtension($ArchivePath, ".manifest.sig")
    $sourceComponents = @($ArchivePath, $sourceManifest, $sourceSignature)
    foreach ($component in $sourceComponents) {
        $componentLock = [IO.File]::Open(
            $component,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        [void]$ReleaseInputLocks.Add($componentLock)
    }
    $ReleaseManifest = Assert-SignedReleaseArchive `
        -Archive $ArchivePath `
        -ExpectedProfile "super1" `
        -RequireProvenance
    $sourceHashes = [ordered]@{}
    foreach ($component in $sourceComponents) {
        $sourceHashes[[IO.Path]::GetFileName($component)] = Get-ReleaseSha256 -Path $component
    }

    $runtimeControlEntered = $true
    Stop-Super1RuntimeForRollback

    $CandidateSpecs = @(
        [pscustomobject]@{ RelativeSource = "scripts\run_capital_forward.py"; Target = (Join-Path $Root "run_capital_forward.py.candidate") },
        [pscustomobject]@{ RelativeSource = "scripts\run_xm_mt5_forward.py"; Target = (Join-Path $Root "run_xm_mt5_forward.py.candidate") },
        [pscustomobject]@{ RelativeSource = "scripts\run_super1_xm_mt5_forward.py"; Target = (Join-Path $Root "run_super1_xm_mt5_forward.py.candidate") },
        [pscustomobject]@{ RelativeSource = "live_forward\super1_xm_mt5_demo_config.json"; Target = (Join-Path $Root "super1_xm_mt5_demo_config.json.candidate") },
        [pscustomobject]@{ RelativeSource = "research_candidates\super1\super1_manifest.json"; Target = (Join-Path $Root "super1_manifest.json.candidate") },
        [pscustomobject]@{ RelativeSource = "research_candidates\super1\super1_signal_contract.json"; Target = (Join-Path $Root "super1_signal_contract.json.candidate") }
    )
    foreach ($spec in $CandidateSpecs) {
        Assert-Super1ChildPath -Path $spec.Target
        if (Test-Path -LiteralPath $spec.Target) {
            throw "Refusing to overwrite an existing Super1 rollover candidate: $($spec.Target)"
        }
    }

    Assert-NoUntrustedDeleteChild `
        -Path $Root `
        -TrustedSids @("S-1-5-18", "S-1-5-32-544", $callerSid)
    Protect-TransactionArchiveRoot `
        -Path $ArchiveRoot `
        -CallerSid $callerSid `
        -RunnerSid $runnerSid
    Initialize-Super1ProbeControl -Path $ProbeControl -RunnerSid $runnerSid
    $RuntimeConfigEvidence = @(Protect-Super1RuntimeConfigFiles `
        -Paths @($ServerConfigPath, $PasswordConfigPath) `
        -RunnerSid $runnerSid)
    $TerminalEvidence = Get-Super1TerminalPinEvidence `
        -PointerPath $TerminalPointer `
        -ExpectedRoot $TerminalRoot `
        -ExpectedSha256 $ExpectedTerminalSha256
    $TerminalLock = $TerminalEvidence.lock

    New-Item -ItemType Directory -Path $UpgradeArchive | Out-Null
    Protect-TransactionArchiveRoot `
        -Path $UpgradeArchive `
        -CallerSid $callerSid `
        -RunnerSid $runnerSid
    $transactionEntered = $true
    New-Item -ItemType Directory -Path $SignedReleaseRoot | Out-Null
    foreach ($component in $sourceComponents) {
        $leaf = [IO.Path]::GetFileName($component)
        $destination = Join-Path $SignedReleaseRoot $leaf
        Copy-FileCreateNew -Source $component -Target $destination
        if ((Get-ReleaseSha256 -Path $destination) -ne [string]$sourceHashes[$leaf]) {
            throw "Verified signed release component copy hash mismatch: $component"
        }
    }
    Protect-SignedReleaseCopy `
        -Path $SignedReleaseRoot `
        -CallerSid $callerSid `
        -RunnerSid $runnerSid
    $verifiedReleaseManifest = Assert-SignedReleaseArchive `
        -Archive $VerifiedArchive `
        -ExpectedProfile "super1" `
        -RequireProvenance
    if ([string]$verifiedReleaseManifest.archive_sha256 -ne [string]$ReleaseManifest.archive_sha256) {
        throw "Verified signed release copy does not match the originally validated release."
    }
    $ReleaseManifest = $verifiedReleaseManifest

    $utf8NoBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText(
        (Join-Path $UpgradeArchive "Super1XM.original.xml"),
        $originalMainTaskXml,
        $utf8NoBom
    )
    [IO.File]::WriteAllText(
        (Join-Path $UpgradeArchive "Super1Watchdog.original.xml"),
        [string](Export-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop),
        $utf8NoBom
    )

    Expand-Archive -LiteralPath $VerifiedArchive -DestinationPath $Staging
    if ((Get-ReleaseSha256 -Path $VerifiedArchive) -ne [string]$ReleaseManifest.archive_sha256) {
        throw "Verified signed release archive changed during extraction."
    }

    $stagedRunner = Join-Path $Staging "scripts\run_super1_xm_mt5_forward.py"
    $stagedRuntime = Join-Path $Staging "live_forward\super1_xm_mt5_demo_config.json"
    $stagedLauncher = Join-Path $Staging "deploy\run_super1_windows.ps1"
    $stagedWatchdog = Join-Path $Staging "deploy\watchdog_windows.ps1"
    $stagedFlatCheck = Join-Path $Staging "deploy\check_super1_flat_windows.ps1"
    $stagedRollover = Join-Path $Staging "deploy\rollover_super1_campaign_windows.ps1"
    $stagedSecureHelper = Join-Path $Staging "deploy\super1_secure_task.ps1"
    $stagedFlatDiagnostic = Join-Path $Staging "scripts\check_mt5_flat.py"
    foreach ($required in @(
        $stagedRunner, $stagedRuntime, $stagedLauncher, $stagedWatchdog,
        $stagedFlatCheck, $stagedRollover, $stagedSecureHelper, $stagedFlatDiagnostic
    )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Signed Super1 staging tree is incomplete: $required"
        }
    }
    $stagedTerminalPin = Join-Path $Staging "deploy\terminal_runtime_pin.json"
    $stagedPowerShellPin = Join-Path $Staging "deploy\powershell_runtime_pin.json"
    foreach ($generatedPin in @($stagedTerminalPin, $stagedPowerShellPin)) {
        if (Test-Path -LiteralPath $generatedPin) {
            throw "Signed Super1 release illegally occupies a generated runtime-pin path: $generatedPin"
        }
    }
    $terminalPinJson = [ordered]@{
        schema_version = 1
        terminal_path = [string]$TerminalEvidence.path
        terminal_sha256 = [string]$TerminalEvidence.sha256
    } | ConvertTo-Json -Compress
    $pinBytes = $utf8NoBom.GetBytes($terminalPinJson)
    $pinStream = [IO.File]::Open(
        $stagedTerminalPin,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $pinStream.Write($pinBytes, 0, $pinBytes.Length)
        $pinStream.Flush($true)
    }
    finally { $pinStream.Dispose() }
    $powerShellPinJson = [ordered]@{
        schema_version = 1
        powershell_path = [string]$PowerShellHostEvidence.path
        powershell_sha256 = [string]$PowerShellHostEvidence.sha256
        signer_subject = [string]$PowerShellHostEvidence.signer_subject
        signer_thumbprint = [string]$PowerShellHostEvidence.signer_thumbprint
        product_version = [string]$PowerShellHostEvidence.product_version
    } | ConvertTo-Json -Compress
    $powerShellPinBytes = $utf8NoBom.GetBytes($powerShellPinJson)
    $powerShellPinStream = [IO.File]::Open(
        $stagedPowerShellPin,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $powerShellPinStream.Write(
            $powerShellPinBytes,
            0,
            $powerShellPinBytes.Length
        )
        $powerShellPinStream.Flush($true)
    }
    finally { $powerShellPinStream.Dispose() }
    $stagedRuntimePayload = [IO.File]::ReadAllText($stagedRuntime) | ConvertFrom-Json
    $protectedServer = [IO.File]::ReadAllText($ServerConfigPath).Trim()
    if ([string]::IsNullOrWhiteSpace($protectedServer) -or
        $protectedServer -cne [string]$stagedRuntimePayload.expected_server) {
        throw "Protected Super1 server config does not match the signed broker identity contract."
    }

    $BootstrapPythonEvidence = Get-TrustedBootstrapPythonEvidence `
        -PyvenvConfig (Join-Path $Venv "pyvenv.cfg") `
        -ExpectedSha256 $ExpectedPythonSha256 `
        -RunnerSid $runnerSid `
        -CallerSid $callerSid
    $BootstrapPython = [string]$BootstrapPythonEvidence.path
    $BootstrapPythonLock = $BootstrapPythonEvidence.lock
    $bootstrapParent = Split-Path -Parent $BootstrapPython
    Push-Location -LiteralPath $bootstrapParent
    try {
        & $BootstrapPython -I -S -E -B -m venv $StagedVenv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $StagedPython -PathType Leaf)) {
            throw "Could not create the isolated staged Super1 virtual environment."
        }
        if (
            (Get-ReleaseSha256 -Path $BootstrapPython) -cne
                [string]$BootstrapPythonEvidence.sha256
        ) {
            throw "Super1 bootstrap Python changed while creating the staged environment."
        }
    }
    finally {
        Pop-Location
        if ($BootstrapPythonLock) {
            $BootstrapPythonLock.Dispose()
            $BootstrapPythonLock = $null
        }
    }
    Install-LockedRelease -Python $StagedPython -App $Staging
    $DependencyValidation = Assert-ReleasePythonDependencies `
        -Python $StagedPython `
        -ExpectedDependencies $ReleaseManifest.dependencies
    & $StagedPython -I -E -B -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Staged Super1 virtual environment dependency check failed."
    }
    & $StagedPython -I -E -B -m compileall -q $Staging
    if ($LASTEXITCODE -ne 0) {
        throw "Staged Super1 Python compile validation failed."
    }
    Protect-ReleaseApp -App $Staging -RunnerIdentity $runnerIdentity
    Protect-ReleaseApp -App $StagedVenv -RunnerIdentity $runnerIdentity
    Assert-RunnerReadExecuteAcl -Path $Staging -RunnerIdentity $runnerIdentity
    Assert-RunnerReadExecuteAcl -Path $StagedVenv -RunnerIdentity $runnerIdentity
    foreach ($criticalFile in @(
        $stagedRunner, $stagedRuntime, $stagedLauncher, $stagedWatchdog,
        $stagedFlatCheck, $stagedRollover, $stagedSecureHelper, $stagedFlatDiagnostic,
        $stagedTerminalPin, $stagedPowerShellPin, $StagedPython
    )) {
        Assert-EffectiveReleaseFileAcl `
            -Path $criticalFile `
            -RunnerIdentity $runnerIdentity `
            -CallerSid $callerSid
    }
    $OfflineValidation = Invoke-Super1OfflineValidation `
        -Python $StagedPython `
        -ReleaseApp $Staging

    foreach ($spec in $CandidateSpecs) {
        $stagedSource = Join-Path $Staging $spec.RelativeSource
        if (-not (Test-Path -LiteralPath $stagedSource -PathType Leaf)) {
            throw "Signed Super1 candidate source is missing: $stagedSource"
        }
        if (Test-Path -LiteralPath $spec.Target) {
            throw "Refusing to overwrite an existing Super1 rollover candidate: $($spec.Target)"
        }
    }

    Move-Item -LiteralPath $App -Destination $ArchivedApp
    $originalAppArchived = $true
    Move-Item -LiteralPath $Venv -Destination $ArchivedVenv
    $originalVenvArchived = $true
    Move-Item -LiteralPath $Staging -Destination $App
    $appPromoted = $true
    Move-Item -LiteralPath $StagedVenv -Destination $Venv
    $venvPromoted = $true

    Protect-ReleaseApp -App $App -RunnerIdentity $runnerIdentity
    Protect-ReleaseApp -App $Venv -RunnerIdentity $runnerIdentity
    Assert-RunnerReadExecuteAcl -Path $App -RunnerIdentity $runnerIdentity
    Assert-RunnerReadExecuteAcl -Path $Venv -RunnerIdentity $runnerIdentity
    foreach ($criticalFile in @(
        (Join-Path $App "scripts\run_super1_xm_mt5_forward.py"),
        (Join-Path $App "live_forward\super1_xm_mt5_demo_config.json"),
        (Join-Path $App "deploy\run_super1_windows.ps1"),
        (Join-Path $App "deploy\watchdog_windows.ps1"),
        (Join-Path $App "deploy\check_super1_flat_windows.ps1"),
        (Join-Path $App "deploy\rollover_super1_campaign_windows.ps1"),
        (Join-Path $App "deploy\super1_secure_task.ps1"),
        (Join-Path $App "deploy\terminal_runtime_pin.json"),
        (Join-Path $App "deploy\powershell_runtime_pin.json"),
        (Join-Path $App "scripts\check_mt5_flat.py"),
        $Python
    )) {
        Assert-EffectiveReleaseFileAcl `
            -Path $criticalFile `
            -RunnerIdentity $runnerIdentity `
            -CallerSid $callerSid
    }

    $DependencyValidation = Assert-ReleasePythonDependencies `
        -Python $Python `
        -ExpectedDependencies $ReleaseManifest.dependencies
    & $Python -I -E -B -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Promoted Super1 virtual environment dependency check failed."
    }
    $FinalOfflineValidation = Invoke-Super1OfflineValidation -Python $Python -ReleaseApp $App
    foreach ($field in @(
        "candidate_artifact_sha256", "candidate_file_sha256", "contract_sha256", "engine_source_sha256"
    )) {
        if ([string]$FinalOfflineValidation.$field -ne [string]$OfflineValidation.$field) {
            throw "Promoted Super1 offline validation changed after app/venv activation: $field"
        }
    }

    $launcher = [IO.Path]::GetFullPath((Join-Path $App "deploy\run_super1_windows.ps1"))
    $watchdog = [IO.Path]::GetFullPath((Join-Path $App "deploy\watchdog_windows.ps1"))
    $healthPath = [IO.Path]::GetFullPath((Join-Path $State "health.json"))
    $watchdogStatus = [IO.Path]::GetFullPath((Join-Path $Root "watchdog_status.json"))
    $mainArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
    if ($mainArguments -cne $expectedMainArguments) {
        throw "Promoted Super1 launcher path differs from the frozen Password task binding."
    }
    $watchdogArguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$watchdog`""
        "-MainTaskName `"$MainTask`""
        "-HealthPath `"$healthPath`""
        '-ProcessPattern "run_super1_xm_mt5_forward.py"'
        "-StatusPath `"$watchdogStatus`""
    ) -join " "
    $watchdogAction = New-ScheduledTaskAction -Execute $script:Super1PowerShellExe -Argument $watchdogArguments
    $watchdogSettings = New-ScheduledTaskSettingsSet `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew
    Assert-FrozenSuper1PasswordTask `
        -ExpectedXml $originalMainTaskXml `
        -ExpectedRunnerSid $runnerSid `
        -ExpectedArguments $expectedMainArguments
    $watchdogActionChanged = $true
    Set-ScheduledTask -TaskName $WatchdogTask -Action $watchdogAction -Settings $watchdogSettings | Out-Null
    Assert-CanonicalSuper1Task -TaskName $WatchdogTask -ExpectedArguments $watchdogArguments

    $flatCheckScript = [IO.Path]::GetFullPath(
        (Join-Path $App "deploy\check_super1_flat_windows.ps1")
    )
    $flatInvocation = @(& $script:Super1PowerShellExe `
        -NoProfile `
        -NonInteractive `
        -ExecutionPolicy Bypass `
        -File $flatCheckScript `
        -KeepStopped 2>&1)
    $flatOutput = @($flatInvocation | Where-Object { $_ -is [string] }) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($flatOutput)) {
        $safeFlatError = @($flatInvocation | ForEach-Object { [string]$_ }) -join " | "
        throw "Super1 protected pre-hardening broker flat probe failed: $safeFlatError"
    }
    $UpgradeReadiness = $flatOutput | ConvertFrom-Json
    $signedRuntime = [IO.File]::ReadAllText(
        (Join-Path $App "live_forward\super1_xm_mt5_demo_config.json")
    ) | ConvertFrom-Json
    if ([string]$UpgradeReadiness.state -cne "READY_FLAT_SEALED" -or
        [string]$UpgradeReadiness.readiness_sha256 -notmatch '^[a-f0-9]{64}$' -or
        (Get-ReleaseSha256 -Path ([string]$UpgradeReadiness.readiness_evidence)) -cne
            [string]$UpgradeReadiness.readiness_sha256 -or
        [int]$UpgradeReadiness.account_login -ne [int]$signedRuntime.account_login -or
        [string]$UpgradeReadiness.server -cne [string]$signedRuntime.expected_server -or
        [string]$UpgradeReadiness.company -cne [string]$signedRuntime.expected_company -or
        [int]$UpgradeReadiness.open_orders -ne 0 -or
        [int]$UpgradeReadiness.open_positions -ne 0 -or
        -not [bool]$UpgradeReadiness.task_xml_unchanged) {
        throw "Super1 protected pre-hardening broker evidence is invalid."
    }
    Wait-Super1Stopped
    if ($TerminalLock) {
        $TerminalLock.Dispose()
        $TerminalLock = $null
    }
    $TerminalEvidence = Protect-Super1TerminalRuntime `
        -PointerPath $TerminalPointer `
        -ExpectedRoot $TerminalRoot `
        -ExpectedSha256 $ExpectedTerminalSha256 `
        -RunnerSid $runnerSid `
        -CallerSid $callerSid
    $TerminalLock = $TerminalEvidence.lock

    foreach ($spec in $CandidateSpecs) {
        $CandidateResults.Add((New-AtomicSuper1Candidate `
            -Source (Join-Path $App $spec.RelativeSource) `
            -Target $spec.Target `
            -TransactionId $Transaction `
            -TransactionRoot $UpgradeArchive `
            -CallerSid $callerSid `
            -RunnerSid $runnerSid))
    }

    Stop-Super1Tasks
    Wait-Super1Stopped
    Assert-FrozenSuper1PasswordTask `
        -ExpectedXml $originalMainTaskXml `
        -ExpectedRunnerSid $runnerSid `
        -ExpectedArguments $expectedMainArguments
    Assert-CanonicalSuper1Task -TaskName $WatchdogTask -ExpectedArguments $watchdogArguments
    $Result = [pscustomobject]@{
        state = "UPGRADED_STOPPED"
        release_profile = [string]$ReleaseManifest.profile
        release_archive = $ArchivePath
        verified_release_archive = $VerifiedArchive
        release_archive_sha256 = [string]$ReleaseManifest.archive_sha256
        active_app = $App
        active_venv = $Venv
        previous_app = $ArchivedApp
        previous_venv = $ArchivedVenv
        state_path = $State
        state_untouched = $true
        offline_validation = $FinalOfflineValidation
        dependencies = $DependencyValidation
        bootstrap_python = [ordered]@{
            path = [string]$BootstrapPythonEvidence.path
            sha256 = [string]$BootstrapPythonEvidence.sha256
            signature_status = [string]$BootstrapPythonEvidence.signature_status
            signer_subject = [string]$BootstrapPythonEvidence.signer_subject
            product_version = [string]$BootstrapPythonEvidence.product_version
        }
        terminal_runtime = [ordered]@{
            path = [string]$TerminalEvidence.path
            root = [string]$TerminalEvidence.root
            sha256 = [string]$TerminalEvidence.sha256
            signature_status = [string]$TerminalEvidence.signature_status
            pointer = [string]$TerminalEvidence.pointer
            pin = (Join-Path $App "deploy\terminal_runtime_pin.json")
        }
        powershell_runtime = [ordered]@{
            path = [string]$PowerShellHostEvidence.path
            sha256 = [string]$PowerShellHostEvidence.sha256
            signer_subject = [string]$PowerShellHostEvidence.signer_subject
            signer_thumbprint = [string]$PowerShellHostEvidence.signer_thumbprint
            product_version = [string]$PowerShellHostEvidence.product_version
            pin = (Join-Path $App "deploy\powershell_runtime_pin.json")
        }
        protected_runtime_config = @($RuntimeConfigEvidence | ForEach-Object {
            [ordered]@{ path = [string]$_.path; sha256 = [string]$_.sha256 }
        })
        pre_hardening_readiness = $UpgradeReadiness
        probe_control = $ProbeControl
        flat_check_script = (Join-Path $App "deploy\check_super1_flat_windows.ps1")
        rollover_script = (Join-Path $App "deploy\rollover_super1_campaign_windows.ps1")
        candidates = @($CandidateResults | ForEach-Object { $_ })
        tasks = [ordered]@{
            main = [string](Get-ScheduledTask -TaskName $MainTask).State
            watchdog = [string](Get-ScheduledTask -TaskName $WatchdogTask).State
        }
    }
}
catch {
    $failure = $_
    if (-not $runtimeControlEntered) {
        throw $failure
    }
    $rollbackErrors = New-Object Collections.Generic.List[string]
    try {
        Stop-Super1RuntimeForRollback
    }
    catch {
        throw [Exception]::new("Super1 signed app/venv upgrade failed ($($failure.Exception.Message)); rollback was not attempted because the stopped-runtime gate failed: $($_.Exception.Message)", $failure.Exception)
    }

    if ($watchdogActionChanged) {
        try {
            Set-ScheduledTask `
                -TaskName $WatchdogTask `
                -Action $originalWatchdogActions `
                -Settings $originalWatchdogSettings | Out-Null
            $watchdogActionChanged = $false
        }
        catch {
            $rollbackErrors.Add("watchdog task action/settings restore: $($_.Exception.Message)")
        }
    }

    foreach ($candidate in @($CreatedCandidates)) {
        try {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                Remove-Item -LiteralPath $candidate -Force
            }
        }
        catch {
            $rollbackErrors.Add("candidate cleanup $candidate`: $($_.Exception.Message)")
        }
    }
    foreach ($temporary in @($CandidateTempPaths)) {
        try {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
        catch {
            $rollbackErrors.Add("candidate temporary cleanup $temporary`: $($_.Exception.Message)")
        }
    }

    if ($venvPromoted -and (Test-Path -LiteralPath $Venv -PathType Container)) {
        try {
            Move-Item -LiteralPath $Venv -Destination $FailedVenv
            $venvPromoted = $false
        }
        catch {
            $rollbackErrors.Add("failed venv archive: $($_.Exception.Message)")
        }
    }
    if ($appPromoted -and (Test-Path -LiteralPath $App -PathType Container)) {
        try {
            Move-Item -LiteralPath $App -Destination $FailedApp
            $appPromoted = $false
        }
        catch {
            $rollbackErrors.Add("failed app archive: $($_.Exception.Message)")
        }
    }
    if ($originalVenvArchived -and (Test-Path -LiteralPath $ArchivedVenv -PathType Container)) {
        try {
            if (Test-Path -LiteralPath $Venv) {
                throw "Active venv path is occupied during rollback."
            }
            Move-Item -LiteralPath $ArchivedVenv -Destination $Venv
            $originalVenvArchived = $false
        }
        catch {
            $rollbackErrors.Add("previous venv restore: $($_.Exception.Message)")
        }
    }
    if ($originalAppArchived -and (Test-Path -LiteralPath $ArchivedApp -PathType Container)) {
        try {
            if (Test-Path -LiteralPath $App) {
                throw "Active app path is occupied during rollback."
            }
            Move-Item -LiteralPath $ArchivedApp -Destination $App
            $originalAppArchived = $false
        }
        catch {
            $rollbackErrors.Add("previous app restore: $($_.Exception.Message)")
        }
    }
    if ($Staging -and (Test-Path -LiteralPath $Staging -PathType Container)) {
        try {
            Move-Item -LiteralPath $Staging -Destination $FailedStaging
        }
        catch {
            $rollbackErrors.Add("staging archive: $($_.Exception.Message)")
        }
    }
    if ($StagedVenv -and (Test-Path -LiteralPath $StagedVenv -PathType Container)) {
        try {
            Move-Item -LiteralPath $StagedVenv -Destination $FailedStagedVenv
        }
        catch {
            $rollbackErrors.Add("staged venv archive: $($_.Exception.Message)")
        }
    }

    if ($originalAppArchived -or $appPromoted -or $originalVenvArchived -or $venvPromoted) {
        $rollbackErrors.Add("app/venv transaction flags show an incomplete rollback")
    }
    if (($App -and -not (Test-Path -LiteralPath $App -PathType Container)) -or
        ($Venv -and -not (Test-Path -LiteralPath $Venv -PathType Container))) {
        $rollbackErrors.Add("active app or venv is missing after rollback")
    }

    Stop-Super1Tasks
    try {
        Wait-Super1Stopped
    }
    catch {
        $rollbackErrors.Add("stopped-state verification: $($_.Exception.Message)")
    }
    if ($originalMainTaskXml -and $runnerSid -and $expectedMainArguments) {
        try {
            Assert-FrozenSuper1PasswordTask `
                -ExpectedXml $originalMainTaskXml `
                -ExpectedRunnerSid $runnerSid `
                -ExpectedArguments $expectedMainArguments
        }
        catch {
            $rollbackErrors.Add("frozen Password task verification: $($_.Exception.Message)")
        }
    }
    if ($rollbackErrors.Count -ne 0) {
        throw [Exception]::new("Super1 signed app/venv upgrade failed ($($failure.Exception.Message)); rollback incomplete: $($rollbackErrors -join '; ')", $failure.Exception)
    }
    throw [Exception]::new("Super1 signed app/venv upgrade failed; app/venv rollback complete and tasks stopped: $($failure.Exception.Message)", $failure.Exception)
}
finally { }
}
catch {
    $primaryError = $_
}
finally {
    if ($runtimeHelpersReady -and $runtimeControlEntered) {
        try { Stop-Super1Tasks } catch { $cleanupErrors.Add("runtime stop: $($_.Exception.Message)") }
        try { Wait-Super1Stopped } catch { $cleanupErrors.Add("stopped-state verification: $($_.Exception.Message)") }
    }
    foreach ($configEvidence in @($RuntimeConfigEvidence)) {
        try {
            if ($configEvidence -and $configEvidence.lock) { $configEvidence.lock.Dispose(); $configEvidence.lock = $null }
        } catch { $cleanupErrors.Add("runtime config lock: $($_.Exception.Message)") }
    }
    try { if ($TerminalLock) { $TerminalLock.Dispose(); $TerminalLock = $null } } catch { $cleanupErrors.Add("TerminalLock: $($_.Exception.Message)") }
    try { if ($BootstrapPythonLock) { $BootstrapPythonLock.Dispose(); $BootstrapPythonLock = $null } } catch { $cleanupErrors.Add("BootstrapPythonLock: $($_.Exception.Message)") }
    try { if ($IntegrityScriptLock) { $IntegrityScriptLock.Dispose(); $IntegrityScriptLock = $null } } catch { $cleanupErrors.Add("IntegrityScriptLock: $($_.Exception.Message)") }
    foreach ($releaseInputLock in @($ReleaseInputLocks)) {
        try { if ($releaseInputLock) { $releaseInputLock.Dispose() } } catch { $cleanupErrors.Add("release input lock: $($_.Exception.Message)") }
    }
    try { if ($PowerShellHostLock) { $PowerShellHostLock.Dispose(); $PowerShellHostLock = $null } } catch { $cleanupErrors.Add("PowerShellHostLock: $($_.Exception.Message)") }
    try { if ($SelfScriptLock) { $SelfScriptLock.Dispose(); $SelfScriptLock = $null } } catch { $cleanupErrors.Add("SelfScriptLock: $($_.Exception.Message)") }
    try { $env:PSModulePath = $OriginalPSModulePath } catch { $cleanupErrors.Add("PSModulePath restore: $($_.Exception.Message)") }
    try { $env:PYTHONHOME = $OriginalPythonHome } catch { $cleanupErrors.Add("PYTHONHOME restore: $($_.Exception.Message)") }
    try { $env:PYTHONPATH = $OriginalPythonPath } catch { $cleanupErrors.Add("PYTHONPATH restore: $($_.Exception.Message)") }
}

if ($primaryError) {
    if ($cleanupErrors.Count -gt 0) { throw [Exception]::new("Super1 upgrade failed; cleanup incomplete: $($cleanupErrors -join '; ')", $primaryError.Exception) }
    throw $primaryError
}
if ($cleanupErrors.Count -gt 0) { throw "Super1 upgrade cleanup incomplete: $($cleanupErrors -join '; ')" }
$Result | ConvertTo-Json -Depth 8
