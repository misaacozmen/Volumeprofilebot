[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TargetRunnerIdentity,
    [Parameter(Mandatory = $true)][ValidateRange(1, 2147483647)][int]$ExpectedMt5Login,
    [string]$ExpectedMt5Server = "XMGlobal-MT5 7",
    [string]$TerminalPath = "C:\Program Files\XM MT5\terminal64.exe",
    [string]$PythonPath = "C:\ForwardShadow\venv311\Scripts\python.exe",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$ExpectedTerminalSha256,
    [string]$MainTask = "ForwardShadowXM",
    [string]$WatchdogTask = "ForwardShadowWatchdog"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (
    [string]$PSVersionTable.PSEdition -cne "Desktop" -or
    [int]$PSVersionTable.PSVersion.Major -ne 5
) { throw "ForwardShadow migration probe requires Windows PowerShell 5.1." }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "ForwardShadow migration probe must run elevated."
}

$SystemSid = "S-1-5-18"
$AdministratorsSid = "S-1-5-32-544"
$UsersSid = "S-1-5-32-545"
$WindowsPowerShellExe = [IO.Path]::GetFullPath((Join-Path $PSHOME "powershell.exe"))
$TerminalPath = [IO.Path]::GetFullPath($TerminalPath)
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
$ExpectedTerminalSha256 = $ExpectedTerminalSha256.ToLowerInvariant()
$RunId = [Guid]::NewGuid().ToString("N")
$ScratchBase = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::GetFolderPath("CommonApplicationData")) `
        "ForwardShadowRunnerMigrationProbe")
)
$ScratchRoot = [IO.Path]::GetFullPath((Join-Path $ScratchBase $RunId))
$InputRoot = Join-Path $ScratchRoot "input"
$ReadOnlyOutput = Join-Path $ScratchRoot "read-only-output"
$ProductionOutput = Join-Path $ScratchRoot "production-output"
$ProbeScript = Join-Path $InputRoot "runner-probe.ps1"
$ReadOnlyConfig = Join-Path $InputRoot "terminal-read-only.ini"
$ProductionConfig = Join-Path $InputRoot "terminal-production.ini"
$Sentinel = Join-Path $InputRoot "runner-token-sentinel.txt"
$createdScratchBase = $false
$originalRights = $null
$passwordRotated = $false
$RunnerSid = $null
$targetAccount = $null
$runnerPassword = $null
$productionSnapshots = @{}
$registeredTaskNames = New-Object Collections.Generic.List[string]
$primaryError = $null
$cleanupErrors = New-Object Collections.Generic.List[string]
$evidence = New-Object Collections.Generic.List[object]

if (
    $TargetRunnerIdentity -match '["\r\n]' -or
    $ExpectedMt5Server -notmatch '^[A-Za-z0-9 ._-]{1,64}$' -or
    $TerminalPath -match '"' -or
    $PythonPath -match '"' -or
    $MainTask -match '[\\"\r\n]' -or
    $WatchdogTask -match '[\\"\r\n]'
) { throw "ForwardShadow migration probe received an unsafe parameter." }

$OriginalPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$OriginalPythonEnvironment = @{}
$TrustedPSModulePath = [string]::Join(";", @(
    (Join-Path $PSHOME "Modules"),
    (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\Modules")
))
[Environment]::SetEnvironmentVariable("PSModulePath", $TrustedPSModulePath, "Process")
foreach ($name in @(
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE"
)) {
    $OriginalPythonEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    [Environment]::SetEnvironmentVariable($name, $null, "Process")
}
Import-Module `
    (Join-Path $PSHOME "Modules\ScheduledTasks\ScheduledTasks.psd1") `
    -Force `
    -ErrorAction Stop

function Resolve-ProbeSid {
    param([Parameter(Mandatory = $true)][string]$Identity)
    try {
        return ([Security.Principal.NTAccount]::new($Identity)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch { throw "Could not resolve ForwardShadow runner identity: $Identity" }
}

function Assert-ProbeNonAdminLocalRunner {
    param(
        [Parameter(Mandatory = $true)][string]$Identity,
        [Parameter(Mandatory = $true)][string]$Sid
    )
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $context = $null
    $user = $null
    $authorizationGroups = $null
    $directGroups = $null
    $disposables = New-Object Collections.Generic.List[IDisposable]
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
            throw "Target runner is not an enabled local account."
        }
        $directGroups = $user.GetGroups()
        foreach ($group in $directGroups) {
            if ($group -is [IDisposable]) { [void]$disposables.Add($group) }
            if ($null -eq $group.Sid) { throw "A direct runner group is unresolved." }
            $groupSid = [string]$group.Sid.Value
            if ($groupSid -like "S-1-5-32-*" -and $groupSid -cne $UsersSid) {
                throw "Target runner has a direct privileged built-in group: $groupSid"
            }
        }
        $authorizationGroups = $user.GetAuthorizationGroups()
        $privilegedSids = @(
            "S-1-5-32-544", "S-1-5-32-547", "S-1-5-32-548",
            "S-1-5-32-549", "S-1-5-32-550", "S-1-5-32-551", "S-1-5-32-552"
        )
        foreach ($group in $authorizationGroups) {
            if ($group -is [IDisposable]) { [void]$disposables.Add($group) }
            if ($null -eq $group.Sid) { throw "A runner authorization group is unresolved." }
            if ([string]$group.Sid.Value -in $privilegedSids) {
                throw "Target runner is transitively privileged: $Identity"
            }
        }
    }
    finally {
        foreach ($item in $disposables) { $item.Dispose() }
        if ($authorizationGroups) { $authorizationGroups.Dispose() }
        if ($directGroups) { $directGroups.Dispose() }
        if ($user) { $user.Dispose() }
        if ($context) { $context.Dispose() }
    }
}

function Initialize-ProbeAccountRightsType {
    if ("ForwardShadowMigrationProbe.NativeAccountRights" -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace ForwardShadowMigrationProbe {
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
        private static extern uint LsaOpenPolicy(IntPtr systemName,
            ref LsaObjectAttributes attributes, uint access, out IntPtr policy);
        [DllImport("advapi32.dll")]
        private static extern uint LsaEnumerateAccountRights(IntPtr policy, IntPtr sid,
            out IntPtr rights, out uint count);
        [DllImport("advapi32.dll")]
        private static extern uint LsaAddAccountRights(IntPtr policy, IntPtr sid,
            [In] LsaUnicodeString[] rights, uint count);
        [DllImport("advapi32.dll")]
        private static extern uint LsaRemoveAccountRights(IntPtr policy, IntPtr sid,
            [MarshalAs(UnmanagedType.Bool)] bool all, IntPtr rights, uint count);
        [DllImport("advapi32.dll")]
        private static extern uint LsaNtStatusToWinError(uint status);
        [DllImport("advapi32.dll")]
        private static extern uint LsaFreeMemory(IntPtr memory);
        [DllImport("advapi32.dll")]
        private static extern uint LsaClose(IntPtr handle);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);

        public static string[] Get(string stringSid) {
            IntPtr sid = IntPtr.Zero, policy = IntPtr.Zero, rights = IntPtr.Zero;
            try {
                if (!ConvertStringSidToSid(stringSid, out sid))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                LsaObjectAttributes attributes = new LsaObjectAttributes();
                attributes.Length = (uint)Marshal.SizeOf(typeof(LsaObjectAttributes));
                uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, 0x00000800, out policy);
                if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
                uint count;
                status = LsaEnumerateAccountRights(policy, sid, out rights, out count);
                if (status == 0xC0000034) return new string[0];
                if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
                List<string> result = new List<string>();
                int size = Marshal.SizeOf(typeof(LsaUnicodeString));
                for (uint index = 0; index < count; index++) {
                    IntPtr item = IntPtr.Add(rights, checked((int)index * size));
                    LsaUnicodeString value = (LsaUnicodeString)Marshal.PtrToStructure(
                        item, typeof(LsaUnicodeString));
                    result.Add(Marshal.PtrToStringUni(value.Buffer, value.Length / 2));
                }
                return result.ToArray();
            }
            finally {
                if (rights != IntPtr.Zero) LsaFreeMemory(rights);
                if (policy != IntPtr.Zero) LsaClose(policy);
                if (sid != IntPtr.Zero) LocalFree(sid);
            }
        }

        public static void SetExact(string stringSid, string[] requestedRights) {
            IntPtr sid = IntPtr.Zero, policy = IntPtr.Zero;
            List<IntPtr> buffers = new List<IntPtr>();
            try {
                if (!ConvertStringSidToSid(stringSid, out sid))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                LsaObjectAttributes attributes = new LsaObjectAttributes();
                attributes.Length = (uint)Marshal.SizeOf(typeof(LsaObjectAttributes));
                uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, 0x00000810, out policy);
                if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
                status = LsaRemoveAccountRights(policy, sid, true, IntPtr.Zero, 0);
                if (status != 0 && status != 0xC0000034)
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                if (requestedRights == null || requestedRights.Length == 0) return;
                LsaUnicodeString[] rights = new LsaUnicodeString[requestedRights.Length];
                for (int index = 0; index < requestedRights.Length; index++) {
                    string value = requestedRights[index];
                    IntPtr buffer = Marshal.StringToHGlobalUni(value);
                    buffers.Add(buffer);
                    rights[index].Buffer = buffer;
                    rights[index].Length = checked((ushort)(value.Length * 2));
                    rights[index].MaximumLength = checked((ushort)((value.Length + 1) * 2));
                }
                status = LsaAddAccountRights(policy, sid, rights, (uint)rights.Length);
                if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
            }
            finally {
                foreach (IntPtr buffer in buffers)
                    if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer);
                if (policy != IntPtr.Zero) LsaClose(policy);
                if (sid != IntPtr.Zero) LocalFree(sid);
            }
        }
    }
}
'@
}

function Get-ProbeAccountRights {
    param([Parameter(Mandatory = $true)][string]$Sid)
    Initialize-ProbeAccountRightsType
    return @([ForwardShadowMigrationProbe.NativeAccountRights]::Get($Sid) | Sort-Object -Unique)
}

function Set-ProbeAccountRightsExact {
    param(
        [Parameter(Mandatory = $true)][string]$Sid,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Rights
    )
    Initialize-ProbeAccountRightsType
    [ForwardShadowMigrationProbe.NativeAccountRights]::SetExact($Sid, @($Rights))
}

function Assert-ProbeRightsExact {
    param(
        [Parameter(Mandatory = $true)][string]$Sid,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Expected
    )
    $expectedSorted = @($Expected | Sort-Object -Unique)
    $actual = @(Get-ProbeAccountRights -Sid $Sid)
    if (
        $actual.Count -ne $expectedSorted.Count -or
        [string]::Join("`n", $actual) -cne [string]::Join("`n", $expectedSorted)
    ) { throw "Target runner account-right set is not exact." }
}

function Reset-ProbeRunnerPassword {
    param([Parameter(Mandatory = $true)][string]$Sid)
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
            throw "Target runner disappeared before password rotation."
        }
        $user.SetPassword($plainPassword)
        return ConvertTo-SecureString -String $plainPassword -AsPlainText -Force
    }
    finally {
        $plainPassword = $null
        [Array]::Clear($bytes, 0, $bytes.Length)
        if ($random) { $random.Dispose() }
        if ($user) { $user.Dispose() }
        if ($context) { $context.Dispose() }
    }
}

function New-ProbeAclRule {
    param(
        [Parameter(Mandatory = $true)][string]$Sid,
        [Parameter(Mandatory = $true)][Security.AccessControl.FileSystemRights]$Rights,
        [Parameter(Mandatory = $true)][bool]$Directory
    )
    $inheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else { [Security.AccessControl.InheritanceFlags]::None }
    return [Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($Sid),
        $Rights,
        $inheritance,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
}

function Set-ProbeExactAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateSet("Private", "RunnerRead", "RunnerModify")][string]$Mode,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
        throw "Probe scratch path became a reparse point: $Path"
    }
    $isDirectory = [bool]$item.PSIsContainer
    $acl = if ($isDirectory) {
        New-Object Security.AccessControl.DirectorySecurity
    } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new($AdministratorsSid))
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $acl.AddAccessRule((New-ProbeAclRule -Sid $SystemSid -Rights $full -Directory $isDirectory))
    $acl.AddAccessRule((New-ProbeAclRule -Sid $AdministratorsSid -Rights $full -Directory $isDirectory))
    if ($Mode -eq "RunnerRead") {
        $acl.AddAccessRule((New-ProbeAclRule `
            -Sid $RunnerSid `
            -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute) `
            -Directory $isDirectory))
    }
    elseif ($Mode -eq "RunnerModify") {
        $acl.AddAccessRule((New-ProbeAclRule `
            -Sid $RunnerSid `
            -Rights ([Security.AccessControl.FileSystemRights]::Modify) `
            -Directory $isDirectory))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Seal-ProbeTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Recurse -Force | Sort-Object FullName -Descending)) {
        Set-ProbeExactAcl -Path $item.FullName -Mode Private -RunnerSid $RunnerSid
    }
    Set-ProbeExactAcl -Path $Path -Mode Private -RunnerSid $RunnerSid
}

function Get-ProbeOwnedProcesses {
    param(
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [AllowEmptyString()][string]$ExactExecutable = ""
    )
    $result = @()
    foreach ($process in Get-CimInstance Win32_Process -ErrorAction Stop) {
        if (
            -not [string]::IsNullOrWhiteSpace($ExactExecutable) -and
            (
                [string]::IsNullOrWhiteSpace([string]$process.ExecutablePath) -or
                -not [IO.Path]::GetFullPath([string]$process.ExecutablePath).Equals(
                    [IO.Path]::GetFullPath($ExactExecutable),
                    [StringComparison]::OrdinalIgnoreCase
                )
            )
        ) { continue }
        $owner = $null
        try { $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop }
        catch {
            if (-not [string]::IsNullOrWhiteSpace($ExactExecutable)) {
                throw "Could not resolve the owner of a matching MT5 process."
            }
            continue
        }
        if (
            [uint32]$owner.ReturnValue -ne 0 -or
            [string]::IsNullOrWhiteSpace([string]$owner.User)
        ) {
            if (-not [string]::IsNullOrWhiteSpace($ExactExecutable)) {
                throw "Could not resolve the owner of a matching MT5 process."
            }
            continue
        }
        $ownerIdentity = if ([string]::IsNullOrWhiteSpace([string]$owner.Domain)) {
            [string]$owner.User
        } else { "{0}\{1}" -f [string]$owner.Domain, [string]$owner.User }
        try { $ownerSid = Resolve-ProbeSid -Identity $ownerIdentity }
        catch {
            if (-not [string]::IsNullOrWhiteSpace($ExactExecutable)) {
                throw "Could not translate the owner of a matching MT5 process."
            }
            continue
        }
        if ($ownerSid -ceq $RunnerSid) { $result += $process }
    }
    return $result
}

function Stop-ProbeRunnerTerminals {
    param(
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$Terminal
    )
    foreach ($process in @(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid -ExactExecutable $Terminal)) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
    }
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
    while (
        @(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid -ExactExecutable $Terminal).Count -ne 0 -and
        [DateTimeOffset]::UtcNow -lt $deadline
    ) { Start-Sleep -Milliseconds 200 }
    if (@(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid -ExactExecutable $Terminal).Count -ne 0) {
        throw "Target-runner MT5 process cleanup failed."
    }
}

function New-ProbeTaskService {
    $schedulerType = [Type]::GetTypeFromCLSID(
        [Guid]::Parse("0f87369f-a4e5-4cfc-bd3e-73e6154572dd"),
        $true
    )
    $service = [Activator]::CreateInstance($schedulerType)
    [void]$service.Connect()
    return $service
}

function Register-ProbeS4UTask {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)]$Definition,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity,
        [Parameter(Mandatory = $true)][Security.SecureString]$RunnerPassword
    )
    $pointer = [IntPtr]::Zero
    $plain = $null
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($RunnerPassword)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        return $Folder.RegisterTaskDefinition(
            $TaskName, $Definition, 6, $RunnerIdentity, $plain, 2, $null
        )
    }
    finally {
        $plain = $null
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

function Invoke-ProbeMode {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("ReadOnly", "Production")][string]$Mode,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$OutputRoot,
        [Parameter(Mandatory = $true)][string]$RunnerIdentity,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][Security.SecureString]$RunnerPassword,
        [Parameter(Mandatory = $true)][string]$ExpectedProfileRoot
    )
    $taskName = "ForwardShadowMigrationProbe.$Mode.$RunId"
    $resultPath = Join-Path $OutputRoot "result.json"
    $configHash = (Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $arguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$ProbeScript`""
        "-Mode $Mode"
        "-Python `"$PythonPath`""
        "-Terminal `"$TerminalPath`""
        "-Config `"$ConfigPath`""
        "-ExpectedConfigSha256 $configHash"
        "-Sentinel `"$Sentinel`""
        "-Result `"$resultPath`""
        "-EvidenceNonce $RunId"
        "-ExpectedLogin $ExpectedMt5Login"
        "-ExpectedServer `"$ExpectedMt5Server`""
    ) -join " "
    $service = $null
    $folder = $null
    $registered = $null
    $observedTerminal = $false
    $completed = $false
    try {
        if (@(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid).Count -ne 0) {
            throw "Target runner has a process before the $Mode scratch probe."
        }
        $service = New-ProbeTaskService
        $folder = $service.GetFolder("\")
        try {
            $null = $folder.GetTask("\$taskName")
            throw "Scratch task already exists."
        }
        catch {
            if ([int]$_.Exception.HResult -ne -2147024894) { throw }
        }
        $definition = $service.NewTask(0)
        $definition.RegistrationInfo.Description = "ForwardShadow disposable runner migration probe"
        $definition.Principal.UserId = $RunnerIdentity
        $definition.Principal.LogonType = 2
        $definition.Principal.RunLevel = 0
        $definition.Settings.Enabled = $true
        $definition.Settings.AllowDemandStart = $true
        $definition.Settings.ExecutionTimeLimit = "PT3M"
        $definition.Settings.MultipleInstances = 2
        $definition.Settings.RunOnlyIfIdle = $false
        $definition.Settings.DisallowStartIfOnBatteries = $false
        $definition.Settings.StopIfGoingOnBatteries = $false
        $definition.Settings.RunOnlyIfNetworkAvailable = $false
        $action = $definition.Actions.Create(0)
        $action.Path = $WindowsPowerShellExe
        $action.Arguments = $arguments
        $action.WorkingDirectory = ""
        $registered = Register-ProbeS4UTask `
            -Folder $folder `
            -TaskName $taskName `
            -Definition $definition `
            -RunnerIdentity $RunnerIdentity `
            -RunnerPassword $RunnerPassword
        [void]$registeredTaskNames.Add($taskName)
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        if (
            @($task.Actions).Count -ne 1 -or
            [string]$task.Actions[0].Execute -cne $WindowsPowerShellExe -or
            [string]$task.Actions[0].Arguments -cne $arguments -or
            (Resolve-ProbeSid -Identity ([string]$task.Principal.UserId)) -cne $RunnerSid -or
            [string]$task.Principal.LogonType -cne "S4U" -or
            [string]$task.Principal.RunLevel -cne "Limited"
        ) { throw "Scratch task is not exact S4U/Limited with the sealed action." }
        [xml]$xml = Export-ScheduledTask -TaskName $taskName -ErrorAction Stop
        $logonNodes = @($xml.SelectNodes(
            "/*[local-name()='Task']/*[local-name()='Principals']/*[local-name()='Principal']/*[local-name()='LogonType']"
        ))
        $runLevelNodes = @($xml.SelectNodes(
            "/*[local-name()='Task']/*[local-name()='Principals']/*[local-name()='Principal']/*[local-name()='RunLevel']"
        ))
        if (
            $logonNodes.Count -ne 1 -or
            [string]$logonNodes[0].InnerText -cne "S4U" -or
            $runLevelNodes.Count -gt 1 -or
            (
                $runLevelNodes.Count -eq 1 -and
                [string]$runLevelNodes[0].InnerText -cne "LeastPrivilege"
            )
        ) {
            throw "Scratch task XML did not prove S4U/LeastPrivilege."
        }
        $startedAt = [DateTimeOffset]::UtcNow
        [void]$registered.Run($null)
        $deadline = $startedAt.AddSeconds(150)
        do {
            Start-Sleep -Milliseconds 250
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
            $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
            if (@(Get-ProbeOwnedProcesses `
                -RunnerSid $RunnerSid `
                -ExactExecutable $TerminalPath).Count -ne 0) {
                $observedTerminal = $true
            }
            $completed = (
                [string]$task.State -notin @("Running", "Queued") -and
                [int64]$info.LastTaskResult -eq 0 -and
                (Test-Path -LiteralPath $resultPath -PathType Leaf)
            )
            if ($completed) { break }
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        if (-not $completed) { throw "$Mode scratch task timed out." }
        if ([int64]$info.LastTaskResult -ne 0) {
            throw "$Mode scratch task failed: result=$($info.LastTaskResult)"
        }
        if (-not $observedTerminal) { throw "$Mode did not expose a target-owned MT5 process." }
        Stop-ProbeRunnerTerminals -RunnerSid $RunnerSid -Terminal $TerminalPath
        $processDeadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
        while (
            @(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid).Count -ne 0 -and
            [DateTimeOffset]::UtcNow -lt $processDeadline
        ) { Start-Sleep -Milliseconds 200 }
        if (@(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid).Count -ne 0) {
            throw "$Mode left a target-runner process behind."
        }
        $folder.DeleteTask($taskName, 0)
        [void]$registeredTaskNames.Remove($taskName)
        Seal-ProbeTree -Path $OutputRoot -RunnerSid $RunnerSid
        if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
            throw "$Mode result is missing."
        }
        $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        $expectedTradeAllowed = $Mode -ceq "Production"
        $expectedApiDisabled = $false
        $actualDataPath = [IO.Path]::GetFullPath([string]$result.data_path)
        $resultAge = [DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse(
            [string]$result.checked_at_utc
        ).ToUniversalTime()
        if (
            [int]$result.schema_version -ne 1 -or
            [string]$result.evidence_nonce -cne $RunId -or
            [string]$result.mode -cne $Mode -or
            $resultAge.TotalSeconds -lt -60 -or
            $resultAge.TotalMinutes -gt 5 -or
            [int64]$result.login -ne $ExpectedMt5Login -or
            [string]$result.server -cne $ExpectedMt5Server -or
            [bool]$result.connected -ne $true -or
            [bool]$result.demo_verified -ne $true -or
            [int]$result.orders -ne 0 -or
            [int]$result.positions -ne 0 -or
            [bool]$result.order_send_called -ne $false -or
            [bool]$result.sentinel_write_denied -ne $true -or
            [bool]$result.sentinel_delete_denied -ne $true -or
            [bool]$result.account_trade_allowed -ne $true -or
            [bool]$result.account_trade_expert -ne $true -or
            [bool]$result.terminal_trade_allowed -ne $expectedTradeAllowed -or
            [bool]$result.terminal_tradeapi_disabled -ne $expectedApiDisabled -or
            -not $actualDataPath.StartsWith(
                $ExpectedProfileRoot.TrimEnd('\') + '\',
                [StringComparison]::OrdinalIgnoreCase
            )
        ) { throw "$Mode broker/ACL evidence failed exact validation." }
        return [pscustomobject]@{
            mode = $Mode
            task_logon_type = "S4U"
            task_run_level = "Limited"
            owned_terminal_observed = $true
            data_path = $actualDataPath
            login = [int64]$result.login
            server = [string]$result.server
            demo_verified = [bool]$result.demo_verified
            connected = [bool]$result.connected
            account_trade_allowed = [bool]$result.account_trade_allowed
            account_trade_expert = [bool]$result.account_trade_expert
            terminal_trade_allowed = [bool]$result.terminal_trade_allowed
            terminal_tradeapi_disabled = [bool]$result.terminal_tradeapi_disabled
            orders = [int]$result.orders
            positions = [int]$result.positions
            order_send_called = [bool]$result.order_send_called
            sentinel_write_denied = [bool]$result.sentinel_write_denied
            sentinel_delete_denied = [bool]$result.sentinel_delete_denied
            result_sha256 = (Get-FileHash -LiteralPath $resultPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    catch {
        $modeError = $_
        $safeTaskState = $null
        $safeLastTaskResult = $null
        $safeLastRunTime = $null
        try {
            $safeTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
            $safeTaskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
            $safeTaskState = [string]$safeTask.State
            $safeLastTaskResult = [int64]$safeTaskInfo.LastTaskResult
            $safeLastRunTime = $safeTaskInfo.LastRunTime.ToUniversalTime().ToString("o")
        } catch {}
        Write-Warning (([ordered]@{
            event = "FORWARD_SHADOW_SCRATCH_PROBE_FAILURE"
            mode = $Mode
            task = $taskName
            task_state = $safeTaskState
            last_task_result = $safeLastTaskResult
            last_run_time_utc = $safeLastRunTime
            owned_terminal_observed = $observedTerminal
            result_exists = (Test-Path -LiteralPath $resultPath -PathType Leaf)
            error = $modeError.Exception.Message
        } | ConvertTo-Json -Compress))
        throw $modeError
    }
    finally {
        if ($registered) { try { $registered.Stop(0) } catch {} }
        if ($folder -and $registeredTaskNames.Contains($taskName)) {
            try {
                $folder.DeleteTask($taskName, 0)
                [void]$registeredTaskNames.Remove($taskName)
            } catch {}
        }
        Stop-ProbeRunnerTerminals -RunnerSid $RunnerSid -Terminal $TerminalPath
    }
}

$runnerProbeScript = @'
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet("ReadOnly", "Production")][string]$Mode,
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Terminal,
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$ExpectedConfigSha256,
    [Parameter(Mandatory = $true)][string]$Sentinel,
    [Parameter(Mandatory = $true)][string]$Result,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{32}$')][string]$EvidenceNonce,
    [Parameter(Mandatory = $true)][int]$ExpectedLogin,
    [Parameter(Mandatory = $true)][string]$ExpectedServer
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
foreach ($name in @("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE")) {
    [Environment]::SetEnvironmentVariable($name, $null, "Process")
}
if ((Get-FileHash -LiteralPath $Config -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedConfigSha256) {
    throw "Scratch terminal config hash mismatch."
}
$writeDenied = $false
$deleteDenied = $false
try {
    $stream = [IO.File]::Open($Sentinel, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $stream.Dispose()
} catch [UnauthorizedAccessException] { $writeDenied = $true }
try { [IO.File]::Delete($Sentinel) }
catch [UnauthorizedAccessException] { $deleteDenied = $true }
if (-not $writeDenied -or -not $deleteDenied -or -not (Test-Path -LiteralPath $Sentinel -PathType Leaf)) {
    throw "Target S4U token can mutate the protected sentinel."
}
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $Terminal
$startInfo.Arguments = "/config:`"$Config`""
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$terminalProcess = $null
$terminalProcess = [Diagnostics.Process]::Start($startInfo)
try {
    Start-Sleep -Seconds 2
    $pythonProbe = @"
import json
import os
import sys
import MetaTrader5 as mt5

terminal, expected_login, expected_server, expected_mode = sys.argv[1:5]
initialized = False
try:
    if not mt5.initialize(path=terminal, timeout=30000):
        raise RuntimeError(f"initialize failed: {mt5.last_error()!r}")
    initialized = True
    terminal_info = mt5.terminal_info()
    account_info = mt5.account_info()
    orders = mt5.orders_get()
    positions = mt5.positions_get()
    if terminal_info is None or not bool(terminal_info.connected):
        raise RuntimeError(f"terminal is not connected: {mt5.last_error()!r}")
    if account_info is None:
        raise RuntimeError(f"account_info failed: {mt5.last_error()!r}")
    if int(account_info.login) != int(expected_login):
        raise RuntimeError("unexpected login")
    if str(account_info.server) != expected_server:
        raise RuntimeError("unexpected server")
    if int(account_info.trade_mode) != int(mt5.ACCOUNT_TRADE_MODE_DEMO):
        raise RuntimeError("account is not demo-only")
    if not bool(account_info.trade_allowed) or not bool(account_info.trade_expert):
        raise RuntimeError("account does not permit automated demo trading")
    profile_root = os.path.normcase(os.path.abspath(os.path.join(
        os.environ["APPDATA"], "MetaQuotes", "Terminal"
    )))
    data_path = os.path.normcase(os.path.abspath(str(terminal_info.data_path)))
    if os.path.commonpath((profile_root, data_path)) != profile_root:
        raise RuntimeError("terminal attached to another Windows profile")
    if expected_mode == "ReadOnly":
        if bool(terminal_info.trade_allowed) or bool(terminal_info.tradeapi_disabled):
            raise RuntimeError("read-only terminal controls are not active")
    elif expected_mode == "Production":
        if not bool(terminal_info.trade_allowed) or bool(terminal_info.tradeapi_disabled):
            raise RuntimeError("production terminal controls were not restored")
    else:
        raise RuntimeError("invalid probe mode")
    if orders is None or positions is None:
        raise RuntimeError(f"orders/positions read failed: {mt5.last_error()!r}")
    print(json.dumps({
        "schema_version": 1,
        "mode": expected_mode,
        "connected": True,
        "login": int(account_info.login),
        "server": str(account_info.server),
        "demo_verified": True,
        "data_path": str(terminal_info.data_path),
        "account_trade_allowed": bool(account_info.trade_allowed),
        "account_trade_expert": bool(account_info.trade_expert),
        "terminal_trade_allowed": bool(terminal_info.trade_allowed),
        "terminal_tradeapi_disabled": bool(terminal_info.tradeapi_disabled),
        "orders": len(orders),
        "positions": len(positions),
        "order_send_called": False,
    }, sort_keys=True))
finally:
    if initialized:
        mt5.shutdown()
"@
    $output = @($pythonProbe | & $Python -I -E -B - $Terminal ([string]$ExpectedLogin) $ExpectedServer $Mode 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Broker probe failed: $($output -join ' ')" }
    $line = @($output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })[-1]
    $broker = ([string]$line) | ConvertFrom-Json
    $payload = [ordered]@{
        schema_version = 1
        evidence_nonce = $EvidenceNonce
        mode = $Mode
        connected = [bool]$broker.connected
        login = [int64]$broker.login
        server = [string]$broker.server
        demo_verified = [bool]$broker.demo_verified
        data_path = [string]$broker.data_path
        account_trade_allowed = [bool]$broker.account_trade_allowed
        account_trade_expert = [bool]$broker.account_trade_expert
        terminal_trade_allowed = [bool]$broker.terminal_trade_allowed
        terminal_tradeapi_disabled = [bool]$broker.terminal_tradeapi_disabled
        orders = [int]$broker.orders
        positions = [int]$broker.positions
        order_send_called = [bool]$broker.order_send_called
        sentinel_write_denied = $writeDenied
        sentinel_delete_denied = $deleteDenied
        checked_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    }
    $json = $payload | ConvertTo-Json -Depth 4
    $temporary = "$Result.tmp"
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temporary, $json, $utf8)
    [IO.File]::Move($temporary, $Result)
}
finally {
    if ($terminalProcess -and -not $terminalProcess.HasExited) {
        try { $terminalProcess.Kill() } catch {}
    }
}
'@

try {
    $RunnerSid = Resolve-ProbeSid -Identity $TargetRunnerIdentity
    $targetAccount = [Security.Principal.SecurityIdentifier]::new($RunnerSid).Translate(
        [Security.Principal.NTAccount]
    ).Value
    if ($targetAccount.Split('\')[0] -ine $env:COMPUTERNAME) {
        throw "Target runner must be a local account on this host."
    }
    Assert-ProbeNonAdminLocalRunner -Identity $TargetRunnerIdentity -Sid $RunnerSid
    foreach ($path in @($TerminalPath, $PythonPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required probe executable is missing: $path"
        }
        if ((Get-Item -LiteralPath $path -Force).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "Probe executable cannot be a reparse point: $path"
        }
    }
    if ((Get-FileHash -LiteralPath $TerminalPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedTerminalSha256) {
        throw "Terminal SHA256 does not match the pinned live audit."
    }
    if (@(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid).Count -ne 0) {
        throw "Target runner has an active process before the migration probe."
    }
    foreach ($taskName in @($MainTask, $WatchdogTask)) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        if ([string]$task.State -in @("Running", "Queued")) {
            throw "Production task must already be stopped: $taskName"
        }
        $productionSnapshots[$taskName] = [pscustomobject]@{
            xml = (Export-ScheduledTask -TaskName $taskName -ErrorAction Stop).Trim()
            state = [string]$task.State
        }
    }
    $profileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$RunnerSid"
    $profilePath = [Environment]::ExpandEnvironmentVariables(
        [string](Get-ItemProperty -LiteralPath $profileKey -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath
    )
    $ExpectedProfileRoot = [IO.Path]::GetFullPath(
        (Join-Path $profilePath "AppData\Roaming\MetaQuotes\Terminal")
    )
    if (-not (Test-Path -LiteralPath $ExpectedProfileRoot -PathType Container)) {
        throw "Target runner MT5 profile is missing."
    }

    if (-not (Test-Path -LiteralPath $ScratchBase)) {
        [void][IO.Directory]::CreateDirectory($ScratchBase)
        $createdScratchBase = $true
    }
    elseif (
        -not (Test-Path -LiteralPath $ScratchBase -PathType Container) -or
        (Get-Item -LiteralPath $ScratchBase -Force).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)
    ) { throw "Scratch base is not a safe directory." }
    Set-ProbeExactAcl -Path $ScratchBase -Mode Private -RunnerSid $RunnerSid
    if (Test-Path -LiteralPath $ScratchRoot) { throw "Unique scratch root already exists." }
    [void][IO.Directory]::CreateDirectory($ScratchRoot)
    Set-ProbeExactAcl -Path $ScratchRoot -Mode Private -RunnerSid $RunnerSid
    foreach ($path in @($InputRoot, $ReadOnlyOutput, $ProductionOutput)) {
        [void][IO.Directory]::CreateDirectory($path)
    }
    Set-ProbeExactAcl -Path $InputRoot -Mode Private -RunnerSid $RunnerSid
    Set-ProbeExactAcl -Path $ReadOnlyOutput -Mode RunnerModify -RunnerSid $RunnerSid
    Set-ProbeExactAcl -Path $ProductionOutput -Mode RunnerModify -RunnerSid $RunnerSid
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($ProbeScript, $runnerProbeScript, $utf8)
    [IO.File]::WriteAllText(
        $ReadOnlyConfig,
        "[Experts]`r`nEnabled=0`r`nAllowLiveTrading=0`r`nAllowDllImport=0`r`nWebRequest=0`r`n",
        $utf8
    )
    [IO.File]::WriteAllText(
        $ProductionConfig,
        "[Experts]`r`nEnabled=1`r`nAllowLiveTrading=1`r`nAllowDllImport=0`r`nWebRequest=0`r`n",
        $utf8
    )
    [IO.File]::WriteAllText($Sentinel, "ForwardShadow protected token sentinel`r`n", $utf8)
    foreach ($path in @($ProbeScript, $ReadOnlyConfig, $ProductionConfig, $Sentinel)) {
        Set-ProbeExactAcl -Path $path -Mode RunnerRead -RunnerSid $RunnerSid
    }
    Set-ProbeExactAcl -Path $InputRoot -Mode RunnerRead -RunnerSid $RunnerSid
    Set-ProbeExactAcl -Path $ScratchRoot -Mode RunnerRead -RunnerSid $RunnerSid

    $originalRights = @(Get-ProbeAccountRights -Sid $RunnerSid)
    $requiredRights = @(
        "SeBatchLogonRight",
        "SeDenyInteractiveLogonRight",
        "SeDenyNetworkLogonRight",
        "SeDenyRemoteInteractiveLogonRight",
        "SeDenyServiceLogonRight"
    ) | Sort-Object
    Set-ProbeAccountRightsExact -Sid $RunnerSid -Rights $requiredRights
    Assert-ProbeRightsExact -Sid $RunnerSid -Expected $requiredRights
    $runnerPassword = Reset-ProbeRunnerPassword -Sid $RunnerSid
    $passwordRotated = $true

    [void]$evidence.Add((Invoke-ProbeMode `
        -Mode ReadOnly `
        -ConfigPath $ReadOnlyConfig `
        -OutputRoot $ReadOnlyOutput `
        -RunnerIdentity $targetAccount `
        -RunnerSid $RunnerSid `
        -RunnerPassword $runnerPassword `
        -ExpectedProfileRoot $ExpectedProfileRoot))
    [void]$evidence.Add((Invoke-ProbeMode `
        -Mode Production `
        -ConfigPath $ProductionConfig `
        -OutputRoot $ProductionOutput `
        -RunnerIdentity $targetAccount `
        -RunnerSid $RunnerSid `
        -RunnerPassword $runnerPassword `
        -ExpectedProfileRoot $ExpectedProfileRoot))
}
catch { $primaryError = $_ }
finally {
    try {
        $service = New-ProbeTaskService
        $folder = $service.GetFolder("\")
        foreach ($taskName in @($registeredTaskNames)) {
            try { $folder.GetTask("\$taskName").Stop(0) } catch {}
            try { $folder.DeleteTask($taskName, 0) } catch {}
        }
        foreach ($taskName in @($registeredTaskNames)) {
            if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
                throw "Scratch task cleanup could not be verified: $taskName"
            }
        }
    } catch { [void]$cleanupErrors.Add("task cleanup: $($_.Exception.Message)") }
    if ($null -ne $RunnerSid) {
        try { Stop-ProbeRunnerTerminals -RunnerSid $RunnerSid -Terminal $TerminalPath }
        catch { [void]$cleanupErrors.Add("terminal cleanup: $($_.Exception.Message)") }
        try {
            if (@(Get-ProbeOwnedProcesses -RunnerSid $RunnerSid).Count -ne 0) {
                throw "Target runner still owns a process after scratch cleanup."
            }
        } catch { [void]$cleanupErrors.Add("process cleanup: $($_.Exception.Message)") }
        if ($null -ne $originalRights) {
            try {
                Set-ProbeAccountRightsExact -Sid $RunnerSid -Rights $originalRights
                Assert-ProbeRightsExact -Sid $RunnerSid -Expected $originalRights
            } catch { [void]$cleanupErrors.Add("account-right restore: $($_.Exception.Message)") }
        }
    }
    try {
        if (Test-Path -LiteralPath $ScratchRoot) {
            $expectedPrefix = $ScratchBase.TrimEnd('\') + '\'
            if (
                -not $ScratchRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
                [IO.Path]::GetFileName($ScratchRoot) -cnotmatch '^[a-f0-9]{32}$'
            ) { throw "Refusing unsafe scratch cleanup target." }
            if ($null -ne $RunnerSid) { Seal-ProbeTree -Path $ScratchRoot -RunnerSid $RunnerSid }
            Remove-Item -LiteralPath $ScratchRoot -Recurse -Force
            if (Test-Path -LiteralPath $ScratchRoot) { throw "Scratch root cleanup failed." }
        }
        if ($createdScratchBase -and (Test-Path -LiteralPath $ScratchBase)) {
            if (@(Get-ChildItem -LiteralPath $ScratchBase -Force).Count -eq 0) {
                Remove-Item -LiteralPath $ScratchBase -Force
            }
        }
    } catch { [void]$cleanupErrors.Add("scratch cleanup: $($_.Exception.Message)") }
    foreach ($taskName in @($productionSnapshots.Keys)) {
        try {
            $current = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
            $currentXml = (Export-ScheduledTask -TaskName $taskName -ErrorAction Stop).Trim()
            if (
                $currentXml -cne [string]$productionSnapshots[$taskName].xml -or
                [string]$current.State -cne [string]$productionSnapshots[$taskName].state
            ) { throw "Production task changed during scratch probe: $taskName" }
        } catch { [void]$cleanupErrors.Add("production-task verification: $($_.Exception.Message)") }
    }
    [Environment]::SetEnvironmentVariable("PSModulePath", $OriginalPSModulePath, "Process")
    foreach ($name in $OriginalPythonEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name,
            [string]$OriginalPythonEnvironment[$name],
            "Process"
        )
    }
}

if ($cleanupErrors.Count -ne 0) {
    throw "ForwardShadow migration probe cleanup/immutability failure: $([string]::Join('; ', $cleanupErrors))"
}
if ($primaryError) { throw $primaryError }
if ($evidence.Count -ne 2) { throw "ForwardShadow migration probe evidence is incomplete." }

[pscustomobject]@{
    status = "PASS"
    runner_identity = $targetAccount
    runner_sid = $RunnerSid
    password_rotated_in_memory = $passwordRotated
    original_account_rights_restored = $true
    production_tasks_unchanged = $true
    scratch_cleaned = $true
    read_only = $evidence[0]
    production_restore = $evidence[1]
} | ConvertTo-Json -Depth 8
