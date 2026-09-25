[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedSelfSha256,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Archive,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedTerminalSha256,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$TargetRunnerIdentity,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 2147483647)]
    [int]$ExpectedMt5Login,
    [string]$Root = "C:\ForwardShadow"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (
    [string]$PSVersionTable.PSEdition -cne "Desktop" -or
    [int]$PSVersionTable.PSVersion.Major -ne 5
) {
    throw "ForwardShadow upgrade requires Windows PowerShell 5.1."
}
$ExpectedSelfSha256 = $ExpectedSelfSha256.ToLowerInvariant()
$cleanupErrors = New-Object Collections.Generic.List[string]
$PreviousPSModulePath = $null
$PreviousPSModulePathCaptured = $false
$failureToReport = $null
$SelfPath = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
$ExpectedDeployBase = [IO.Path]::GetFullPath("C:\Program Files\OtoBacktestDeploy")
$ExpectedReleaseDirectory = [IO.Path]::GetFullPath(
    (Join-Path $ExpectedDeployBase "forward-shadow-$ExpectedSelfSha256")
)
$ExpectedSelfPath = [IO.Path]::GetFullPath(
    (Join-Path $ExpectedReleaseDirectory "upgrade_forward_shadow_windows.ps1")
)
if (-not $SelfPath.Equals($ExpectedSelfPath, [StringComparison]::OrdinalIgnoreCase)) {
    throw "ForwardShadow upgrade must run from its pinned protected Program Files release path."
}
$SelfReadLock = [IO.File]::Open(
    $SelfPath,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
try {
    $selfSha = [Security.Cryptography.SHA256]::Create()
    try {
        $actualSelfSha256 = ([BitConverter]::ToString(
            $selfSha.ComputeHash($SelfReadLock)
        )).Replace("-", "").ToLowerInvariant()
    }
    finally { $selfSha.Dispose() }
    if ($actualSelfSha256 -cne $ExpectedSelfSha256) {
        throw "ForwardShadow upgrader self SHA256 does not match the mandatory pin."
    }
    foreach ($path in @(
        [IO.Path]::GetFullPath("C:\Program Files"),
        $ExpectedDeployBase,
        $ExpectedReleaseDirectory,
        $SelfPath
    )) {
        if (
            -not (Test-Path -LiteralPath $path) -or
            (Get-Item -LiteralPath $path -Force).Attributes.HasFlag(
                [IO.FileAttributes]::ReparsePoint
            )
        ) { throw "ForwardShadow upgrader trust path is missing or contains a reparse point: $path" }
    }
    foreach ($path in @($ExpectedDeployBase, $ExpectedReleaseDirectory, $SelfPath)) {
        $item = Get-Item -LiteralPath $path -Force
        $security = if ($item.PSIsContainer) {
            [IO.Directory]::GetAccessControl(
                $path,
                [Security.AccessControl.AccessControlSections]::Access -bor
                    [Security.AccessControl.AccessControlSections]::Owner
            )
        } else {
            [IO.File]::GetAccessControl(
                $path,
                [Security.AccessControl.AccessControlSections]::Access -bor
                    [Security.AccessControl.AccessControlSections]::Owner
            )
        }
        if (
            -not $security.AreAccessRulesProtected -or
            [string]$security.GetOwner(
                [Security.Principal.SecurityIdentifier]
            ).Value -cne "S-1-5-18"
        ) { throw "ForwardShadow upgrader trust path is not protected with SYSTEM owner: $path" }
        $rules = @($security.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        ))
        if ($rules.Count -ne 2) {
            throw "ForwardShadow upgrader trust path must have exactly SYSTEM/BA DACL entries: $path"
        }
        $seen = @{}
        foreach ($rule in $rules) {
            $sid = [string]$rule.IdentityReference.Value
            if (
                $rule.IsInherited -or
                [string]$rule.AccessControlType -cne "Allow" -or
                $sid -notin @("S-1-5-18", "S-1-5-32-544") -or
                [Security.AccessControl.FileSystemRights]$rule.FileSystemRights -ne
                    [Security.AccessControl.FileSystemRights]::FullControl -or
                $seen.ContainsKey($sid)
            ) { throw "ForwardShadow upgrader trust path DACL is not exact SYSTEM/BA FullControl: $path" }
            $seen[$sid] = $true
        }
        if (-not $seen.ContainsKey("S-1-5-18") -or -not $seen.ContainsKey("S-1-5-32-544")) {
            throw "ForwardShadow upgrader trust path lacks a trusted full-control principal: $path"
        }
    }
}
catch {
    $trustError = $_
    try {
        if ($SelfReadLock) {
            $SelfReadLock.Dispose()
            $SelfReadLock = $null
        }
    }
    catch { $cleanupErrors.Add("self read lock cleanup: $($_.Exception.Message)") }
    if ($cleanupErrors.Count -gt 0) {
        throw [InvalidOperationException]::new(
            "ForwardShadow trust preflight failed: $($trustError.Exception.Message); cleanup: $($cleanupErrors -join '; ')",
            $trustError.Exception
        )
    }
    throw $trustError
}
try {
$PreviousPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$PreviousPSModulePathCaptured = $true
$TrustedPSModulePath = [IO.Path]::GetFullPath((Join-Path $PSHOME "Modules"))
[Environment]::SetEnvironmentVariable("PSModulePath", $TrustedPSModulePath, "Process")
$ScheduledTasksModule = Join-Path $TrustedPSModulePath "ScheduledTasks\ScheduledTasks.psd1"
if (-not (Test-Path -LiteralPath $ScheduledTasksModule -PathType Leaf)) {
    throw "Trusted Windows ScheduledTasks module is missing."
}
Import-Module -Name $ScheduledTasksModule -Force -ErrorAction Stop
foreach ($commandName in @(
    "Export-ScheduledTask",
    "Get-ScheduledTask",
    "Get-ScheduledTaskInfo",
    "New-ScheduledTaskAction",
    "New-ScheduledTaskPrincipal",
    "New-ScheduledTaskSettingsSet",
    "New-ScheduledTaskTrigger",
    "Set-ScheduledTask",
    "Stop-ScheduledTask"
)) {
    $command = Get-Command -Name $commandName -ErrorAction Stop
    if (
        $null -eq $command.Module -or
        [IO.Path]::GetFullPath([string]$command.Module.Path) -cne
            [IO.Path]::GetFullPath($ScheduledTasksModule)
    ) {
        throw "ScheduledTasks command did not resolve from the trusted module: $commandName"
    }
}
$CimCmdletsModule = Join-Path $TrustedPSModulePath "CimCmdlets\CimCmdlets.psd1"
$ArchiveModule = Join-Path `
    $TrustedPSModulePath `
    "Microsoft.PowerShell.Archive\Microsoft.PowerShell.Archive.psd1"
foreach ($modulePath in @($CimCmdletsModule, $ArchiveModule)) {
    if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
        throw "Trusted Windows PowerShell module is missing: $modulePath"
    }
    Import-Module -Name $modulePath -Force -ErrorAction Stop
}
$archiveCommand = Get-Command -Name "Expand-Archive" -ErrorAction Stop
if (
    $null -eq $archiveCommand.Module -or
    [IO.Path]::GetFullPath([string]$archiveCommand.Module.Path) -cne
        [IO.Path]::GetFullPath($ArchiveModule)
) {
    throw "Expand-Archive did not resolve from the trusted module."
}
$SystemDirectory = [IO.Path]::GetFullPath([Environment]::SystemDirectory)
$WindowsRoot = [IO.Directory]::GetParent($SystemDirectory).FullName
$TrustedCimBinaryRoot = [IO.Path]::GetFullPath((Join-Path `
    $WindowsRoot `
    "Microsoft.Net\assembly\GAC_MSIL\Microsoft.Management.Infrastructure.CimCmdlets"))
$TrustedCimPrefix = $TrustedCimBinaryRoot.TrimEnd('\') + '\'
foreach ($commandName in @("Get-CimInstance", "Invoke-CimMethod")) {
    $command = Get-Command -Name $commandName -ErrorAction Stop
    $commandPath = [IO.Path]::GetFullPath([string]$command.Module.Path)
    if (
        $null -eq $command.Module -or
        [string]$command.ModuleName -cne "CimCmdlets" -or
        -not $commandPath.StartsWith($TrustedCimPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "CIM command did not resolve from the trusted Windows binary: $commandName"
    }
}
$IcaclsExe = [IO.Path]::GetFullPath((Join-Path $SystemDirectory "icacls.exe"))
$WindowsPowerShellExe = [IO.Path]::GetFullPath((Join-Path $PSHOME "powershell.exe"))
foreach ($trustedExecutable in @($IcaclsExe, $WindowsPowerShellExe)) {
    if (-not (Test-Path -LiteralPath $trustedExecutable -PathType Leaf)) {
        throw "Trusted Windows executable is missing: $trustedExecutable"
    }
}
}
catch {
    $moduleError = $_
    if ($PreviousPSModulePathCaptured) {
        try {
            [Environment]::SetEnvironmentVariable("PSModulePath", $PreviousPSModulePath, "Process")
        }
        catch { $cleanupErrors.Add("PSModulePath restore: $($_.Exception.Message)") }
    }
    try {
        if ($SelfReadLock) {
            $SelfReadLock.Dispose()
            $SelfReadLock = $null
        }
    }
    catch { $cleanupErrors.Add("self read lock cleanup: $($_.Exception.Message)") }
    if ($cleanupErrors.Count -gt 0) {
        throw [InvalidOperationException]::new(
            "ForwardShadow trusted module preflight failed: $($moduleError.Exception.Message); cleanup: $($cleanupErrors -join '; ')",
            $moduleError.Exception
        )
    }
    throw $moduleError
}

$MainTask = "ForwardShadowXM"
$WatchdogTask = "ForwardShadowWatchdog"

try {
    $Root = [IO.Path]::GetFullPath($Root)
    $ArchivePath = [IO.Path]::GetFullPath($Archive)
    $App = [IO.Path]::GetFullPath((Join-Path $Root "app"))
    $ArchiveRoot = [IO.Path]::GetFullPath((Join-Path $Root "archive"))
    $CanonicalState = [IO.Path]::GetFullPath((Join-Path $Root "state"))
    $LegacyState = [IO.Path]::GetFullPath(
        (Join-Path $App "outputs\xm_mt5_forward\nq3m_spx5m")
    )
    $Venv = [IO.Path]::GetFullPath((Join-Path $Root "venv311"))
    $Python = [IO.Path]::GetFullPath((Join-Path $Venv "Scripts\python.exe"))
    $ServerFile = [IO.Path]::GetFullPath((Join-Path $Root "xm-server.txt"))
    $TerminalFile = [IO.Path]::GetFullPath((Join-Path $Root "mt5-terminal.txt"))
    $PasswordFile = [IO.Path]::GetFullPath((Join-Path $Root "xm-readonly-password.dpapi"))
    $ProductionTerminalConfig = [IO.Path]::GetFullPath(
        (Join-Path $Root "mt5-production.ini")
    )
    $RunnerTokenSentinel = [IO.Path]::GetFullPath(
        (Join-Path $Root "runner-token-sentinel.dat")
    )
    $WatchdogHealth = [IO.Path]::GetFullPath((Join-Path $CanonicalState "health.json"))
    $WatchdogStatus = [IO.Path]::GetFullPath((Join-Path $Root "watchdog_status.json"))
    $IntegrityScript = Join-Path $PSScriptRoot "release_integrity.ps1"
$ExpectedIntegrityScriptSha256 = "9e29b8d0c34127e3caa74b2625e63fe02e1fc6b927dc992dae77baa54f4131a9"
    $ExpectedTerminalSha256 = $ExpectedTerminalSha256.ToLowerInvariant()
    $RunId = [Guid]::NewGuid().ToString("N")
    $RunnerProbeTerminalConfig = [IO.Path]::GetFullPath(
        (Join-Path $Root "runner-terminal-probe.$RunId.ini")
    )
    $Staging = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot "app.stage.$RunId"))
    $VenvStaging = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot "venv311.stage.$RunId"))
    $StagedPython = [IO.Path]::GetFullPath((Join-Path $VenvStaging "Scripts\python.exe"))
    $ValidationRoot = [IO.Path]::GetFullPath(
        (Join-Path $ArchiveRoot "upgrade-validation.$RunId")
    )
    $ValidationRootCreated = $false
    $LegacyStateHold = [IO.Path]::GetFullPath(
        (Join-Path $ArchiveRoot "legacy-state.hold.$RunId")
    )
    $CapitalCandidate = [IO.Path]::GetFullPath((Join-Path $Root "run_capital_forward.py.candidate"))
    $XmCandidate = [IO.Path]::GetFullPath((Join-Path $Root "run_xm_mt5_forward.py.candidate"))
    $LauncherCandidate = [IO.Path]::GetFullPath(
        (Join-Path $Root "run_forward_shadow_windows.ps1.candidate")
    )
    $CandidateTempPaths = [Collections.Generic.List[string]]::new()
    $CreatedCandidates = [Collections.Generic.List[string]]::new()
    $SignedReleaseLocks = [Collections.Generic.List[IDisposable]]::new()
    $TransactionArchive = $null
    $SignedReleaseRoot = $null
    $VerifiedArchivePath = $null
    $PreviousApp = $null
    $PreviousVenv = $null
    $PreviousAppFingerprint = $null
    $PreviousVenvFingerprint = $null
    $AppBootstrapSealed = $false
    $VenvBootstrapSealed = $false
    $BootstrapPython = $null
    $BootstrapPythonEvidence = $null
    $TerminalExecutableEvidence = $null
    $ServerSettingCopy = $null
    $TerminalSettingCopy = $null
    $BrokerSettingsHardened = $false
    $OldAppArchived = $false
    $OldVenvArchived = $false
    $NewAppActivated = $false
    $NewVenvActivated = $false
    $OriginalMainTaskXml = $null
    $OriginalWatchdogTaskXml = $null
    $OriginalMainActions = $null
    $OriginalMainTriggers = $null
    $OriginalMainSettings = $null
    $OriginalMainPrincipal = $null
    $OriginalWatchdogActions = $null
    $OriginalWatchdogTriggers = $null
    $OriginalWatchdogSettings = $null
    $OriginalWatchdogPrincipal = $null
    $OriginalRunnerIdentity = $null
    $OriginalRunnerSid = $null
    $TaskDefinitionsChanged = $false
    $UpgradeSucceeded = $false
    $runtimeControlEntered = $false
    $StateMode = $null
    $CanonicalStateFingerprint = $null
    $CanonicalStateAclSnapshot = $null
    $CanonicalStateAclHardened = $false
    $CanonicalStateCreated = $false
    $LegacyStateFingerprint = $null
    $LegacyStateAclSnapshot = $null
    $LegacyStateAclEvidence = $null
    $RunnerSid = $null
    $RunnerIdentity = $null
    $RunnerTokenSentinelCreated = $false
    $RunnerTokenProbePassed = $false
    $RunnerBrokerProof = $null
    $RunnerProbeTerminalConfigLock = $null
    $RunnerProbeTerminalConfigCreated = $false
    $ProductionTerminalConfigLock = $null
    $ProductionTerminalConfigCreated = $false
    $ProductionTerminalConfigHardened = $false
    $TargetRunnerPassword = $null
    $RunnerOriginalAccountRights = $null
    $RunnerRightsHardened = $false
    $RootAclSnapshot = $null
    $RootAclHardened = $false
    $ArchiveBoundaryHardened = $false
    $PreviousServerEnv = [Environment]::GetEnvironmentVariable("XM_MT5_SERVER", "Process")
    $PreviousTerminalEnv = [Environment]::GetEnvironmentVariable("XM_MT5_TERMINAL_PATH", "Process")
    $PreviousPythonEnvironment = @{}
    foreach ($name in @(
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE"
    )) {
        $PreviousPythonEnvironment[$name] = [Environment]::GetEnvironmentVariable(
            $name,
            "Process"
        )
    }
}
catch {
    $initializationError = $_
    if ($PreviousPSModulePathCaptured) {
        try {
            [Environment]::SetEnvironmentVariable("PSModulePath", $PreviousPSModulePath, "Process")
        }
        catch { $cleanupErrors.Add("PSModulePath restore: $($_.Exception.Message)") }
    }
    try {
        if ($SelfReadLock) {
            $SelfReadLock.Dispose()
            $SelfReadLock = $null
        }
    }
    catch { $cleanupErrors.Add("self read lock cleanup: $($_.Exception.Message)") }
    if ($cleanupErrors.Count -gt 0) {
        throw [InvalidOperationException]::new(
            "ForwardShadow path initialization failed: $($initializationError.Exception.Message); cleanup: $($cleanupErrors -join '; ')",
            $initializationError.Exception
        )
    }
    throw $initializationError
}

function Assert-RootChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = $Root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escapes the ForwardShadow root: $resolved"
    }
}

function Resolve-ForwardIdentitySid {
    param([Parameter(Mandatory = $true)][string]$Identity)
    if ($Identity -match '^S-\d-(?:\d+-)+\d+$') {
        return ([Security.Principal.SecurityIdentifier]::new($Identity)).Value
    }
    try {
        return ([Security.Principal.NTAccount]$Identity).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch {
        throw "Could not resolve ForwardShadow ACL identity to a SID: $Identity"
    }
}

function Get-ForwardAclRuleSid {
    param(
        [Parameter(Mandatory = $true)]
        [Security.Principal.IdentityReference]$Identity
    )
    try {
        return $Identity.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        throw "Could not resolve ForwardShadow ACL rule identity: $Identity"
    }
}

function Assert-ForwardTrustedOwner {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse,
        [switch]$AllowRoot
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($AllowRoot) {
        if (-not $resolved.Equals($Root, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Trusted-owner root exception is limited to the ForwardShadow root: $resolved"
        }
    }
    else {
        Assert-RootChildPath -Path $resolved -Label "Trusted-owner path"
    }
    $items = @((Get-Item -LiteralPath $resolved -Force))
    if ($Recurse) {
        if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
            throw "Recursive trusted-owner path is not a directory: $resolved"
        }
        $items += @(Get-ChildItem -LiteralPath $resolved -Recurse -Force)
    }
    foreach ($item in $items) {
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "Trusted-owner path contains a reparse point: $($item.FullName)"
        }
        $ownerSid = [string](Get-Acl -LiteralPath $item.FullName).GetOwner(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if ($ownerSid -cne "S-1-5-18") {
            throw "ForwardShadow immutable owner is not SYSTEM: $($item.FullName) owner=$ownerSid"
        }
    }
}

function Set-ForwardTrustedOwner {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse,
        [switch]$AllowRoot
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($AllowRoot) {
        if (-not $resolved.Equals($Root, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Trusted-owner root exception is limited to the ForwardShadow root: $resolved"
        }
    }
    else {
        Assert-RootChildPath -Path $resolved -Label "Trusted-owner path"
    }
    $ownerTargets = @((Get-Item -LiteralPath $resolved -Force))
    if ($Recurse) {
        if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
            throw "Recursive trusted-owner path is not a directory: $resolved"
        }
        $ownerTargets += @(Get-ChildItem -LiteralPath $resolved -Recurse -Force)
    }
    $ownerReparse = @(
        $ownerTargets |
            Where-Object { $_.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) } |
            Select-Object -First 1
    )
    if ($ownerReparse.Count) {
        throw "Refusing to set owner through a ForwardShadow reparse point: $($ownerReparse[0].FullName)"
    }
    if ($Recurse) {
        & $IcaclsExe $resolved /setowner "*S-1-5-18" /T /C /Q | Out-Null
    }
    else {
        & $IcaclsExe $resolved /setowner "*S-1-5-18" /Q | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not set ForwardShadow immutable owner to SYSTEM: $resolved"
    }
    Assert-ForwardTrustedOwner `
        -Path $resolved `
        -Recurse:$Recurse `
        -AllowRoot:$AllowRoot
}

function Set-ForwardExactAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$FullControlSids,
        [AllowEmptyString()][string]$ReadOnlySid = "",
        [AllowEmptyString()][string]$ModifySid = "",
        [switch]$Directory,
        [switch]$AllowRoot
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($AllowRoot) {
        if (-not $resolved.Equals($Root, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Exact root ACL exception is limited to the ForwardShadow root."
        }
    }
    else {
        Assert-RootChildPath -Path $resolved -Label "Exact ForwardShadow ACL path"
    }
    $acl = if ($Directory) {
        New-Object Security.AccessControl.DirectorySecurity
    } else {
        New-Object Security.AccessControl.FileSecurity
    }
    $acl.SetAccessRuleProtection($true, $false)
    $inheritanceFlags = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
        [Security.AccessControl.InheritanceFlags]::None
    }
    foreach ($sid in @($FullControlSids | Select-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($sid)) { continue }
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritanceFlags,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    if (
        -not [string]::IsNullOrWhiteSpace($ReadOnlySid) -and
        $ReadOnlySid -notin $FullControlSids
    ) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($ReadOnlySid),
            [Security.AccessControl.FileSystemRights]::ReadAndExecute,
            $inheritanceFlags,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    if (
        -not [string]::IsNullOrWhiteSpace($ModifySid) -and
        $ModifySid -notin $FullControlSids -and
        $ModifySid -cne $ReadOnlySid
    ) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($ModifySid),
            [Security.AccessControl.FileSystemRights]::Modify,
            $inheritanceFlags,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    if ($Directory) {
        [IO.Directory]::SetAccessControl($resolved, $acl)
    } else {
        [IO.File]::SetAccessControl($resolved, $acl)
    }
}

function Assert-NoUntrustedDeleteChild {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$TrustedSids
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    if ((Get-Item -LiteralPath $resolved -Force).Attributes.HasFlag(
        [IO.FileAttributes]::ReparsePoint
    )) {
        throw "ForwardShadow trusted parent cannot be a reparse point: $resolved"
    }
    $parentMutation = (
        [int][Security.AccessControl.FileSystemRights]::Write -bor
        [int][Security.AccessControl.FileSystemRights]::WriteData -bor
        [int][Security.AccessControl.FileSystemRights]::CreateFiles -bor
        [int][Security.AccessControl.FileSystemRights]::AppendData -bor
        [int][Security.AccessControl.FileSystemRights]::CreateDirectories -bor
        [int][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [int][Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [int][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [int][Security.AccessControl.FileSystemRights]::Delete -bor
        [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [int][Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    foreach ($rule in @(Get-Acl -LiteralPath $resolved).Access) {
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)
        ) {
            continue
        }
        if (([int]$rule.FileSystemRights -band $parentMutation) -eq 0) {
            continue
        }
        $sid = Get-ForwardAclRuleSid -Identity $rule.IdentityReference
        if ($sid -notin $TrustedSids) {
            throw "Untrusted SID can mutate/delete the ForwardShadow trusted boundary $resolved`: $sid"
        }
    }
}

function Move-ForwardDirectoryExact {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $resolvedSource = [IO.Path]::GetFullPath($Source)
    $resolvedDestination = [IO.Path]::GetFullPath($Destination)
    Assert-RootChildPath -Path $resolvedSource -Label "$Label source"
    Assert-RootChildPath -Path $resolvedDestination -Label "$Label destination"
    if (-not (Test-Path -LiteralPath $resolvedSource -PathType Container)) {
        throw "$Label source directory is missing: $resolvedSource"
    }
    if ((Get-Item -LiteralPath $resolvedSource -Force).Attributes.HasFlag(
        [IO.FileAttributes]::ReparsePoint
    )) {
        throw "$Label source cannot be a reparse point: $resolvedSource"
    }
    if (Test-Path -LiteralPath $resolvedDestination) {
        throw "$Label destination already exists: $resolvedDestination"
    }
    foreach ($parent in @(
        [IO.Path]::GetFullPath((Split-Path -Parent $resolvedSource)),
        [IO.Path]::GetFullPath((Split-Path -Parent $resolvedDestination))
    ) | Select-Object -Unique) {
        $parentIsRoot = $parent.Equals($Root, [StringComparison]::OrdinalIgnoreCase)
        Assert-ForwardTrustedOwner -Path $parent -AllowRoot:$parentIsRoot
        Assert-NoUntrustedDeleteChild `
            -Path $parent `
            -TrustedSids @("S-1-5-18", "S-1-5-32-544")
    }
    [IO.Directory]::Move($resolvedSource, $resolvedDestination)
    if (
        (Test-Path -LiteralPath $resolvedSource) -or
        -not (Test-Path -LiteralPath $resolvedDestination -PathType Container) -or
        (Get-Item -LiteralPath $resolvedDestination -Force).Attributes.HasFlag(
            [IO.FileAttributes]::ReparsePoint
        )
    ) {
        throw "$Label exact directory move postcondition failed."
    }
}

function New-ForwardRestorePrivilegeScope {
    if (-not ("ForwardShadowUpgrade.NativeTokenPrivileges" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace ForwardShadowUpgrade {
    public static class NativeTokenPrivileges {
        [StructLayout(LayoutKind.Sequential)]
        private struct Luid {
            public UInt32 LowPart;
            public Int32 HighPart;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TokenPrivileges {
            public UInt32 PrivilegeCount;
            public Luid Luid;
            public UInt32 Attributes;
        }

        private const UInt32 TokenAdjustPrivileges = 0x20;
        private const UInt32 TokenQuery = 0x08;
        private const UInt32 PrivilegeEnabled = 0x02;
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(
            IntPtr processHandle,
            UInt32 desiredAccess,
            out IntPtr tokenHandle
        );

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool LookupPrivilegeValue(
            string systemName,
            string name,
            out Luid luid
        );

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool AdjustTokenPrivileges(
            IntPtr tokenHandle,
            bool disableAllPrivileges,
            ref TokenPrivileges newState,
            UInt32 bufferLength,
            ref TokenPrivileges previousState,
            out UInt32 returnLength
        );

        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        private sealed class PrivilegeScope : IDisposable {
            private IntPtr tokenHandle;
            private TokenPrivileges previousState;
            private bool restore;

            public PrivilegeScope(string privilegeName) {
                tokenHandle = IntPtr.Zero;
                if (!OpenProcessToken(
                    Process.GetCurrentProcess().Handle,
                    TokenAdjustPrivileges | TokenQuery,
                    out tokenHandle
                )) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                try {
                    Luid luid;
                    if (!LookupPrivilegeValue(null, privilegeName, out luid)) {
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    TokenPrivileges privileges = new TokenPrivileges();
                    privileges.PrivilegeCount = 1;
                    privileges.Luid = luid;
                    privileges.Attributes = PrivilegeEnabled;
                    previousState = new TokenPrivileges();
                    UInt32 returnLength;
                    if (!AdjustTokenPrivileges(
                        tokenHandle,
                        false,
                        ref privileges,
                        (UInt32)Marshal.SizeOf(typeof(TokenPrivileges)),
                        ref previousState,
                        out returnLength
                    )) {
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    int error = Marshal.GetLastWin32Error();
                    if (error != 0) {
                        throw new Win32Exception(error);
                    }
                    restore = true;
                }
                catch {
                    CloseHandle(tokenHandle);
                    tokenHandle = IntPtr.Zero;
                    throw;
                }
            }

            public void Dispose() {
                if (tokenHandle == IntPtr.Zero) {
                    return;
                }
                int restoreError = 0;
                if (restore) {
                    TokenPrivileges ignoredState = new TokenPrivileges();
                    UInt32 returnLength;
                    if (!AdjustTokenPrivileges(
                        tokenHandle,
                        false,
                        ref previousState,
                        (UInt32)Marshal.SizeOf(typeof(TokenPrivileges)),
                        ref ignoredState,
                        out returnLength
                    )) {
                        restoreError = Marshal.GetLastWin32Error();
                    }
                    else {
                        restoreError = Marshal.GetLastWin32Error();
                    }
                }
                CloseHandle(tokenHandle);
                tokenHandle = IntPtr.Zero;
                restore = false;
                if (restoreError != 0) {
                    throw new Win32Exception(restoreError);
                }
            }
        }

        public static IDisposable EnableScoped(string privilegeName) {
            return new PrivilegeScope(privilegeName);
        }
    }
}
'@
    }
    return [ForwardShadowUpgrade.NativeTokenPrivileges]::EnableScoped(
        "SeRestorePrivilege"
    )
}

function New-ForwardPrivateDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowNull()][ref]$Created
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    Assert-RootChildPath -Path $resolved -Label "Private ForwardShadow directory"
    if (Test-Path -LiteralPath $resolved) {
        throw "Private ForwardShadow directory already exists: $resolved"
    }
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $resolved))
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "Private ForwardShadow parent directory is missing: $parent"
    }
    $parentIsRoot = $parent.Equals($Root, [StringComparison]::OrdinalIgnoreCase)
    Assert-ForwardTrustedOwner `
        -Path $parent `
        -AllowRoot:$parentIsRoot
    Assert-NoUntrustedDeleteChild `
        -Path $parent `
        -TrustedSids @("S-1-5-18", "S-1-5-32-544")

    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $administratorsSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $security.SetOwner($systemSid)
    $security.SetGroup($administratorsSid)
    $inheritance = (
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    foreach ($sid in @($systemSid, $administratorsSid)) {
        [void]$security.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    $directory = New-Object IO.DirectoryInfo -ArgumentList $resolved
    $restorePrivilege = New-ForwardRestorePrivilegeScope
    try {
        $directory.Create($security)
        if ($null -ne $Created) {
            $Created.Value = $true
        }
    }
    finally {
        $restorePrivilege.Dispose()
    }
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Atomic private ForwardShadow directory creation failed: $resolved"
    }
    Assert-ForwardTrustedOwner -Path $resolved
    Assert-ForwardAclSemantics -Path $resolved -DirectoryRoot
    & $IcaclsExe $resolved /verify /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Atomic private ForwardShadow directory ACL verification failed: $resolved"
    }
}

function Assert-ForwardAclSemantics {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$RunnerSid = "",
        [AllowEmptyString()][string]$ModifySid = "",
        [switch]$Inherited,
        [switch]$DirectoryRoot
    )
    $acl = Get-Acl -LiteralPath $Path
    if ($Inherited -and $acl.AreAccessRulesProtected) {
        throw "ForwardShadow descendant ACL does not inherit: $Path"
    }
    if (-not $Inherited -and -not $acl.AreAccessRulesProtected) {
        throw "ForwardShadow protected root/file ACL still inherits: $Path"
    }
    $systemSid = "S-1-5-18"
    $administratorsSid = "S-1-5-32-544"
    $expectedSids = @(
        @($systemSid, $administratorsSid, $RunnerSid, $ModifySid) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Select-Object -Unique
    )
    $rightsBySid = @{}
    foreach ($rule in $acl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )) {
        $sid = [string]$rule.IdentityReference.Value
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $sid -notin $expectedSids -or
            [bool]$rule.IsInherited -ne [bool]$Inherited
        ) {
            throw "ForwardShadow ACL has an unexpected rule: $Path sid=$sid"
        }
        if ($DirectoryRoot) {
            $requiredInheritance = (
                [int][Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [int][Security.AccessControl.InheritanceFlags]::ObjectInherit
            )
            if (([int]$rule.InheritanceFlags -band $requiredInheritance) -ne $requiredInheritance) {
                throw "ForwardShadow root ACL rule does not inherit to files/directories: $Path sid=$sid"
            }
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
    foreach ($fullControlSid in @($systemSid, $administratorsSid)) {
        if (
            -not [string]::IsNullOrWhiteSpace($fullControlSid) -and
            ([int]$rightsBySid[$fullControlSid] -band $full) -ne $full
        ) {
            throw "ForwardShadow trusted ACL identity lacks full control: $Path sid=$fullControlSid"
        }
    }
    if (
        -not [string]::IsNullOrWhiteSpace($RunnerSid) -and
        (
            ([int]$rightsBySid[$RunnerSid] -band $readExecute) -ne $readExecute -or
            ([int]$rightsBySid[$RunnerSid] -band $mutation) -ne 0
        )
    ) {
        throw "ForwardShadow runner ACL is not read/execute-only: $Path"
    }
    if (-not [string]::IsNullOrWhiteSpace($ModifySid)) {
        $modify = [int][Security.AccessControl.FileSystemRights]::Modify
        $administrativeMutation = (
            [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor
            [int][Security.AccessControl.FileSystemRights]::TakeOwnership
        )
        if (
            ([int]$rightsBySid[$ModifySid] -band $modify) -ne $modify -or
            ([int]$rightsBySid[$ModifySid] -band $administrativeMutation) -ne 0
        ) {
            throw "ForwardShadow state runner ACL is not modify-only: $Path"
        }
    }
}

function Protect-ForwardTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$RunnerIdentity = "",
        [switch]$AllowEmpty,
        [switch]$RootOnly,
        [switch]$SealOwner
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    Assert-RootChildPath -Path $resolved -Label "Protected ForwardShadow tree"
    if ((Get-Item -LiteralPath $resolved -Force).Attributes.HasFlag(
        [IO.FileAttributes]::ReparsePoint
    )) {
        throw "Protected ForwardShadow tree root cannot be a reparse point: $resolved"
    }
    $runnerSid = if ([string]::IsNullOrWhiteSpace($RunnerIdentity)) {
        ""
    } else {
        Resolve-ForwardIdentitySid -Identity $RunnerIdentity
    }
    $fullControlSids = @("S-1-5-18", "S-1-5-32-544")
    $readOnlyRunnerSid = if (
        $runnerSid -and $runnerSid -notin $fullControlSids
    ) { $runnerSid } else { "" }
    Set-ForwardExactAcl `
        -Path $resolved `
        -FullControlSids $fullControlSids `
        -ReadOnlySid $readOnlyRunnerSid `
        -Directory
    Assert-ForwardAclSemantics `
        -Path $resolved `
        -RunnerSid $readOnlyRunnerSid `
        -DirectoryRoot
    & $IcaclsExe $resolved /verify /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "ForwardShadow protected root ACL verification failed: $resolved"
    }
    if ($RootOnly) {
        if ($SealOwner) {
            Set-ForwardTrustedOwner -Path $resolved
        }
        return
    }
    $reparse = Get-ChildItem -LiteralPath $resolved -Recurse -Force |
        Where-Object { $_.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) } |
        Select-Object -First 1
    if ($reparse) {
        throw "Protected ForwardShadow tree contains a reparse point: $($reparse.FullName)"
    }
    if (@(Get-ChildItem -LiteralPath $resolved -Force).Count -ne 0) {
        $children = Join-Path $resolved "*"
        & $IcaclsExe $children /reset /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not reset ForwardShadow descendant ACLs from the protected root: $resolved"
        }
    }
    & $IcaclsExe $resolved /verify /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "ForwardShadow protected tree ACL verification failed: $resolved"
    }
    Assert-ForwardAclSemantics `
        -Path $resolved `
        -RunnerSid $readOnlyRunnerSid `
        -DirectoryRoot
    $sampleDirectory = Get-ChildItem -LiteralPath $resolved -Recurse -Force -Directory |
        Select-Object -First 1
    $sampleFile = Get-ChildItem -LiteralPath $resolved -Recurse -Force -File |
        Select-Object -First 1
    $samples = @()
    if ($sampleDirectory) { $samples += $sampleDirectory }
    if ($sampleFile) { $samples += $sampleFile }
    if ($samples.Count -eq 0) {
        if (-not $AllowEmpty) {
            throw "ForwardShadow protected tree has no descendant ACL sample: $resolved"
        }
    }
    foreach ($sample in $samples) {
        Assert-ForwardAclSemantics `
            -Path $sample.FullName `
            -RunnerSid $readOnlyRunnerSid `
            -Inherited
    }
    if ($SealOwner) {
        Set-ForwardTrustedOwner -Path $resolved -Recurse
    }
}

function Protect-ForwardPrivateFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$RunnerSid = ""
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    Assert-RootChildPath -Path $resolved -Label "Private ForwardShadow file"
    if ((Get-Item -LiteralPath $resolved -Force).Attributes.HasFlag(
        [IO.FileAttributes]::ReparsePoint
    )) {
        throw "Private ForwardShadow file cannot be a reparse point: $resolved"
    }
    [IO.File]::SetAttributes(
        $resolved,
        ([IO.File]::GetAttributes($resolved) -bor [IO.FileAttributes]::ReadOnly)
    )
    $fullControlSids = @("S-1-5-18", "S-1-5-32-544")
    $readOnlyRunnerSid = if (
        -not [string]::IsNullOrWhiteSpace($RunnerSid) -and
        $RunnerSid -notin $fullControlSids
    ) { $RunnerSid } else { "" }
    Set-ForwardExactAcl `
        -Path $resolved `
        -FullControlSids $fullControlSids `
        -ReadOnlySid $readOnlyRunnerSid
    & $IcaclsExe $resolved /verify /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "ForwardShadow private file ACL verification failed: $resolved"
    }
    Assert-ForwardAclSemantics `
        -Path $resolved `
        -RunnerSid $readOnlyRunnerSid
    if (-not [IO.File]::GetAttributes($resolved).HasFlag([IO.FileAttributes]::ReadOnly)) {
        throw "ForwardShadow private file is not read-only: $resolved"
    }
    Set-ForwardTrustedOwner -Path $resolved
}

function Initialize-ForwardRunnerTokenSentinel {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    Assert-RootChildPath -Path $resolved -Label "Runner token sentinel"
    if ($RunnerSid -in @("S-1-5-18", "S-1-5-32-544")) {
        throw "ForwardShadow task runner must resolve to a user SID, not a trusted group SID."
    }
    $marker = "FORWARD_SHADOW_RUNNER_TOKEN_SENTINEL_V1"
    $created = $false
    if (Test-Path -LiteralPath $resolved) {
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "ForwardShadow runner token sentinel is not a file."
        }
    }
    else {
        $stream = $null
        try {
            $stream = [IO.File]::Open(
                $resolved,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            $payload = [Text.Encoding]::UTF8.GetBytes($marker)
            $stream.Write($payload, 0, $payload.Length)
            $stream.Flush($true)
            $created = $true
        }
        catch {
            if ($stream) { $stream.Dispose(); $stream = $null }
            if (Test-Path -LiteralPath $resolved -PathType Leaf) {
                Remove-Item -LiteralPath $resolved -Force
            }
            throw
        }
        finally {
            if ($stream) { $stream.Dispose() }
        }
    }
    $item = Get-Item -LiteralPath $resolved -Force
    if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
        throw "ForwardShadow runner token sentinel cannot be a reparse point."
    }
    if ([IO.File]::ReadAllText($resolved) -cne $marker) {
        throw "ForwardShadow runner token sentinel content is invalid."
    }
    [IO.File]::SetAttributes($resolved, [IO.FileAttributes]::Normal)
    Set-ForwardExactAcl `
        -Path $resolved `
        -FullControlSids @("S-1-5-18", "S-1-5-32-544") `
        -ReadOnlySid $RunnerSid
    Set-ForwardTrustedOwner -Path $resolved
    Assert-ForwardAclSemantics -Path $resolved -RunnerSid $RunnerSid
    if (
        [IO.File]::GetAttributes($resolved).HasFlag([IO.FileAttributes]::ReadOnly) -or
        [IO.File]::ReadAllText($resolved) -cne $marker
    ) {
        throw "ForwardShadow runner token sentinel final verification failed."
    }
    return $created
}

function Protect-ForwardStateTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    Assert-RootChildPath -Path $resolved -Label "ForwardShadow writable state"
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "ForwardShadow writable state directory is missing: $resolved"
    }
    if ($RunnerSid -in @("S-1-5-18", "S-1-5-32-544")) {
        throw "ForwardShadow state runner must resolve to a user SID."
    }
    $reparse = @(
        Get-Item -LiteralPath $resolved -Force
        Get-ChildItem -LiteralPath $resolved -Recurse -Force
    ) | Where-Object { $_.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) } |
        Select-Object -First 1
    if ($reparse) {
        throw "ForwardShadow writable state contains a reparse point: $($reparse.FullName)"
    }
    Set-ForwardExactAcl `
        -Path $resolved `
        -FullControlSids @("S-1-5-18", "S-1-5-32-544") `
        -ModifySid $RunnerSid `
        -Directory
    if (@(Get-ChildItem -LiteralPath $resolved -Force).Count -ne 0) {
        & $IcaclsExe (Join-Path $resolved "*") /reset /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not reset ForwardShadow writable-state descendant ACLs."
        }
    }
    Set-ForwardTrustedOwner -Path $resolved -Recurse
    & $IcaclsExe $resolved /verify /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "ForwardShadow writable-state ACL verification failed."
    }
    Assert-ForwardAclSemantics `
        -Path $resolved `
        -ModifySid $RunnerSid `
        -DirectoryRoot
    foreach ($item in Get-ChildItem -LiteralPath $resolved -Recurse -Force) {
        Assert-ForwardAclSemantics `
            -Path $item.FullName `
            -ModifySid $RunnerSid `
            -Inherited
    }
}

function Get-ForwardRootAclSnapshot {
    $acl = Get-Acl -LiteralPath $Root
    return [pscustomobject]@{
        owner_sid = [string]$acl.GetOwner(
            [Security.Principal.SecurityIdentifier]
        ).Value
        group_sid = [string]$acl.GetGroup(
            [Security.Principal.SecurityIdentifier]
        ).Value
        access_sddl = [string]$acl.GetSecurityDescriptorSddlForm(
            [Security.AccessControl.AccessControlSections]::Access
        )
    }
}

function Assert-ForwardRootAclSnapshot {
    param([Parameter(Mandatory = $true)]$Snapshot)
    $actual = Get-ForwardRootAclSnapshot
    if (
        [string]$actual.owner_sid -cne [string]$Snapshot.owner_sid -or
        [string]$actual.group_sid -cne [string]$Snapshot.group_sid -or
        [string]$actual.access_sddl -cne [string]$Snapshot.access_sddl
    ) {
        throw "ForwardShadow root owner/group/DACL restore mismatch."
    }
}

function Restore-ForwardRootAclSnapshot {
    param([Parameter(Mandatory = $true)]$Snapshot)
    $restorePrivilege = New-ForwardRestorePrivilegeScope
    try {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetSecurityDescriptorSddlForm(
            [string]$Snapshot.access_sddl,
            [Security.AccessControl.AccessControlSections]::Access
        )
        $acl.SetOwner(
            [Security.Principal.SecurityIdentifier]::new([string]$Snapshot.owner_sid)
        )
        $acl.SetGroup(
            [Security.Principal.SecurityIdentifier]::new([string]$Snapshot.group_sid)
        )
        [IO.Directory]::SetAccessControl($Root, $acl)
    }
    finally {
        $restorePrivilege.Dispose()
    }
    Assert-ForwardRootAclSnapshot -Snapshot $Snapshot
}

function Protect-ForwardRoot {
    param([Parameter(Mandatory = $true)][string]$RunnerSid)
    if ((Get-Item -LiteralPath $Root -Force).Attributes.HasFlag(
        [IO.FileAttributes]::ReparsePoint
    )) {
        throw "ForwardShadow root cannot be a reparse point."
    }
    Set-ForwardExactAcl `
        -Path $Root `
        -FullControlSids @("S-1-5-18", "S-1-5-32-544") `
        -ReadOnlySid $RunnerSid `
        -Directory `
        -AllowRoot
    Set-ForwardTrustedOwner -Path $Root -AllowRoot
    Assert-ForwardAclSemantics `
        -Path $Root `
        -RunnerSid $RunnerSid `
        -DirectoryRoot
    & $IcaclsExe $Root /verify /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "ForwardShadow root ACL verification failed."
    }
    Assert-NoUntrustedDeleteChild `
        -Path $Root `
        -TrustedSids @("S-1-5-18", "S-1-5-32-544")
}

function Assert-ForwardNonAdminRunner {
    param(
        [Parameter(Mandatory = $true)][string]$Identity,
        [Parameter(Mandatory = $true)][string]$Sid
    )
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $context = $null
    $user = $null
    $authorizationGroups = $null
    $groupObjects = [Collections.Generic.List[IDisposable]]::new()
    try {
        $context = [System.DirectoryServices.AccountManagement.PrincipalContext]::new(
            [System.DirectoryServices.AccountManagement.ContextType]::Machine,
            $env:COMPUTERNAME
        )
        $user = [System.DirectoryServices.AccountManagement.UserPrincipal]::FindByIdentity(
            $context,
            [System.DirectoryServices.AccountManagement.IdentityType]::Sid,
            $Sid
        )
        if ($null -eq $user -or [bool]$user.Enabled -ne $true) {
            throw "ForwardShadow target runner must be an enabled local user: $Identity"
        }
        $authorizationGroups = $user.GetAuthorizationGroups()
        $privilegedSids = @(
            "S-1-5-32-544",
            "S-1-5-32-547",
            "S-1-5-32-548",
            "S-1-5-32-549",
            "S-1-5-32-550",
            "S-1-5-32-551",
            "S-1-5-32-552"
        )
        foreach ($group in $authorizationGroups) {
            if ($group -is [IDisposable]) { [void]$groupObjects.Add($group) }
            if ($null -eq $group.Sid) {
                throw "ForwardShadow target runner has an unresolved authorization group."
            }
            if ([string]$group.Sid.Value -in $privilegedSids) {
                throw "ForwardShadow target runner is transitively privileged: $Identity"
            }
        }
    }
    catch {
        throw "ForwardShadow target runner privilege proof failed closed: $($_.Exception.Message)"
    }
    finally {
        foreach ($group in $groupObjects) { $group.Dispose() }
        if ($authorizationGroups) { $authorizationGroups.Dispose() }
        if ($user) { $user.Dispose() }
        if ($context) { $context.Dispose() }
    }
}

function Get-ForwardAccountRights {
    param([Parameter(Mandatory = $true)][string]$Sid)
    if (-not ("ForwardShadowUpgrade.NativeAccountRights" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace ForwardShadowUpgrade {
    public static class NativeAccountRights {
        [StructLayout(LayoutKind.Sequential)]
        private struct LsaObjectAttributes {
            public uint Length;
            public IntPtr RootDirectory;
            public IntPtr ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LsaUnicodeString {
            public ushort Length;
            public ushort MaximumLength;
            public IntPtr Buffer;
        }

        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern bool ConvertStringSidToSid(string stringSid, out IntPtr sid);

        [DllImport("advapi32.dll")]
        private static extern uint LsaOpenPolicy(
            IntPtr systemName,
            ref LsaObjectAttributes objectAttributes,
            uint desiredAccess,
            out IntPtr policyHandle);

        [DllImport("advapi32.dll")]
        private static extern uint LsaEnumerateAccountRights(
            IntPtr policyHandle,
            IntPtr accountSid,
            out IntPtr userRights,
            out uint countOfRights);

        [DllImport("advapi32.dll")]
        private static extern uint LsaAddAccountRights(
            IntPtr policyHandle,
            IntPtr accountSid,
            [In] LsaUnicodeString[] userRights,
            uint countOfRights);

        [DllImport("advapi32.dll")]
        private static extern uint LsaRemoveAccountRights(
            IntPtr policyHandle,
            IntPtr accountSid,
            [MarshalAs(UnmanagedType.Bool)] bool allRights,
            IntPtr userRights,
            uint countOfRights);

        [DllImport("advapi32.dll")]
        private static extern uint LsaNtStatusToWinError(uint status);

        [DllImport("advapi32.dll")]
        private static extern uint LsaFreeMemory(IntPtr buffer);

        [DllImport("advapi32.dll")]
        private static extern uint LsaClose(IntPtr handle);

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);

        public static string[] Get(string stringSid) {
            IntPtr sid = IntPtr.Zero;
            IntPtr policy = IntPtr.Zero;
            IntPtr rights = IntPtr.Zero;
            try {
                if (!ConvertStringSidToSid(stringSid, out sid)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                LsaObjectAttributes attributes = new LsaObjectAttributes();
                attributes.Length = (uint)Marshal.SizeOf(typeof(LsaObjectAttributes));
                uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, 0x00000800, out policy);
                if (status != 0) {
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                }
                uint count;
                status = LsaEnumerateAccountRights(policy, sid, out rights, out count);
                if (status == 0xC0000034) {
                    return new string[0];
                }
                if (status != 0) {
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                }
                List<string> result = new List<string>();
                int size = Marshal.SizeOf(typeof(LsaUnicodeString));
                for (uint index = 0; index < count; index++) {
                    IntPtr item = IntPtr.Add(rights, checked((int)index * size));
                    LsaUnicodeString value = (LsaUnicodeString)Marshal.PtrToStructure(
                        item,
                        typeof(LsaUnicodeString));
                    result.Add(Marshal.PtrToStringUni(value.Buffer, value.Length / 2));
                }
                return result.ToArray();
            }
            finally {
                if (rights != IntPtr.Zero) { LsaFreeMemory(rights); }
                if (policy != IntPtr.Zero) { LsaClose(policy); }
                if (sid != IntPtr.Zero) { LocalFree(sid); }
            }
        }

        public static void SetExact(string stringSid, string[] requestedRights) {
            IntPtr sid = IntPtr.Zero;
            IntPtr policy = IntPtr.Zero;
            List<IntPtr> buffers = new List<IntPtr>();
            try {
                if (!ConvertStringSidToSid(stringSid, out sid)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                LsaObjectAttributes attributes = new LsaObjectAttributes();
                attributes.Length = (uint)Marshal.SizeOf(typeof(LsaObjectAttributes));
                uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, 0x00000810, out policy);
                if (status != 0) {
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                }
                status = LsaRemoveAccountRights(policy, sid, true, IntPtr.Zero, 0);
                if (status != 0 && status != 0xC0000034) {
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                }
                if (requestedRights == null || requestedRights.Length == 0) {
                    return;
                }
                LsaUnicodeString[] rights = new LsaUnicodeString[requestedRights.Length];
                for (int index = 0; index < requestedRights.Length; index++) {
                    string value = requestedRights[index];
                    if (String.IsNullOrWhiteSpace(value) || value.Length > 0x7ffe) {
                        throw new ArgumentException("Invalid LSA account right.");
                    }
                    IntPtr buffer = Marshal.StringToHGlobalUni(value);
                    buffers.Add(buffer);
                    rights[index].Buffer = buffer;
                    rights[index].Length = checked((ushort)(value.Length * 2));
                    rights[index].MaximumLength = checked((ushort)((value.Length + 1) * 2));
                }
                status = LsaAddAccountRights(policy, sid, rights, (uint)rights.Length);
                if (status != 0) {
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                }
            }
            finally {
                foreach (IntPtr buffer in buffers) {
                    if (buffer != IntPtr.Zero) { Marshal.FreeHGlobal(buffer); }
                }
                if (policy != IntPtr.Zero) { LsaClose(policy); }
                if (sid != IntPtr.Zero) { LocalFree(sid); }
            }
        }
    }
}
'@
    }
    return @([ForwardShadowUpgrade.NativeAccountRights]::Get($Sid))
}

function Assert-ForwardRunnerLogonRights {
    param([Parameter(Mandatory = $true)][string]$RunnerSid)
    $expected = @(Get-ForwardRequiredRunnerLogonRights)
    $actual = @(Get-ForwardAccountRights -Sid $RunnerSid | Sort-Object -Unique)
    if (
        $actual.Count -ne $expected.Count -or
        [string]::Join("`n", $actual) -cne [string]::Join("`n", $expected)
    ) {
        throw "ForwardShadow target runner does not have the exact deny-interactive/network/service plus batch-logon rights."
    }
}

function Get-ForwardRequiredRunnerLogonRights {
    return @(
        "SeBatchLogonRight",
        "SeDenyInteractiveLogonRight",
        "SeDenyNetworkLogonRight",
        "SeDenyRemoteInteractiveLogonRight",
        "SeDenyServiceLogonRight"
    ) | Sort-Object
}

function Set-ForwardAccountRightsExact {
    param(
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Rights
    )
    [ForwardShadowUpgrade.NativeAccountRights]::SetExact($RunnerSid, @($Rights))
}

function Reset-ForwardRunnerPasswordInMemory {
    param([Parameter(Mandatory = $true)][string]$RunnerSid)
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $context = $null
    $user = $null
    $random = $null
    $bytes = New-Object byte[] 48
    $plainPassword = $null
    try {
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        $random.GetBytes($bytes)
        $plainPassword = [Convert]::ToBase64String($bytes) + "!aA9"
        $context = [System.DirectoryServices.AccountManagement.PrincipalContext]::new(
            [System.DirectoryServices.AccountManagement.ContextType]::Machine,
            $env:COMPUTERNAME
        )
        $user = [System.DirectoryServices.AccountManagement.UserPrincipal]::FindByIdentity(
            $context,
            [System.DirectoryServices.AccountManagement.IdentityType]::Sid,
            $RunnerSid
        )
        if ($null -eq $user -or [bool]$user.Enabled -ne $true) {
            throw "ForwardShadow target runner disappeared before password rotation."
        }
        $user.SetPassword($plainPassword)
        $securePassword = New-Object Security.SecureString
        foreach ($character in $plainPassword.ToCharArray()) {
            $securePassword.AppendChar($character)
        }
        $securePassword.MakeReadOnly()
        return $securePassword
    }
    finally {
        $plainPassword = $null
        [Array]::Clear($bytes, 0, $bytes.Length)
        if ($random) { $random.Dispose() }
        if ($user) { $user.Dispose() }
        if ($context) { $context.Dispose() }
    }
}

function Assert-ForwardCriticalLeafAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$RunnerIdentity = "",
        [switch]$Protected,
        [switch]$RequireTrustedOwner
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "ForwardShadow critical protected leaf is missing: $Path"
    }
    $runnerSid = if ([string]::IsNullOrWhiteSpace($RunnerIdentity)) {
        ""
    } else {
        Resolve-ForwardIdentitySid -Identity $RunnerIdentity
    }
    $fullControlSids = @("S-1-5-18", "S-1-5-32-544")
    $readOnlyRunnerSid = if (
        $runnerSid -and $runnerSid -notin $fullControlSids
    ) { $runnerSid } else { "" }
    if ($Protected) {
        Assert-ForwardAclSemantics -Path $Path -RunnerSid $readOnlyRunnerSid
    }
    else {
        Assert-ForwardAclSemantics `
            -Path $Path `
            -RunnerSid $readOnlyRunnerSid `
            -Inherited
    }
    if ($RequireTrustedOwner) {
        Assert-ForwardTrustedOwner -Path $Path
    }
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $stream.Dispose()
}

function Get-TrustedExecutableEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [AllowEmptyString()][string]$SignerSubjectPattern = "",
        [AllowEmptyString()][string]$ExpectedSha256 = "",
        [Parameter(Mandatory = $true)][string[]]$AllowedRoots,
        [Parameter(Mandatory = $true)][string[]]$TrustedMutationSids
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Label executable is missing: $resolved"
    }
    if ((Get-Item -LiteralPath $resolved -Force).Attributes.HasFlag(
        [IO.FileAttributes]::ReparsePoint
    )) {
        throw "$Label executable cannot be a reparse point: $resolved"
    }
    $matchedRoot = $null
    foreach ($allowedRoot in @($AllowedRoots)) {
        if ([string]::IsNullOrWhiteSpace($allowedRoot)) { continue }
        $candidateRoot = [IO.Path]::GetFullPath($allowedRoot)
        $prefix = $candidateRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) +
            [IO.Path]::DirectorySeparatorChar
        if ($resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            $matchedRoot = $candidateRoot
            break
        }
    }
    if (-not $matchedRoot) {
        throw "$Label executable is outside its trusted installation roots: $resolved"
    }

    $pathsToCheck = [Collections.Generic.List[string]]::new()
    $pathsToCheck.Add($resolved)
    $current = [IO.Path]::GetFullPath((Split-Path -Parent $resolved))
    while ($true) {
        $pathsToCheck.Add($current)
        if ($current.Equals($matchedRoot, [StringComparison]::OrdinalIgnoreCase)) { break }
        $next = [IO.Path]::GetFullPath((Split-Path -Parent $current))
        if ($next.Equals($current, [StringComparison]::OrdinalIgnoreCase)) {
            throw "$Label executable trust walk escaped its installation root."
        }
        $current = $next
    }
    foreach ($checkedPath in $pathsToCheck) {
        $item = Get-Item -LiteralPath $checkedPath -Force
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "$Label executable path contains a reparse point: $checkedPath"
        }
        $ownerSid = [string](Get-Acl -LiteralPath $checkedPath).GetOwner(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if ($ownerSid -notin $TrustedMutationSids) {
            throw "$Label executable path has an untrusted owner: $checkedPath owner=$ownerSid"
        }
        Assert-NoUntrustedDeleteChild `
            -Path $checkedPath `
            -TrustedSids $TrustedMutationSids
    }

    $hash = Get-ReleaseSha256 -Path $resolved
    if (
        -not [string]::IsNullOrWhiteSpace($ExpectedSha256) -and
        $hash -cne $ExpectedSha256.ToLowerInvariant()
    ) {
        throw "$Label executable does not match its independently audited pinned SHA-256."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    if (-not [string]::IsNullOrWhiteSpace($SignerSubjectPattern)) {
        if (
            [string]$signature.Status -cne "Valid" -or
            -not $signature.SignerCertificate -or
            [string]$signature.SignerCertificate.Subject -notmatch $SignerSubjectPattern
        ) {
            throw "$Label executable has no valid trusted Authenticode signer: $resolved"
        }
    }
    elseif ([string]::IsNullOrWhiteSpace($ExpectedSha256)) {
        throw "$Label executable requires a trusted signer or a pinned SHA-256."
    }
    $lock = [IO.File]::Open(
        $resolved,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    if ((Get-ReleaseSha256 -Path $resolved) -cne $hash) {
        $lock.Dispose()
        throw "$Label executable changed while its read lock was acquired."
    }
    return [pscustomobject]@{
        path = $resolved
        sha256 = $hash
        signature_status = [string]$signature.Status
        signer_subject = if ($signature.SignerCertificate) {
            [string]$signature.SignerCertificate.Subject
        } else { "" }
        product_version = [string](Get-Item -LiteralPath $resolved).VersionInfo.ProductVersion
        lock = $lock
    }
}

function Close-SignedReleaseLocks {
    param(
        [Parameter(Mandatory = $true)]$Locks,
        [AllowNull()][Collections.Generic.List[string]]$Errors = $null
    )
    $localErrors = New-Object Collections.Generic.List[string]
    foreach ($lock in @($Locks)) {
        if (-not $lock) {
            [void]$Locks.Remove($lock)
            continue
        }
        try {
            $lock.Dispose()
            [void]$Locks.Remove($lock)
        }
        catch { $localErrors.Add("signed release lock dispose: $($_.Exception.Message)") }
    }
    if ($null -ne $Errors) {
        foreach ($errorText in @($localErrors)) { [void]$Errors.Add($errorText) }
    }
    elseif ($localErrors.Count -gt 0) {
        throw [InvalidOperationException]::new(
            "Signed release lock cleanup failed: $($localErrors -join '; ')",
            [Exception]::new($localErrors[0])
        )
    }
}

function Get-ForwardPythonProcesses {
    $venvPrefixes = @($Venv, $VenvStaging) | ForEach-Object {
        [IO.Path]::GetFullPath($_).TrimEnd([IO.Path]::DirectorySeparatorChar) +
            [IO.Path]::DirectorySeparatorChar
    }
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                if ($_.Name -notlike "python*.exe") { return $false }
                $executable = [string]$_.ExecutablePath
                $commandLine = [string]$_.CommandLine
                $managedExecutable = $false
                if (-not [string]::IsNullOrWhiteSpace($executable)) {
                    $resolvedExecutable = [IO.Path]::GetFullPath($executable)
                    $managedExecutable = @(
                        $venvPrefixes | Where-Object {
                            $resolvedExecutable.StartsWith(
                                $_,
                                [StringComparison]::OrdinalIgnoreCase
                            )
                        }
                    ).Count -ne 0
                }
                $managedCommand = (
                    -not [string]::IsNullOrWhiteSpace($commandLine) -and
                    $commandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                    (
                        $commandLine.IndexOf(
                            "run_xm_mt5_forward.py",
                            [StringComparison]::OrdinalIgnoreCase
                        ) -ge 0 -or
                        $commandLine.IndexOf(
                            "run_capital_forward.py",
                            [StringComparison]::OrdinalIgnoreCase
                        ) -ge 0
                    )
                )
                return ($managedExecutable -or $managedCommand)
            }
    )
}

function Assert-TaskPairStopped {
    foreach ($taskName in @($MainTask, $WatchdogTask)) {
        $state = (Get-ScheduledTask -TaskName $taskName -ErrorAction Stop).State.ToString()
        if ($state -in @("Running", "Queued")) {
            throw "Scheduled task did not stop: $taskName state=$state"
        }
    }
}

function Stop-ForwardRuntime {
    Stop-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $MainTask -ErrorAction SilentlyContinue

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
    do {
        $runningTasks = @(
            @($MainTask, $WatchdogTask) | Where-Object {
                (Get-ScheduledTask -TaskName $_ -ErrorAction Stop).State.ToString() -in @("Running", "Queued")
            }
        )
        if ($runningTasks.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    Assert-TaskPairStopped

    foreach ($process in Get-ForwardPythonProcesses) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
    }
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
    do {
        $remaining = @(Get-ForwardPythonProcesses)
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "ForwardShadow Python processes remained after task shutdown: $($remaining.ProcessId -join ',')"
}

function Assert-NoForwardPythonProcesses {
    $remaining = @(Get-ForwardPythonProcesses)
    if ($remaining.Count -ne 0) {
        throw "ForwardShadow Python process is unexpectedly running: $($remaining.ProcessId -join ',')"
    }
}

function Assert-ExactTaskAction {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ExpectedExecute,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ExpectedArguments,
        [AllowEmptyString()][string]$ExpectedWorkingDirectory = ""
    )
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $actions = @($task.Actions)
    if (
        $actions.Count -ne 1 -or
        [string]$actions[0].Execute -cne $ExpectedExecute -or
        [string]$actions[0].Arguments -cne $ExpectedArguments -or
        [string]$actions[0].WorkingDirectory -cne $ExpectedWorkingDirectory
    ) {
        throw "$TaskName action does not exactly match the required binding."
    }
}

function Assert-CanonicalTaskDefinition {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments,
        [Parameter(Mandatory = $true)][string]$ExpectedUserId,
        [Parameter(Mandatory = $true)][string]$ExpectedLogonType,
        [Parameter(Mandatory = $true)][string]$ExpectedRunLevel
    )
    Assert-ExactTaskAction `
        -TaskName $TaskName `
        -ExpectedExecute $WindowsPowerShellExe `
        -ExpectedArguments $ExpectedArguments
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $triggers = @($task.Triggers)
    $triggerDelay = if (
        $triggers.Count -eq 1 -and
        -not [string]::IsNullOrWhiteSpace([string]$triggers[0].Delay)
    ) { [Xml.XmlConvert]::ToTimeSpan([string]$triggers[0].Delay) } else { [TimeSpan]::Zero }
    if (
        (Resolve-ForwardIdentitySid -Identity ([string]$task.Principal.UserId)) -cne
            (Resolve-ForwardIdentitySid -Identity $ExpectedUserId) -or
        [string]$task.Principal.LogonType -cne $ExpectedLogonType -or
        [string]$task.Principal.RunLevel -cne $ExpectedRunLevel -or
        [int]$task.Settings.RestartCount -ne 3 -or
        [Xml.XmlConvert]::ToTimeSpan([string]$task.Settings.RestartInterval) -ne
            [TimeSpan]::FromMinutes(1) -or
        [Xml.XmlConvert]::ToTimeSpan([string]$task.Settings.ExecutionTimeLimit) -ne
            [TimeSpan]::Zero -or
        [bool]$task.Settings.StartWhenAvailable -ne $true -or
        [string]$task.Settings.MultipleInstances -cne "IgnoreNew" -or
        [bool]$task.Settings.Enabled -ne $true -or
        [bool]$task.Settings.AllowDemandStart -ne $true -or
        [bool]$task.Settings.RunOnlyIfIdle -ne $false -or
        [bool]$task.Settings.DisallowStartIfOnBatteries -ne $false -or
        [bool]$task.Settings.StopIfGoingOnBatteries -ne $false -or
        [bool]$task.Settings.RunOnlyIfNetworkAvailable -ne $false -or
        $triggers.Count -ne 1 -or
        [string]$triggers[0].CimClass.CimClassName -cne "MSFT_TaskBootTrigger" -or
        [bool]$triggers[0].Enabled -ne $true -or
        $triggerDelay -ne [TimeSpan]::Zero
    ) {
        throw "$TaskName principal or execution settings are not the canonical safe definition."
    }
}

function Get-ForwardRunnerTerminalProcesses {
    param(
        [Parameter(Mandatory = $true)][string]$TerminalPath,
        [AllowEmptyString()][string]$RunnerSid = ""
    )
    $resolvedTerminal = [IO.Path]::GetFullPath($TerminalPath)
    $matches = [Collections.Generic.List[object]]::new()
    foreach ($process in Get-CimInstance Win32_Process -ErrorAction Stop) {
        if ([string]::IsNullOrWhiteSpace([string]$process.ExecutablePath)) {
            continue
        }
        $processPath = [IO.Path]::GetFullPath([string]$process.ExecutablePath)
        $isCanonicalTerminal = $processPath.Equals(
            $resolvedTerminal,
            [StringComparison]::OrdinalIgnoreCase
        )
        $isRunnerTerminalCopy = (
            -not [string]::IsNullOrWhiteSpace($RunnerSid) -and
            [string]$process.Name -ieq "terminal64.exe"
        )
        if (-not $isCanonicalTerminal -and -not $isRunnerTerminalCopy) { continue }
        $owner = Invoke-CimMethod `
            -InputObject $process `
            -MethodName GetOwnerSid `
            -ErrorAction Stop
        if ([uint32]$owner.ReturnValue -ne 0 -or [string]::IsNullOrWhiteSpace([string]$owner.Sid)) {
            throw "Could not prove the owner of a ForwardShadow MT5 process."
        }
        if (
            [string]::IsNullOrWhiteSpace($RunnerSid) -or
            [string]$owner.Sid -ceq $RunnerSid
        ) {
            [void]$matches.Add($process)
        }
    }
    return $matches.ToArray()
}

function New-ForwardTaskService {
    $schedulerType = [Type]::GetTypeFromCLSID(
        [Guid]::Parse("0f87369f-a4e5-4cfc-bd3e-73e6154572dd"),
        $true
    )
    $service = [Activator]::CreateInstance($schedulerType)
    [void]$service.Connect()
    return $service
}

function Register-ForwardS4UTaskDefinition {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)]$Definition,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity,
        [Parameter(Mandatory = $true)][Security.SecureString]$RunnerPassword
    )
    if ($RunnerPassword.Length -eq 0) {
        throw "ForwardShadow target runner password is empty."
    }
    $passwordPointer = [IntPtr]::Zero
    $plainPassword = $null
    try {
        $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $RunnerPassword
        )
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
            $passwordPointer
        )
        return $Folder.RegisterTaskDefinition(
            $TaskName,
            $Definition,
            6,
            $RunnerIdentity,
            $plainPassword,
            2,
            $null
        )
    }
    finally {
        $plainPassword = $null
        if ($passwordPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
    }
}

function Set-ForwardMainTaskWithS4UCredential {
    param(
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity,
        [Parameter(Mandatory = $true)][Security.SecureString]$RunnerPassword
    )
    $service = New-ForwardTaskService
    $folder = $service.GetFolder("\")
    $registered = $folder.GetTask("\$MainTask")
    $definition = $registered.Definition
    $definition.Actions.Clear()
    $action = $definition.Actions.Create(0)
    $action.Path = $WindowsPowerShellExe
    $action.Arguments = $Arguments
    $action.WorkingDirectory = ""
    $definition.Principal.UserId = $RunnerIdentity
    $definition.Principal.LogonType = 2
    $definition.Principal.RunLevel = 0
    $definition.Triggers.Clear()
    $bootTrigger = $definition.Triggers.Create(8)
    $bootTrigger.Enabled = $true
    $bootTrigger.Delay = "PT0S"
    $definition.Settings.RestartCount = 3
    $definition.Settings.RestartInterval = "PT1M"
    $definition.Settings.ExecutionTimeLimit = "PT0S"
    $definition.Settings.StartWhenAvailable = $true
    $definition.Settings.MultipleInstances = 2
    $definition.Settings.Enabled = $true
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.RunOnlyIfIdle = $false
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.RunOnlyIfNetworkAvailable = $false
    [void](Register-ForwardS4UTaskDefinition `
        -Folder $folder `
        -TaskName $MainTask `
        -Definition $definition `
        -RunnerIdentity $RunnerIdentity `
        -RunnerPassword $RunnerPassword)
}

function Invoke-ForwardRunnerBrokerProof {
    param(
        [Parameter(Mandatory = $true)][string]$Launcher,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$TerminalPath,
        [Parameter(Mandatory = $true)][string]$TerminalConfigPath,
        [Parameter(Mandatory = $true)][string]$TerminalConfigSha256,
        [Parameter(Mandatory = $true)][ValidateSet("ReadOnly", "Production")][string]$TerminalMode,
        [Parameter(Mandatory = $true)][Security.SecureString]$RunnerPassword,
        [Parameter(Mandatory = $true)][int]$ExpectedLogin
    )
    $probeTask = "ForwardShadowRunnerProof.$RunId"
    $probeDiagnostic = [IO.Path]::GetFullPath(
        (Join-Path $CanonicalState "runner-broker-proof-$RunId-$TerminalMode.json")
    )
    if (Test-Path -LiteralPath $probeDiagnostic) {
        throw "ForwardShadow runner proof diagnostic unexpectedly exists."
    }
    if (Get-ScheduledTask -TaskName $probeTask -ErrorAction SilentlyContinue) {
        throw "ForwardShadow runner proof task unexpectedly exists."
    }
    $probeArguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$Launcher`""
        "-Root `"$Root`""
        "-BrokerProbeOnly"
        "-ExpectedLogin $ExpectedLogin"
        "-BrokerProbeTerminalConfig `"$TerminalConfigPath`""
        "-ExpectedBrokerProbeConfigSha256 $TerminalConfigSha256"
        "-BrokerProbeMode $TerminalMode"
        "-BrokerProbeDiagnosticPath `"$probeDiagnostic`""
    ) -join " "
    $registered = $false
    $service = $null
    $folder = $null
    $registeredTask = $null
    $primaryError = $null
    $cleanupError = $null
    $evidence = $null
    $observedOwnedTerminal = $false
    try {
        if (@(Get-ForwardRunnerTerminalProcesses `
            -TerminalPath $TerminalPath `
            -RunnerSid $RunnerSid).Count -ne 0) {
            throw "ForwardShadow broker proof requires every matching MT5 terminal process to be stopped."
        }
        $service = New-ForwardTaskService
        $folder = $service.GetFolder("\")
        $definition = $service.NewTask(0)
        $definition.RegistrationInfo.Description = "ForwardShadow one-time ACL and broker proof"
        $definition.Principal.UserId = $RunnerIdentity
        $definition.Principal.LogonType = 2
        $definition.Principal.RunLevel = 0
        $definition.Settings.Enabled = $true
        $definition.Settings.AllowDemandStart = $true
        $definition.Settings.ExecutionTimeLimit = "PT2M"
        $definition.Settings.MultipleInstances = 2
        $action = $definition.Actions.Create(0)
        $action.Path = $WindowsPowerShellExe
        $action.Arguments = $probeArguments
        $action.WorkingDirectory = ""
        $registeredTask = Register-ForwardS4UTaskDefinition `
            -Folder $folder `
            -TaskName $probeTask `
            -Definition $definition `
            -RunnerIdentity $RunnerIdentity `
            -RunnerPassword $RunnerPassword
        $registered = $true
        Assert-ExactTaskAction `
            -TaskName $probeTask `
            -ExpectedExecute $WindowsPowerShellExe `
            -ExpectedArguments $probeArguments
        $task = Get-ScheduledTask -TaskName $probeTask -ErrorAction Stop
        if (
            (Resolve-ForwardIdentitySid -Identity ([string]$task.Principal.UserId)) -cne
                $RunnerSid -or
            [string]$task.Principal.LogonType -cne "S4U" -or
            [string]$task.Principal.RunLevel -cne "Limited"
        ) {
            throw "ForwardShadow runner proof task principal was not registered exactly."
        }
        $startedAt = [DateTimeOffset]::UtcNow
        [void]$registeredTask.Run($null)
        $deadline = $startedAt.AddMinutes(2)
        do {
            Start-Sleep -Milliseconds 250
            $task = Get-ScheduledTask -TaskName $probeTask -ErrorAction Stop
            $info = Get-ScheduledTaskInfo -TaskName $probeTask -ErrorAction Stop
            if (@(Get-ForwardRunnerTerminalProcesses `
                -TerminalPath $TerminalPath `
                -RunnerSid $RunnerSid).Count -ne 0) {
                $observedOwnedTerminal = $true
            }
            $completed = (
                [string]$task.State -notin @("Running", "Queued") -and
                $info.LastRunTime.Year -gt 2000
            )
            if ($completed) { break }
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        if (-not $completed) {
            $safeDiagnostic = if (Test-Path -LiteralPath $probeDiagnostic -PathType Leaf) {
                [IO.File]::ReadAllText($probeDiagnostic)
            } else { "missing" }
            throw "ForwardShadow runner broker proof task timed out: diagnostic=$safeDiagnostic"
        }
        if ([int64]$info.LastTaskResult -ne 0) {
            $safeDiagnostic = if (Test-Path -LiteralPath $probeDiagnostic -PathType Leaf) {
                [IO.File]::ReadAllText($probeDiagnostic)
            } else { "missing" }
            throw "ForwardShadow runner broker/ACL proof failed: result=$($info.LastTaskResult) diagnostic=$safeDiagnostic"
        }
        if (-not $observedOwnedTerminal) {
            throw "ForwardShadow broker proof did not observe an MT5 process owned by the target runner."
        }
        $evidence = [pscustomobject]@{
            task = $probeTask
            runner = $RunnerIdentity
            logon_type = "S4U"
            run_level = "Limited"
            last_task_result = [int64]$info.LastTaskResult
            last_run_time = $info.LastRunTime.ToUniversalTime().ToString("o")
            expected_login = $ExpectedLogin
            terminal_mode = $TerminalMode
            owned_terminal_observed = $observedOwnedTerminal
        }
    }
    catch { $primaryError = $_ }
    finally {
        if ($registered) {
            try {
                try { $registeredTask.Stop(0) } catch {}
                $folder.DeleteTask($probeTask, 0)
                if (Get-ScheduledTask -TaskName $probeTask -ErrorAction SilentlyContinue) {
                    throw "ForwardShadow runner proof task cleanup could not be verified."
                }
            }
            catch { $cleanupError = $_ }
        }
        try {
            $terminalCleanupDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
            do {
                $remainingTerminalProcesses = @(Get-ForwardRunnerTerminalProcesses `
                    -TerminalPath $TerminalPath `
                    -RunnerSid $RunnerSid)
                if ($remainingTerminalProcesses.Count -eq 0) { break }
                foreach ($process in $remainingTerminalProcesses) {
                    Stop-Process `
                        -Id ([int]$process.ProcessId) `
                        -Force `
                        -ErrorAction SilentlyContinue
                }
                Start-Sleep -Milliseconds 250
            } while ([DateTimeOffset]::UtcNow -lt $terminalCleanupDeadline)
            if (@(Get-ForwardRunnerTerminalProcesses `
                    -TerminalPath $TerminalPath `
                    -RunnerSid $RunnerSid).Count -ne 0) {
                throw "ForwardShadow runner proof left an MT5 terminal process behind."
            }
        }
        catch {
            if ($cleanupError) {
                $cleanupError = [InvalidOperationException]::new(
                    "$($cleanupError.Exception.Message); terminal cleanup: $($_.Exception.Message)"
                )
            }
            else { $cleanupError = $_ }
        }
        if (Test-Path -LiteralPath $probeDiagnostic -PathType Leaf) {
            try { Remove-Item -LiteralPath $probeDiagnostic -Force }
            catch {
                if ($cleanupError) {
                    $cleanupError = [InvalidOperationException]::new(
                        "$($cleanupError.Exception.Message); diagnostic cleanup: $($_.Exception.Message)"
                    )
                }
                else { $cleanupError = $_ }
            }
        }
    }
    if ($cleanupError) {
        throw "ForwardShadow runner proof cleanup failed: $($cleanupError.Exception.Message)"
    }
    if ($primaryError) { throw $primaryError }
    return $evidence
}

function Assert-TaskXmlRestored {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ExpectedXml
    )
    $actualXml = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($actualXml.Trim() -cne $ExpectedXml.Trim()) {
        throw "$TaskName XML was not restored exactly during rollback."
    }
}

function Assert-ReleasePythonRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimePython,
        [Parameter(Mandatory = $true)]$Manifest
    )
    $requiredNames = @("pandas", "MetaTrader5", "setuptools", "wheel")
    $manifestDependencies = @($Manifest.dependencies.PSObject.Properties)
    if ($manifestDependencies.Count -ne $requiredNames.Count) {
        throw "Signed release dependency manifest is not the exact Windows runtime tuple."
    }
    $probe = @'
import importlib
import importlib.metadata
import json
import sys

names = ("pandas", "MetaTrader5", "setuptools", "wheel")
for name in names:
    importlib.import_module(name)
print(json.dumps({
    "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    "dependencies": {name: importlib.metadata.version(name) for name in names},
}, sort_keys=True))
'@
    $probeOutput = @($probe | & $RuntimePython -I -E -B - 2>&1)
    if ($LASTEXITCODE -ne 0 -or $probeOutput.Count -eq 0) {
        throw "Release Python runtime dependency probe failed: $($probeOutput -join ' ')"
    }
    $actual = $probeOutput[-1] | ConvertFrom-Json
    if ([string]$actual.python -cne [string]$Manifest.python) {
        throw "Release Python runtime version mismatch."
    }
    foreach ($name in $requiredNames) {
        $expectedProperty = $Manifest.dependencies.PSObject.Properties[$name]
        $actualProperty = $actual.dependencies.PSObject.Properties[$name]
        if (
            $null -eq $expectedProperty -or
            $null -eq $actualProperty -or
            [string]$actualProperty.Value -cne [string]$expectedProperty.Value
        ) {
            throw "Release Python dependency mismatch: $name"
        }
    }
    return $actual
}

function Get-RelativeFileHashes {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$RelativeDirectory
    )
    $resolvedBase = [IO.Path]::GetFullPath($Base)
    $directory = [IO.Path]::GetFullPath((Join-Path $resolvedBase $RelativeDirectory))
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "Release directory is missing: $directory"
    }
    $hashes = [ordered]@{}
    foreach ($file in Get-ChildItem -LiteralPath $directory -Recurse -File | Sort-Object FullName) {
        $relative = $file.FullName.Substring($resolvedBase.Length).TrimStart('\')
        $hashes[$relative] = Get-ReleaseSha256 -Path $file.FullName
    }
    return $hashes
}

function Get-TreeFingerprint {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{
            exists = $false
            file_count = 0
            total_bytes = 0
            tree_sha256 = $null
        }
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "State fingerprint target is not a directory: $Path"
    }
    $resolvedRoot = [IO.Path]::GetFullPath($Path)
    $reparsePoints = @(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force |
            Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
    )
    if ($reparsePoints.Count -ne 0) {
        throw "State tree contains unsupported reparse points: $($reparsePoints.FullName -join ',')"
    }
    $records = [Collections.Generic.List[string]]::new()
    [long]$totalBytes = 0
    $files = @(Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force -File | Sort-Object FullName)
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($resolvedRoot.Length).TrimStart('\')
        $hash = Get-ReleaseSha256 -Path $file.FullName
        $records.Add("$relative`0$($file.Length)`0$hash")
        $totalBytes += [long]$file.Length
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $payload = [Text.Encoding]::UTF8.GetBytes(($records -join "`n"))
        $treeHash = ([BitConverter]::ToString($sha.ComputeHash($payload))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    return [pscustomobject]@{
        exists = $true
        file_count = $files.Count
        total_bytes = $totalBytes
        tree_sha256 = $treeHash
    }
}

function Assert-TreeFingerprint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $actual = Get-TreeFingerprint -Path $Path
    if (
        [bool]$actual.exists -ne [bool]$Expected.exists -or
        [int]$actual.file_count -ne [int]$Expected.file_count -or
        [long]$actual.total_bytes -ne [long]$Expected.total_bytes -or
        [string]$actual.tree_sha256 -ne [string]$Expected.tree_sha256
    ) {
        throw "$Label changed during signed app upgrade."
    }
}

function Get-ForwardAclSnapshot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolvedRoot = [IO.Path]::GetFullPath($Path)
    Assert-RootChildPath -Path $resolvedRoot -Label "ACL snapshot root"
    if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
        throw "ACL snapshot root is not a directory: $resolvedRoot"
    }
    $rootPrefix = $resolvedRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    $items = @((Get-Item -LiteralPath $resolvedRoot -Force)) + @(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force
    )
    $snapshot = [Collections.Generic.List[object]]::new()
    foreach ($item in @($items | Sort-Object FullName)) {
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "ACL snapshot tree contains a reparse point: $($item.FullName)"
        }
        $relative = if ($item.FullName.Equals(
            $resolvedRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            "."
        } else {
            $item.FullName.Substring($rootPrefix.Length).Replace("\", "/")
        }
        $itemAcl = Get-Acl -LiteralPath $item.FullName
        $snapshot.Add([pscustomobject]@{
            relative_path = $relative
            kind = if ($item -is [IO.DirectoryInfo]) { "directory" } else { "file" }
            owner_sid = [string]$itemAcl.GetOwner(
                [Security.Principal.SecurityIdentifier]
            ).Value
            group_sid = [string]$itemAcl.GetGroup(
                [Security.Principal.SecurityIdentifier]
            ).Value
            access_sddl = [string]$itemAcl.GetSecurityDescriptorSddlForm(
                [Security.AccessControl.AccessControlSections]::Access
            )
        })
    }
    return $snapshot.ToArray()
}

function Get-ForwardAclSnapshotPath {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    $resolvedRoot = [IO.Path]::GetFullPath($RootPath)
    if ($RelativePath -eq ".") { return $resolvedRoot }
    $target = [IO.Path]::GetFullPath(
        (Join-Path $resolvedRoot $RelativePath.Replace("/", "\"))
    )
    $prefix = $resolvedRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "ACL snapshot path escapes its root: $RelativePath"
    }
    return $target
}

function Assert-ForwardAclSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Snapshot
    )
    $actual = @(Get-ForwardAclSnapshot -Path $Path)
    $expected = @($Snapshot)
    if ($actual.Count -ne $expected.Count) {
        throw "ForwardShadow ACL snapshot path count changed: $Path"
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if (
            [string]$actual[$index].relative_path -cne [string]$expected[$index].relative_path -or
            [string]$actual[$index].kind -cne [string]$expected[$index].kind -or
            [string]$actual[$index].owner_sid -cne [string]$expected[$index].owner_sid -or
            [string]$actual[$index].group_sid -cne [string]$expected[$index].group_sid -or
            [string]$actual[$index].access_sddl -cne [string]$expected[$index].access_sddl
        ) {
            throw "ForwardShadow ACL snapshot restore mismatch: $($expected[$index].relative_path)"
        }
    }
}

function Restore-ForwardAclSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Snapshot
    )
    $metadataRestoreRequired = $false
    foreach ($entry in @($Snapshot)) {
        $target = Get-ForwardAclSnapshotPath `
            -RootPath $Path `
            -RelativePath ([string]$entry.relative_path)
        $currentAcl = Get-Acl -LiteralPath $target
        if (
            [string]$currentAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne
                [string]$entry.owner_sid -or
            [string]$currentAcl.GetGroup([Security.Principal.SecurityIdentifier]).Value -cne
                [string]$entry.group_sid
        ) {
            $metadataRestoreRequired = $true
            break
        }
    }
    $restorePrivilege = if ($metadataRestoreRequired) {
        New-ForwardRestorePrivilegeScope
    } else { $null }
    try {
        foreach ($entry in @($Snapshot | Sort-Object {
            ([string]$_.relative_path).Length
        } -Descending)) {
        $target = Get-ForwardAclSnapshotPath `
            -RootPath $Path `
            -RelativePath ([string]$entry.relative_path)
        $expectedContainer = [string]$entry.kind -eq "directory"
        if (
            -not (Test-Path -LiteralPath $target) -or
            $expectedContainer -ne (Test-Path -LiteralPath $target -PathType Container)
        ) {
            throw "ACL snapshot restore target changed: $target"
        }
        $accessAcl = if ($expectedContainer) {
            New-Object Security.AccessControl.DirectorySecurity
        } else {
            New-Object Security.AccessControl.FileSecurity
        }
        $accessAcl.SetSecurityDescriptorSddlForm(
            [string]$entry.access_sddl,
            [Security.AccessControl.AccessControlSections]::Access
        )
        $currentAcl = Get-Acl -LiteralPath $target
        if (
            [string]$currentAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne
                [string]$entry.owner_sid
        ) {
            $accessAcl.SetOwner(
                [Security.Principal.SecurityIdentifier]::new([string]$entry.owner_sid)
            )
        }
        if (
            [string]$currentAcl.GetGroup([Security.Principal.SecurityIdentifier]).Value -cne
                [string]$entry.group_sid
        ) {
            $accessAcl.SetGroup(
                [Security.Principal.SecurityIdentifier]::new([string]$entry.group_sid)
            )
        }
            if ($expectedContainer) {
                [IO.Directory]::SetAccessControl($target, $accessAcl)
            } else {
                [IO.File]::SetAccessControl($target, $accessAcl)
            }
        }
    }
    finally {
        if ($restorePrivilege) { $restorePrivilege.Dispose() }
    }
    Assert-ForwardAclSnapshot -Path $Path -Snapshot $Snapshot
}

function Assert-ForwardPrivateAclForSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Snapshot
    )
    foreach ($entry in @($Snapshot)) {
        $target = Get-ForwardAclSnapshotPath `
            -RootPath $Path `
            -RelativePath ([string]$entry.relative_path)
        Assert-ForwardTrustedOwner -Path $target
        if ([string]$entry.relative_path -eq ".") {
            Assert-ForwardAclSemantics `
                -Path $target `
                -DirectoryRoot
        }
        else {
            Assert-ForwardAclSemantics `
                -Path $target `
                -Inherited
        }
    }
}

function Assert-RelativeFileHashes {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)]$Expected
    )
    foreach ($entry in $Expected.GetEnumerator()) {
        $target = Join-Path $Base ([string]$entry.Key)
        if (
            -not (Test-Path -LiteralPath $target -PathType Leaf) -or
            (Get-ReleaseSha256 -Path $target) -ne [string]$entry.Value
        ) {
            throw "Activated release hash mismatch: $($entry.Key)"
        }
    }
}

function Remove-GeneratedTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedLeafPattern
    )
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-RootChildPath -Path $Path -Label "Generated cleanup path"
    if ([IO.Path]::GetFileName($Path) -notmatch $ExpectedLeafPattern) {
        throw "Refusing unsafe generated-directory cleanup: $Path"
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Remove-StagedCompiledArtifacts {
    param([Parameter(Mandatory = $true)][string]$Stage)
    $stagePrefix = [IO.Path]::GetFullPath($Stage).TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    foreach ($cache in @(Get-ChildItem -LiteralPath $Stage -Recurse -Directory -Filter "__pycache__")) {
        $resolved = [IO.Path]::GetFullPath($cache.FullName)
        if (-not $resolved.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe staged cache path: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    foreach ($compiled in @(
        Get-ChildItem -LiteralPath $Stage -Recurse -File |
            Where-Object { $_.Extension -in @(".pyc", ".pyo") }
    )) {
        $resolved = [IO.Path]::GetFullPath($compiled.FullName)
        if (-not $resolved.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe staged compiled path: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Force
    }
}

function Copy-FileCreateNew {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $sourceStream = [IO.File]::Open(
        $Source,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $destinationStream = $null
    try {
        $destinationStream = [IO.File]::Open(
            $Destination,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $sourceStream.CopyTo($destinationStream)
        $destinationStream.Flush($true)
    }
    finally {
        if ($destinationStream) { $destinationStream.Dispose() }
        $sourceStream.Dispose()
    }
    Protect-ForwardPrivateFile -Path $Destination
    return [IO.File]::Open(
        $Destination,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
}

function New-ForwardLockedSettingCopy {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    Protect-ForwardPrivateFile -Path $Source -RunnerSid $RunnerSid
    $sourceHash = Get-ReleaseSha256 -Path $Source
    $destination = Join-Path $TransactionArchive (
        "setting-$Name-$RunId.txt"
    )
    if (Test-Path -LiteralPath $destination) {
        throw "Locked ForwardShadow setting copy already exists: $destination"
    }
    $lock = Copy-FileCreateNew -Source $Source -Destination $destination
    try {
        if (
            (Get-ReleaseSha256 -Path $Source) -cne $sourceHash -or
            (Get-ReleaseSha256 -Path $destination) -cne $sourceHash
        ) {
            throw "ForwardShadow $Name setting changed during private copy."
        }
        $value = (Get-Content -LiteralPath $destination -Raw).Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "ForwardShadow $Name setting is empty."
        }
        return [pscustomobject]@{
            path = $destination
            sha256 = $sourceHash
            value = $value
            lock = $lock
        }
    }
    catch {
        $lock.Dispose()
        throw
    }
}

function New-CandidateIfMissing {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    Assert-RootChildPath -Path $Destination -Label "Candidate path"
    if (Test-Path -LiteralPath $Destination) {
        throw "Candidate already exists; refusing to mix it with the signed release: $Destination"
    }
    $temporary = Join-Path $TransactionArchive (
        "candidate-$([IO.Path]::GetFileName($Destination)).$RunId.tmp"
    )
    Assert-RootChildPath -Path $temporary -Label "Candidate temporary path"
    if (Test-Path -LiteralPath $temporary) {
        throw "Candidate temporary path already exists: $temporary"
    }
    $CandidateTempPaths.Add($temporary)
    $sourceHash = Get-ReleaseSha256 -Path $Source
    $candidateLock = $null
    try {
        $candidateLock = Copy-FileCreateNew -Source $Source -Destination $temporary
        if (
            (Get-ReleaseSha256 -Path $temporary) -ne $sourceHash -or
            (Get-ReleaseSha256 -Path $Source) -ne $sourceHash
        ) {
            throw "Candidate private copy hash mismatch: $Destination"
        }
    }
    finally {
        if ($candidateLock) { $candidateLock.Dispose() }
    }
    [IO.File]::Move($temporary, $Destination)
    [void]$CandidateTempPaths.Remove($temporary)
    $CreatedCandidates.Add($Destination)
    Protect-ForwardPrivateFile `
        -Path $Destination `
        -RunnerSid $RunnerSid
    if ((Get-ReleaseSha256 -Path $Destination) -ne $sourceHash) {
        throw "Candidate final hash mismatch: $Destination"
    }
    return "CREATED"
}

$CandidateResults = [ordered]@{}

try {
    foreach ($name in $PreviousPythonEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    $CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$CurrentIdentity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw "Run this script from an elevated PowerShell."
    }
    foreach ($path in @(
        $App, $ArchiveRoot, $CanonicalState, $LegacyState, $Venv, $Python,
        $ServerFile, $TerminalFile, $PasswordFile, $ProductionTerminalConfig,
        $Staging, $VenvStaging, $StagedPython,
        $ValidationRoot, $LegacyStateHold, $WatchdogHealth, $WatchdogStatus,
        $CapitalCandidate, $XmCandidate, $LauncherCandidate, $RunnerTokenSentinel,
        $RunnerProbeTerminalConfig
    )) {
        Assert-RootChildPath -Path $path -Label "ForwardShadow upgrade path"
    }
    $appPrefix = $App.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $statePrefix = $CanonicalState.TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    if (
        $ArchivePath.Equals($App, [StringComparison]::OrdinalIgnoreCase) -or
        $ArchivePath.StartsWith($appPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $ArchivePath.Equals($CanonicalState, [StringComparison]::OrdinalIgnoreCase) -or
        $ArchivePath.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Signed release archive must not be stored inside the app or state directory."
    }
    if (-not (Test-Path -LiteralPath $IntegrityScript -PathType Leaf)) {
        throw "Missing release integrity verifier: $IntegrityScript"
    }
    if ($Root.Contains('"')) {
        throw "ForwardShadow root cannot contain a quote."
    }
    if (Test-Path -LiteralPath $PasswordFile) {
        throw "ForwardShadow cannot migrate a user-scoped DPAPI broker password to a different S4U runner; remove it only after provisioning the runner profile through the approved no-send authentication flow."
    }

    $mainTaskDefinition = Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $watchdogTaskDefinition = Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
    if (
        @($mainTaskDefinition.Actions).Count -ne 1 -or
        @($watchdogTaskDefinition.Actions).Count -ne 1
    ) {
        throw "ForwardShadow tasks must each have exactly one action before upgrade."
    }
    $OriginalRunnerIdentity = [string]$mainTaskDefinition.Principal.UserId
    if ([string]::IsNullOrWhiteSpace($OriginalRunnerIdentity)) {
        throw "ForwardShadow original task runner identity is empty."
    }
    $OriginalRunnerSid = Resolve-ForwardIdentitySid -Identity $OriginalRunnerIdentity
    $RunnerSid = Resolve-ForwardIdentitySid -Identity $TargetRunnerIdentity
    $RunnerIdentity = ([Security.Principal.SecurityIdentifier]::new($RunnerSid)).Translate(
        [Security.Principal.NTAccount]
    ).Value
    Assert-ForwardNonAdminRunner -Identity $RunnerIdentity -Sid $RunnerSid
    $RunnerOriginalAccountRights = @(Get-ForwardAccountRights -Sid $RunnerSid)
    $OriginalMainTaskXml = Export-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $OriginalWatchdogTaskXml = Export-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
    $OriginalMainActions = @($mainTaskDefinition.Actions)
    $OriginalMainTriggers = @($mainTaskDefinition.Triggers)
    $OriginalMainSettings = $mainTaskDefinition.Settings
    $OriginalMainPrincipal = $mainTaskDefinition.Principal
    $OriginalWatchdogActions = @($watchdogTaskDefinition.Actions)
    $OriginalWatchdogTriggers = @($watchdogTaskDefinition.Triggers)
    $OriginalWatchdogSettings = $watchdogTaskDefinition.Settings
    $OriginalWatchdogPrincipal = $watchdogTaskDefinition.Principal

    $canonicalLock = Test-Path -LiteralPath (Join-Path $CanonicalState "campaign_lock.json") -PathType Leaf
    $legacyLock = Test-Path -LiteralPath (Join-Path $LegacyState "campaign_lock.json") -PathType Leaf
    if ($canonicalLock -and $legacyLock) {
        throw "ForwardShadow has duplicate canonical and legacy campaign state."
    }
    if (-not $canonicalLock -and -not $legacyLock) {
        throw "ForwardShadow current campaign state was not found."
    }
    if (Test-Path -LiteralPath $CanonicalState -PathType Container) {
        $CanonicalStateAclSnapshot = @(Get-ForwardAclSnapshot -Path $CanonicalState)
    }
    if ($legacyLock) {
        $StateMode = "LEGACY"
        $LegacyStateAclSnapshot = @(Get-ForwardAclSnapshot -Path $LegacyState)
    }
    else {
        $StateMode = "CANONICAL"
    }

    $TrustedInfrastructureSids = @("S-1-5-18", "S-1-5-32-544")
    if (-not [IO.Path]::GetFullPath($IntegrityScript).StartsWith(
            ($ExpectedReleaseDirectory + [IO.Path]::DirectorySeparatorChar),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        (Get-Item -LiteralPath $IntegrityScript -Force).Attributes.HasFlag(
            [IO.FileAttributes]::ReparsePoint
        )) {
        throw "ForwardShadow release integrity verifier is outside the protected pinned release directory."
    }
    Assert-ForwardAclSemantics -Path $IntegrityScript
    if (-not [IO.File]::GetAttributes($IntegrityScript).HasFlag(
        [IO.FileAttributes]::ReadOnly
    )) {
        throw "ForwardShadow release integrity verifier is not read-only."
    }
    $integrityHash = (Get-FileHash -LiteralPath $IntegrityScript -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($integrityHash -cne $ExpectedIntegrityScriptSha256) {
        throw "ForwardShadow release integrity verifier hash mismatch."
    }
    $integrityLock = [IO.File]::Open(
        $IntegrityScript,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    [void]$SignedReleaseLocks.Add($integrityLock)
    if (
        (Get-FileHash -LiteralPath $IntegrityScript -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            $ExpectedIntegrityScriptSha256
    ) {
        throw "ForwardShadow release integrity verifier changed while acquiring its lock."
    }
    . $IntegrityScript

    $RootAclSnapshot = Get-ForwardRootAclSnapshot
    foreach ($required in @($ArchivePath, $App, $Venv, $Python, $ServerFile, $TerminalFile)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Missing required upgrade path: $required"
        }
    }
    if (-not (Test-Path -LiteralPath $App -PathType Container)) {
        throw "ForwardShadow app path is not a directory: $App"
    }
    if (-not (Test-Path -LiteralPath $Venv -PathType Container)) {
        throw "ForwardShadow Python environment path is not a directory: $Venv"
    }
    if (
        (Test-Path -LiteralPath $Staging) -or
        (Test-Path -LiteralPath $VenvStaging) -or
        (Test-Path -LiteralPath $ValidationRoot)
    ) {
        throw "Unique ForwardShadow validation path already exists."
    }
    if (Test-Path -LiteralPath $LegacyStateHold) {
        throw "Unique ForwardShadow state hold path already exists."
    }
    foreach ($candidate in @($CapitalCandidate, $XmCandidate, $LauncherCandidate)) {
        if (Test-Path -LiteralPath $candidate) {
            throw "ForwardShadow candidate preflight failed; remove or archive the stale candidate first: $candidate"
        }
    }

    $CanonicalStateFingerprint = Get-TreeFingerprint -Path $CanonicalState
    if ($legacyLock) {
        if (
            [bool]$CanonicalStateFingerprint.exists -and
            [int]$CanonicalStateFingerprint.file_count -ne 0
        ) {
            throw "Canonical ForwardShadow placeholder is non-empty beside legacy campaign state."
        }
        $LegacyStateFingerprint = Get-TreeFingerprint -Path $LegacyState
    }

    $ManifestPath = [IO.Path]::ChangeExtension($ArchivePath, ".manifest.json")
    $SignaturePath = [IO.Path]::ChangeExtension($ArchivePath, ".manifest.sig")
    foreach ($component in @($ArchivePath, $ManifestPath, $SignaturePath)) {
        $componentLock = [IO.File]::Open(
            $component,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        [void]$SignedReleaseLocks.Add($componentLock)
    }
    $ExternalReleaseManifest = Assert-SignedReleaseArchive `
        -Archive $ArchivePath `
        -ExpectedProfile "forward-shadow" `
        -RequireProvenance
    if ([string]$ExternalReleaseManifest.archive_file -ne [IO.Path]::GetFileName($ArchivePath)) {
        throw "Signed manifest archive name does not match the requested archive path."
    }
    Assert-NoUntrustedDeleteChild -Path $Root -TrustedSids $TrustedInfrastructureSids
    if (Test-Path -LiteralPath $ArchiveRoot) {
        if (-not (Test-Path -LiteralPath $ArchiveRoot -PathType Container)) {
            throw "ForwardShadow archive root is not a directory: $ArchiveRoot"
        }
        Assert-NoUntrustedDeleteChild `
            -Path $ArchiveRoot `
            -TrustedSids $TrustedInfrastructureSids
    }

    $runtimeControlEntered = $true
    Stop-ForwardRuntime
    Assert-TaskPairStopped
    Assert-NoForwardPythonProcesses

    $RunnerRightsHardened = $true
    try {
        Set-ForwardAccountRightsExact `
            -RunnerSid $RunnerSid `
            -Rights @(Get-ForwardRequiredRunnerLogonRights)
        Assert-ForwardRunnerLogonRights -RunnerSid $RunnerSid
    }
    catch {
        $rightsError = $_
        try {
            Set-ForwardAccountRightsExact `
                -RunnerSid $RunnerSid `
                -Rights @($RunnerOriginalAccountRights)
            $RunnerRightsHardened = $false
        }
        catch {
            throw "ForwardShadow runner-right hardening failed and exact recovery also failed: $($rightsError.Exception.Message); $($_.Exception.Message)"
        }
        throw $rightsError
    }

    $RootAclHardened = $true
    Protect-ForwardRoot -RunnerSid $RunnerSid
    $BrokerSettingsHardened = $true
    Protect-ForwardPrivateFile -Path $ServerFile -RunnerSid $RunnerSid
    Protect-ForwardPrivateFile -Path $TerminalFile -RunnerSid $RunnerSid

    if (Test-Path -LiteralPath $ArchiveRoot) {
        Protect-ForwardTree `
            -Path $ArchiveRoot `
            -AllowEmpty `
            -SealOwner
    }
    else {
        New-ForwardPrivateDirectory -Path $ArchiveRoot
    }
    Assert-NoUntrustedDeleteChild `
        -Path $ArchiveRoot `
        -TrustedSids $TrustedInfrastructureSids
    $ArchiveBoundaryHardened = $true
    foreach ($unused in @($Staging, $VenvStaging, $ValidationRoot, $LegacyStateHold)) {
        if (Test-Path -LiteralPath $unused) {
            throw "ForwardShadow private transaction path appeared after preflight: $unused"
        }
    }
    foreach ($candidate in @($CapitalCandidate, $XmCandidate, $LauncherCandidate)) {
        if (Test-Path -LiteralPath $candidate) {
            throw "ForwardShadow candidate appeared after preflight: $candidate"
        }
    }

    $releaseLeaf = [IO.Path]::GetFileNameWithoutExtension(
        [string]$ExternalReleaseManifest.archive_file
    )
    $safeReleaseLeaf = $releaseLeaf -replace '[^A-Za-z0-9._-]', '_'
    $stamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $TransactionArchive = [IO.Path]::GetFullPath(
        (Join-Path $ArchiveRoot "upgrade-$safeReleaseLeaf-$stamp-$RunId")
    )
    Assert-RootChildPath -Path $TransactionArchive -Label "Transaction archive"
    if (Test-Path -LiteralPath $TransactionArchive) {
        throw "Unique transaction archive already exists: $TransactionArchive"
    }
    New-ForwardPrivateDirectory -Path $TransactionArchive

    $ServerSettingCopy = New-ForwardLockedSettingCopy `
        -Source $ServerFile `
        -Name "xm-server" `
        -RunnerSid $RunnerSid
    [void]$SignedReleaseLocks.Add($ServerSettingCopy.lock)
    $TerminalSettingCopy = New-ForwardLockedSettingCopy `
        -Source $TerminalFile `
        -Name "mt5-terminal" `
        -RunnerSid $RunnerSid
    [void]$SignedReleaseLocks.Add($TerminalSettingCopy.lock)
    $Server = [string]$ServerSettingCopy.value
    $Terminal = [IO.Path]::GetFullPath([string]$TerminalSettingCopy.value)
    if ([IO.Path]::GetFileName($Terminal) -cne "terminal64.exe") {
        throw "ForwardShadow MT5 terminal setting must name terminal64.exe."
    }
    $programFilesRoots = @(
        [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique
    $trustedExecutableMutationSids = @(
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    )
    $TerminalExecutableEvidence = Get-TrustedExecutableEvidence `
        -Path $Terminal `
        -Label "MetaTrader 5 terminal" `
        -ExpectedSha256 $ExpectedTerminalSha256 `
        -AllowedRoots $programFilesRoots `
        -TrustedMutationSids $trustedExecutableMutationSids
    [void]$SignedReleaseLocks.Add($TerminalExecutableEvidence.lock)

    $SignedReleaseRoot = [IO.Path]::GetFullPath(
        (Join-Path $TransactionArchive "signed-release")
    )
    Assert-RootChildPath -Path $SignedReleaseRoot -Label "Verified signed release directory"
    New-ForwardPrivateDirectory -Path $SignedReleaseRoot

    $verifiedComponentHashes = [ordered]@{}
    foreach ($component in @($ArchivePath, $ManifestPath, $SignaturePath)) {
        $destination = Join-Path $SignedReleaseRoot ([IO.Path]::GetFileName($component))
        $copyLock = Copy-FileCreateNew -Source $component -Destination $destination
        [void]$SignedReleaseLocks.Add($copyLock)
        $destinationHash = Get-ReleaseSha256 -Path $destination
        if ((Get-ReleaseSha256 -Path $component) -ne $destinationHash) {
            throw "Archived signed release component hash mismatch: $component"
        }
        $verifiedComponentHashes[$destination] = $destinationHash
    }
    Protect-ForwardTree -Path $SignedReleaseRoot -SealOwner
    foreach ($entry in $verifiedComponentHashes.GetEnumerator()) {
        if ((Get-ReleaseSha256 -Path ([string]$entry.Key)) -cne [string]$entry.Value) {
            throw "Protected signed release component is unreadable or changed: $($entry.Key)"
        }
    }
    $VerifiedArchivePath = Join-Path $SignedReleaseRoot ([IO.Path]::GetFileName($ArchivePath))
    $ReleaseManifest = Assert-SignedReleaseArchive `
        -Archive $VerifiedArchivePath `
        -ExpectedProfile "forward-shadow" `
        -RequireProvenance
    if (
        [string]$ReleaseManifest.archive_sha256 -cne
            [string]$ExternalReleaseManifest.archive_sha256 -or
        [string]$ReleaseManifest.archive_file -cne
            [string]$ExternalReleaseManifest.archive_file
    ) {
        throw "Verified transaction copy differs from the requested signed release."
    }
    $MainTaskEvidence = Join-Path $TransactionArchive "$MainTask.previous.xml"
    $WatchdogTaskEvidence = Join-Path $TransactionArchive "$WatchdogTask.previous.xml"
    [IO.File]::WriteAllText(
        $MainTaskEvidence,
        $OriginalMainTaskXml,
        (New-Object Text.UTF8Encoding($false))
    )
    Protect-ForwardPrivateFile -Path $MainTaskEvidence
    [IO.File]::WriteAllText(
        $WatchdogTaskEvidence,
        $OriginalWatchdogTaskXml,
        (New-Object Text.UTF8Encoding($false))
    )
    Protect-ForwardPrivateFile -Path $WatchdogTaskEvidence
    if ($StateMode -eq "LEGACY") {
        $LegacyStateAclEvidence = Join-Path $TransactionArchive "legacy-state.original-acl.json"
        [IO.File]::WriteAllText(
            $LegacyStateAclEvidence,
            ($LegacyStateAclSnapshot | ConvertTo-Json -Depth 4),
            (New-Object Text.UTF8Encoding($false))
        )
        Protect-ForwardPrivateFile -Path $LegacyStateAclEvidence
    }

    New-ForwardPrivateDirectory -Path $Staging
    Expand-Archive -LiteralPath $VerifiedArchivePath -DestinationPath $Staging
    Protect-ForwardTree -Path $Staging -SealOwner
    if (
        (Get-ReleaseSha256 -Path $VerifiedArchivePath) -cne
        ([string]$ReleaseManifest.archive_sha256).ToLowerInvariant()
    ) {
        throw "Verified signed archive changed during extraction."
    }
    $PreviousVenvFingerprint = Get-TreeFingerprint -Path $Venv
    Protect-ForwardTree -Path $Venv -SealOwner
    $VenvBootstrapSealed = $true
    Assert-TreeFingerprint `
        -Path $Venv `
        -Expected $PreviousVenvFingerprint `
        -Label "Sealed bootstrap Python environment"
    $pyvenvConfigPath = Join-Path $Venv "pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $pyvenvConfigPath -PathType Leaf)) {
        throw "Existing ForwardShadow environment has no pyvenv.cfg bootstrap provenance."
    }
    $pyvenvConfig = @{}
    foreach ($line in @(Get-Content -LiteralPath $pyvenvConfigPath)) {
        if ($line -match '^\s*([^=]+?)\s*=\s*(.*?)\s*$') {
            $pyvenvConfig[$Matches[1].Trim().ToLowerInvariant()] = $Matches[2].Trim()
        }
    }
    $BootstrapPython = if ($pyvenvConfig.ContainsKey("executable")) {
        [IO.Path]::GetFullPath([string]$pyvenvConfig["executable"])
    }
    elseif ($pyvenvConfig.ContainsKey("home")) {
        [IO.Path]::GetFullPath((Join-Path ([string]$pyvenvConfig["home"]) "python.exe"))
    }
    else {
        throw "Existing ForwardShadow pyvenv.cfg has no trusted base interpreter path."
    }
    $BootstrapPythonEvidence = Get-TrustedExecutableEvidence `
        -Path $BootstrapPython `
        -Label "Bootstrap Python" `
        -SignerSubjectPattern "Python Software Foundation" `
        -AllowedRoots $programFilesRoots `
        -TrustedMutationSids $trustedExecutableMutationSids
    if ([string]$BootstrapPythonEvidence.product_version -notmatch '^3\.11\.') {
        throw "Bootstrap Python is not an approved Python 3.11 runtime."
    }
    [void]$SignedReleaseLocks.Add($BootstrapPythonEvidence.lock)
    New-ForwardPrivateDirectory -Path $VenvStaging
    & $BootstrapPython -I -S -E -B -m venv $VenvStaging
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $StagedPython -PathType Leaf)) {
        throw "Isolated staged Python environment creation failed."
    }
    if (
        (Get-ReleaseSha256 -Path $BootstrapPython) -cne
            [string]$BootstrapPythonEvidence.sha256
    ) {
        throw "Bootstrap Python changed while creating the staged environment."
    }
    Assert-TreeFingerprint `
        -Path $Venv `
        -Expected $PreviousVenvFingerprint `
        -Label "Bootstrap Python environment"
    Protect-ForwardTree -Path $VenvStaging -SealOwner
    Install-LockedRelease -Python $StagedPython -App $Staging
    Protect-ForwardTree -Path $VenvStaging -SealOwner
    $StagedPythonRuntime = Assert-ReleasePythonRuntime `
        -RuntimePython $StagedPython `
        -Manifest $ReleaseManifest
    & $StagedPython -I -E -B -m compileall -q $Staging
    if ($LASTEXITCODE -ne 0) {
        throw "Staged Python compile validation failed."
    }
    Protect-ForwardTree -Path $Staging -SealOwner
    Protect-ForwardTree -Path $VenvStaging -SealOwner
    $StagedXmRunnerPath = Join-Path $Staging "scripts\run_xm_mt5_forward.py"
    $StagedCapitalRunnerPath = Join-Path $Staging "scripts\run_capital_forward.py"
    $BaselineLockPath = Join-Path $Staging "forward_shadow\baseline_lock.json"
    $RuntimePath = Join-Path $Staging "live_forward\xm_mt5_demo_config.json"
    foreach ($criticalLeaf in @(
        $StagedPython,
        $StagedXmRunnerPath,
        $StagedCapitalRunnerPath,
        $BaselineLockPath,
        $RuntimePath
    )) {
        Assert-ForwardCriticalLeafAcl `
            -Path $criticalLeaf `
            -RequireTrustedOwner
    }

    New-ForwardPrivateDirectory `
        -Path $ValidationRoot `
        -Created ([ref]$ValidationRootCreated)
    $env:XM_MT5_SERVER = $Server
    $env:XM_MT5_TERMINAL_PATH = $Terminal
    $InitOutput = @(
        & $StagedPython -I -E -B $StagedXmRunnerPath `
            --output-root $ValidationRoot init 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Clean staged campaign initialization failed: $($InitOutput -join ' ')"
    }
    Protect-ForwardTree -Path $ValidationRoot -SealOwner

    $ValidationLockPath = Join-Path $ValidationRoot "campaign_lock.json"
    if (
        -not (Test-Path -LiteralPath $ValidationLockPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $BaselineLockPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $RuntimePath -PathType Leaf)
    ) {
        throw "Staged campaign validation did not produce its sealed inputs."
    }
    $ValidationLock = Get-Content -LiteralPath $ValidationLockPath -Raw | ConvertFrom-Json
    $BaselineLock = Get-Content -LiteralPath $BaselineLockPath -Raw | ConvertFrom-Json
    $BaselineManifestPath = [IO.Path]::GetFullPath(
        (Join-Path $Staging ([string]$BaselineLock.baseline_manifest_path))
    )
    $stagingPrefix = $Staging.TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    if (-not $BaselineManifestPath.StartsWith($stagingPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Baseline manifest escapes signed release staging."
    }
    if (
        -not (Test-Path -LiteralPath $BaselineManifestPath -PathType Leaf) -or
        (Get-ReleaseSha256 -Path $BaselineManifestPath) -ne
            ([string]$BaselineLock.baseline_manifest_sha256).ToLowerInvariant() -or
        [string]$ValidationLock.parent_baseline_sha256 -ne
            [string]$BaselineLock.baseline_manifest_sha256 -or
        [string]$ValidationLock.engine_code_hash -ne
            [string]$BaselineLock.engine_manifest.code_hash -or
        [string]$ValidationLock.runtime_config_hash -ne (Get-ReleaseSha256 -Path $RuntimePath)
    ) {
        throw "Staged baseline/runtime campaign hashes are inconsistent."
    }
    foreach ($field in @("engine_code_hash", "live_config_hash", "runtime_config_hash", "harness_hash")) {
        if ([string]$ValidationLock.$field -notmatch '^[0-9a-f]{64}$') {
            throw "Staged campaign lock has an invalid $field."
        }
    }
    foreach ($setting in @(
        [pscustomobject]@{ Source = $ServerFile; Evidence = $ServerSettingCopy },
        [pscustomobject]@{ Source = $TerminalFile; Evidence = $TerminalSettingCopy }
    )) {
        if (
            (Get-ReleaseSha256 -Path $setting.Source) -cne
                [string]$setting.Evidence.sha256 -or
            (Get-ReleaseSha256 -Path $setting.Evidence.path) -cne
                [string]$setting.Evidence.sha256
        ) {
            throw "A locked ForwardShadow broker setting changed during validation."
        }
    }
    if (
        (Get-ReleaseSha256 -Path $TerminalExecutableEvidence.path) -cne
            [string]$TerminalExecutableEvidence.sha256
    ) {
        throw "The trusted MetaTrader 5 terminal changed during validation."
    }
    if (
        (Get-ReleaseSha256 -Path $BootstrapPython) -cne
            [string]$BootstrapPythonEvidence.sha256
    ) {
        throw "The trusted bootstrap Python changed during validation."
    }
    Assert-TreeFingerprint `
        -Path $Venv `
        -Expected $PreviousVenvFingerprint `
        -Label "Trusted bootstrap Python environment"
    Close-SignedReleaseLocks -Locks $SignedReleaseLocks

    Remove-StagedCompiledArtifacts -Stage $Staging
    Protect-ForwardTree -Path $Staging -SealOwner
    Protect-ForwardTree -Path $VenvStaging -SealOwner
    $ExpectedDeployHashes = Get-RelativeFileHashes -Base $Staging -RelativeDirectory "deploy"
    Protect-ForwardTree `
        -Path $Staging `
        -SealOwner
    Protect-ForwardTree `
        -Path $VenvStaging `
        -SealOwner

    Protect-ForwardTree -Path $App -SealOwner
    $AppBootstrapSealed = $true
    if ($StateMode -eq "LEGACY") {
        Protect-ForwardTree `
            -Path $LegacyState `
            -SealOwner
        Assert-ForwardPrivateAclForSnapshot `
            -Path $LegacyState `
            -Snapshot $LegacyStateAclSnapshot
        Assert-TreeFingerprint `
            -Path $LegacyState `
            -Expected $LegacyStateFingerprint `
            -Label "Private legacy campaign state"
        Move-ForwardDirectoryExact `
            -Source $LegacyState `
            -Destination $LegacyStateHold `
            -Label "Legacy state hold"
        Assert-ForwardPrivateAclForSnapshot `
            -Path $LegacyStateHold `
            -Snapshot $LegacyStateAclSnapshot
        Assert-TreeFingerprint `
            -Path $LegacyStateHold `
            -Expected $LegacyStateFingerprint `
            -Label "Held legacy campaign state"
    }
    $PreviousApp = Join-Path $TransactionArchive "app.previous"
    $PreviousVenv = Join-Path $TransactionArchive "venv311.previous"
    $PreviousAppFingerprint = Get-TreeFingerprint -Path $App
    Move-ForwardDirectoryExact `
        -Source $App `
        -Destination $PreviousApp `
        -Label "Previous app archive"
    $OldAppArchived = $true
    $AppBootstrapSealed = $false
    Protect-ForwardTree -Path $PreviousApp -SealOwner
    Assert-TreeFingerprint `
        -Path $PreviousApp `
        -Expected $PreviousAppFingerprint `
        -Label "Private previous ForwardShadow app"
    Move-ForwardDirectoryExact `
        -Source $Venv `
        -Destination $PreviousVenv `
        -Label "Previous Python environment archive"
    $OldVenvArchived = $true
    $VenvBootstrapSealed = $false
    Protect-ForwardTree -Path $PreviousVenv -SealOwner
    Assert-TreeFingerprint `
        -Path $PreviousVenv `
        -Expected $PreviousVenvFingerprint `
        -Label "Private previous ForwardShadow Python environment"
    Move-ForwardDirectoryExact `
        -Source $Staging `
        -Destination $App `
        -Label "New app activation"
    $NewAppActivated = $true
    Move-ForwardDirectoryExact `
        -Source $VenvStaging `
        -Destination $Venv `
        -Label "New Python environment activation"
    $NewVenvActivated = $true

    if ($StateMode -eq "LEGACY" -and (Test-Path -LiteralPath $LegacyState)) {
        throw "Signed release unexpectedly contains the legacy campaign state path."
    }

    $Launcher = Join-Path $App "deploy\run_forward_shadow_windows.ps1"
    $WatchdogLauncher = Join-Path $App "deploy\watchdog_windows.ps1"
    Assert-RelativeFileHashes -Base $App -Expected $ExpectedDeployHashes

    Protect-ForwardTree `
        -Path $App `
        -RunnerIdentity $RunnerIdentity `
        -SealOwner
    Protect-ForwardTree `
        -Path $Venv `
        -RunnerIdentity $RunnerIdentity `
        -SealOwner
    foreach ($criticalLeaf in @(
        $Python,
        $Launcher,
        $WatchdogLauncher,
        (Join-Path $App "scripts\run_xm_mt5_forward.py"),
        (Join-Path $App "scripts\run_capital_forward.py"),
        (Join-Path $App "forward_shadow\baseline_lock.json"),
        (Join-Path $App "live_forward\xm_mt5_demo_config.json")
    )) {
        Assert-ForwardCriticalLeafAcl `
            -Path $criticalLeaf `
            -RunnerIdentity $RunnerIdentity `
            -RequireTrustedOwner
    }
    $RunnerTokenSentinelCreated = [bool](Initialize-ForwardRunnerTokenSentinel `
        -Path $RunnerTokenSentinel `
        -RunnerSid $RunnerSid)
    $productionConfigText = "[Experts]`r`nEnabled=1`r`nAllowLiveTrading=1`r`nAllowDllImport=0`r`nWebRequest=0`r`n"
    $productionConfigBytes = (New-Object Text.UTF8Encoding($false)).GetBytes(
        $productionConfigText
    )
    $productionConfigSha = [Security.Cryptography.SHA256]::Create()
    try {
        $expectedProductionTerminalConfigSha256 = ([BitConverter]::ToString(
            $productionConfigSha.ComputeHash($productionConfigBytes)
        )).Replace("-", "").ToLowerInvariant()
    }
    finally { $productionConfigSha.Dispose() }
    if (Test-Path -LiteralPath $ProductionTerminalConfig) {
        if (
            -not (Test-Path -LiteralPath $ProductionTerminalConfig -PathType Leaf) -or
            (Get-Item -LiteralPath $ProductionTerminalConfig -Force).Attributes.HasFlag(
                [IO.FileAttributes]::ReparsePoint
            ) -or
            (Get-FileHash -LiteralPath $ProductionTerminalConfig -Algorithm SHA256).Hash.ToLowerInvariant() -cne
                $expectedProductionTerminalConfigSha256
        ) { throw "Existing ForwardShadow production terminal configuration is not canonical." }
    }
    else {
        $productionConfigStream = [IO.File]::Open(
            $ProductionTerminalConfig,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $productionConfigStream.Write(
                $productionConfigBytes,
                0,
                $productionConfigBytes.Length
            )
            $productionConfigStream.Flush($true)
        }
        finally { $productionConfigStream.Dispose() }
        $ProductionTerminalConfigCreated = $true
    }
    Protect-ForwardPrivateFile `
        -Path $ProductionTerminalConfig `
        -RunnerSid $RunnerSid
    $ProductionTerminalConfigHardened = $true
    $productionTerminalConfigSha256 = (
        Get-FileHash -LiteralPath $ProductionTerminalConfig -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $ProductionTerminalConfigLock = [IO.File]::Open(
        $ProductionTerminalConfig,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    if (Test-Path -LiteralPath $RunnerProbeTerminalConfig) {
        throw "ForwardShadow runner terminal probe configuration unexpectedly exists."
    }
    $probeConfigStream = [IO.File]::Open(
        $RunnerProbeTerminalConfig,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    $RunnerProbeTerminalConfigCreated = $true
    try {
        $probeConfigText = "[Experts]`r`nEnabled=0`r`nAllowLiveTrading=0`r`nAllowDllImport=0`r`nWebRequest=0`r`n"
        $probeConfigBytes = (New-Object Text.UTF8Encoding($false)).GetBytes(
            $probeConfigText
        )
        $probeConfigStream.Write($probeConfigBytes, 0, $probeConfigBytes.Length)
        $probeConfigStream.Flush($true)
    }
    finally { $probeConfigStream.Dispose() }
    Protect-ForwardPrivateFile `
        -Path $RunnerProbeTerminalConfig `
        -RunnerSid $RunnerSid
    $runnerProbeTerminalConfigSha256 = (
        Get-FileHash -LiteralPath $RunnerProbeTerminalConfig -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $RunnerProbeTerminalConfigLock = [IO.File]::Open(
        $RunnerProbeTerminalConfig,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $FinalPythonRuntime = Assert-ReleasePythonRuntime `
        -RuntimePython $Python `
        -Manifest $ReleaseManifest

    # The one-time S4U broker proof writes its authenticated diagnostic beneath
    # canonical state.  Create and seal that writable boundary before launching
    # the proof task; otherwise a legacy-state install fails before the launcher
    # can emit SCRIPT_START.
    if (-not (Test-Path -LiteralPath $CanonicalState)) {
        New-ForwardPrivateDirectory -Path $CanonicalState
        $CanonicalStateCreated = $true
        $CanonicalStateFingerprint = Get-TreeFingerprint -Path $CanonicalState
    }
    $CanonicalStateAclHardened = $true
    Protect-ForwardStateTree -Path $CanonicalState -RunnerSid $RunnerSid

    $TargetRunnerPassword = Reset-ForwardRunnerPasswordInMemory `
        -RunnerSid $RunnerSid
    $readOnlyBrokerProof = Invoke-ForwardRunnerBrokerProof `
        -Launcher $Launcher `
        -RunnerIdentity $RunnerIdentity `
        -RunnerSid $RunnerSid `
        -TerminalPath $Terminal `
        -TerminalConfigPath $RunnerProbeTerminalConfig `
        -TerminalConfigSha256 $runnerProbeTerminalConfigSha256 `
        -TerminalMode ReadOnly `
        -RunnerPassword $TargetRunnerPassword `
        -ExpectedLogin $ExpectedMt5Login
    $RunnerProbeTerminalConfigLock.Dispose()
    $RunnerProbeTerminalConfigLock = $null
    Remove-Item -LiteralPath $RunnerProbeTerminalConfig -Force
    $RunnerProbeTerminalConfigCreated = $false
    $productionBrokerProof = Invoke-ForwardRunnerBrokerProof `
        -Launcher $Launcher `
        -RunnerIdentity $RunnerIdentity `
        -RunnerSid $RunnerSid `
        -TerminalPath $Terminal `
        -TerminalConfigPath $ProductionTerminalConfig `
        -TerminalConfigSha256 $productionTerminalConfigSha256 `
        -TerminalMode Production `
        -RunnerPassword $TargetRunnerPassword `
        -ExpectedLogin $ExpectedMt5Login
    $ProductionTerminalConfigLock.Dispose()
    $ProductionTerminalConfigLock = $null
    $RunnerBrokerProof = [ordered]@{
        read_only = $readOnlyBrokerProof
        production_restore = $productionBrokerProof
    }
    $RunnerTokenProbePassed = $true

    $mainArguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$Launcher`""
        "-Root `"$Root`""
    ) -join " "
    $watchdogArguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$WatchdogLauncher`""
        "-MainTaskName `"$MainTask`""
        "-HealthPath `"$WatchdogHealth`""
        "-ProcessPattern `"run_xm_mt5_forward.py`""
        "-StatusPath `"$WatchdogStatus`""
    ) -join " "
    $watchdogAction = New-ScheduledTaskAction `
        -Execute $WindowsPowerShellExe `
        -Argument $watchdogArguments
    $watchdogPrincipal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $watchdogTrigger = New-ScheduledTaskTrigger -AtStartup
    $watchdogSettings = New-ScheduledTaskSettingsSet `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew
    $TaskDefinitionsChanged = $true
    Set-ForwardMainTaskWithS4UCredential `
        -Arguments $mainArguments `
        -RunnerIdentity $RunnerIdentity `
        -RunnerPassword $TargetRunnerPassword
    Set-ScheduledTask `
        -TaskName $WatchdogTask `
        -Action $watchdogAction `
        -Trigger $watchdogTrigger `
        -Principal $watchdogPrincipal `
        -Settings $watchdogSettings | Out-Null
    Assert-CanonicalTaskDefinition `
        -TaskName $MainTask `
        -ExpectedArguments $mainArguments `
        -ExpectedUserId $RunnerIdentity `
        -ExpectedLogonType "S4U" `
        -ExpectedRunLevel "Limited"
    Assert-CanonicalTaskDefinition `
        -TaskName $WatchdogTask `
        -ExpectedArguments $watchdogArguments `
        -ExpectedUserId "SYSTEM" `
        -ExpectedLogonType "ServiceAccount" `
        -ExpectedRunLevel "Highest"
    Stop-ForwardRuntime
    Assert-TaskPairStopped
    Assert-NoForwardPythonProcesses

    # Keep writable legacy state outside the app while recursive release ACLs are applied.
    if ($StateMode -eq "LEGACY") {
        New-Item -ItemType Directory -Path (Split-Path -Parent $LegacyState) -Force | Out-Null
        Protect-ForwardTree `
            -Path $App `
            -RunnerIdentity $RunnerIdentity `
            -SealOwner
        Move-ForwardDirectoryExact `
            -Source $LegacyStateHold `
            -Destination $LegacyState `
            -Label "Legacy state reattachment"
        # The original legacy ACL contains inherited ACE metadata from the old
        # app tree.  Recreating those inherited bits beneath the newly sealed
        # app is neither stable nor desirable.  Keep the unchanged legacy state
        # private while tasks remain stopped; rollover atomically migrates it to
        # the canonical runner-writable state tree.  Exact original ACL restore
        # remains mandatory on the rollback path after the old app is restored.
        Assert-ForwardPrivateAclForSnapshot `
            -Path $LegacyState `
            -Snapshot $LegacyStateAclSnapshot
        Assert-TreeFingerprint `
            -Path $LegacyState `
            -Expected $LegacyStateFingerprint `
            -Label "Reattached legacy campaign state"
    }

    $candidateSpecs = @(
        [pscustomobject]@{
            Source = Join-Path $App "scripts\run_capital_forward.py"
            Destination = $CapitalCandidate
        },
        [pscustomobject]@{
            Source = Join-Path $App "scripts\run_xm_mt5_forward.py"
            Destination = $XmCandidate
        },
        [pscustomobject]@{
            Source = Join-Path $App "deploy\run_forward_shadow_windows.ps1"
            Destination = $LauncherCandidate
        }
    )
    foreach ($spec in $candidateSpecs) {
        $CandidateResults[[IO.Path]::GetFileName($spec.Destination)] =
            New-CandidateIfMissing `
                -Source $spec.Source `
                -Destination $spec.Destination `
                -RunnerSid $RunnerSid
    }

    Assert-TreeFingerprint `
        -Path $CanonicalState `
        -Expected $CanonicalStateFingerprint `
        -Label "Canonical campaign state or placeholder"
    if ($StateMode -eq "LEGACY") {
        Assert-TreeFingerprint `
            -Path $LegacyState `
            -Expected $LegacyStateFingerprint `
            -Label "Legacy campaign state"
    }
    Assert-CanonicalTaskDefinition `
        -TaskName $MainTask `
        -ExpectedArguments $mainArguments `
        -ExpectedUserId $RunnerIdentity `
        -ExpectedLogonType "S4U" `
        -ExpectedRunLevel "Limited"
    Assert-CanonicalTaskDefinition `
        -TaskName $WatchdogTask `
        -ExpectedArguments $watchdogArguments `
        -ExpectedUserId "SYSTEM" `
        -ExpectedLogonType "ServiceAccount" `
        -ExpectedRunLevel "Highest"
    Assert-TaskPairStopped
    Assert-NoForwardPythonProcesses
    Assert-ForwardAclSemantics `
        -Path $Root `
        -RunnerSid $RunnerSid `
        -DirectoryRoot
    Assert-ForwardAclSemantics `
        -Path $CanonicalState `
        -ModifySid $RunnerSid `
        -DirectoryRoot
    Assert-ForwardCriticalLeafAcl `
        -Path $RunnerTokenSentinel `
        -RunnerIdentity $RunnerIdentity `
        -Protected `
        -RequireTrustedOwner
    Assert-ForwardCriticalLeafAcl `
        -Path $ProductionTerminalConfig `
        -RunnerIdentity $RunnerIdentity `
        -Protected `
        -RequireTrustedOwner
    if (
        (Get-FileHash -LiteralPath $ProductionTerminalConfig -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            $expectedProductionTerminalConfigSha256
    ) { throw "ForwardShadow production terminal configuration changed before commit." }
    Remove-GeneratedTree `
        -Path $ValidationRoot `
        -ExpectedLeafPattern '^upgrade-validation\.[0-9a-f]{32}$'
    $ValidationRootCreated = $false
    Assert-TreeFingerprint `
        -Path $PreviousApp `
        -Expected $PreviousAppFingerprint `
        -Label "Archived previous ForwardShadow app"
    Assert-TreeFingerprint `
        -Path $PreviousVenv `
        -Expected $PreviousVenvFingerprint `
        -Label "Archived previous ForwardShadow Python environment"
    Protect-ForwardTree `
        -Path $TransactionArchive `
        -SealOwner
    $TransactionEvidenceLeaves = @(
        $VerifiedArchivePath,
        (Join-Path $SignedReleaseRoot ([IO.Path]::GetFileName($ManifestPath))),
        (Join-Path $SignedReleaseRoot ([IO.Path]::GetFileName($SignaturePath))),
        $ServerSettingCopy.path,
        $TerminalSettingCopy.path,
        $MainTaskEvidence,
        $WatchdogTaskEvidence
    )
    if ($LegacyStateAclEvidence) { $TransactionEvidenceLeaves += $LegacyStateAclEvidence }
    foreach ($criticalLeaf in $TransactionEvidenceLeaves) {
        Assert-ForwardCriticalLeafAcl `
            -Path $criticalLeaf `
            -RequireTrustedOwner
    }
    Protect-ForwardTree `
        -Path $ArchiveRoot `
        -AllowEmpty `
        -SealOwner
    Assert-NoUntrustedDeleteChild `
        -Path $ArchiveRoot `
        -TrustedSids $TrustedInfrastructureSids
    $UpgradeSucceeded = $true

    [ordered]@{
        state = "UPGRADED_TASKS_STOPPED"
        release_archive = $ArchivePath
        release_sha256 = [string]$ReleaseManifest.archive_sha256
        verified_release_archive = $VerifiedArchivePath
        archived_previous_app = $PreviousApp
        archived_previous_venv = $PreviousVenv
        state_mode = $StateMode
        runner_security = [ordered]@{
            identity = $RunnerIdentity
            sid = $RunnerSid
            non_admin_group_proof = $true
            exact_logon_rights = @(Get-ForwardAccountRights -Sid $RunnerSid | Sort-Object)
            acl_probe_passed = $RunnerTokenProbePassed
            broker_probe = $RunnerBrokerProof
            root_acl = "SYSTEM_AND_ADMINISTRATORS_FULL_RUNNER_RX"
            state_acl = "SYSTEM_AND_ADMINISTRATORS_FULL_RUNNER_MODIFY"
            legacy_state_acl = if ($StateMode -eq "LEGACY") {
                "PRIVATE_UNTIL_TRANSACTIONAL_ROLLOVER"
            } else { "NOT_APPLICABLE" }
        }
        python_runtime = $FinalPythonRuntime
        trusted_bootstrap = [ordered]@{
            python_path = [string]$BootstrapPythonEvidence.path
            python_sha256 = [string]$BootstrapPythonEvidence.sha256
            python_signer = [string]$BootstrapPythonEvidence.signer_subject
            terminal_path = [string]$TerminalExecutableEvidence.path
            terminal_sha256 = [string]$TerminalExecutableEvidence.sha256
            terminal_expected_sha256 = $ExpectedTerminalSha256
            production_terminal_config = $ProductionTerminalConfig
            production_terminal_config_sha256 = $productionTerminalConfigSha256
            terminal_signature_status = [string]$TerminalExecutableEvidence.signature_status
            terminal_signer = [string]$TerminalExecutableEvidence.signer_subject
            server_setting_sha256 = [string]$ServerSettingCopy.sha256
            terminal_setting_sha256 = [string]$TerminalSettingCopy.sha256
        }
        validation = [ordered]@{
            engine_code_hash = [string]$ValidationLock.engine_code_hash
            live_config_hash = [string]$ValidationLock.live_config_hash
            runtime_config_hash = [string]$ValidationLock.runtime_config_hash
            harness_hash = [string]$ValidationLock.harness_hash
        }
        candidates = $CandidateResults
        main_task_state = (Get-ScheduledTask -TaskName $MainTask).State.ToString()
        watchdog_task_state = (Get-ScheduledTask -TaskName $WatchdogTask).State.ToString()
    } | ConvertTo-Json -Depth 8
}
catch {
    $UpgradeSucceeded = $false
    $primaryError = $_
    $failureToReport = $primaryError.Exception
    if (-not $runtimeControlEntered) {
        throw $primaryError
    }
    $rollbackErrors = [Collections.Generic.List[string]]::new()
    $stateRollbackVerified = $true

    Close-SignedReleaseLocks -Locks $SignedReleaseLocks -Errors $rollbackErrors
    $rollbackRuntimeStopped = $false
    try {
        Stop-ForwardRuntime
        Assert-TaskPairStopped
        Assert-NoForwardPythonProcesses
        $rollbackRuntimeStopped = $true
    }
    catch { $rollbackErrors.Add("rollback stop gate: $($_.Exception.Message)") }
    if (-not $rollbackRuntimeStopped) {
        $failureToReport = [InvalidOperationException]::new(
            "ForwardShadow upgrade failed and rollback was not attempted because stopped state could not be proven: $($primaryError.Exception.Message); $($rollbackErrors -join '; ')",
            $primaryError.Exception
        )
        throw $failureToReport
    }
    if ($RunnerProbeTerminalConfigLock) {
        try {
            $RunnerProbeTerminalConfigLock.Dispose()
            $RunnerProbeTerminalConfigLock = $null
        }
        catch { $rollbackErrors.Add("runner terminal config lock cleanup: $($_.Exception.Message)") }
    }
    if ($RunnerProbeTerminalConfigCreated -and (Test-Path -LiteralPath $RunnerProbeTerminalConfig)) {
        try {
            Remove-Item -LiteralPath $RunnerProbeTerminalConfig -Force
            $RunnerProbeTerminalConfigCreated = $false
        }
        catch { $rollbackErrors.Add("runner terminal config cleanup: $($_.Exception.Message)") }
    }
    if ($ProductionTerminalConfigLock) {
        try {
            $ProductionTerminalConfigLock.Dispose()
            $ProductionTerminalConfigLock = $null
        }
        catch { $rollbackErrors.Add("production terminal config lock cleanup: $($_.Exception.Message)") }
    }
    if ($AppBootstrapSealed -and (Test-Path -LiteralPath $App -PathType Container)) {
        try {
            Protect-ForwardTree `
                -Path $App `
                -RunnerIdentity $OriginalRunnerIdentity `
                -SealOwner
            $AppBootstrapSealed = $false
        }
        catch { $rollbackErrors.Add("bootstrap app ACL recovery: $($_.Exception.Message)") }
    }
    if ($VenvBootstrapSealed -and (Test-Path -LiteralPath $Venv -PathType Container)) {
        try {
            Protect-ForwardTree `
                -Path $Venv `
                -RunnerIdentity $OriginalRunnerIdentity `
                -SealOwner
            $VenvBootstrapSealed = $false
        }
        catch { $rollbackErrors.Add("bootstrap Python ACL recovery: $($_.Exception.Message)") }
    }
    if ($TaskDefinitionsChanged) {
        $taskRestoreApplied = $false
        try {
            Set-ScheduledTask `
                -TaskName $MainTask `
                -Action $OriginalMainActions `
                -Trigger $OriginalMainTriggers `
                -Principal $OriginalMainPrincipal `
                -Settings $OriginalMainSettings | Out-Null
            Set-ScheduledTask `
                -TaskName $WatchdogTask `
                -Action $OriginalWatchdogActions `
                -Trigger $OriginalWatchdogTriggers `
                -Principal $OriginalWatchdogPrincipal `
                -Settings $OriginalWatchdogSettings | Out-Null
            $taskRestoreApplied = $true
        }
        catch { $rollbackErrors.Add("task definition restore: $($_.Exception.Message)") }
        try {
            Stop-ForwardRuntime
            Assert-TaskPairStopped
            Assert-NoForwardPythonProcesses
        }
        catch {
            throw [InvalidOperationException]::new(
                "ForwardShadow task restore could not be followed by a proven stopped state; filesystem rollback was not attempted: $($_.Exception.Message)",
                $primaryError.Exception
            )
        }
        if ($taskRestoreApplied) {
            try {
                Assert-TaskXmlRestored -TaskName $MainTask -ExpectedXml $OriginalMainTaskXml
                Assert-TaskXmlRestored `
                    -TaskName $WatchdogTask `
                    -ExpectedXml $OriginalWatchdogTaskXml
                $TaskDefinitionsChanged = $false
            }
            catch { $rollbackErrors.Add("task XML verification: $($_.Exception.Message)") }
        }
    }
    foreach ($candidate in @($CreatedCandidates)) {
        try {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                Remove-Item -LiteralPath $candidate -Force
            }
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    foreach ($temporary in @($CandidateTempPaths)) {
        try {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }

    $legacyStateDetached = $true
    if ($StateMode -eq "LEGACY" -and $NewAppActivated) {
        $legacyStateDetached = $false
        try {
            if (Test-Path -LiteralPath $LegacyStateHold -PathType Container) {
                # Rollback-held authoritative legacy campaign state remains outside new-app debris.
            }
            elseif (Test-Path -LiteralPath $LegacyState -PathType Container) {
                Assert-TreeFingerprint `
                    -Path $LegacyState `
                    -Expected $LegacyStateFingerprint `
                    -Label "Rollback legacy campaign state before private seal"
                Protect-ForwardTree `
                    -Path $LegacyState `
                    -SealOwner
                Assert-ForwardPrivateAclForSnapshot `
                    -Path $LegacyState `
                    -Snapshot $LegacyStateAclSnapshot
                Move-ForwardDirectoryExact `
                    -Source $LegacyState `
                    -Destination $LegacyStateHold `
                    -Label "Rollback legacy state hold"
            }
            else {
                throw "Rollback could not locate the legacy campaign state."
            }
            Assert-TreeFingerprint `
                -Path $LegacyStateHold `
                -Expected $LegacyStateFingerprint `
                -Label "Rollback-detached legacy campaign state"
            $legacyStateDetached = $true
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
        if ($legacyStateDetached) {
            try {
            Protect-ForwardTree `
                -Path $LegacyStateHold `
                -SealOwner
            Assert-ForwardPrivateAclForSnapshot `
                -Path $LegacyStateHold `
                -Snapshot $LegacyStateAclSnapshot
            Assert-TreeFingerprint `
                -Path $LegacyStateHold `
                -Expected $LegacyStateFingerprint `
                -Label "Rollback-held legacy campaign state"
            }
            catch {
                # The stopped-state gate and detached fingerprint still permit exact snapshot recovery.
                $rollbackErrors.Add("legacy hold hardening: $($_.Exception.Message)")
            }
        }
    }

    if (
        $NewAppActivated -and
        (Test-Path -LiteralPath $App -PathType Container) -and
        ($StateMode -ne "LEGACY" -or $legacyStateDetached)
    ) {
        try {
            if (Test-Path -LiteralPath $Staging) {
                throw "Rollback staging path unexpectedly exists: $Staging"
            }
            Move-ForwardDirectoryExact `
                -Source $App `
                -Destination $Staging `
                -Label "Rollback new app removal"
            Protect-ForwardTree -Path $Staging -SealOwner
            $NewAppActivated = $false
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    if ($NewVenvActivated -and (Test-Path -LiteralPath $Venv -PathType Container)) {
        try {
            if (Test-Path -LiteralPath $VenvStaging) {
                throw "Rollback Python staging path unexpectedly exists: $VenvStaging"
            }
            Move-ForwardDirectoryExact `
                -Source $Venv `
                -Destination $VenvStaging `
                -Label "Rollback new Python environment removal"
            Protect-ForwardTree -Path $VenvStaging -SealOwner
            $NewVenvActivated = $false
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    if ($OldAppArchived -and (Test-Path -LiteralPath $PreviousApp -PathType Container)) {
        try {
            if (Test-Path -LiteralPath $App) {
                throw "Rollback cannot restore app because target exists: $App"
            }
            Assert-TreeFingerprint `
                -Path $PreviousApp `
                -Expected $PreviousAppFingerprint `
                -Label "Rollback previous ForwardShadow app"
            Move-ForwardDirectoryExact `
                -Source $PreviousApp `
                -Destination $App `
                -Label "Rollback previous app restore"
            $OldAppArchived = $false
            Protect-ForwardTree `
                -Path $App `
                -RunnerIdentity $OriginalRunnerIdentity `
                -SealOwner
            Assert-TreeFingerprint `
                -Path $App `
                -Expected $PreviousAppFingerprint `
                -Label "Restored previous ForwardShadow app"
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    if ($OldVenvArchived -and (Test-Path -LiteralPath $PreviousVenv -PathType Container)) {
        try {
            if (Test-Path -LiteralPath $Venv) {
                throw "Rollback cannot restore Python environment because target exists: $Venv"
            }
            Assert-TreeFingerprint `
                -Path $PreviousVenv `
                -Expected $PreviousVenvFingerprint `
                -Label "Rollback previous ForwardShadow Python environment"
            Move-ForwardDirectoryExact `
                -Source $PreviousVenv `
                -Destination $Venv `
                -Label "Rollback previous Python environment restore"
            $OldVenvArchived = $false
            Protect-ForwardTree `
                -Path $Venv `
                -RunnerIdentity $OriginalRunnerIdentity `
                -SealOwner
            Assert-TreeFingerprint `
                -Path $Venv `
                -Expected $PreviousVenvFingerprint `
                -Label "Restored previous ForwardShadow Python environment"
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    if ($StateMode -eq "LEGACY" -and -not $OldAppArchived -and -not $NewAppActivated) {
        try {
            if (Test-Path -LiteralPath $LegacyStateHold -PathType Container) {
                if (Test-Path -LiteralPath $LegacyState) {
                    throw "Rollback legacy state target unexpectedly exists: $LegacyState"
                }
                New-Item -ItemType Directory -Path (Split-Path -Parent $LegacyState) -Force | Out-Null
                Protect-ForwardTree `
                    -Path $App `
                    -RunnerIdentity $OriginalRunnerIdentity `
                    -SealOwner
                Move-ForwardDirectoryExact `
                    -Source $LegacyStateHold `
                    -Destination $LegacyState `
                    -Label "Rollback legacy state restore"
            }
            Restore-ForwardAclSnapshot `
                -Path $LegacyState `
                -Snapshot $LegacyStateAclSnapshot
            Assert-TreeFingerprint `
                -Path $LegacyState `
                -Expected $LegacyStateFingerprint `
                -Label "Rolled-back legacy campaign state"
        }
        catch {
            $stateRollbackVerified = $false
            $rollbackErrors.Add($_.Exception.Message)
        }
    }

    if ($CanonicalStateFingerprint) {
        try {
            Assert-TreeFingerprint `
                -Path $CanonicalState `
                -Expected $CanonicalStateFingerprint `
                -Label "Rolled-back canonical campaign state or placeholder"
        }
        catch {
            $stateRollbackVerified = $false
            $rollbackErrors.Add($_.Exception.Message)
        }
    }

    if ($CanonicalStateAclHardened) {
        try {
            if ($CanonicalStateCreated) {
                if (
                    -not (Test-Path -LiteralPath $CanonicalState -PathType Container) -or
                    @(Get-ChildItem -LiteralPath $CanonicalState -Force).Count -ne 0
                ) {
                    throw "Rollback-created canonical state is missing or non-empty."
                }
                [IO.Directory]::Delete($CanonicalState, $false)
                $CanonicalStateCreated = $false
            }
            elseif ($CanonicalStateAclSnapshot) {
                Restore-ForwardAclSnapshot `
                    -Path $CanonicalState `
                    -Snapshot $CanonicalStateAclSnapshot
            }
            else {
                throw "Canonical state ACL rollback evidence is missing."
            }
            $CanonicalStateAclHardened = $false
        }
        catch {
            $stateRollbackVerified = $false
            $rollbackErrors.Add("canonical state ACL restore: $($_.Exception.Message)")
        }
    }
    if ($RunnerTokenSentinelCreated) {
        try {
            if (Test-Path -LiteralPath $RunnerTokenSentinel) {
                if (-not (Test-Path -LiteralPath $RunnerTokenSentinel -PathType Leaf)) {
                    throw "Rollback runner token sentinel changed type."
                }
                Remove-Item -LiteralPath $RunnerTokenSentinel -Force
            }
            $RunnerTokenSentinelCreated = $false
        }
        catch { $rollbackErrors.Add("runner token sentinel cleanup: $($_.Exception.Message)") }
    }
    if ($BrokerSettingsHardened -and $OriginalRunnerSid) {
        foreach ($settingPath in @($ServerFile, $TerminalFile)) {
            try {
                if (Test-Path -LiteralPath $settingPath -PathType Leaf) {
                    Protect-ForwardPrivateFile `
                        -Path $settingPath `
                        -RunnerSid $OriginalRunnerSid
                }
            }
            catch { $rollbackErrors.Add("broker setting ACL restore: $($_.Exception.Message)") }
        }
        $BrokerSettingsHardened = $false
    }
    if ($ProductionTerminalConfigHardened) {
        try {
            if ($ProductionTerminalConfigCreated) {
                if (Test-Path -LiteralPath $ProductionTerminalConfig -PathType Leaf) {
                    Remove-Item -LiteralPath $ProductionTerminalConfig -Force
                }
                $ProductionTerminalConfigCreated = $false
            }
            elseif (Test-Path -LiteralPath $ProductionTerminalConfig -PathType Leaf) {
                Protect-ForwardPrivateFile `
                    -Path $ProductionTerminalConfig `
                    -RunnerSid $OriginalRunnerSid
            }
            $ProductionTerminalConfigHardened = $false
        }
        catch { $rollbackErrors.Add("production terminal config restore: $($_.Exception.Message)") }
    }
    $rollbackComplete = (
        -not $OldAppArchived -and
        -not $OldVenvArchived -and
        -not $NewAppActivated -and
        -not $NewVenvActivated -and
        -not $AppBootstrapSealed -and
        -not $VenvBootstrapSealed -and
        -not $TaskDefinitionsChanged -and
        -not $CanonicalStateAclHardened -and
        -not $CanonicalStateCreated -and
        -not $RunnerTokenSentinelCreated -and
        -not $BrokerSettingsHardened -and
        -not $ProductionTerminalConfigHardened -and
        $rollbackErrors.Count -eq 0 -and
        $stateRollbackVerified -and
        (Test-Path -LiteralPath $App -PathType Container) -and
        (Test-Path -LiteralPath $Venv -PathType Container) -and
        ($StateMode -ne "LEGACY" -or -not (Test-Path -LiteralPath $LegacyStateHold))
    )
    if ($rollbackComplete) {
        try { Remove-GeneratedTree -Path $Staging -ExpectedLeafPattern '^app\.stage\.[0-9a-f]{32}$' } `
            catch { $rollbackErrors.Add($_.Exception.Message) }
        try {
            Remove-GeneratedTree `
                -Path $VenvStaging `
                -ExpectedLeafPattern '^venv311\.stage\.[0-9a-f]{32}$'
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    if ($TransactionArchive -and $rollbackComplete) {
        try {
            if (Test-Path -LiteralPath $TransactionArchive) {
                Assert-RootChildPath -Path $TransactionArchive -Label "Failed transaction archive"
                Remove-Item -LiteralPath $TransactionArchive -Recurse -Force
            }
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    if ($RunnerRightsHardened -and $null -ne $RunnerOriginalAccountRights) {
        try {
            Set-ForwardAccountRightsExact `
                -RunnerSid $RunnerSid `
                -Rights @($RunnerOriginalAccountRights)
            $restoredRights = @(Get-ForwardAccountRights -Sid $RunnerSid | Sort-Object -Unique)
            $expectedRights = @($RunnerOriginalAccountRights | Sort-Object -Unique)
            if (
                $restoredRights.Count -ne $expectedRights.Count -or
                [string]::Join("`n", $restoredRights) -cne
                    [string]::Join("`n", $expectedRights)
            ) {
                throw "ForwardShadow runner account rights were not restored exactly."
            }
            $RunnerRightsHardened = $false
        }
        catch { $rollbackErrors.Add("runner account-right restore: $($_.Exception.Message)") }
    }
    if ($RootAclHardened -and $RootAclSnapshot) {
        try {
            Restore-ForwardRootAclSnapshot -Snapshot $RootAclSnapshot
            $RootAclHardened = $false
        }
        catch { $rollbackErrors.Add("root ACL restore: $($_.Exception.Message)") }
    }
    $rollbackComplete = (
        $rollbackComplete -and
        -not $RunnerRightsHardened -and
        -not $RootAclHardened
    )

    $suffix = if ($rollbackErrors.Count) {
        " Rollback errors: $($rollbackErrors -join '; ')"
    } else {
        " App rollback completed."
    }
    $failureToReport = [InvalidOperationException]::new(
        "ForwardShadow signed app upgrade failed: $($primaryError.Exception.Message).$suffix",
        $primaryError.Exception
    )
    throw $failureToReport
}
finally {
    try {
        if ($TargetRunnerPassword) {
            $TargetRunnerPassword.Dispose()
            $TargetRunnerPassword = $null
        }
    }
    catch { $cleanupErrors.Add("runner credential cleanup: $($_.Exception.Message)") }
    try {
        if ($RunnerProbeTerminalConfigLock) {
            $RunnerProbeTerminalConfigLock.Dispose()
            $RunnerProbeTerminalConfigLock = $null
        }
    }
    catch { $cleanupErrors.Add("runner terminal config lock cleanup: $($_.Exception.Message)") }
    try {
        if ($ProductionTerminalConfigLock) {
            $ProductionTerminalConfigLock.Dispose()
            $ProductionTerminalConfigLock = $null
        }
    }
    catch { $cleanupErrors.Add("production terminal config lock cleanup: $($_.Exception.Message)") }
    Close-SignedReleaseLocks -Locks $SignedReleaseLocks -Errors $cleanupErrors
    $finalStopError = $null
    if ($runtimeControlEntered) {
        try {
            Stop-ForwardRuntime
            Assert-TaskPairStopped
            Assert-NoForwardPythonProcesses
        }
        catch { $finalStopError = $_ }
    }
    if ($finalStopError) {
        $cleanupErrors.Add("final stopped-state enforcement: $($finalStopError.Exception.Message)")
    }
    if (-not $finalStopError -and $runtimeControlEntered -and $RunnerProbeTerminalConfigCreated) {
        try {
            if (Test-Path -LiteralPath $RunnerProbeTerminalConfig) {
                Remove-Item -LiteralPath $RunnerProbeTerminalConfig -Force
            }
            $RunnerProbeTerminalConfigCreated = $false
        }
        catch { $cleanupErrors.Add("runner terminal config cleanup: $($_.Exception.Message)") }
    }
    if ($PreviousPSModulePathCaptured) {
        try {
            [Environment]::SetEnvironmentVariable("PSModulePath", $PreviousPSModulePath, "Process")
        }
        catch { $cleanupErrors.Add("PSModulePath restore: $($_.Exception.Message)") }
    }
    try {
        [Environment]::SetEnvironmentVariable("XM_MT5_SERVER", $PreviousServerEnv, "Process")
    }
    catch { $cleanupErrors.Add("XM_MT5_SERVER restore: $($_.Exception.Message)") }
    try {
        [Environment]::SetEnvironmentVariable("XM_MT5_TERMINAL_PATH", $PreviousTerminalEnv, "Process")
    }
    catch { $cleanupErrors.Add("XM_MT5_TERMINAL_PATH restore: $($_.Exception.Message)") }
    foreach ($name in $PreviousPythonEnvironment.Keys) {
        try {
            [Environment]::SetEnvironmentVariable(
                $name,
                [string]$PreviousPythonEnvironment[$name],
                "Process"
            )
        }
        catch { $cleanupErrors.Add("$name restore: $($_.Exception.Message)") }
    }
    if (-not $finalStopError -and $runtimeControlEntered -and $ValidationRootCreated) {
        try {
            Remove-GeneratedTree `
                -Path $ValidationRoot `
                -ExpectedLeafPattern '^upgrade-validation\.[0-9a-f]{32}$'
            $ValidationRootCreated = $false
        }
        catch { $cleanupErrors.Add("validation tree cleanup: $($_.Exception.Message)") }
    }
    if (
        $ArchiveBoundaryHardened
    ) {
        try {
            Protect-ForwardTree `
                -Path $ArchiveRoot `
                -AllowEmpty `
                -RootOnly `
                -SealOwner
            if ($UpgradeSucceeded) {
                Assert-ForwardTrustedOwner -Path $Root -AllowRoot
                Assert-ForwardAclSemantics `
                    -Path $Root `
                    -RunnerSid $RunnerSid `
                    -DirectoryRoot
            }
        }
        catch { $cleanupErrors.Add("final archive ACL seal: $($_.Exception.Message)") }
    }
    try {
        if ($SelfReadLock) {
            $SelfReadLock.Dispose()
            $SelfReadLock = $null
        }
    }
    catch { $cleanupErrors.Add("upgrader self-lock cleanup: $($_.Exception.Message)") }
    if ($cleanupErrors.Count -gt 0) {
        $cleanupSummary = $cleanupErrors -join '; '
        if ($failureToReport) {
            throw [InvalidOperationException]::new(
                "ForwardShadow operation failed: $($failureToReport.Message); cleanup: $cleanupSummary",
                $failureToReport
            )
        }
        throw [InvalidOperationException]::new(
            "ForwardShadow cleanup failed: $cleanupSummary",
            [Exception]::new($cleanupSummary)
        )
    }
}
