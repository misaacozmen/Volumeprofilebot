[CmdletBinding()]
param(
    [switch]$KeepStopped,
    [switch]$LibraryOnly,
    [string]$Root = "C:\ForwardShadow"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (
    [string]$PSVersionTable.PSEdition -cne "Desktop" -or
    [int]$PSVersionTable.PSVersion.Major -ne 5
) {
    throw "ForwardShadow readiness requires Windows PowerShell 5.1."
}
$PreviousPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$TrustedPSModulePath = [IO.Path]::GetFullPath((Join-Path $PSHOME "Modules"))
[Environment]::SetEnvironmentVariable("PSModulePath", $TrustedPSModulePath, "Process")
$ScheduledTasksModule = [IO.Path]::GetFullPath(
    (Join-Path $TrustedPSModulePath "ScheduledTasks\ScheduledTasks.psd1")
)
$CimCmdletsModule = [IO.Path]::GetFullPath(
    (Join-Path $TrustedPSModulePath "CimCmdlets\CimCmdlets.psd1")
)
foreach ($module in @($ScheduledTasksModule, $CimCmdletsModule)) {
    if (-not (Test-Path -LiteralPath $module -PathType Leaf)) {
        throw "Trusted ForwardShadow readiness module is missing: $module"
    }
    Import-Module -Name $module -Force -ErrorAction Stop
}

$Root = [IO.Path]::GetFullPath($Root)
$App = [IO.Path]::GetFullPath((Join-Path $Root "app"))
$State = [IO.Path]::GetFullPath((Join-Path $Root "state"))
$ArchiveRoot = [IO.Path]::GetFullPath((Join-Path $Root "archive"))
$Launcher = [IO.Path]::GetFullPath((Join-Path $App "deploy\run_forward_shadow_windows.ps1"))
$WatchdogLauncher = [IO.Path]::GetFullPath((Join-Path $App "deploy\watchdog_windows.ps1"))
$TrustedScript = [IO.Path]::GetFullPath((Join-Path $App "deploy\check_forward_flat_windows.ps1"))
$CurrentScript = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
$RuntimeConfig = [IO.Path]::GetFullPath(
    (Join-Path $App "live_forward\xm_mt5_demo_config.json")
)
$TerminalFile = [IO.Path]::GetFullPath((Join-Path $Root "mt5-terminal.txt"))
$ProbeRequest = [IO.Path]::GetFullPath((Join-Path $Root "broker-probe-request.json"))
$ProbeResultsRoot = [IO.Path]::GetFullPath((Join-Path $Root "broker-probe-results"))
$MainTask = "ForwardShadowXM"
$WatchdogTask = "ForwardShadowWatchdog"
$WatchdogHealth = [IO.Path]::GetFullPath((Join-Path $State "health.json"))
$WatchdogStatus = [IO.Path]::GetFullPath((Join-Path $Root "watchdog_status.json"))
$WindowsPowerShellExe = [IO.Path]::GetFullPath((Join-Path $PSHOME "powershell.exe"))
$IcaclsExe = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "icacls.exe")
)
$SystemSid = "S-1-5-18"
$AdministratorsSid = "S-1-5-32-544"
$Nonce = [Guid]::NewGuid().ToString("N")
$ProbeTransaction = [IO.Path]::GetFullPath((Join-Path $ProbeResultsRoot $Nonce))
$Result = [IO.Path]::GetFullPath((Join-Path $ProbeTransaction "result.json"))
$Diagnostic = [IO.Path]::GetFullPath((Join-Path $ProbeTransaction "diagnostic.json"))
$TerminalConfig = [IO.Path]::GetFullPath(
    (Join-Path $ProbeTransaction "terminal-readonly.ini")
)

function Resolve-ForwardSid {
    param([Parameter(Mandatory = $true)][string]$Identity)
    if ($Identity -match '^S-\d-(?:\d+-)+\d+$') {
        return ([Security.Principal.SecurityIdentifier]::new($Identity)).Value
    }
    return ([Security.Principal.NTAccount]::new($Identity)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
}

function Set-ForwardSystemOwner {
    param([Parameter(Mandatory = $true)][string]$Path)
    $ownerOutput = @(& $IcaclsExe $Path /setowner "*$SystemSid" /Q 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not set SYSTEM owner on ForwardShadow readiness storage: $Path $($ownerOutput -join ' ')"
    }
}

function New-ForwardExactDirectoryAcl {
    param(
        [string]$RunnerSid = "",
        [switch]$RunnerModify
    )
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($sid in @($SystemSid, $AdministratorsSid)) {
        [void]$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            ([Security.Principal.SecurityIdentifier]::new($sid)),
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )))
    }
    if (-not [string]::IsNullOrWhiteSpace($RunnerSid)) {
        $runnerRights = if ($RunnerModify) {
            [Security.AccessControl.FileSystemRights]::Modify
        } else {
            [Security.AccessControl.FileSystemRights]::ReadAndExecute
        }
        [void]$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            ([Security.Principal.SecurityIdentifier]::new($RunnerSid)),
            $runnerRights,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )))
    }
    return $acl
}

function New-ForwardExactFileAcl {
    param([string]$RunnerSid = "")
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($SystemSid, $AdministratorsSid)) {
        [void]$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            ([Security.Principal.SecurityIdentifier]::new($sid)),
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )))
    }
    if (-not [string]::IsNullOrWhiteSpace($RunnerSid)) {
        [void]$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            ([Security.Principal.SecurityIdentifier]::new($RunnerSid)),
            [Security.AccessControl.FileSystemRights]::ReadAndExecute,
            [Security.AccessControl.AccessControlType]::Allow
        )))
    }
    return $acl
}

function Set-ForwardExactDirectoryAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$RunnerSid = "",
        [switch]$RunnerModify
    )
    [IO.Directory]::SetAccessControl(
        $Path,
        (New-ForwardExactDirectoryAcl -RunnerSid $RunnerSid -RunnerModify:$RunnerModify)
    )
    Set-ForwardSystemOwner -Path $Path
}

function Set-ForwardExactFileAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$RunnerSid = ""
    )
    [IO.File]::SetAccessControl($Path, (New-ForwardExactFileAcl -RunnerSid $RunnerSid))
    Set-ForwardSystemOwner -Path $Path
}

function Assert-ForwardExactAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$RunnerSid = "",
        [switch]$Directory,
        [switch]$RunnerModify
    )
    $acl = if ($Directory) {
        [IO.Directory]::GetAccessControl(
            $Path,
            [Security.AccessControl.AccessControlSections]::Owner -bor
                [Security.AccessControl.AccessControlSections]::Access
        )
    } else {
        [IO.File]::GetAccessControl(
            $Path,
            [Security.AccessControl.AccessControlSections]::Owner -bor
                [Security.AccessControl.AccessControlSections]::Access
        )
    }
    if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner(
        [Security.Principal.SecurityIdentifier]
    ).Value -cne $SystemSid) {
        throw "ForwardShadow readiness ACL owner/protection mismatch: $Path"
    }
    $expected = @{
        $SystemSid = [int][Security.AccessControl.FileSystemRights]::FullControl
        $AdministratorsSid = [int][Security.AccessControl.FileSystemRights]::FullControl
    }
    if (-not [string]::IsNullOrWhiteSpace($RunnerSid)) {
        $runnerExpectedRights = if ($RunnerModify) {
            [Security.AccessControl.FileSystemRights]::Modify
        } else {
            [Security.AccessControl.FileSystemRights]::ReadAndExecute
        }
        # FileSystemAccessRule normalizes non-FullControl allow rules by adding
        # Synchronize.  Compare against that effective Windows mask.
        $expected[$RunnerSid] = [int]($runnerExpectedRights -bor
            [Security.AccessControl.FileSystemRights]::Synchronize)
    }
    $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne $expected.Count) {
        throw "ForwardShadow readiness ACL rule count mismatch: $Path"
    }
    $requiredInheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($rule in $rules) {
        $sid = [string]$rule.IdentityReference.Value
        if (
            -not $expected.ContainsKey($sid) -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            [int]$rule.FileSystemRights -ne [int]$expected[$sid] -or
            [bool]$rule.IsInherited -or
            ($Directory -and [int]$rule.InheritanceFlags -ne [int]$requiredInheritance) -or
            (-not $Directory -and $rule.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None) -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None
        ) {
            throw "ForwardShadow readiness ACL contains an unexpected rule: $Path"
        }
    }
}

function Assert-NoForwardReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    foreach ($item in @((Get-Item -LiteralPath $Path -Force)) + @(
        Get-ChildItem -LiteralPath $Path -Recurse -Force
    )) {
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "ForwardShadow readiness storage contains a reparse point: $($item.FullName)"
        }
    }
}

function Protect-ForwardPrivateTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-NoForwardReparsePoint -Path $Path
    $items = @((Get-Item -LiteralPath $Path -Force)) + @(
        Get-ChildItem -LiteralPath $Path -Recurse -Force
    )
    foreach ($item in @($items | Sort-Object { $_.FullName.Length } -Descending)) {
        if ($item.PSIsContainer) {
            Set-ForwardExactDirectoryAcl -Path $item.FullName
        } else {
            Set-ForwardExactFileAcl -Path $item.FullName
        }
    }
    foreach ($item in $items) {
        Assert-ForwardExactAcl -Path $item.FullName -Directory:$item.PSIsContainer
    }
}

function Get-ForwardProcesses {
    return @(Get-CimInstance Win32_Process -ErrorAction Stop)
}

function Get-ForwardProcessOwnerSid {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [switch]$AllowUnresolved
    )
    $owner = $null
    try { $owner = Invoke-CimMethod -InputObject $Process -MethodName GetOwner -ErrorAction Stop }
    catch {
        if ($AllowUnresolved) { return $null }
        throw "Could not prove the owner of process $($Process.ProcessId)."
    }
    if (
        [uint32]$owner.ReturnValue -ne 0 -or
        [string]::IsNullOrWhiteSpace([string]$owner.User)
    ) {
        if ($AllowUnresolved) { return $null }
        throw "Could not prove the owner of process $($Process.ProcessId)."
    }
    $ownerIdentity = if ([string]::IsNullOrWhiteSpace([string]$owner.Domain)) {
        [string]$owner.User
    } else { "{0}\{1}" -f [string]$owner.Domain, [string]$owner.User }
    try { return Resolve-ForwardSid -Identity $ownerIdentity }
    catch {
        if ($AllowUnresolved) { return $null }
        throw "Could not translate the owner of process $($Process.ProcessId)."
    }
}

function Get-ForwardRunnerProcesses {
    param([Parameter(Mandatory = $true)][string]$RunnerSid)
    $matches = New-Object Collections.Generic.List[object]
    foreach ($process in Get-ForwardProcesses) {
        if ((Get-ForwardProcessOwnerSid -Process $process -AllowUnresolved) -ceq $RunnerSid) {
            [void]$matches.Add($process)
        }
    }
    return $matches.ToArray()
}

function Get-ForwardRootPythonProcesses {
    $matches = New-Object Collections.Generic.List[object]
    foreach ($process in Get-ForwardProcesses) {
        if ($process.Name -notlike "python*.exe") { continue }
        $text = "{0}`n{1}" -f [string]$process.ExecutablePath, [string]$process.CommandLine
        if ($text.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            [void]$matches.Add($process)
        }
    }
    return $matches.ToArray()
}

function Get-ForwardTerminalProcesses {
    param(
        [Parameter(Mandatory = $true)][string]$TerminalPath,
        [string]$RunnerSid = ""
    )
    $resolvedTerminal = [IO.Path]::GetFullPath($TerminalPath)
    $matches = New-Object Collections.Generic.List[object]
    foreach ($process in Get-ForwardProcesses) {
        if ([string]::IsNullOrWhiteSpace([string]$process.ExecutablePath)) { continue }
        if (-not [IO.Path]::GetFullPath([string]$process.ExecutablePath).Equals(
            $resolvedTerminal,
            [StringComparison]::OrdinalIgnoreCase
        )) { continue }
        if (
            [string]::IsNullOrWhiteSpace($RunnerSid) -or
            (Get-ForwardProcessOwnerSid -Process $process) -ceq $RunnerSid
        ) {
            [void]$matches.Add($process)
        }
    }
    return $matches.ToArray()
}

function Stop-ForwardRuntimeExact {
    param(
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$TerminalPath
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    do {
        Stop-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
        Stop-ScheduledTask -TaskName $MainTask -ErrorAction SilentlyContinue
        foreach ($process in @((Get-ForwardRootPythonProcesses) + @(
            Get-ForwardTerminalProcesses -TerminalPath $TerminalPath -RunnerSid $RunnerSid
        ))) {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
        }
        $mainState = [string](Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop).State
        $watchdogState = [string](Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop).State
        $rootPython = @(Get-ForwardRootPythonProcesses)
        $runnerProcesses = @(Get-ForwardRunnerProcesses -RunnerSid $RunnerSid)
        if (
            $mainState -notin @("Running", "Queued") -and
            $watchdogState -notin @("Running", "Queued") -and
            $rootPython.Count -eq 0 -and
            $runnerProcesses.Count -eq 0
        ) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "ForwardShadow readiness could not prove tasks, root Python, and runner processes stopped."
}

function New-ForwardLockedRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }
    Set-ForwardExactFileAcl -Path $Path -RunnerSid $RunnerSid
    Assert-ForwardExactAcl -Path $Path -RunnerSid $RunnerSid
    return [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
}

function Assert-ForwardNonAdminLocalRunner {
    param(
        [Parameter(Mandatory = $true)][string]$Identity,
        [Parameter(Mandatory = $true)][string]$Sid
    )
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $translated = [Security.Principal.SecurityIdentifier]::new($Sid).Translate(
        [Security.Principal.NTAccount]
    ).Value
    if ($translated.Split('\')[0] -ine $env:COMPUTERNAME) {
        throw "ForwardShadow task runner must be a local account."
    }
    $context = $null
    $user = $null
    $directGroups = $null
    $authorizationGroups = $null
    try {
        $context = [DirectoryServices.AccountManagement.PrincipalContext]::new(
            [DirectoryServices.AccountManagement.ContextType]::Machine,
            $env:COMPUTERNAME
        )
        $user = [DirectoryServices.AccountManagement.UserPrincipal]::FindByIdentity(
            $context,
            [DirectoryServices.AccountManagement.IdentityType]::Sid,
            $Sid
        )
        if ($null -eq $user -or [bool]$user.Enabled -ne $true) {
            throw "ForwardShadow task runner is not an enabled local account."
        }
        $directGroups = $user.GetGroups()
        foreach ($group in $directGroups) {
            if ($null -eq $group.Sid) { throw "ForwardShadow runner direct group is unresolved." }
            $groupSid = [string]$group.Sid.Value
            if ($groupSid -like "S-1-5-32-*" -and $groupSid -cne "S-1-5-32-545") {
                throw "ForwardShadow runner has a privileged direct group: $groupSid"
            }
        }
        $authorizationGroups = $user.GetAuthorizationGroups()
        foreach ($group in $authorizationGroups) {
            if ($null -eq $group.Sid) { throw "ForwardShadow runner authorization group is unresolved." }
            if ([string]$group.Sid.Value -in @(
                "S-1-5-32-544", "S-1-5-32-547", "S-1-5-32-548",
                "S-1-5-32-549", "S-1-5-32-550", "S-1-5-32-551", "S-1-5-32-552"
            )) { throw "ForwardShadow runner is transitively privileged: $Identity" }
        }
    }
    finally {
        if ($authorizationGroups) { $authorizationGroups.Dispose() }
        if ($directGroups) { $directGroups.Dispose() }
        if ($user) { $user.Dispose() }
        if ($context) { $context.Dispose() }
    }
}

function Assert-ForwardCanonicalTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments,
        [Parameter(Mandatory = $true)][string]$ExpectedUserSid,
        [Parameter(Mandatory = $true)][string]$ExpectedLogonType,
        [Parameter(Mandatory = $true)][string]$ExpectedRunLevel
    )
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $actions = @($task.Actions)
    $triggers = @($task.Triggers)
    $delay = if (
        $triggers.Count -eq 1 -and
        -not [string]::IsNullOrWhiteSpace([string]$triggers[0].Delay)
    ) { [Xml.XmlConvert]::ToTimeSpan([string]$triggers[0].Delay) } else { [TimeSpan]::Zero }
    if (
        $actions.Count -ne 1 -or
        -not [IO.Path]::GetFullPath([string]$actions[0].Execute).Equals(
            $WindowsPowerShellExe,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$actions[0].Arguments -cne $ExpectedArguments -or
        -not [string]::IsNullOrWhiteSpace([string]$actions[0].WorkingDirectory) -or
        (Resolve-ForwardSid -Identity ([string]$task.Principal.UserId)) -cne $ExpectedUserSid -or
        [string]$task.Principal.LogonType -cne $ExpectedLogonType -or
        [string]$task.Principal.RunLevel -cne $ExpectedRunLevel -or
        [int]$task.Settings.RestartCount -ne 3 -or
        [Xml.XmlConvert]::ToTimeSpan([string]$task.Settings.RestartInterval) -ne [TimeSpan]::FromMinutes(1) -or
        [Xml.XmlConvert]::ToTimeSpan([string]$task.Settings.ExecutionTimeLimit) -ne [TimeSpan]::Zero -or
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
        $delay -ne [TimeSpan]::Zero
    ) { throw "$TaskName is not the exact canonical safe task definition." }
    return $task
}

function Assert-ForwardCanonicalTaskPair {
    $expectedMainArguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$Launcher`""
        "-Root `"$Root`""
    ) -join " "
    $expectedWatchdogArguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$WatchdogLauncher`""
        "-MainTaskName `"$MainTask`""
        "-HealthPath `"$WatchdogHealth`""
        "-ProcessPattern `"run_xm_mt5_forward.py`""
        "-StatusPath `"$WatchdogStatus`""
    ) -join " "
    $main = Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $runnerIdentity = [string]$main.Principal.UserId
    $runnerSid = Resolve-ForwardSid -Identity $runnerIdentity
    Assert-ForwardNonAdminLocalRunner -Identity $runnerIdentity -Sid $runnerSid
    $main = Assert-ForwardCanonicalTask `
        -TaskName $MainTask `
        -ExpectedArguments $expectedMainArguments `
        -ExpectedUserSid $runnerSid `
        -ExpectedLogonType "S4U" `
        -ExpectedRunLevel "Limited"
    $watchdog = Assert-ForwardCanonicalTask `
        -TaskName $WatchdogTask `
        -ExpectedArguments $expectedWatchdogArguments `
        -ExpectedUserSid $SystemSid `
        -ExpectedLogonType "ServiceAccount" `
        -ExpectedRunLevel "Highest"
    return [pscustomobject]@{
        Main = $main
        Watchdog = $watchdog
        RunnerIdentity = $runnerIdentity
        RunnerSid = $runnerSid
    }
}

if ($LibraryOnly) { return }

$requestLock = $null
$terminalConfigLock = $null
$runnerSid = $null
$terminal = $null
$succeeded = $false
$stopped = $false
$failure = $null
$cleanupErrors = New-Object Collections.Generic.List[string]
$resultEnvelope = $null
try {
    if (-not $CurrentScript.Equals($TrustedScript, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Run the ForwardShadow readiness check only from the protected app deploy directory."
    }
    foreach ($path in @(
        $App, $ArchiveRoot, $Launcher, $RuntimeConfig, $TerminalFile,
        $ProbeRequest, $ProbeResultsRoot, $ProbeTransaction, $Result
        $TerminalConfig
    )) {
        if (-not $path.StartsWith(
            ($Root.TrimEnd('\') + '\'),
            [StringComparison]::OrdinalIgnoreCase
        )) { throw "Unsafe ForwardShadow readiness path: $path" }
    }
    foreach ($path in @($App, $ArchiveRoot, $Launcher, $RuntimeConfig, $TerminalFile)) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Required ForwardShadow readiness path is missing: $path"
        }
    }
    if (Test-Path -LiteralPath $ProbeRequest) {
        throw "ForwardShadow readiness request already exists."
    }
    $taskBinding = Assert-ForwardCanonicalTaskPair
    $taskDefinition = $taskBinding.Main
    $runnerSid = [string]$taskBinding.RunnerSid
    $originalTaskXml = Export-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $originalWatchdogTaskXml = Export-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
    $runtime = Get-Content -LiteralPath $RuntimeConfig -Raw | ConvertFrom-Json
    if (
        [int]$runtime.account_login -le 0 -or
        [string]::IsNullOrWhiteSpace([string]$runtime.expected_server) -or
        [string]::IsNullOrWhiteSpace([string]$runtime.expected_company)
    ) { throw "ForwardShadow runtime broker identity is incomplete." }
    $terminal = (Get-Content -LiteralPath $TerminalFile -Raw).Trim()
    if (
        [string]::IsNullOrWhiteSpace($terminal) -or
        -not (Test-Path -LiteralPath $terminal -PathType Leaf)
    ) { throw "ForwardShadow terminal setting is missing or invalid." }

    Stop-ForwardRuntimeExact -RunnerSid $runnerSid -TerminalPath $terminal
    $stopped = $true
    if (@(Get-ForwardTerminalProcesses -TerminalPath $terminal).Count -ne 0) {
        throw "ForwardShadow readiness requires every matching terminal process to be stopped."
    }
    if (-not (Test-Path -LiteralPath $ProbeResultsRoot)) {
        [void][IO.Directory]::CreateDirectory($ProbeResultsRoot)
    }
    Protect-ForwardPrivateTree -Path $ProbeResultsRoot
    [void][IO.Directory]::CreateDirectory($ProbeTransaction)
    Set-ForwardExactDirectoryAcl -Path $ProbeTransaction -RunnerSid $runnerSid -RunnerModify
    Assert-ForwardExactAcl -Path $ProbeTransaction -RunnerSid $runnerSid -RunnerModify -Directory
    $terminalConfigText = @'
[Experts]
Enabled=0
AllowLiveTrading=0
AllowDllImport=0
WebRequest=0
'@
    $terminalConfigLock = New-ForwardLockedRequest `
        -Path $TerminalConfig `
        -RunnerSid $runnerSid `
        -Content $terminalConfigText
    $terminalConfigHash = (Get-FileHash `
        -LiteralPath $TerminalConfig `
        -Algorithm SHA256).Hash.ToLowerInvariant()
    $requestJson = [ordered]@{
        schema_version = 1
        mode = "FLAT"
        nonce = $Nonce
        terminal_config_path = $TerminalConfig
        terminal_config_sha256 = $terminalConfigHash
        diagnostic_path = $Diagnostic
    } | ConvertTo-Json -Compress
    $requestLock = New-ForwardLockedRequest `
        -Path $ProbeRequest `
        -RunnerSid $runnerSid `
        -Content $requestJson

    $previousTaskInfo = Get-ScheduledTaskInfo -TaskName $MainTask -ErrorAction Stop
    $previousLastRunTime = $previousTaskInfo.LastRunTime
    $startedAt = [DateTimeOffset]::UtcNow
    Start-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $observedRunnerTerminal = $false
    $deadline = $startedAt.AddMinutes(3)
    do {
        Start-Sleep -Milliseconds 250
        if (@(Get-ForwardTerminalProcesses `
            -TerminalPath $terminal `
            -RunnerSid $runnerSid).Count -ne 0) {
            $observedRunnerTerminal = $true
        }
        $taskState = [string](Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop).State
        $taskInfo = Get-ScheduledTaskInfo -TaskName $MainTask -ErrorAction Stop
        $completed = (
            $taskState -notin @("Running", "Queued") -and
            $taskInfo.LastRunTime -gt $previousLastRunTime
        )
        if ($completed) { break }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    if (-not $completed) { throw "ForwardShadow readiness task timed out." }
    Stop-ForwardRuntimeExact -RunnerSid $runnerSid -TerminalPath $terminal
    if ((Export-ScheduledTask -TaskName $MainTask -ErrorAction Stop).Trim() -cne $originalTaskXml.Trim()) {
        throw "ForwardShadow main task XML changed during readiness proof."
    }
    if ((Export-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop).Trim() -cne $originalWatchdogTaskXml.Trim()) {
        throw "ForwardShadow watchdog task XML changed during readiness proof."
    }
    if (
        [int64]$taskInfo.LastTaskResult -ne 0 -or
        -not $observedRunnerTerminal -or
        -not (Test-Path -LiteralPath $Result -PathType Leaf)
    ) {
        throw "ForwardShadow sealed readiness task failed or lacked runner-owned terminal evidence."
    }
    $requestLock.Dispose()
    $requestLock = $null
    Remove-Item -LiteralPath $ProbeRequest -Force
    Protect-ForwardPrivateTree -Path $ProbeTransaction
    $resultHash = (Get-FileHash -LiteralPath $Result -Algorithm SHA256).Hash.ToLowerInvariant()
    $resultLock = [IO.File]::Open(
        $Result,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        if ((Get-FileHash -LiteralPath $Result -Algorithm SHA256).Hash.ToLowerInvariant() -cne $resultHash) {
            throw "ForwardShadow sealed readiness result changed while read-locked."
        }
        $flat = Get-Content -LiteralPath $Result -Raw | ConvertFrom-Json
        $now = [DateTimeOffset]::UtcNow
        $payloadAge = $now - [DateTimeOffset]::Parse(
            [string]$flat.checked_at_utc
        ).ToUniversalTime()
        $profilePath = [Environment]::ExpandEnvironmentVariables([string](
            Get-ItemProperty -LiteralPath (
                "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$runnerSid"
            ) -Name ProfileImagePath -ErrorAction Stop
        ).ProfileImagePath)
        $expectedDataRoot = [IO.Path]::GetFullPath(
            (Join-Path $profilePath "AppData\Roaming\MetaQuotes\Terminal")
        )
        $actualDataPath = [IO.Path]::GetFullPath([string]$flat.terminal_data_path)
        if (
            [string]$flat.evidence_nonce -cne $Nonce -or
            [string]$flat.profile -cne "forward" -or
            $payloadAge.TotalSeconds -lt -60 -or
            $payloadAge.TotalMinutes -gt 5 -or
            -not [bool]$flat.ready -or
            -not [bool]$flat.flat -or
            -not [bool]$flat.identity_ready -or
            -not [bool]$flat.transport_ready -or
            -not [bool]$flat.identity_checks.windows_profile -or
            [int]$flat.account_login -ne [int]$runtime.account_login -or
            [string]$flat.server -cne [string]$runtime.expected_server -or
            [string]$flat.company -cne [string]$runtime.expected_company -or
            [int]$flat.open_orders -ne 0 -or
            [int]$flat.open_positions -ne 0 -or
            [string]$flat.permission.state -cne "READ_ONLY_PROOF" -or
            [string]$flat.order_transport_preflight.state -cne
                "DISABLED_FOR_READ_ONLY_PROOF" -or
            -not [bool]$flat.transport_preflight_deferred -or
            [bool]$flat.permission_checks.terminal_trade_disabled -ne $true -or
            [bool]$flat.permission_checks.python_trade_api_enabled -ne $true -or
            [bool]$flat.order_transport_preflight.order_send_called -or
            -not $actualDataPath.StartsWith(
                ($expectedDataRoot.TrimEnd('\') + '\'),
                [StringComparison]::OrdinalIgnoreCase
            )
        ) { throw "ForwardShadow broker readiness evidence failed the sealed strict gate." }
        $resultEnvelope = [ordered]@{
            state = "FORWARD_FLAT_PROVEN"
            evidence_path = $Result
            evidence_sha256 = $resultHash
            evidence_nonce = $Nonce
            checked_at_utc = [string]$flat.checked_at_utc
            account_login = [int]$flat.account_login
            server = [string]$flat.server
            company = [string]$flat.company
            terminal_data_path = [string]$flat.terminal_data_path
            open_orders = 0
            open_positions = 0
            task_xml_unchanged = $true
            runner_terminal_observed = $true
            tasks_stopped = $true
        }
    }
    finally { $resultLock.Dispose() }
    $succeeded = $true
}
catch {
    $failure = $_
}
finally {
    try {
        if ($runnerSid -and $terminal) {
            Stop-ForwardRuntimeExact -RunnerSid $runnerSid -TerminalPath $terminal
            $stopped = $true
        }
    }
    catch {
        $stopped = $false
        $cleanupErrors.Add("stopped-state enforcement: $($_.Exception.Message)")
    }
    if ($requestLock) {
        try { $requestLock.Dispose(); $requestLock = $null }
        catch { $cleanupErrors.Add("request lock cleanup: $($_.Exception.Message)") }
    }
    if ($terminalConfigLock) {
        try { $terminalConfigLock.Dispose(); $terminalConfigLock = $null }
        catch { $cleanupErrors.Add("terminal config lock cleanup: $($_.Exception.Message)") }
    }
    if ($stopped -and (Test-Path -LiteralPath $ProbeRequest)) {
        try { Remove-Item -LiteralPath $ProbeRequest -Force }
        catch { $cleanupErrors.Add("request cleanup: $($_.Exception.Message)") }
    }
    if ($stopped -and (Test-Path -LiteralPath $ProbeTransaction -PathType Container)) {
        try {
            Protect-ForwardPrivateTree -Path $ProbeTransaction
            if (-not $succeeded) {
                Remove-Item -LiteralPath $ProbeTransaction -Recurse -Force
            }
        }
        catch { $cleanupErrors.Add("result transaction cleanup: $($_.Exception.Message)") }
    }
    if ($succeeded -and -not $KeepStopped -and $cleanupErrors.Count -eq 0) {
        try {
            Start-ScheduledTask -TaskName $MainTask -ErrorAction Stop
            Start-Sleep -Seconds 2
            Start-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
        }
        catch {
            $cleanupErrors.Add("normal task restart: $($_.Exception.Message)")
            Stop-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
            Stop-ScheduledTask -TaskName $MainTask -ErrorAction SilentlyContinue
        }
    }
    [Environment]::SetEnvironmentVariable(
        "PSModulePath",
        $PreviousPSModulePath,
        "Process"
    )
}

if ($failure) {
    $suffix = if ($cleanupErrors.Count) { "; cleanup: $($cleanupErrors -join '; ')" } else { "" }
    throw "ForwardShadow readiness failed: $($failure.Exception.Message)$suffix"
}
if ($cleanupErrors.Count) {
    throw "ForwardShadow readiness cleanup failed: $($cleanupErrors -join '; ')"
}
$resultEnvelope | ConvertTo-Json -Depth 8 -Compress
