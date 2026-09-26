$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# This is the only source of Super1 local-runtime topology.  All scripts are
# copied into the signed app and dot-source this file before doing any work.
$script:Super1RuntimeRoot = "C:\Super1"
$runtimeScriptDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
$runtimeAppDirectory = [IO.Path]::GetFullPath((Join-Path $runtimeScriptDirectory ".."))
if ([IO.Path]::GetFileName($runtimeScriptDirectory).Equals("deploy", [StringComparison]::OrdinalIgnoreCase) -and
    [IO.Path]::GetFileName($runtimeAppDirectory).Equals("app", [StringComparison]::OrdinalIgnoreCase)) {
    $script:Super1RuntimeRoot = [IO.Path]::GetFullPath((Join-Path $runtimeAppDirectory ".."))
}
$script:Super1RuntimeRoot = [IO.Path]::GetFullPath($script:Super1RuntimeRoot)
$script:Super1RuntimeContract = [ordered]@{
    schema_version = 1
    root = $script:Super1RuntimeRoot
    app = Join-Path $script:Super1RuntimeRoot "app"
    terminal = Join-Path $script:Super1RuntimeRoot "mt5\terminal64.exe"
    python = Join-Path $script:Super1RuntimeRoot "venv311\Scripts\python.exe"
    state = Join-Path $script:Super1RuntimeRoot "state"
    control = Join-Path $script:Super1RuntimeRoot "control"
    runtime_trust = Join-Path $script:Super1RuntimeRoot "runtime-trust"
    health = Join-Path $script:Super1RuntimeRoot "state\health.json"
    launcher_failure = Join-Path $script:Super1RuntimeRoot "state\launcher_failure.json"
    watchdog_status = Join-Path $script:Super1RuntimeRoot "state\watchdog\watchdog_status.json"
    watchdog_state = Join-Path $script:Super1RuntimeRoot "state\watchdog\watchdog_state.json"
    release_archive = Join-Path $script:Super1RuntimeRoot "super1-forward.zip"
    release_manifest = Join-Path $script:Super1RuntimeRoot "super1-forward.manifest.json"
    release_signature = Join-Path $script:Super1RuntimeRoot "super1-forward.manifest.sig"
    main_task = "Super1XM"
    watchdog_task = "Super1Watchdog"
    runner_account = "Super1Runner"
    credential_relative = "AppData\Local\Super1\xm-password.dpapi"
    order_mutex = "Global\Super1OrderTransport"
    runtime_config = "live_forward\super1_xm_mt5_demo_config.json"
    manifest = "research_candidates\super1\super1_manifest.json"
    calendar = "live_forward\calendars\us_equity_rth_2026.json"
    launcher = "deploy\run_super1_windows.ps1"
    watchdog = "deploy\watchdog_windows.ps1"
    secure_task_helper = "deploy\super1_secure_task.ps1"
    readiness = "deploy\test_super1_local_readiness.ps1"
}

function Get-Super1RuntimeContract {
    $copy = [ordered]@{}
    foreach ($key in $script:Super1RuntimeContract.Keys) {
        $copy[$key] = $script:Super1RuntimeContract[$key]
    }
    return [pscustomobject]$copy
}

function Get-Super1RuntimePath {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("root", "app", "terminal", "python", "state", "control", "runtime_trust", "health", "launcher_failure", "watchdog_status", "watchdog_state", "release_archive", "release_manifest", "release_signature")]
        [string]$Name
    )
    return [string]$script:Super1RuntimeContract[$Name]
}

function Get-Super1RuntimeAppPath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    if ([IO.Path]::IsPathRooted($RelativePath)) {
        throw "Super1 app path must be relative: $RelativePath"
    }
    $candidate = [IO.Path]::GetFullPath((Join-Path (Get-Super1RuntimePath -Name app) $RelativePath))
    $app = [IO.Path]::GetFullPath((Get-Super1RuntimePath -Name app))
    if (-not $candidate.StartsWith($app + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Super1 app path escapes the protected app: $RelativePath"
    }
    return $candidate
}

function Get-Super1RuntimeCredentialPath {
    $local = [Environment]::GetFolderPath("LocalApplicationData")
    if ([string]::IsNullOrWhiteSpace($local)) { throw "Current Runner LocalApplicationData is unavailable." }
    return [IO.Path]::GetFullPath((Join-Path $local "Super1\xm-password.dpapi"))
}

function Assert-Super1RuntimeContract {
    $contract = Get-Super1RuntimeContract
    $root = [IO.Path]::GetFullPath($script:Super1RuntimeRoot)
    if ([int]$contract.schema_version -ne 1 -or
        [string]$contract.root -cne $root -or
        [string]$contract.app -cne (Join-Path $root "app") -or
        [string]$contract.state -cne (Join-Path $root "state") -or
        [string]$contract.control -cne (Join-Path $root "control") -or
        [string]$contract.runtime_trust -cne (Join-Path $root "runtime-trust") -or
        [string]$contract.health -cne (Join-Path $root "state\health.json") -or
        [string]$contract.launcher_failure -cne (Join-Path $root "state\launcher_failure.json") -or
        [string]$contract.watchdog_status -cne (Join-Path $root "state\watchdog\watchdog_status.json") -or
        [string]$contract.watchdog_state -cne (Join-Path $root "state\watchdog\watchdog_state.json") -or
        [string]$contract.release_archive -cne (Join-Path $root "super1-forward.zip") -or
        [string]$contract.release_manifest -cne (Join-Path $root "super1-forward.manifest.json") -or
        [string]$contract.release_signature -cne (Join-Path $root "super1-forward.manifest.sig") -or
        [string]$contract.terminal -cne (Join-Path $root "mt5\terminal64.exe") -or
        [string]$contract.python -cne (Join-Path $root "venv311\Scripts\python.exe") -or
        [string]$contract.order_mutex -cne "Global\Super1OrderTransport" -or
        [string]$contract.main_task -cne "Super1XM" -or
        [string]$contract.watchdog_task -cne "Super1Watchdog" -or
        [string]$contract.runner_account -cne "Super1Runner" -or
        [string]$contract.credential_relative -cne "AppData\Local\Super1\xm-password.dpapi") {
        throw "Super1 runtime contract is invalid."
    }
    return $contract
}
