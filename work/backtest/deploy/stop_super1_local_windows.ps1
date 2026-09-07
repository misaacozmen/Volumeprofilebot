[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract

function Test-Super1Administrator {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

if (-not (Test-Super1Administrator)) {
    $child = Start-Process `
        -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"") `
        -Verb RunAs `
        -Wait `
        -PassThru
    exit ([int]$child.ExitCode)
}

function Write-Super1AtomicJson([string]$Path, [object]$Value) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes(
            (ConvertTo-Json -InputObject $Value -Depth 16 -Compress) + [Environment]::NewLine
        )
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}

function Write-Super1UnsafeStopLatch([string]$State, [string]$Reason) {
    $path = Join-Path $State "UNSAFE_STOP_NO_SEND.json"
    Write-Super1AtomicJson -Path $path -Value ([ordered]@{
        schema_version = 1
        state = "UNSAFE_STOP_NO_SEND"
        reason = $Reason
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    })
}

$root = [string]$Contract.root
$state = [string]$Contract.state
$control = [string]$Contract.control
$leasePath = Join-Path $control "session-lease.json"
$stopPath = Join-Path $control "stop-request.json"
$lease = $null
$mutex = [Threading.Mutex]::new($false, [string]$Contract.order_mutex)
$held = $false
try {
    $held = $mutex.WaitOne(30000)
    if (-not $held) { throw "Could not acquire Super1 order transport mutex." }
    if (Test-Path -LiteralPath $leasePath -PathType Leaf) {
        $lease = Get-Content -Raw -LiteralPath $leasePath | ConvertFrom-Json
    }
    $leaseSha256 = if (Test-Path -LiteralPath $leasePath -PathType Leaf) {
        (Get-FileHash -LiteralPath $leasePath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { "" }
    if ($null -ne $lease) {
        $lease.state = "REVOKED"
        $lease.revoked_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        $lease.revocation_reason = "operator stop"
        Write-Super1AtomicJson -Path $leasePath -Value $lease
    }
    $stopRequest = [ordered]@{
        schema_version = 1
        request_id = [Guid]::NewGuid().ToString()
        lease_id = if ($null -eq $lease) { "" } else { [string]$lease.lease_id }
        lease_sha256 = $leaseSha256
        invocation_nonce = if ($null -eq $lease) { "" } else { [string]$lease.invocation_nonce }
        requested_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        reason = "OPERATOR_STOP"
    }
    Write-Super1AtomicJson -Path $stopPath -Value $stopRequest
}
finally {
    if ($held) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}

$deadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
$safe = $false
$lastHealth = $null
$requestedAt = [DateTimeOffset]::UtcNow
try {
    $request = Get-Content -Raw -LiteralPath $stopPath | ConvertFrom-Json
    $requestedAt = [DateTimeOffset]::Parse([string]$request.requested_at_utc).ToUniversalTime()
}
catch { throw "Stop request evidence is unreadable; refusing to claim a safe stop." }
do {
    $healthPath = [string]$Contract.health
    if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
        try {
            $lastHealth = Get-Content -Raw -LiteralPath $healthPath | ConvertFrom-Json
            $safe = [string]$lastHealth.state -ceq "STOPPED" -and
                ([DateTimeOffset]::Parse([string]$lastHealth.updated_at).ToUniversalTime() -gt $requestedAt) -and
                [string]$lastHealth.request_id -ceq [string]$stopRequest.request_id -and
                [string]$lastHealth.lease_id -ceq [string]$stopRequest.lease_id -and
                [string]$lastHealth.safe_stop -ceq "PASS" -and
                [int]$lastHealth.owned_pending -eq 0 -and
                (([int]$lastHealth.open_positions -eq 0) -or
                    ([int]$lastHealth.protected_open -eq [int]$lastHealth.owned_positions)) -and
                [int]$lastHealth.foreign_exposure -eq 0 -and
                [int]$lastHealth.unknown -eq 0
            if ([string]$lastHealth.state -in @("UNSAFE_STOP_NO_SEND", "UNSAFE_OPEN_ORDERS", "CRITICAL_STOP")) { break }
        }
        catch { $safe = $false }
    }
    if ($safe) { break }
    Start-Sleep -Seconds 2
} while ([DateTimeOffset]::UtcNow -lt $deadline)

if (-not $safe) {
    $reason = if ($null -eq $lastHealth) { "No safe-stop health was published." } else { "Broker reconciliation did not prove owned_pending=0 and protected/zero positions." }
    Write-Super1UnsafeStopLatch -State $state -Reason $reason
    throw "UNSAFE_STOP_NO_SEND: $reason Processes were not forcibly terminated."
}

foreach ($taskName in @([string]$Contract.main_task, [string]$Contract.watchdog_task)) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}
$runnerSid = if ($null -ne $lease) { [string]$lease.runner_sid } else { "" }
$terminal = [string]$Contract.terminal
$remaining = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue | Where-Object {
    [string]$_.ExecutablePath -ceq $terminal
})
if ($remaining.Count -gt 0 -and [string]::IsNullOrWhiteSpace($runnerSid)) {
    Write-Super1UnsafeStopLatch -State $state -Reason "Canonical terminal owner cannot be bound to a Super1 lease."
    throw "UNSAFE_STOP_NO_SEND: canonical terminal owner is unproven; process was not terminated."
}
foreach ($process in $remaining) {
    if ($runnerSid) {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
        $name = if ([string]$owner.Domain) { "$($owner.Domain)\$($owner.User)" } else { [string]$owner.User }
        $sid = (New-Object Security.Principal.NTAccount($name)).Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -cne $runnerSid) { throw "Refusing to terminate a terminal owned by an unexpected SID." }
    }
    Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
}
$archiveRoot = Join-Path $control "archive"
New-Item -ItemType Directory -Force -Path $archiveRoot | Out-Null
$archivePath = Join-Path $archiveRoot ("stop-" + [string]$stopRequest.request_id + ".json")
if (Test-Path -LiteralPath $stopPath -PathType Leaf) {
    Move-Item -LiteralPath $stopPath -Destination $archivePath -Force
}
elseif (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    throw "Completed stop request was neither active nor archived."
}
Write-Host "Super1 stopped after broker reconciliation: owned_pending=0, unknown=0." -ForegroundColor Green
exit 0
