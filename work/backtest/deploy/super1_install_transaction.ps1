function Invoke-Super1TransactionRollback {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$TransactionPath,
        [Parameter(Mandatory = $true)][string[]]$TaskNameList,
        [Parameter(Mandatory = $true)][bool]$OldTasksRemoved,
        [object[]]$NewTargetList = @(),
        [object[]]$MovedEntryList = @(),
        [object[]]$TaskBackupList = @(),
        [string]$CredentialBackup,
        [string]$CredentialPath,
        [Parameter(Mandatory = $true)][bool]$RunnerWasCreated,
        [string]$RunnerName,
        [object[]]$AclBackupList = @(),
        [string]$RootAclBackup,
        [string]$IcaclsPath
    )
    $errors = New-Object Collections.Generic.List[string]
    if ($OldTasksRemoved) {
        try {
            foreach ($taskName in $TaskNameList) {
                if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
                }
            }
        }
        catch { $errors.Add("task removal: $($_.Exception.Message)") }
    }
    foreach ($target in $NewTargetList) {
        if (Test-Path -LiteralPath $target) {
            try {
                $failedTarget = Join-Path $TransactionPath (([IO.Path]::GetFileName($target)) + ".failed")
                if (-not (Test-Path -LiteralPath $failedTarget)) {
                    Move-Item -LiteralPath $target -Destination $failedTarget -Force
                }
            }
            catch { $errors.Add("new target ${target}: $($_.Exception.Message)") }
        }
    }
    for ($index = $MovedEntryList.Count - 1; $index -ge 0; $index--) {
        $entry = $MovedEntryList[$index]
        if ((Test-Path -LiteralPath $entry.archive) -and (-not (Test-Path -LiteralPath $entry.source))) {
            try { Move-Item -LiteralPath $entry.archive -Destination $entry.source }
            catch { $errors.Add("restore $($entry.source): $($_.Exception.Message)") }
        }
    }
    foreach ($backup in $TaskBackupList) {
        try {
            Register-ScheduledTask -TaskName ([string]$backup.task) -Xml (Get-Content -Raw -LiteralPath ([string]$backup.xml)) -Force | Out-Null
        }
        catch { $errors.Add("restore task $($backup.task): $($_.Exception.Message)") }
    }
    if ($CredentialBackup -and $CredentialPath -and (Test-Path -LiteralPath $CredentialBackup)) {
        try { Copy-Item -LiteralPath $CredentialBackup -Destination $CredentialPath -Force }
        catch { $errors.Add("restore Runner credential metadata: $($_.Exception.Message)") }
    }
    if ($RunnerWasCreated) {
        try { Remove-LocalUser -Name $RunnerName -ErrorAction Stop }
        catch { $errors.Add("remove newly created Runner: $($_.Exception.Message)") }
    }
    for ($index = $AclBackupList.Count - 1; $index -ge 0; $index--) {
        $entry = $AclBackupList[$index]
        if ((Test-Path -LiteralPath ([string]$entry.backup) -PathType Leaf) -and (Test-Path -LiteralPath ([string]$entry.path))) {
            try {
                & $IcaclsPath ([string]$entry.path) /restore ([string]$entry.backup) /c /q | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "icacls exit code $LASTEXITCODE" }
            }
            catch { $errors.Add("restore ACL $($entry.path): $($_.Exception.Message)") }
        }
    }
    if ($RootAclBackup -and (Test-Path -LiteralPath $RootAclBackup -PathType Leaf)) {
        try {
            & $IcaclsPath $RootPath /restore $RootAclBackup /c /q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls exit code $LASTEXITCODE" }
        }
        catch { $errors.Add("restore ACL ${RootPath}: $($_.Exception.Message)") }
    }
    return $errors
}
