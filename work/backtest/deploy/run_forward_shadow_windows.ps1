[CmdletBinding()]
param(
    [switch]$AclProbeOnly,
    [switch]$BrokerProbeOnly,
    [ValidateRange(0, 2147483647)]
    [int]$ExpectedLogin = 0,
    [string]$BrokerProbeTerminalConfig = "",
    [ValidatePattern('^$|^[a-f0-9]{64}$')]
    [string]$ExpectedBrokerProbeConfigSha256 = "",
    [ValidateSet("ReadOnly", "Production")]
    [string]$BrokerProbeMode = "ReadOnly",
    [string]$BrokerProbeDiagnosticPath = "",
    [string]$Root = "C:\ForwardShadow"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = [IO.Path]::GetFullPath($Root)
$RunnerTokenSentinel = Join-Path $Root "runner-token-sentinel.dat"
$ExpectedSentinelMarker = "FORWARD_SHADOW_RUNNER_TOKEN_SENTINEL_V1"

function Write-ForwardBrokerProbeDiagnostic {
    param(
        [Parameter(Mandatory = $true)][string]$Phase,
        [string]$Message = "",
        [int]$ExitCode = -1
    )
    if ([string]::IsNullOrWhiteSpace($BrokerProbeDiagnosticPath)) {
        return
    }
    $payload = [ordered]@{
        schema_version = 1
        checked_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        phase = $Phase
        broker_probe_mode = $BrokerProbeMode
        exit_code = $ExitCode
        message = $Message
    } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText(
        $BrokerProbeDiagnosticPath,
        $payload,
        (New-Object Text.UTF8Encoding($false))
    )
}

function Get-ForwardSha256Lower {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace(
            "-",
            ""
        ).ToLowerInvariant()
    }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Start-ForwardTerminalWithConfig {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$TerminalPath,
        [Parameter(Mandatory = $true)][ValidateSet("ReadOnly", "Production")][string]$Mode
    )
    $resolvedConfig = [IO.Path]::GetFullPath($ConfigPath)
    $rootPrefix = $Root.TrimEnd('\') + '\'
    if (
        -not $resolvedConfig.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf) -or
        (Get-Item -LiteralPath $resolvedConfig -Force).Attributes.HasFlag(
            [IO.FileAttributes]::ReparsePoint
        ) -or
        (Get-ForwardSha256Lower -Path $resolvedConfig) -cne $ExpectedSha256
    ) { throw "ForwardShadow read-only terminal configuration is missing or untrusted." }
    $configText = [IO.File]::ReadAllText($resolvedConfig).Replace("`r`n", "`n")
    $expectedSwitch = if ($Mode -ceq "ReadOnly") { "0" } else { "1" }
    foreach ($required in @(
        "[Experts]",
        "Enabled=$expectedSwitch",
        "AllowLiveTrading=$expectedSwitch",
        "AllowDllImport=0"
    )) {
        if ($configText.IndexOf($required, [StringComparison]::Ordinal) -lt 0) {
            throw "ForwardShadow terminal configuration does not match $Mode mode."
        }
    }
    if ($configText -match '(?m)^Api=') {
        throw "ForwardShadow terminal configuration contains an undocumented API switch."
    }
    $configLock = [IO.File]::Open(
        $resolvedConfig,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = [IO.Path]::GetFullPath($TerminalPath)
        $start.Arguments = "/skipupdate /config:`"$resolvedConfig`""
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $process = [Diagnostics.Process]::Start($start)
        if ($null -eq $process) {
            throw "ForwardShadow read-only terminal did not start."
        }
        $process.Dispose()
        Start-Sleep -Seconds 2
    }
    finally { $configLock.Dispose() }
}

function Assert-ForwardRunnerTokenRestricted {
    if (-not (Test-Path -LiteralPath $RunnerTokenSentinel -PathType Leaf)) {
        throw "ForwardShadow runner token sentinel is missing."
    }
    $sentinel = Get-Item -LiteralPath $RunnerTokenSentinel -Force
    if (
        $sentinel.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) -or
        $sentinel.Attributes.HasFlag([IO.FileAttributes]::ReadOnly)
    ) {
        throw "ForwardShadow runner token sentinel attributes are unsafe for an ACL probe."
    }
    if ([IO.File]::ReadAllText($RunnerTokenSentinel) -cne $ExpectedSentinelMarker) {
        throw "ForwardShadow runner token sentinel content is invalid."
    }

    try {
        $writeProbe = [IO.File]::Open(
            $RunnerTokenSentinel,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Write,
            [IO.FileShare]::Read
        )
        $writeProbe.Dispose()
        throw "ForwardShadow runner token has effective write access to protected code storage."
    }
    catch [UnauthorizedAccessException] {}

    try {
        [IO.File]::Delete($RunnerTokenSentinel)
        throw "ForwardShadow runner token has effective delete access to protected code storage."
    }
    catch [UnauthorizedAccessException] {}
}

if ($BrokerProbeOnly -and $ExpectedLogin -le 0) {
    throw "ForwardShadow broker probe requires an expected MT5 login."
}
if ($BrokerProbeOnly -and (
    [string]::IsNullOrWhiteSpace($BrokerProbeTerminalConfig) -or
    [string]::IsNullOrWhiteSpace($ExpectedBrokerProbeConfigSha256) -or
    [string]::IsNullOrWhiteSpace($BrokerProbeDiagnosticPath)
)) { throw "ForwardShadow broker probe requires a pinned read-only terminal configuration." }
if ($BrokerProbeOnly) {
    $BrokerProbeDiagnosticPath = [IO.Path]::GetFullPath($BrokerProbeDiagnosticPath)
    $statePrefix = [IO.Path]::GetFullPath((Join-Path $Root "state")).TrimEnd('\') + '\'
    if (-not $BrokerProbeDiagnosticPath.StartsWith(
            $statePrefix,
            [StringComparison]::OrdinalIgnoreCase
        ) -or (Test-Path -LiteralPath $BrokerProbeDiagnosticPath)) {
        throw "ForwardShadow broker probe diagnostic target is unsafe or stale."
    }
    Write-ForwardBrokerProbeDiagnostic -Phase "SCRIPT_START"
}

try { Assert-ForwardRunnerTokenRestricted }
catch {
    Write-ForwardBrokerProbeDiagnostic `
        -Phase "TOKEN_RESTRICTION_FAILURE" `
        -Message $_.Exception.Message `
        -ExitCode 1
    throw
}
if ($AclProbeOnly) { exit 0 }

$Python = Join-Path $Root "venv311\Scripts\python.exe"
$Server = (Get-Content -Raw (Join-Path $Root "xm-server.txt")).Trim()
$Terminal = (Get-Content -Raw (Join-Path $Root "mt5-terminal.txt")).Trim()
$PasswordPath = Join-Path $Root "xm-readonly-password.dpapi"
$PasswordPtr = [IntPtr]::Zero

try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:XM_MT5_SERVER = $Server
    $env:XM_MT5_TERMINAL_PATH = $Terminal
    if (Test-Path -LiteralPath $PasswordPath -PathType Leaf) {
        $SecurePassword = (Get-Content -Raw $PasswordPath).Trim() | ConvertTo-SecureString
        $PasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
        $env:XM_MT5_READ_ONLY_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPtr)
    }
    if ($BrokerProbeOnly) {
        Write-ForwardBrokerProbeDiagnostic -Phase "TERMINAL_START"
        Start-ForwardTerminalWithConfig `
            -ConfigPath $BrokerProbeTerminalConfig `
            -ExpectedSha256 $ExpectedBrokerProbeConfigSha256 `
            -TerminalPath $Terminal `
            -Mode $BrokerProbeMode
        Write-ForwardBrokerProbeDiagnostic -Phase "PYTHON_PROBE_START"
        $probe = @'
import json
import os
import sys

import MetaTrader5 as mt5

terminal = sys.argv[1]
expected_login = int(sys.argv[2])
expected_server = sys.argv[3]
expected_mode = sys.argv[4]
initialized = False
try:
    if not mt5.initialize(path=terminal, timeout=30000):
        raise RuntimeError(f"initialize failed: {mt5.last_error()!r}")
    initialized = True
    terminal_info = mt5.terminal_info()
    account_info = mt5.account_info()
    orders = mt5.orders_get()
    positions = mt5.positions_get()
    if terminal_info is None or not bool(terminal_info.connected):
        raise RuntimeError(f"terminal is not connected: {mt5.last_error()!r}")
    if account_info is None:
        raise RuntimeError(f"account_info failed: {mt5.last_error()!r}")
    if int(account_info.login) != expected_login:
        raise RuntimeError("unexpected MT5 account login")
    if str(account_info.server) != expected_server:
        raise RuntimeError("unexpected MT5 account server")
    if int(account_info.trade_mode) != int(mt5.ACCOUNT_TRADE_MODE_DEMO):
        raise RuntimeError("MT5 account is not demo-only")
    expected_data_root = os.path.normcase(os.path.abspath(os.path.join(
        os.environ["APPDATA"], "MetaQuotes", "Terminal"
    )))
    actual_data_path = os.path.normcase(os.path.abspath(str(terminal_info.data_path)))
    if os.path.commonpath((expected_data_root, actual_data_path)) != expected_data_root:
        raise RuntimeError("MT5 terminal attached to another Windows profile")
    if expected_mode == "ReadOnly":
        if bool(terminal_info.trade_allowed) or bool(terminal_info.tradeapi_disabled):
            raise RuntimeError("MT5 terminal is not locked in read-only proof mode")
    elif expected_mode == "Production":
        if not bool(terminal_info.trade_allowed) or bool(terminal_info.tradeapi_disabled):
            raise RuntimeError("MT5 terminal is not restored to production trading mode")
        if not bool(account_info.trade_allowed) or not bool(account_info.trade_expert):
            raise RuntimeError("MT5 account does not permit automated demo trading")
    else:
        raise RuntimeError("invalid terminal proof mode")
    if orders is None:
        raise RuntimeError(f"orders_get failed: {mt5.last_error()!r}")
    if positions is None:
        raise RuntimeError(f"positions_get failed: {mt5.last_error()!r}")
    print(json.dumps({
        "connected": True,
        "login": int(account_info.login),
        "server": str(account_info.server),
        "demo_verified": True,
        "terminal_mode": expected_mode,
        "data_path": str(terminal_info.data_path),
        "account_trade_allowed": bool(account_info.trade_allowed),
        "account_trade_expert": bool(account_info.trade_expert),
        "terminal_trade_allowed": bool(terminal_info.trade_allowed),
        "terminal_tradeapi_disabled": bool(terminal_info.tradeapi_disabled),
        "orders_read": len(orders),
        "positions_read": len(positions),
    }, sort_keys=True))
finally:
    if initialized:
        mt5.shutdown()
'@
        $probeOutput = @(
            $probe | & $Python -I -E -B - $Terminal ([string]$ExpectedLogin) $Server $BrokerProbeMode 2>&1
        )
        $probeExit = $LASTEXITCODE
        $probeMessage = @($probeOutput | ForEach-Object { [string]$_ }) -join " | "
        Write-ForwardBrokerProbeDiagnostic `
            -Phase "PYTHON_PROBE_FINISHED" `
            -Message $probeMessage `
            -ExitCode $probeExit
        exit $probeExit
    }
    $flatRequestPath = Join-Path $Root "broker-probe-request.json"
    if (Test-Path -LiteralPath $flatRequestPath) {
        if (-not (Test-Path -LiteralPath $flatRequestPath -PathType Leaf)) {
            throw "ForwardShadow broker probe request changed type."
        }
        $request = Get-Content -LiteralPath $flatRequestPath -Raw | ConvertFrom-Json
        if (
            [int]$request.schema_version -ne 1 -or
            [string]$request.mode -cne "FLAT" -or
            [string]$request.nonce -cnotmatch '^[a-f0-9]{32}$' -or
            [string]$request.terminal_config_sha256 -cnotmatch '^[a-f0-9]{64}$'
        ) {
            throw "ForwardShadow broker probe request is invalid."
        }
        $resultRoot = [IO.Path]::GetFullPath(
            (Join-Path $Root "broker-probe-results")
        )
        if (-not (Test-Path -LiteralPath $resultRoot -PathType Container)) {
            throw "ForwardShadow broker probe result directory is missing."
        }
        $nonceResultRoot = [IO.Path]::GetFullPath(
            (Join-Path $resultRoot ([string]$request.nonce))
        )
        $resultPath = [IO.Path]::GetFullPath(
            (Join-Path $nonceResultRoot "result.json")
        )
        $diagnosticPath = [IO.Path]::GetFullPath(
            [string]$request.diagnostic_path
        )
        $resultPrefix = $nonceResultRoot.TrimEnd('\') + '\'
        if (
            -not $nonceResultRoot.StartsWith(
                ($resultRoot.TrimEnd('\') + '\'),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            -not (Test-Path -LiteralPath $nonceResultRoot -PathType Container) -or
            -not $resultPath.StartsWith($resultPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            (Test-Path -LiteralPath $resultPath) -or
            -not $diagnosticPath.Equals(
                (Join-Path $nonceResultRoot "diagnostic.json"),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            (Test-Path -LiteralPath $diagnosticPath)
        ) {
            throw "ForwardShadow broker probe result target is unsafe or stale."
        }
        $BrokerProbeDiagnosticPath = $diagnosticPath
        Write-ForwardBrokerProbeDiagnostic -Phase "REQUEST_VALIDATED"
        $terminalConfig = [IO.Path]::GetFullPath(
            [string]$request.terminal_config_path
        )
        if (-not $terminalConfig.Equals(
            (Join-Path $nonceResultRoot "terminal-readonly.ini"),
            [StringComparison]::OrdinalIgnoreCase
        )) { throw "ForwardShadow broker probe terminal configuration path is invalid." }
        Write-ForwardBrokerProbeDiagnostic -Phase "TERMINAL_START"
        Start-ForwardTerminalWithConfig `
            -ConfigPath $terminalConfig `
            -ExpectedSha256 ([string]$request.terminal_config_sha256) `
            -TerminalPath $Terminal `
            -Mode ReadOnly
        Write-ForwardBrokerProbeDiagnostic -Phase "PYTHON_PROBE_START"
        $flatOutput = @(
            & $Python -I -E -B (Join-Path $Root "app\scripts\check_mt5_flat.py") `
                --root $Root `
                --config "live_forward\xm_mt5_demo_config.json" `
                --profile forward `
                --output $resultPath `
                --evidence-nonce ([string]$request.nonce) `
                --read-only-proof 2>&1
        )
        $flatExit = $LASTEXITCODE
        Write-ForwardBrokerProbeDiagnostic `
            -Phase "PYTHON_PROBE_FINISHED" `
            -Message (@($flatOutput | ForEach-Object { [string]$_ }) -join " | ") `
            -ExitCode $flatExit
        exit $flatExit
    }
    $productionConfig = Join-Path $Root "mt5-production.ini"
    $productionConfigText = "[Experts]`r`nEnabled=1`r`nAllowLiveTrading=1`r`nAllowDllImport=0`r`nWebRequest=0`r`n"
    $productionBytes = (New-Object Text.UTF8Encoding($false)).GetBytes(
        $productionConfigText
    )
    $productionSha = [Security.Cryptography.SHA256]::Create()
    try {
        $expectedProductionHash = ([BitConverter]::ToString(
            $productionSha.ComputeHash($productionBytes)
        )).Replace("-", "").ToLowerInvariant()
    }
    finally { $productionSha.Dispose() }
    Start-ForwardTerminalWithConfig `
        -ConfigPath $productionConfig `
        -ExpectedSha256 $expectedProductionHash `
        -TerminalPath $Terminal `
        -Mode Production
    & $Python -I -E -B (Join-Path $Root "app\scripts\run_xm_mt5_forward.py") `
        --output-root (Join-Path $Root "state") daemon
    exit $LASTEXITCODE
}
catch {
    Write-ForwardBrokerProbeDiagnostic -Phase "POWERSHELL_FAILURE" -Message $_.Exception.Message
    throw
}
finally {
    $env:XM_MT5_SERVER = $null
    $env:XM_MT5_TERMINAL_PATH = $null
    $env:XM_MT5_READ_ONLY_PASSWORD = $null
    $env:PYTHONDONTWRITEBYTECODE = $null
    if ($PasswordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPtr)
    }
}
