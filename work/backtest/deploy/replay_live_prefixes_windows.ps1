[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][ValidateSet("forward", "super1")][string]$Profile,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [Parameter(Mandatory = $true)][string]$Dates
)

$ErrorActionPreference = "Stop"
$python = Join-Path $Root "venv311\Scripts\python.exe"
$script = Join-Path $Root "replay_live_prefixes.py"
try {
    $env:XM_MT5_SERVER = (Get-Content -LiteralPath (Join-Path $Root "xm-server.txt") -Raw).Trim()
    $arguments = @($script, "--profile", $Profile, "--data-root", $DataRoot, "--output-root", $OutputRoot)
    foreach ($date in $Dates.Split(",", [StringSplitOptions]::RemoveEmptyEntries)) {
        $arguments += @("--date", $date)
    }
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    $env:XM_MT5_SERVER = $null
}
