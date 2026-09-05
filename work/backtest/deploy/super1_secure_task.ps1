$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$script:Super1RuntimeContractValues = Assert-Super1RuntimeContract
$script:Super1SecureIcaclsExe = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "icacls.exe")
)
$script:Super1SecurePowerShellExe = [IO.Path]::GetFullPath((Join-Path $PSHOME "powershell.exe"))
foreach ($trustedExecutable in @($script:Super1SecureIcaclsExe, $script:Super1SecurePowerShellExe)) {
    if (-not (Test-Path -LiteralPath $trustedExecutable -PathType Leaf)) {
        throw "Trusted Windows executable is missing: $trustedExecutable"
    }
}

function Get-Super1SecureSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha.Dispose()
    }
}

function Get-Super1SecureTaskXml {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    [xml]$xml = [string](Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop)
    return $xml.OuterXml
}

function Get-Super1SecurePythonProcesses {
    param([Parameter(Mandatory = $true)][string]$Root)
    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $venvRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot "venv311"))
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $name = [string]$_.Name
                $commandLine = [string]$_.CommandLine
                $executable = [string]$_.ExecutablePath
                $isPython = $name -ieq "python.exe" -or $name -ieq "pythonw.exe"
                $belongsToSuper1 = (
                    (-not [string]::IsNullOrWhiteSpace($commandLine) -and
                        $commandLine.IndexOf($resolvedRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
                    (-not [string]::IsNullOrWhiteSpace($executable) -and
                        ($executable.Equals($venvRoot, [StringComparison]::OrdinalIgnoreCase) -or
                            $executable.StartsWith(
                                ($venvRoot + [IO.Path]::DirectorySeparatorChar),
                                [StringComparison]::OrdinalIgnoreCase
                            )))
                )
                $isPython -and $belongsToSuper1
            }
    )
}

function Get-Super1SecureTerminalProcesses {
    param([Parameter(Mandatory = $true)][string]$Root)
    $terminal = [IO.Path]::GetFullPath((Get-Super1RuntimePath -Name terminal))
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

function Get-Super1SecureProcessOwnerSid {
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
    try { return Get-Super1SecurePrincipalSid -Identity $identity }
    catch {
        if ($AllowUnresolved) { return $null }
        throw "Could not translate the owner of process $($Process.ProcessId)."
    }
}

function Get-Super1SecureRunnerProcesses {
    param([Parameter(Mandatory = $true)][string]$RunnerSid)
    $matches = New-Object Collections.Generic.List[object]
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ((Get-Super1SecureProcessOwnerSid `
                -Process $process `
                -AllowUnresolved) -ceq $RunnerSid) {
            $matches.Add([pscustomobject]@{
                ProcessId = [int]$process.ProcessId
                Name = [string]$process.Name
                ExecutablePath = [string]$process.ExecutablePath
                CommandLine = [string]$process.CommandLine
            })
        }
    }
    return $matches.ToArray()
}

function Get-Super1SecureUnexpectedRunnerProcesses {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [switch]$PreserveTerminal
    )
    $terminal = [IO.Path]::GetFullPath((Get-Super1RuntimePath -Name terminal))
    return @(Get-Super1SecureRunnerProcesses -RunnerSid $RunnerSid | Where-Object {
        if (-not $PreserveTerminal) { return $true }
        if ([string]::IsNullOrWhiteSpace([string]$_.ExecutablePath)) { return $true }
        return -not [IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
            $terminal,
            [StringComparison]::OrdinalIgnoreCase
        )
    })
}

function Assert-Super1SecureStopped {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MainTask,
        [Parameter(Mandatory = $true)][string]$WatchdogTask,
        [switch]$PreserveTerminal,
        [int]$TimeoutSeconds = 30
    )
    $runnerSid = Get-Super1SecurePrincipalSid `
        -Identity "$env:COMPUTERNAME\Super1Runner"
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $mainState = [string](Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop).State
        $watchdogState = [string](Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop).State
        $processes = @(Get-Super1SecurePythonProcesses -Root $Root)
        $terminalProcesses = @(Get-Super1SecureTerminalProcesses -Root $Root)
        $terminalOwnerMismatch = @($terminalProcesses | Where-Object {
            [string]$_.OwnerSid -cne $runnerSid
        })
        $unexpectedRunnerProcesses = @(Get-Super1SecureUnexpectedRunnerProcesses `
            -Root $Root `
            -RunnerSid $runnerSid `
            -PreserveTerminal:$PreserveTerminal)
        if (
            $mainState -notin @("Running", "Queued") -and
            $watchdogState -notin @("Running", "Queued") -and
            $processes.Count -eq 0 -and
            ((-not $PreserveTerminal -and $terminalProcesses.Count -eq 0) -or
                ($PreserveTerminal -and $terminalProcesses.Count -le 1)) -and
            $terminalOwnerMismatch.Count -eq 0 -and
            $unexpectedRunnerProcesses.Count -eq 0
        ) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    $pids = @($processes | ForEach-Object { [string]$_.ProcessId }) -join ","
    $terminalPids = @($terminalProcesses | ForEach-Object { [string]$_.ProcessId }) -join ","
    $runnerPids = @($unexpectedRunnerProcesses | ForEach-Object {
        "$($_.ProcessId):$($_.Name)"
    }) -join ","
    throw "Super1 stopped-state gate failed: main=$mainState watchdog=$watchdogState python_pids=$pids terminal_pids=$terminalPids unexpected_runner_pids=$runnerPids"
}

function Stop-Super1SecureRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MainTask,
        [Parameter(Mandatory = $true)][string]$WatchdogTask,
        [switch]$PreserveTerminal
    )
    $runnerIdentity = [string](
        Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    ).Principal.UserId
    $runnerSid = Get-Super1SecurePrincipalSid -Identity $runnerIdentity
    Stop-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $MainTask -ErrorAction SilentlyContinue
    try {
        Assert-Super1SecureStopped `
            -Root $Root `
            -MainTask $MainTask `
            -WatchdogTask $WatchdogTask `
            -PreserveTerminal:$PreserveTerminal `
            -TimeoutSeconds 5
        return
    }
    catch {
        $gracefulFailure = $_
    }
    if ($PreserveTerminal -and @(Get-Super1SecureTerminalProcesses -Root $Root).Count -gt 1) {
        throw "Refusing to choose among multiple canonical Super1 terminal processes."
    }
    foreach ($process in @(Get-Super1SecurePythonProcesses -Root $Root)) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if (-not $PreserveTerminal) {
        foreach ($process in @(Get-Super1SecureTerminalProcesses -Root $Root)) {
            if ([string]$process.OwnerSid -cne $runnerSid) {
                throw "Refusing to stop canonical Super1 terminal owned by unexpected SID $($process.OwnerSid)."
            }
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
    for ($pass = 0; $pass -lt 3; $pass++) {
        $unexpectedRunnerProcesses = @(Get-Super1SecureUnexpectedRunnerProcesses `
            -Root $Root `
            -RunnerSid $runnerSid `
            -PreserveTerminal:$PreserveTerminal)
        foreach ($process in $unexpectedRunnerProcesses) {
            Stop-Process `
                -Id ([int]$process.ProcessId) `
                -Force `
                -ErrorAction SilentlyContinue
        }
        if ($unexpectedRunnerProcesses.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    }
    Stop-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $MainTask -ErrorAction SilentlyContinue
    try {
        Assert-Super1SecureStopped `
            -Root $Root `
            -MainTask $MainTask `
            -WatchdogTask $WatchdogTask `
            -PreserveTerminal:$PreserveTerminal `
            -TimeoutSeconds 30
    }
    catch {
        throw "Super1 forced stopped-state gate failed; graceful=$($gracefulFailure.Exception.Message); forced=$($_.Exception.Message)"
    }
}

function New-Super1SecureDirectoryAcl {
    param(
        [AllowEmptyString()][string]$RunnerSid = "",
        [Security.AccessControl.FileSystemRights]$RunnerRights =
            [Security.AccessControl.FileSystemRights]::ReadAndExecute
    )
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) {
        [void]$acl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new($sid),
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    if (-not [string]::IsNullOrWhiteSpace($RunnerSid)) {
        [void]$acl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new($RunnerSid),
                $RunnerRights,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    return $acl
}

function Assert-Super1SecureDirectoryAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$RunnerSid = "",
        [Security.AccessControl.FileSystemRights]$RunnerRights =
            [Security.AccessControl.FileSystemRights]::ReadAndExecute
    )
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "Super1 secure directory inherits ACLs: $Path"
    }
    if ([string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18") {
        throw "Super1 secure directory is not owned by SYSTEM: $Path"
    }
    $expected = @{
        "S-1-5-18" = [int][Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-544" = [int][Security.AccessControl.FileSystemRights]::FullControl
    }
    if (-not [string]::IsNullOrWhiteSpace($RunnerSid)) {
        $expected[$RunnerSid] = [int]$RunnerRights
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
        if (
            $rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            [int]$rule.InheritanceFlags -ne $inheritance -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
            -not $expected.ContainsKey($sid)
        ) {
            throw "Super1 secure directory has an unexpected ACL rule $Path`: $sid"
        }
        $current = if ($actual.ContainsKey($sid)) { [int]$actual[$sid] } else { 0 }
        $actual[$sid] = $current -bor [int]$rule.FileSystemRights
    }
    foreach ($sid in $expected.Keys) {
        $expectedRights = [int]$expected[$sid] -bor
            [int][Security.AccessControl.FileSystemRights]::Synchronize
        if ([int]$actual[$sid] -ne $expectedRights) {
            throw "Super1 secure directory rights mismatch $Path`: $sid"
        }
    }
    if ($actual.Keys.Count -ne $expected.Keys.Count) {
        throw "Super1 secure directory ACL is incomplete: $Path"
    }
}

function New-Super1SecureDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$RunnerSid = "",
        [Security.AccessControl.FileSystemRights]$RunnerRights =
            [Security.AccessControl.FileSystemRights]::ReadAndExecute
    )
    if (Test-Path -LiteralPath $Path) {
        throw "Refusing to reuse a Super1 secure directory: $Path"
    }
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "Super1 secure directory parent is missing: $parent"
    }
    New-Item -ItemType Directory -Path $Path | Out-Null
    $acl = New-Super1SecureDirectoryAcl -RunnerSid $RunnerSid -RunnerRights $RunnerRights
    [IO.Directory]::SetAccessControl($Path, $acl)
    & $script:Super1SecureIcaclsExe $Path /setowner "*S-1-5-18" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set SYSTEM ownership: $Path" }
    Assert-Super1SecureDirectoryAcl `
        -Path $Path `
        -RunnerSid $RunnerSid `
        -RunnerRights $RunnerRights
}

function New-Super1SecureLockedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Content)
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    [IO.File]::SetAttributes($Path, [IO.File]::GetAttributes($Path) -bor [IO.FileAttributes]::ReadOnly)
    & $script:Super1SecureIcaclsExe $Path /setowner "*S-1-5-18" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set SYSTEM ownership on secure file: $Path" }
    $acl = Get-Acl -LiteralPath $Path
    if ([string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18") {
        throw "Super1 secure file is not owned by SYSTEM: $Path"
    }
    $mutation = [int](
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    $runnerCanRead = $false
    foreach ($rule in $acl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )) {
        $sid = [string]$rule.IdentityReference.Value
        if (
            -not $rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $sid -notin @("S-1-5-18", "S-1-5-32-544", $RunnerSid)
        ) {
            throw "Super1 secure file has an unexpected ACL rule $Path`: $sid"
        }
        if ($sid -eq $RunnerSid) {
            if (([int]$rule.FileSystemRights -band $mutation) -ne 0) {
                throw "Super1 runner can mutate secure launcher: $Path"
            }
            if (
                ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq
                    [Security.AccessControl.FileSystemRights]::ReadAndExecute
            ) {
                $runnerCanRead = $true
            }
        }
    }
    if (-not $runnerCanRead) { throw "Super1 runner cannot read the secure launcher: $Path" }
    $hash = Get-Super1SecureSha256 -Path $Path
    $lock = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    if ((Get-Super1SecureSha256 -Path $Path) -cne $hash) {
        $lock.Dispose()
        throw "Super1 secure launcher changed while its read lock was acquired."
    }
    return [pscustomobject]@{ path = $Path; sha256 = $hash; lock = $lock }
}

function Open-Super1SecureLockedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or
        (Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 secure request is missing or is a reparse point: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.IsReadOnly) { throw "Super1 secure request is not read-only: $Path" }
    $acl = Get-Acl -LiteralPath $Path
    if ([string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18") {
        throw "Super1 secure request is not owned by SYSTEM: $Path"
    }
    $mutation = [int](
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    $runnerCanRead = $false
    foreach ($rule in $acl.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )) {
        $sid = [string]$rule.IdentityReference.Value
        if (-not $rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $sid -notin @("S-1-5-18", "S-1-5-32-544", $RunnerSid)) {
            throw "Super1 secure request has an unexpected ACL rule $Path`: $sid"
        }
        if ($sid -eq $RunnerSid) {
            if (([int]$rule.FileSystemRights -band $mutation) -ne 0) {
                throw "Super1 runner can mutate the secure request: $Path"
            }
            if (($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq
                [Security.AccessControl.FileSystemRights]::ReadAndExecute) {
                $runnerCanRead = $true
            }
        }
    }
    if (-not $runnerCanRead) { throw "Super1 runner cannot read the secure request: $Path" }
    $hash = Get-Super1SecureSha256 -Path $Path
    $lock = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $reader = New-Object IO.StreamReader($lock, [Text.Encoding]::UTF8, $true, 4096, $true)
        try { $content = $reader.ReadToEnd() }
        finally { $reader.Dispose() }
        if ((Get-Super1SecureSha256 -Path $Path) -cne $hash) {
            throw "Super1 secure request changed while read-locked."
        }
        return [pscustomobject]@{ path = $Path; sha256 = $hash; content = $content; lock = $lock }
    }
    catch {
        $lock.Dispose()
        throw
    }
}

function Get-Super1SecureProducerEnvelope {
    param(
        [Parameter(Mandatory = $true)][string]$ProducerPath,
        [Parameter(Mandatory = $true)][string]$RequestPath,
        [Parameter(Mandatory = $true)][string]$ResultPath,
        [Parameter(Mandatory = $true)][string]$TransactionId,
        [Parameter(Mandatory = $true)][string]$Nonce,
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$ExpectedLauncherPath,
        [Parameter(Mandatory = $true)][string]$ExpectedLauncherSha256,
        [Parameter(Mandatory = $true)][DateTimeOffset]$NotBefore,
        [Parameter(Mandatory = $true)][int]$ExpectedExitCode,
        [int]$MaxAgeSeconds = 90
    )
    $producer = [IO.Path]::GetFullPath($ProducerPath)
    $request = [IO.Path]::GetFullPath($RequestPath)
    $result = [IO.Path]::GetFullPath($ResultPath)
    $launcher = [IO.Path]::GetFullPath($ExpectedLauncherPath)
    foreach ($path in @($producer, $request, $result, $launcher)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-Item -LiteralPath $path -Force).Attributes -band
                [IO.FileAttributes]::ReparsePoint) {
            throw "Super1 producer binding file is missing or is a reparse point: $path"
        }
    }
    $locks = New-Object Collections.Generic.List[IO.FileStream]
    try {
        foreach ($path in @($request, $result, $producer)) {
            $locks.Add([IO.File]::Open(
                $path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::Read
            ))
        }
        $hash = [Security.Cryptography.SHA256]::Create()
        try {
            $requestStream = $locks[0]; $resultStream = $locks[1]; $producerStream = $locks[2]
            $requestStream.Position = 0; $requestBytes = $hash.ComputeHash($requestStream); $requestStream.Position = 0
            $resultStream.Position = 0; $resultBytes = $hash.ComputeHash($resultStream); $resultStream.Position = 0
            $producerStream.Position = 0; $producerBytes = $hash.ComputeHash($producerStream); $producerStream.Position = 0
            $requestSha256 = ([BitConverter]::ToString($requestBytes)).Replace("-", "").ToLowerInvariant()
            $resultSha256 = ([BitConverter]::ToString($resultBytes)).Replace("-", "").ToLowerInvariant()
            $producerSha256 = ([BitConverter]::ToString($producerBytes)).Replace("-", "").ToLowerInvariant()
            $requestReader = New-Object IO.StreamReader($requestStream, [Text.Encoding]::UTF8, $true, 4096, $true)
            try { $requestStream.Position = 0; $requestText = $requestReader.ReadToEnd(); $requestPayload = $requestText | ConvertFrom-Json }
            finally { $requestReader.Dispose() }
            $resultReader = New-Object IO.StreamReader($resultStream, [Text.Encoding]::UTF8, $true, 4096, $true)
            try { $resultStream.Position = 0; $resultText = $resultReader.ReadToEnd(); $resultPayload = $resultText | ConvertFrom-Json }
            finally { $resultReader.Dispose() }
            $producerReader = New-Object IO.StreamReader($producerStream, [Text.Encoding]::UTF8, $true, 4096, $true)
            try { $producerStream.Position = 0; $producerText = $producerReader.ReadToEnd(); $producerPayload = $producerText | ConvertFrom-Json }
            finally { $producerReader.Dispose() }
        } finally { $hash.Dispose() }
        try {
            $requestedAt = [DateTimeOffset]::Parse(
                [string]$requestPayload.requested_at_utc
            ).ToUniversalTime()
            $producerStartedAt = [DateTimeOffset]::Parse(
                [string]$producerPayload.started_at_utc
            ).ToUniversalTime()
            $producedAt = [DateTimeOffset]::Parse(
                [string]$producerPayload.produced_at_utc
            ).ToUniversalTime()
        }
        catch { throw "Super1 producer binding timestamps are invalid." }
        $now = [DateTimeOffset]::UtcNow
        $requestResult = [IO.Path]::GetFullPath([string]$requestPayload.result_path)
        $requestProducer = [IO.Path]::GetFullPath([string]$requestPayload.producer_path)
        $producerLauncher = [IO.Path]::GetFullPath([string]$producerPayload.launcher_path)
        if ([int]$requestPayload.schema_version -ne 1 -or
            [string]$requestPayload.kind -cne $Kind -or
            [string]$requestPayload.transaction_id -cne $TransactionId -or
            [string]$requestPayload.nonce -cne $Nonce -or
            [string]$requestPayload.expected_runner_sid -cne $RunnerSid -or
            [string]$requestPayload.expected_launcher_sha256 -cne
                $ExpectedLauncherSha256.ToLowerInvariant() -or
            -not $requestResult.Equals($result, [StringComparison]::OrdinalIgnoreCase) -or
            -not $requestProducer.Equals($producer, [StringComparison]::OrdinalIgnoreCase) -or
            [int]$producerPayload.schema_version -ne 1 -or
            [string]$producerPayload.kind -cne $Kind -or
            [string]$producerPayload.transaction_id -cne $TransactionId -or
            [string]$producerPayload.nonce -cne $Nonce -or
            [string]$producerPayload.request_sha256 -cne $requestSha256 -or
            [string]$producerPayload.result_sha256 -cne $resultSha256 -or
            [int]$producerPayload.exit_code -ne $ExpectedExitCode -or
            [string]$producerPayload.producer_runner_sid -cne $RunnerSid -or
            [int]$producerPayload.producer_process_id -le 4 -or
            [string]$producerPayload.launcher_sha256 -cne
                $ExpectedLauncherSha256.ToLowerInvariant() -or
            -not $producerLauncher.Equals($launcher, [StringComparison]::OrdinalIgnoreCase) -or
            $requestedAt -lt $NotBefore.AddSeconds(-5) -or
            $producerStartedAt -lt $requestedAt.AddSeconds(-5) -or
            $producedAt -lt $producerStartedAt -or
            $producedAt -gt $now.AddSeconds(5) -or
            ($now - $producedAt).TotalSeconds -gt $MaxAgeSeconds) {
            throw "Super1 sealed producer/request/result binding is invalid or stale."
        }
        return [pscustomobject]@{
            producer_sha256 = $producerSha256
            request_sha256 = $requestSha256
            result_sha256 = $resultSha256
            requested_at_utc = $requestedAt.ToString("o")
            started_at_utc = $producerStartedAt.ToString("o")
            produced_at_utc = $producedAt.ToString("o")
            producer_process_id = [int]$producerPayload.producer_process_id
            result_text = $resultText
            result_payload = $resultPayload
            producer_exit_code = [int]$producerPayload.exit_code
        }
    }
    finally {
        foreach ($lock in @($locks)) { $lock.Dispose() }
    }
}

function Seal-Super1SecureEvidenceTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    $items = @((Get-Item -LiteralPath $Path -Force)) + @(
        Get-ChildItem -LiteralPath $Path -Recurse -Force
    )
    $reparse = @($items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
    if ($reparse.Count -ne 0) {
        throw "Super1 evidence tree contains a reparse point: $($reparse[0].FullName)"
    }
    $acl = New-Super1SecureDirectoryAcl
    [IO.Directory]::SetAccessControl($Path, $acl)
    if (@(Get-ChildItem -LiteralPath $Path -Force).Count -ne 0) {
        & $script:Super1SecureIcaclsExe (Join-Path $Path "*") /reset /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not seal Super1 evidence descendants." }
    }
    & $script:Super1SecureIcaclsExe $Path /setowner "*S-1-5-18" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set SYSTEM ownership on Super1 evidence." }
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force)) {
        [IO.File]::SetAttributes(
            $file.FullName,
            [IO.File]::GetAttributes($file.FullName) -bor [IO.FileAttributes]::ReadOnly
        )
    }
    & $script:Super1SecureIcaclsExe $Path /verify /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Super1 evidence ACL verification failed." }
    Assert-Super1SecureSealedTree -Path $Path
}

function Assert-Super1SecureSealedTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    $root = [IO.Path]::GetFullPath($Path)
    $items = @((Get-Item -LiteralPath $root -Force)) + @(
        Get-ChildItem -LiteralPath $root -Recurse -Force
    )
    $expected = @("S-1-5-18", "S-1-5-32-544")
    $full = [int][Security.AccessControl.FileSystemRights]::FullControl
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Sealed Super1 evidence contains a reparse point: $($item.FullName)"
        }
        $acl = Get-Acl -LiteralPath $item.FullName
        if ([string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18") {
            throw "Sealed Super1 evidence is not owned by SYSTEM: $($item.FullName)"
        }
        $isRoot = $item.FullName.Equals($root, [StringComparison]::OrdinalIgnoreCase)
        if ($isRoot -and -not $acl.AreAccessRulesProtected) {
            throw "Sealed Super1 evidence root inherits ACLs: $root"
        }
        $rights = @{}
        foreach ($rule in $acl.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        )) {
            $sid = [string]$rule.IdentityReference.Value
            if (
                $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                [bool]$rule.IsInherited -eq $isRoot -or
                $sid -notin $expected
            ) {
                throw "Sealed Super1 evidence has an unexpected ACL rule $($item.FullName): $sid"
            }
            $current = if ($rights.ContainsKey($sid)) { [int]$rights[$sid] } else { 0 }
            $rights[$sid] = $current -bor [int]$rule.FileSystemRights
        }
        foreach ($sid in $expected) {
            if ([int]$rights[$sid] -ne $full) {
                throw "Sealed Super1 evidence rights mismatch $($item.FullName): $sid"
            }
        }
        if ($rights.Keys.Count -ne $expected.Count) {
            throw "Sealed Super1 evidence ACL is incomplete: $($item.FullName)"
        }
        if ($item -is [IO.FileInfo] -and -not $item.IsReadOnly) {
            throw "Sealed Super1 evidence file is not read-only: $($item.FullName)"
        }
    }
}

function ConvertFrom-Super1SecureTaskDuration {
    param([Parameter(Mandatory = $true)][object]$Value)
    if ($Value -is [TimeSpan]) { return [TimeSpan]$Value }
    try { return [Xml.XmlConvert]::ToTimeSpan([string]$Value) }
    catch { return [TimeSpan]::Parse([string]$Value) }
}

function Get-Super1SecurePrincipalSid {
    param([Parameter(Mandatory = $true)][string]$Identity)
    if ($Identity -match '^S-\d-(?:\d+-)+\d+$') {
        return [Security.Principal.SecurityIdentifier]::new($Identity).Value
    }
    return (New-Object Security.Principal.NTAccount($Identity)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
}

function Assert-Super1SecureNoTriggers {
    param([Parameter(Mandatory = $true)][object]$Task)
    $triggers = @($Task.Triggers)
    if ($triggers.Count -ne 0) {
        throw "$($Task.TaskName) must have no trigger; manual lease controls every start."
    }
}

# Kept as a compatibility name for callers that only imported the helper in
# older evidence harnesses. It enforces the new no-trigger contract.
function Assert-Super1SecureBootTrigger {
    param([Parameter(Mandatory = $true)][object]$Task)
    Assert-Super1SecureNoTriggers -Task $Task
}

function Assert-Super1SecureTaskBindings {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MainTask,
        [Parameter(Mandatory = $true)][string]$WatchdogTask
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $main = Get-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $watchdog = Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
    $mainActions = @($main.Actions)
    $watchdogActions = @($watchdog.Actions)
    $runnerSid = Get-Super1SecurePrincipalSid -Identity "$env:COMPUTERNAME\Super1Runner"
    $mainPrincipalSid = Get-Super1SecurePrincipalSid -Identity ([string]$main.Principal.UserId)
    $watchdogPrincipalSid = Get-Super1SecurePrincipalSid `
        -Identity ([string]$watchdog.Principal.UserId)
    $mainArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$resolvedRoot\app\deploy\run_super1_windows.ps1`""
    $watchdogArguments = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-File `"$resolvedRoot\app\deploy\watchdog_windows.ps1`""
        "-MainTaskName `"$MainTask`""
        "-HealthPath `"$resolvedRoot\state\health.json`""
        "-StatusPath `"$resolvedRoot\watchdog_status.json`""
    ) -join " "
    $mainRestartInterval = ConvertFrom-Super1SecureTaskDuration `
        -Value $main.Settings.RestartInterval
    $mainExecutionLimit = ConvertFrom-Super1SecureTaskDuration `
        -Value $main.Settings.ExecutionTimeLimit
    $watchdogRestartInterval = ConvertFrom-Super1SecureTaskDuration `
        -Value $watchdog.Settings.RestartInterval
    $watchdogExecutionLimit = ConvertFrom-Super1SecureTaskDuration `
        -Value $watchdog.Settings.ExecutionTimeLimit

    if ([string]$main.TaskPath -cne "\" -or
        $mainActions.Count -ne 1 -or
        -not [IO.Path]::GetFullPath([string]$mainActions[0].Execute).Equals(
            $script:Super1SecurePowerShellExe,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$mainActions[0].Arguments -cne $mainArguments -or
        -not [string]::IsNullOrEmpty([string]$mainActions[0].WorkingDirectory) -or
        $mainPrincipalSid -cne $runnerSid -or
        [string]$main.Principal.LogonType -cne "Password" -or
        [string]$main.Principal.RunLevel -cne "Limited" -or
        -not [bool]$main.Settings.Enabled -or
         [int]$main.Settings.RestartCount -ne 0 -or
        $mainRestartInterval -ne [TimeSpan]::FromMinutes(15) -or
        $mainExecutionLimit -ne [TimeSpan]::Zero -or
        -not [bool]$main.Settings.StartWhenAvailable -or
         [string]$main.Settings.MultipleInstances -cne "IgnoreNew" -or
         [bool]$main.Settings.DisallowStartIfOnBatteries -or
         [bool]$main.Settings.StopIfGoingOnBatteries -or
         [bool]$main.Settings.WakeToRun) {
        throw "Super1 main Password task differs from the exact fixed contract."
    }
    Assert-Super1SecureNoTriggers -Task $main

    $otherRunnerTasks = New-Object Collections.Generic.List[string]
    foreach ($candidateTask in @(Get-ScheduledTask -ErrorAction Stop)) {
        if ([string]$candidateTask.TaskPath -ceq "\" -and
            [string]$candidateTask.TaskName -ceq $MainTask) {
            continue
        }
        $candidateIdentity = [string]$candidateTask.Principal.UserId
        if ([string]::IsNullOrWhiteSpace($candidateIdentity)) { continue }
        try { $candidateSid = Get-Super1SecurePrincipalSid -Identity $candidateIdentity }
        catch { continue }
        if ($candidateSid -ceq $runnerSid) {
            $otherRunnerTasks.Add("$($candidateTask.TaskPath)$($candidateTask.TaskName)")
        }
    }
    if ($otherRunnerTasks.Count -ne 0) {
        throw "Super1Runner is bound to an unexpected scheduled task: $($otherRunnerTasks -join ',')"
    }

    if ([string]$watchdog.TaskPath -cne "\" -or
        $watchdogActions.Count -ne 1 -or
        -not [IO.Path]::GetFullPath([string]$watchdogActions[0].Execute).Equals(
            $script:Super1SecurePowerShellExe,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$watchdogActions[0].Arguments -cne $watchdogArguments -or
        -not [string]::IsNullOrEmpty([string]$watchdogActions[0].WorkingDirectory) -or
        $watchdogPrincipalSid -cne "S-1-5-18" -or
        [string]$watchdog.Principal.LogonType -cne "ServiceAccount" -or
        [string]$watchdog.Principal.RunLevel -cne "Highest" -or
        -not [bool]$watchdog.Settings.Enabled -or
         [int]$watchdog.Settings.RestartCount -ne 0 -or
        $watchdogRestartInterval -ne [TimeSpan]::FromMinutes(15) -or
        $watchdogExecutionLimit -ne [TimeSpan]::Zero -or
        -not [bool]$watchdog.Settings.StartWhenAvailable -or
         [string]$watchdog.Settings.MultipleInstances -cne "IgnoreNew" -or
         [bool]$watchdog.Settings.DisallowStartIfOnBatteries -or
         [bool]$watchdog.Settings.StopIfGoingOnBatteries -or
         [bool]$watchdog.Settings.WakeToRun) {
        throw "Super1 watchdog task differs from the exact fixed contract."
    }
    Assert-Super1SecureNoTriggers -Task $watchdog
    return $runnerSid
}

function Get-Super1SecureMarketScheduleState {
    param(
        [Parameter(Mandatory = $true)][DateTimeOffset]$CheckedAt,
        [Parameter(Mandatory = $true)][object]$Runtime
    )
    $schedule = $Runtime.market_schedule_ny
    if (-not $schedule -or [string]$schedule.timezone -cne "America/New_York" -or
        [string]$schedule.weekly_open_sunday -notmatch '^\d{2}:\d{2}$' -or
        [string]$schedule.weekly_close_friday -notmatch '^\d{2}:\d{2}$') {
        throw "Super1 signed New York market schedule is invalid."
    }
    $zone = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $local = [TimeZoneInfo]::ConvertTime($CheckedAt, $zone)
    $clock = $local.ToString("HH:mm", [Globalization.CultureInfo]::InvariantCulture)
    $isWeekend = $local.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)
    $isWeekday = -not $isWeekend
    $scheduledClosed = (
        $local.DayOfWeek -eq [DayOfWeek]::Saturday -or
        ($local.DayOfWeek -eq [DayOfWeek]::Friday -and
            $clock -ge [string]$schedule.weekly_close_friday) -or
        ($local.DayOfWeek -eq [DayOfWeek]::Sunday -and
            $clock -lt [string]$schedule.weekly_open_sunday)
    )
    foreach ($dailyBreak in @($schedule.daily_breaks)) {
        $bounds = @($dailyBreak)
        if ($bounds.Count -ne 2 -or
            [string]$bounds[0] -notmatch '^\d{2}:\d{2}$' -or
            [string]$bounds[1] -notmatch '^\d{2}:\d{2}$') {
            throw "Super1 signed daily market break is invalid."
        }
        if ([string]$bounds[0] -le $clock -and $clock -lt [string]$bounds[1]) {
            $scheduledClosed = $true
        }
    }
    foreach ($closure in @($schedule.planned_closures)) {
        try {
            $closureStart = [DateTimeOffset]::Parse([string]$closure.start).ToUniversalTime()
            $closureEnd = [DateTimeOffset]::Parse([string]$closure.end).ToUniversalTime()
        }
        catch { throw "Super1 signed planned market closure is invalid." }
        if ($closureEnd -le $closureStart) {
            throw "Super1 signed planned market closure has an invalid interval."
        }
        $utc = $CheckedAt.ToUniversalTime()
        if ($closureStart -le $utc -and $utc -lt $closureEnd) {
            $scheduledClosed = $true
        }
    }
    return [pscustomobject]@{
        local_time = $local
        is_weekend = $isWeekend
        before_preflight_window = ($isWeekday -and $clock -lt "09:30")
        scheduled_closed = [bool]$scheduledClosed
    }
}
