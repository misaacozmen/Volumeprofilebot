[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$WatchdogTaskName
)

$ErrorActionPreference = "Stop"
$resolvedRoot = [IO.Path]::GetFullPath($Root)
$candidate = [IO.Path]::GetFullPath((Join-Path $resolvedRoot "watchdog_windows.ps1.candidate"))
$target = [IO.Path]::GetFullPath((Join-Path $resolvedRoot "app\deploy\watchdog_windows.ps1"))
$archiveRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot "archive"))
foreach ($path in @($candidate, $target, $archiveRoot)) {
    if (-not $path.StartsWith(($resolvedRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe watchdog upgrade path: $path"
    }
}
if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
    throw "Watchdog candidate is missing: $candidate"
}
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile($candidate, [ref]$null, [ref]$parseErrors) | Out-Null
if (@($parseErrors).Count -ne 0) {
    throw "Watchdog candidate has PowerShell syntax errors."
}

$stamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$archive = [IO.Path]::GetFullPath((Join-Path $archiveRoot "watchdog-$stamp"))
if (-not $archive.StartsWith(($archiveRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe watchdog archive path: $archive"
}
New-Item -ItemType Directory -Path $archive -Force | Out-Null

Stop-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Copy-Item -LiteralPath $target -Destination (Join-Path $archive "watchdog_windows.ps1.previous") -Force
try {
    Move-Item -LiteralPath $candidate -Destination $target -Force
    Start-ScheduledTask -TaskName $WatchdogTaskName
    Start-Sleep -Seconds 5
    $state = (Get-ScheduledTask -TaskName $WatchdogTaskName).State.ToString()
    if ($state -ne "Running") {
        throw "Updated watchdog task did not remain running: $state"
    }
}
catch {
    Copy-Item -LiteralPath (Join-Path $archive "watchdog_windows.ps1.previous") -Destination $target -Force
    Start-ScheduledTask -TaskName $WatchdogTaskName
    throw
}

[pscustomobject]@{
    archive = $archive
    task = $WatchdogTaskName
    state = (Get-ScheduledTask -TaskName $WatchdogTaskName).State.ToString()
    code_hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
} | ConvertTo-Json -Compress
