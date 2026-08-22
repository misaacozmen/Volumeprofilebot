[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][ValidateSet("forward", "super1")][string]$Profile
)

$ErrorActionPreference = "Stop"
$resolvedRoot = [IO.Path]::GetFullPath($Root)
$current = if ($Profile -eq "forward") {
    $externalState = Join-Path $resolvedRoot "state"
    if (Test-Path -LiteralPath $externalState -PathType Container) {
        $externalState
    } else {
        Join-Path $resolvedRoot "app\outputs\xm_mt5_forward\nq3m_spx5m"
    }
} else {
    Join-Path $resolvedRoot "state"
}

$campaigns = @()
$archiveRoot = Join-Path $resolvedRoot "archive"
if (Test-Path -LiteralPath $archiveRoot) {
    foreach ($archive in Get-ChildItem -LiteralPath $archiveRoot -Directory -Filter "campaign-*" | Sort-Object Name) {
        $state = if ($Profile -eq "forward") {
            $externalState = Join-Path $archive.FullName "state"
            if (Test-Path -LiteralPath $externalState -PathType Container) {
                $externalState
            } else {
                Join-Path $archive.FullName "nq3m_spx5m"
            }
        } else {
            Join-Path $archive.FullName "state"
        }
        if (-not (Test-Path -LiteralPath $state)) { continue }
        $daily = @()
        $dailyRoot = Join-Path $state "daily_health"
        if (Test-Path -LiteralPath $dailyRoot) {
            foreach ($file in Get-ChildItem -LiteralPath $dailyRoot -File -Filter "*.json" | Sort-Object Name) {
                $daily += [pscustomobject]@{
                    path = $file.FullName
                    payload = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
                }
            }
        }
        $prefixStates = @{}
        $take = 0
        $skip = 0
        $invalid = 0
        $prefixRoot = Join-Path $state "prefix"
        if (Test-Path -LiteralPath $prefixRoot) {
            foreach ($file in Get-ChildItem -LiteralPath $prefixRoot -Recurse -File -Filter "*.json") {
                $record = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
                $recordState = [string]$record.state
                if (-not $prefixStates.ContainsKey($recordState)) { $prefixStates[$recordState] = 0 }
                $prefixStates[$recordState]++
                foreach ($decision in @($record.payload.decisions)) {
                    if ([string]$decision.final_decision -eq "TAKE") { $take++ }
                    elseif ([string]$decision.final_decision -eq "SKIP") { $skip++ }
                    else { $invalid++ }
                }
            }
        }
        $events = @()
        foreach ($eventFile in Get-ChildItem -LiteralPath $state -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'order.*event|execution.*event|outbox' }) {
            $events += $eventFile.FullName
        }
        $campaigns += [pscustomobject]@{
            archive = $archive.FullName
            lock = if (Test-Path -LiteralPath (Join-Path $state "campaign_lock.json")) {
                Get-Content -LiteralPath (Join-Path $state "campaign_lock.json") -Raw | ConvertFrom-Json
            } else { $null }
            health = if (Test-Path -LiteralPath (Join-Path $state "health.json")) {
                Get-Content -LiteralPath (Join-Path $state "health.json") -Raw | ConvertFrom-Json
            } else { $null }
            fatal_latch = Test-Path -LiteralPath (Join-Path $state "fatal_latch.json")
            daily_health = $daily
            prefix_states = $prefixStates
            decision_counts = [pscustomobject]@{ take = $take; skip = $skip; other = $invalid }
            event_files = $events
        }
    }
}

[pscustomobject]@{
    root = $resolvedRoot
    profile = $Profile
    current_lock = if (Test-Path -LiteralPath (Join-Path $current "campaign_lock.json")) {
        Get-Content -LiteralPath (Join-Path $current "campaign_lock.json") -Raw | ConvertFrom-Json
    } else { $null }
    current_health = if (Test-Path -LiteralPath (Join-Path $current "health.json")) {
        Get-Content -LiteralPath (Join-Path $current "health.json") -Raw | ConvertFrom-Json
    } else { $null }
    current_fatal_latch = Test-Path -LiteralPath (Join-Path $current "fatal_latch.json")
    archives = $campaigns
} | ConvertTo-Json -Depth 20 -Compress
