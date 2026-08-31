Set-StrictMode -Version Latest

function Invoke-V16Child {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ScriptBody,
        [Parameter(Mandatory = $true)][string]$LogDirectory,
        [Parameter(Mandatory = $true)][string]$RunId,
        [Parameter(Mandatory = $true)][ValidateRange(1, 900)][int]$TimeoutSeconds
    )
    $powerShell = [IO.Path]::GetFullPath((Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'))
    if (-not [Environment]::Is64BitProcess -or -not (Test-Path -LiteralPath $powerShell -PathType Leaf)) { throw 'TRUSTED_POWERSHELL_UNAVAILABLE' }
    if (-not (Test-Path -LiteralPath $LogDirectory -PathType Container)) { throw 'LOG_DIRECTORY_MISSING' }
    $wrapper = @"
`$ErrorActionPreference = 'Stop'
`$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding(`$false)
try {
$ScriptBody
exit 0
} catch {
`$e = [ordered]@{ exception_type = `$_.Exception.GetType().FullName; message = `$_.Exception.Message; hresult = `$_.Exception.HResult; script = `$_.InvocationInfo.ScriptName; line = `$_.InvocationInfo.ScriptLineNumber; position = `$_.InvocationInfo.PositionMessage; stack = `$_.ScriptStackTrace; caught_at_utc = [DateTimeOffset]::UtcNow.ToString('o') }
`$json = `$e | ConvertTo-Json -Depth 8 -Compress
[Console]::Error.WriteLine(`$json)
exit 1
}
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapper))
    $stdoutPath = Join-Path $LogDirectory "$RunId.stdout.txt"
    $stderrPath = Join-Path $LogDirectory "$RunId.stderr.txt"
    $eventPath = Join-Path $LogDirectory 'events.jsonl'
    $outStream = $null; $errStream = $null; $process = $null; $outTask = $null; $errTask = $null; $started = $false; $pidValue = $null; $creationUtc = $null; $exitCodeValue = $null; $captureComplete = $false; $processSucceeded = $false; $primaryError = $null; $cleanupError = $null; $finalEventError = $null; $leaveStreamsOpen = $false; $activeChild = $false; $exitObserved = $false
    $startedUtc = [DateTimeOffset]::UtcNow
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $streamOptions = [IO.FileOptions]::Asynchronous -bor [IO.FileOptions]::WriteThrough
        $outStream = New-Object IO.FileStream($stdoutPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read, 65536, $streamOptions)
        $errStream = New-Object IO.FileStream($stderrPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read, 65536, $streamOptions)
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $powerShell
        $psi.Arguments = "-NoLogo -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand $encoded"
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $psi
        $metadata = [ordered]@{ event='child_start'; run_id=$RunId; started_at_utc=$startedUtc.ToString('o'); executable=$powerShell; executable_sha256=(Get-FileHash -LiteralPath $powerShell -Algorithm SHA256).Hash.ToLowerInvariant(); script_sha256=(Get-FileHash -InputStream ([IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($wrapper))) -Algorithm SHA256).Hash.ToLowerInvariant(); pid=$null; timeout_seconds=$TimeoutSeconds; stdout=$stdoutPath; stderr=$stderrPath }
        [IO.File]::AppendAllText($eventPath, (($metadata | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        [IO.File]::AppendAllText($eventPath, (([ordered]@{event='launch_intent';run_id=$RunId;at_utc=[DateTimeOffset]::UtcNow.ToString('o');executable=$powerShell;timeout_seconds=$TimeoutSeconds;stdout=$stdoutPath;stderr=$stderrPath} | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        if (-not $process.Start()) { throw 'CHILD_START_FAILED' }
        $started = $true
        $pidValue = $process.Id
        $metadata.pid = $pidValue
        $outTask = $process.StandardOutput.BaseStream.CopyToAsync($outStream)
        $errTask = $process.StandardError.BaseStream.CopyToAsync($errStream)
        try { $creationUtc = $process.StartTime.ToUniversalTime().ToString('o') } catch { $creationUtc = $null }
        [IO.File]::AppendAllText($eventPath, (([ordered]@{event='native_started';run_id=$RunId;pid=$pidValue;process_creation_time_utc=$creationUtc;at_utc=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        $deadlineTicks = [Diagnostics.Stopwatch]::GetTimestamp() + [int64]($TimeoutSeconds * [Diagnostics.Stopwatch]::Frequency)
        while (-not $process.HasExited) { if ([Diagnostics.Stopwatch]::GetTimestamp() -ge $deadlineTicks) { $leaveStreamsOpen = $true; $activeChild=$true; throw 'CHILD_TIMEOUT' }; if ($outTask.IsFaulted -or $errTask.IsFaulted) { $leaveStreamsOpen=$true; $activeChild=$true; throw 'OUTPUT_CAPTURE_FAILED_WHILE_RUNNING' }; Start-Sleep -Milliseconds 100 }
        $exitObserved = $true
        [IO.File]::AppendAllText($eventPath, (([ordered]@{event='exit_observed';run_id=$RunId;pid=$pidValue;at_utc=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        $exitCodeValue = $process.ExitCode
        if ($exitCodeValue -isnot [int]) { throw 'BLOCKED_EXIT_CODE_UNKNOWN' }
        $processSucceeded = ($exitCodeValue -eq 0)
        $drainDeadlineTicks = [Diagnostics.Stopwatch]::GetTimestamp() + [int64](10 * [Diagnostics.Stopwatch]::Frequency)
        while ((-not $outTask.IsCompleted -or -not $errTask.IsCompleted) -and [Diagnostics.Stopwatch]::GetTimestamp() -lt $drainDeadlineTicks) { Start-Sleep -Milliseconds 50 }
        if (-not $outTask.IsCompleted -or -not $errTask.IsCompleted) { throw 'OUTPUT_DRAIN_TIMEOUT' }
        if ($outTask.IsFaulted -or $errTask.IsFaulted) { throw 'OUTPUT_DRAIN_FAILED' }
        [IO.File]::AppendAllText($eventPath, (([ordered]@{event='stream_drain_complete';run_id=$RunId;at_utc=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        $outStream.Flush($true); $errStream.Flush($true)
        $outStream.Dispose(); $outStream = $null
        $errStream.Dispose(); $errStream = $null
        [void][IO.File]::ReadAllBytes($stdoutPath)
        [void][IO.File]::ReadAllBytes($stderrPath)
        $stdoutHash=(Get-FileHash -LiteralPath $stdoutPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $stderrHash=(Get-FileHash -LiteralPath $stderrPath -Algorithm SHA256).Hash.ToLowerInvariant()
        [IO.File]::AppendAllText($eventPath, (([ordered]@{event='flush_readback_hash';run_id=$RunId;at_utc=[DateTimeOffset]::UtcNow.ToString('o');stdout_bytes=(Get-Item -LiteralPath $stdoutPath).Length;stderr_bytes=(Get-Item -LiteralPath $stderrPath).Length;stdout_sha256=$stdoutHash;stderr_sha256=$stderrHash} | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        [IO.File]::AppendAllText($eventPath, (([ordered]@{event='capture_complete';run_id=$RunId;at_utc=[DateTimeOffset]::UtcNow.ToString('o');stdout_sha256=$stdoutHash;stderr_sha256=$stderrHash} | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        $captureComplete = $true
    } catch { $primaryError = $_; if($started){try{if(-not $process.HasExited){$leaveStreamsOpen=$true;$activeChild=$true}}catch{$leaveStreamsOpen=$true;$activeChild=$true}} }
    finally {
        $sw.Stop()
        try { if (-not $leaveStreamsOpen) { if ($outStream) { $outStream.Dispose() }; if ($errStream) { $errStream.Dispose() } } } catch { $cleanupError = $_ }
        $end = [DateTimeOffset]::UtcNow
        $operationAccepted = ($captureComplete -and $processSucceeded -and $null -eq $primaryError -and $null -eq $cleanupError)
        $endEvent = [ordered]@{event='child_end';run_id=$RunId;pid=$pidValue;process_creation_time_utc=$creationUtc;ended_at_utc=$end.ToString('o');elapsed_ms=$sw.ElapsedMilliseconds;exit_observed=$exitObserved;exit_code=$exitCodeValue;process_succeeded=$processSucceeded;capture_complete=$captureComplete;operation_accepted=$operationAccepted;active_child=$activeChild;primary_error=if($primaryError){$primaryError.Exception.ToString()}else{$null};cleanup_error=if($cleanupError){$cleanupError.Exception.ToString()}else{$null}}
        try { [IO.File]::AppendAllText($eventPath, (($endEvent | ConvertTo-Json -Compress) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false))) } catch { $finalEventError = $_; $captureComplete = $false; if ($null -eq $primaryError) { $primaryError = $_ } }
    }
    $operationAccepted = ($captureComplete -and $processSucceeded -and $null -eq $primaryError -and $null -eq $cleanupError -and $null -eq $finalEventError -and -not $activeChild)
    [pscustomobject]@{run_id=$RunId;started=$started;native_started=$started;started_at_utc=$startedUtc.ToString('o');pid=$pidValue;process_creation_time_utc=$creationUtc;exit_observed=$exitObserved;exit_code=$exitCodeValue;process_succeeded=$processSucceeded;capture_complete=$captureComplete;operation_accepted=$operationAccepted;active_child=$activeChild;stdout=$stdoutPath;stderr=$stderrPath;primary_error=if($primaryError){$primaryError.Exception.ToString()}else{$null};cleanup_error=if($cleanupError){$cleanupError.Exception.ToString()}else{$null};final_event_error=if($finalEventError){$finalEventError.Exception.ToString()}else{$null};process_handle=if($activeChild){$process}else{$null};stdout_task=if($activeChild){$outTask}else{$null};stderr_task=if($activeChild){$errTask}else{$null};stdout_stream=if($activeChild){$outStream}else{$null};stderr_stream=if($activeChild){$errStream}else{$null}}
}
