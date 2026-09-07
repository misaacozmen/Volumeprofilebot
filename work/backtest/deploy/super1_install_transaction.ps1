function Add-Super1RollbackError {
    param(
        [System.Collections.Generic.List[string]]$Errors,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if ($null -eq $Errors) { throw "Rollback caller must provide a typed error list." }
    [void]$Errors.Add($Message)
}

function Get-Super1ExactProcessMatches {
    param(
        [string]$RunnerProcessPath,
        [string]$RunnerHarnessPath,
        [string]$RunnerSid
    )
    if ([string]::IsNullOrWhiteSpace($RunnerProcessPath) -or
        [string]::IsNullOrWhiteSpace($RunnerHarnessPath) -or
        [string]::IsNullOrWhiteSpace($RunnerSid)) { return @() }
    $matches = New-Object Collections.Generic.List[object]
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ([string]$process.ExecutablePath -cne $RunnerProcessPath -or
            [string]$process.CommandLine -notmatch [regex]::Escape($RunnerHarnessPath)) { continue }
        try {
            $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
            $account = if ([string]$owner.Domain) { "$($owner.Domain)\$($owner.User)" } else { [string]$owner.User }
            $sid = ([Security.Principal.NTAccount]::new($account)).Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            if ($sid -ceq $RunnerSid) { $matches.Add($process) }
        }
        catch { throw "Could not prove Runner process identity for PID $($process.ProcessId): $($_.Exception.Message)" }
    }
    return @($matches)
}

function Assert-Super1RollbackQuiesced {
    param(
        [Parameter(Mandatory = $true)][string[]]$TaskNameList,
        [System.Collections.Generic.List[string]]$Errors,
        [string]$RunnerProcessPath,
        [string]$RunnerHarnessPath,
        [string]$RunnerSid
    )
    try {
        foreach ($taskName in $TaskNameList) {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($null -ne $task -and [string]$task.State -in @("Running", "Queued")) {
                Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
            }
        }
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
        do {
            $runningTasks = @($TaskNameList | ForEach-Object {
                $task = Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
                if ($null -ne $task -and [string]$task.State -in @("Running", "Queued")) { $task }
            })
            $processes = @(Get-Super1ExactProcessMatches -RunnerProcessPath $RunnerProcessPath -RunnerHarnessPath $RunnerHarnessPath -RunnerSid $RunnerSid)
            if ($runningTasks.Count -eq 0 -and $processes.Count -eq 0) { return $true }
            Start-Sleep -Milliseconds 250
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        Add-Super1RollbackError -Errors $Errors -Message "ROLLBACK_INCOMPLETE_NO_MUTATION: exact Runner task/process did not quiesce."
        return $false
    }
    catch {
        Add-Super1RollbackError -Errors $Errors -Message "ROLLBACK_INCOMPLETE_NO_MUTATION: could not prove quiescence: $($_.Exception.Message)"
        return $false
    }
}

function Restore-Super1SecuritySnapshots {
    param(
        [object[]]$SecuritySnapshotList = @(),
        [System.Collections.Generic.List[string]]$Errors
    )
    foreach ($container in @($SecuritySnapshotList)) {
        $basePath = [string]$container.path
        $snapshot = $container.snapshot
        if ($null -eq $snapshot -or -not [bool]$snapshot.exists) { continue }
        foreach ($entry in @($snapshot.entries | Sort-Object { ([string]$_.relative_path).Length } -Descending)) {
            $target = if ([string]$entry.relative_path -ceq ".") {
                $basePath
            } else {
                Join-Path $basePath (([string]$entry.relative_path) -replace '/', '\')
            }
            if (-not (Test-Path -LiteralPath $target)) {
                Add-Super1RollbackError -Errors $Errors -Message "restore security ${target}: original path is missing"
                continue
            }
            try {
                $acl = Get-Acl -LiteralPath $target -ErrorAction Stop
                $acl.SetSecurityDescriptorSddlForm([string]$entry.sddl)
                if (-not [string]::IsNullOrWhiteSpace([string]$entry.owner)) {
                    $acl.SetOwner([Security.Principal.NTAccount]::new([string]$entry.owner))
                }
                Set-Acl -LiteralPath $target -AclObject $acl -ErrorAction Stop
            }
            catch { Add-Super1RollbackError -Errors $Errors -Message "restore security ${target}: $($_.Exception.Message)" }
        }
    }
}

function Seal-Super1TransactionTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return }
    $icacls = Join-Path ([Environment]::SystemDirectory) "icacls.exe"
    & $icacls $Path /inheritance:r /setowner "*S-1-5-18" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set SYSTEM owner on transaction evidence tree." }
    & $icacls $Path /grant:r "*S-1-5-18:(OI)(CI)(F)" "*S-1-5-32-544:(OI)(CI)(RX)" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not seal transaction evidence tree ACL." }
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction Stop)) {
        $file.IsReadOnly = $true
    }
}

function Invoke-Super1TransactionRollback {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$TransactionPath,
        [Parameter(Mandatory = $true)][string[]]$TaskNameList,
        [Parameter(Mandatory = $true)][bool]$OldTasksRemoved,
        [System.Collections.Generic.List[string]]$Errors,
        [bool]$RootExistedBefore = $true,
        [object[]]$NewTargetList = @(),
        [object[]]$MovedEntryList = @(),
        [object[]]$TaskBackupList = @(),
        [string]$CredentialBackup,
        [string]$CredentialPath,
        [Parameter(Mandatory = $true)][bool]$RunnerWasCreated,
        [string]$RunnerName,
        [object[]]$AclBackupList = @(),
        [object[]]$SecuritySnapshotList = @(),
        [string]$RootAclBackup,
        [string]$IcaclsPath,
        [string]$RunnerProcessPath,
        [string]$RunnerHarnessPath,
        [string]$RunnerSid
    )
    if ($null -eq $Errors) { throw "Rollback caller must provide a typed error list." }
    # The first operation is quiescence: rollback must not mutate an active
    # runtime or remove evidence of a process whose identity is unproven.
    if (-not (Assert-Super1RollbackQuiesced -TaskNameList $TaskNameList -Errors $Errors -RunnerProcessPath $RunnerProcessPath -RunnerHarnessPath $RunnerHarnessPath -RunnerSid $RunnerSid)) {
        return
    }

    if ($OldTasksRemoved) {
        foreach ($taskName in $TaskNameList) {
            try {
                if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
                }
            }
            catch { Add-Super1RollbackError -Errors $Errors -Message "task removal ${taskName}: $($_.Exception.Message)" }
        }
    }
    foreach ($target in @($NewTargetList)) {
        if (Test-Path -LiteralPath $target) {
            try {
                $failedTarget = Join-Path $TransactionPath (([IO.Path]::GetFileName($target)) + ".failed")
                if (-not (Test-Path -LiteralPath $failedTarget)) { Move-Item -LiteralPath $target -Destination $failedTarget -Force }
            }
            catch { Add-Super1RollbackError -Errors $Errors -Message "new target ${target}: $($_.Exception.Message)" }
        }
    }
    for ($index = @($MovedEntryList).Count - 1; $index -ge 0; $index--) {
        $entry = $MovedEntryList[$index]
        if ((Test-Path -LiteralPath $entry.archive) -and (-not (Test-Path -LiteralPath $entry.source))) {
            try { Move-Item -LiteralPath $entry.archive -Destination $entry.source }
            catch { Add-Super1RollbackError -Errors $Errors -Message "restore $($entry.source): $($_.Exception.Message)" }
        }
    }
    foreach ($backup in @($TaskBackupList)) {
        try {
            Register-ScheduledTask -TaskName ([string]$backup.task) -Xml (Get-Content -Raw -LiteralPath ([string]$backup.xml)) -Force | Out-Null
        }
        catch { Add-Super1RollbackError -Errors $Errors -Message "restore task $($backup.task): $($_.Exception.Message)" }
    }
    if ($CredentialBackup -and $CredentialPath -and (Test-Path -LiteralPath $CredentialBackup)) {
        try { Copy-Item -LiteralPath $CredentialBackup -Destination $CredentialPath -Force }
        catch { Add-Super1RollbackError -Errors $Errors -Message "restore Runner credential metadata: $($_.Exception.Message)" }
    }
    elseif ($CredentialPath -and (Test-Path -LiteralPath $CredentialPath)) {
        try { Remove-Item -LiteralPath $CredentialPath -Force }
        catch { Add-Super1RollbackError -Errors $Errors -Message "remove newly created Runner credential metadata: $($_.Exception.Message)" }
    }
    if ($RunnerWasCreated) {
        try { Remove-LocalUser -Name $RunnerName -ErrorAction Stop }
        catch { Add-Super1RollbackError -Errors $Errors -Message "remove newly created Runner: $($_.Exception.Message)" }
    }
    foreach ($entry in @($AclBackupList | Sort-Object { ([string]$_.path).Length } -Descending)) {
        if ((Test-Path -LiteralPath ([string]$entry.backup) -PathType Leaf) -and (Test-Path -LiteralPath ([string]$entry.path))) {
            try {
                & $IcaclsPath ([string]$entry.path) /restore ([string]$entry.backup) /c /q | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "icacls exit code $LASTEXITCODE" }
            }
            catch { Add-Super1RollbackError -Errors $Errors -Message "restore ACL $($entry.path): $($_.Exception.Message)" }
        }
    }
    if ($RootAclBackup -and (Test-Path -LiteralPath $RootAclBackup -PathType Leaf) -and (Test-Path -LiteralPath $RootPath)) {
        try {
            & $IcaclsPath $RootPath /restore $RootAclBackup /c /q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls exit code $LASTEXITCODE" }
        }
        catch { Add-Super1RollbackError -Errors $Errors -Message "restore ACL ${RootPath}: $($_.Exception.Message)" }
    }
    Restore-Super1SecuritySnapshots -SecuritySnapshotList $SecuritySnapshotList -Errors $Errors
    if (-not $RootExistedBefore -and (Test-Path -LiteralPath $RootPath -PathType Container)) {
        try { Remove-Item -LiteralPath $RootPath -Recurse -Force -ErrorAction Stop }
        catch { Add-Super1RollbackError -Errors $Errors -Message "remove newly created Super1 root: $($_.Exception.Message)" }
    }
}
