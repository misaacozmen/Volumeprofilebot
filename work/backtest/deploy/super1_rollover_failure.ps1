Set-StrictMode -Version Latest

function Invoke-Super1RolloverCatchPolicy {
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][bool]$BrokerSideEffectPossible,
        [Parameter(Mandatory = $true)][bool]$TasksStopped,
        [Parameter(Mandatory = $true)][bool]$AppMutation,
        [Parameter(Mandatory = $true)][bool]$StateMutation,
        [Parameter(Mandatory = $true)][bool]$TaskXmlMutation,
        [Parameter(Mandatory = $true)][bool]$FatalLatchPreexisting,
        [Parameter(Mandatory = $true)][bool]$RecoveryLatchPreexisting,
        [scriptblock]$Restore,
        [scriptblock]$VerifyOldHashes,
        [scriptblock]$VerifyOldTaskXml,
        [scriptblock]$StartOldMain,
        [scriptblock]$StartOldWatchdog,
        [scriptblock]$VerifyOldHealth,
        [scriptblock]$StopOldMain,
        [scriptblock]$StopOldWatchdog,
        [scriptblock]$WriteLatch,
        [scriptblock]$WriteEvidence,
        [scriptblock]$WriteHealth
    )
    $errors = New-Object System.Collections.Generic.List[string]
    $trace = New-Object System.Collections.Generic.List[object]
    $called = @{}
    $results = @{}
    function Invoke-Structured {
        param([string]$Name, [scriptblock]$Callback)
        if ($called.ContainsKey($Name)) { return $false }
        $called[$Name] = $true
        $entry = [ordered]@{ name = $Name; status = "FAIL"; error = $null }
        $trace.Add($entry)
        if ($null -eq $Callback) {
            $entry.error = "callback missing"
            $errors.Add("${Name}: callback missing")
            return $false
        }
        try {
            $value = & $Callback
            if ($null -ne $value -and $value.PSObject.Properties["status"] -ne $null) {
                $results[$Name] = $value
            }
            if ($null -eq $value -or $value.PSObject.Properties["status"] -eq $null -or [string]$value.status -ne "PASS") {
                $entry.error = "callback did not return structured PASS"
                $errors.Add("${Name}: callback did not return structured PASS")
                return $false
            }
            $entry.status = "PASS"
            return $true
        }
        catch {
            $entry.error = $_.Exception.Message
            $errors.Add("${Name}: $($_.Exception.Message)")
            return $false
        }
    }
    function Invoke-UnsafeTail {
        [void](Invoke-Structured "StopOldWatchdog" $StopOldWatchdog)
        [void](Invoke-Structured "StopOldMain" $StopOldMain)
        [void](Invoke-Structured "WriteLatch" $WriteLatch)
        [void](Invoke-Structured "WriteEvidence" $WriteEvidence)
        [void](Invoke-Structured "WriteHealth" $WriteHealth)
    }

    $preexistingLatch = $FatalLatchPreexisting -or $RecoveryLatchPreexisting
    $status = "UNSAFE_NO_SEND"
    $safe = $false
    if ($preexistingLatch) {
        Invoke-UnsafeTail
    }
    elseif ($BrokerSideEffectPossible) {
        [void](Invoke-Structured "WriteLatch" $WriteLatch)
        [void](Invoke-Structured "StopOldWatchdog" $StopOldWatchdog)
        [void](Invoke-Structured "StopOldMain" $StopOldMain)
        [void](Invoke-Structured "WriteEvidence" $WriteEvidence)
        [void](Invoke-Structured "WriteHealth" $WriteHealth)
    }
    elseif ($TasksStopped) {
        $safe = (
            (Invoke-Structured "Restore" $Restore) -and
            (Invoke-Structured "VerifyOldHashes" $VerifyOldHashes) -and
            (Invoke-Structured "VerifyOldTaskXml" $VerifyOldTaskXml) -and
            (Invoke-Structured "StartOldMain" $StartOldMain) -and
            (Invoke-Structured "StartOldWatchdog" $StartOldWatchdog) -and
            (Invoke-Structured "VerifyOldHealth" $VerifyOldHealth)
        )
        if ($safe) { $status = "SAFE_ROLLBACK" } else { Invoke-UnsafeTail }
    }
    else {
        $clean = -not ($AppMutation -or $StateMutation -or $TaskXmlMutation)
        if ($clean) {
            $safe = (
                (Invoke-Structured "VerifyOldHashes" $VerifyOldHashes) -and
                (Invoke-Structured "VerifyOldTaskXml" $VerifyOldTaskXml) -and
                (Invoke-Structured "VerifyOldHealth" $VerifyOldHealth)
            )
            if ($safe) { $status = "SAFE_ROLLBACK" } else { Invoke-UnsafeTail }
        }
        else {
            Invoke-UnsafeTail
        }
    }
    $latchResult = if ($results.ContainsKey("WriteLatch")) { $results["WriteLatch"] } else { $null }
    $latchWritten = if ($null -ne $latchResult) { [bool]$latchResult.latch_written } else { $false }
    $recoveryWritten = if ($null -ne $latchResult) { [bool]$latchResult.recovery_written } else { $false }
    $latchPreexisting = if ($null -ne $latchResult) { [bool]$latchResult.latch_preexisting } else { $FatalLatchPreexisting }
    $recoveryPreexisting = if ($null -ne $latchResult) { [bool]$latchResult.recovery_preexisting } else { $RecoveryLatchPreexisting }
    $latchErrors = @()
    if ($null -ne $latchResult) { $latchErrors = @($latchResult.errors) }
    $latchStatus = "NOT_ATTEMPTED"
    if ($null -ne $latchResult) { $latchStatus = [string]$latchResult.status }
    $gateProven = (
        (Test-Path -LiteralPath (Join-Path $StateRoot "fatal_latch.json") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $StateRoot "runtime\broker_recovery_required.json") -PathType Leaf)
    )
    if ($status -eq "UNSAFE_NO_SEND" -and -not $gateProven) {
        $status = "UNSAFE_UNLATCHED"
    }
    $callbackCounts = [ordered]@{}
    foreach ($name in $called.Keys) { $callbackCounts[$name] = 1 }
    [pscustomobject]@{
        status = $status
        BrokerSideEffectPossible = $BrokerSideEffectPossible
        TasksStopped = $TasksStopped
        PreexistingLatch = $preexistingLatch
        fatal_latch_preexisting = $FatalLatchPreexisting
        recovery_latch_preexisting = $RecoveryLatchPreexisting
        latch_preexisting = $latchPreexisting
        recovery_preexisting = $recoveryPreexisting
        latch_status = $latchStatus
        latch_errors = $latchErrors
        latch_written = $latchWritten
        recovery_written = $recoveryWritten
        persistent_gate_proven = $gateProven
        callback_trace = $trace.ToArray()
        callback_counts = $callbackCounts
        errors = $errors.ToArray()
        automatic_restart = $false
    }
}

function Write-Super1FailureLatchesCreateNew {
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$FailureMessage,
        [Parameter(Mandatory = $true)][string]$FailureType
    )
    $paths = [ordered]@{
        latch = [IO.Path]::GetFullPath((Join-Path $StateRoot "fatal_latch.json"))
        recovery = [IO.Path]::GetFullPath((Join-Path $StateRoot "runtime\broker_recovery_required.json"))
    }
    $errors = New-Object System.Collections.Generic.List[string]
    $written = @{}
    $preexisting = @{}
    $stamp = [DateTimeOffset]::UtcNow.ToString("o")
    $payloads = @{
        latch = [ordered]@{ schema_version = 1; state = "ROLLOVER_FAILURE_NO_SEND"; error = $FailureMessage; error_type = $FailureType; latched_at = $stamp; recovery_required = $true; automatic_restart = $false }
        recovery = [ordered]@{ schema_version = 1; state = "UNKNOWN_NO_SEND"; reason = "ROLLOVER_FAILURE"; error = $FailureMessage; error_type = $FailureType; first_detected_at = $stamp; updated_at = $stamp; recovery_required = $true; automatic_restart = $false }
    }
    foreach ($name in @("latch", "recovery")) {
        $preexisting[$name] = Test-Path -LiteralPath $paths[$name] -PathType Leaf
        if ($preexisting[$name]) {
            $written[$name] = $false
            continue
        }
        try {
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $paths[$name]))
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes((($payloads[$name] | ConvertTo-Json -Depth 8 -Compress) + [Environment]::NewLine))
            $stream = [IO.File]::Open($paths[$name], [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
            $written[$name] = $true
        }
        catch { $written[$name] = $false; [void]$errors.Add("${name}: $($_.Exception.Message)") }
    }
    return [pscustomobject]@{
        status = if ((Test-Path -LiteralPath $paths["latch"] -PathType Leaf) -and (Test-Path -LiteralPath $paths["recovery"] -PathType Leaf)) { "PASS" } else { "FAIL" }
        latch_preexisting = [bool]$preexisting["latch"]
        recovery_preexisting = [bool]$preexisting["recovery"]
        latch_written = [bool]$written["latch"]
        recovery_written = [bool]$written["recovery"]
        paths = $paths
        errors = @($errors)
    }
}
