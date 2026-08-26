$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = [IO.Path]::GetFullPath("C:\Super1")
$script:LauncherPhase = "BOOTSTRAP_START"
trap {
    try {
        $fatalState = [IO.Path]::GetFullPath((Join-Path $Root "state\health.json"))
        $launcherFailure = [IO.Path]::GetFullPath(
            (Join-Path $Root "state\launcher_failure.json")
        )
        $fatalDirectory = Split-Path -Parent $fatalState
        [void][IO.Directory]::CreateDirectory($fatalDirectory)
        $fatalStamp = [DateTimeOffset]::UtcNow.ToString("o")
        $failureJson = [ordered]@{
            schema_version = 1
            state = "LAUNCHER_FATAL_NO_SEND"
            phase = $script:LauncherPhase
            updated_at_utc = $fatalStamp
        } | ConvertTo-Json -Compress
        $failureTemp = "$launcherFailure.$([Guid]::NewGuid().ToString('N')).tmp"
        [IO.File]::WriteAllText(
            $failureTemp,
            $failureJson + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false))
        )
        if ([IO.File]::Exists($launcherFailure)) {
            [IO.File]::Replace($failureTemp, $launcherFailure, $null)
        }
        else { [IO.File]::Move($failureTemp, $launcherFailure) }
    }
    catch {
        # Continue to the health fail-safe even if phase evidence cannot be written.
    }
    try {
        $fatalState = [IO.Path]::GetFullPath((Join-Path $Root "state\health.json"))
        $fatalDirectory = Split-Path -Parent $fatalState
        [void][IO.Directory]::CreateDirectory($fatalDirectory)
        $fatalStamp = [DateTimeOffset]::UtcNow.ToString("o")
        $fatalJson = (
            '{"schema_version":1,"state":"CRITICAL_STOP",' +
            '"error":"Super1 launcher failed its protected bootstrap/runtime gate.",' +
            '"updated_at":"' + $fatalStamp + '","markets":{' +
            '"nq":{"streaming":false},"spx":{"streaming":false}},' +
            '"last_cycle":{"execution":{"state":"LAUNCHER_FATAL_NO_SEND"},' +
            '"daily_report":{"path":""},"prefix":{"path":""},"fetches":{' +
            '"nq":{"latest_bar_utc":"","conflicts":0,"rejected":0},' +
            '"spx":{"latest_bar_utc":"","conflicts":0,"rejected":0}}}}'
        )
        $fatalTemp = "$fatalState.$([Guid]::NewGuid().ToString('N')).tmp"
        [IO.File]::WriteAllText(
            $fatalTemp,
            $fatalJson + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false))
        )
        if ([IO.File]::Exists($fatalState)) {
            [IO.File]::Replace($fatalTemp, $fatalState, $null)
        }
        else {
            [IO.File]::Move($fatalTemp, $fatalState)
        }
    }
    catch {
        # The task must still return success to prevent its fixed restart-999 policy looping.
    }
    exit 0
}

$App = [IO.Path]::GetFullPath((Join-Path $Root "app"))
$script:LauncherPhase = "POWERSHELL_PATH_GATE"
$TrustedScript = [IO.Path]::GetFullPath((Join-Path $App "deploy\run_super1_windows.ps1"))
$CurrentScript = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
if (-not $CurrentScript.Equals($TrustedScript, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run Super1 only from the protected app launcher: $TrustedScript"
}
$ExpectedPSHome = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0")
)
$ExpectedPowerShell = [IO.Path]::GetFullPath(
    (Join-Path $ExpectedPSHome "powershell.exe")
)
$CurrentPowerShell = [IO.Path]::GetFullPath(
    [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
)
if (-not [IO.Path]::GetFullPath($PSHOME).Equals(
        $ExpectedPSHome,
        [StringComparison]::OrdinalIgnoreCase
    ) -or
    -not $CurrentPowerShell.Equals(
        $ExpectedPowerShell,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Super1 was not launched by trusted 64-bit Windows PowerShell."
}
$PreviousPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$TrustedPSModulePath = [IO.Path]::GetFullPath((Join-Path $ExpectedPSHome "Modules"))
$env:PSModulePath = $TrustedPSModulePath
$PowerShellPinPath = [IO.Path]::GetFullPath(
    (Join-Path $App "deploy\powershell_runtime_pin.json")
)
$script:LauncherPhase = "POWERSHELL_PIN_GATE"
$PowerShellPinLock = $null
$PowerShellHostLock = $null
if (-not (Test-Path -LiteralPath $PowerShellPinPath -PathType Leaf) -or
    (Get-Item -LiteralPath $PowerShellPinPath -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
    throw "Protected Super1 Windows PowerShell pin is missing or is a reparse point."
}
$PowerShellPinLock = [IO.File]::Open(
    $PowerShellPinPath,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
$powerShellPinReader = New-Object IO.StreamReader(
    $PowerShellPinLock,
    [Text.Encoding]::UTF8,
    $true,
    1024,
    $true
)
try { $powerShellPin = $powerShellPinReader.ReadToEnd() | ConvertFrom-Json }
finally { $powerShellPinReader.Dispose() }
if ([int]$powerShellPin.schema_version -ne 1 -or
    [string]$powerShellPin.powershell_sha256 -notmatch '^[a-f0-9]{64}$' -or
    [string]$powerShellPin.signer_subject -notmatch
        '^CN=Microsoft Windows, O=Microsoft Corporation,' -or
    [string]$powerShellPin.signer_thumbprint -notmatch '^[A-Fa-f0-9]{40}$' -or
    -not [IO.Path]::GetFullPath([string]$powerShellPin.powershell_path).Equals(
        $ExpectedPowerShell,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Protected Super1 Windows PowerShell pin is invalid."
}
if ((Get-Item -LiteralPath $ExpectedPowerShell -Force).Attributes -band
    [IO.FileAttributes]::ReparsePoint) {
    throw "Trusted Windows PowerShell executable is a reparse point."
}
$PowerShellHostLock = [IO.File]::Open(
    $ExpectedPowerShell,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
$script:LauncherPhase = "POWERSHELL_HASH_GATE"
$powerShellHasher = [Security.Cryptography.SHA256]::Create()
try {
    $powerShellSha256 = [BitConverter]::ToString(
        $powerShellHasher.ComputeHash($PowerShellHostLock)
    ).Replace("-", "").ToLowerInvariant()
    $PowerShellHostLock.Position = 0
}
finally { $powerShellHasher.Dispose() }
if ($powerShellSha256 -cne [string]$powerShellPin.powershell_sha256) {
    throw "Windows PowerShell executable differs from the protected SHA-256 pin."
}
# The elevated upgrader validates Authenticode while holding the system binary
# read lock, then seals this exact SHA-256 into the signed immutable app. Runtime
# sessions validate that pin directly so certificate-store/profile availability
# cannot turn a healthy Password-task launch into a false fatal stop.
$script:LauncherPhase = "POWERSHELL_HASH_VERIFIED"
$SecureHelper = [IO.Path]::GetFullPath((Join-Path $App "deploy\super1_secure_task.ps1"))
$script:LauncherPhase = "SECURE_HELPER_GATE"
if (-not (Test-Path -LiteralPath $SecureHelper -PathType Leaf) -or
    (Get-Item -LiteralPath $SecureHelper -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Protected Super1 secure-task helper is missing or is a reparse point."
}
. $SecureHelper
$script:LauncherPhase = "SECURE_HELPER_LOADED"

function Write-Super1ProbeProducerEnvelope {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ResultPath,
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][string]$TransactionId,
        [Parameter(Mandatory = $true)][string]$Nonce,
        [Parameter(Mandatory = $true)][string]$RequestSha256,
        [Parameter(Mandatory = $true)][string]$RunnerSid,
        [Parameter(Mandatory = $true)][string]$LauncherPath,
        [Parameter(Mandatory = $true)][string]$LauncherSha256,
        [Parameter(Mandatory = $true)][DateTimeOffset]$StartedAt,
        [Parameter(Mandatory = $true)][int]$ExitCode
    )
    if (-not (Test-Path -LiteralPath $ResultPath -PathType Leaf) -or
        (Get-Item -LiteralPath $ResultPath -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 probe did not publish a regular result file."
    }
    $producerJson = [ordered]@{
        schema_version = 1
        kind = $Kind
        transaction_id = $TransactionId
        nonce = $Nonce
        request_sha256 = $RequestSha256
        result_sha256 = Get-Super1SecureSha256 -Path $ResultPath
        producer_runner_sid = $RunnerSid
        producer_process_id = $PID
        launcher_path = $LauncherPath
        launcher_sha256 = $LauncherSha256
        started_at_utc = $StartedAt.ToUniversalTime().ToString("o")
        produced_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        exit_code = $ExitCode
    } | ConvertTo-Json -Compress
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($producerJson)
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }
}

$Python = [IO.Path]::GetFullPath((Join-Path $Root "venv311\Scripts\python.exe"))
$Runner = [IO.Path]::GetFullPath((Join-Path $App "scripts\run_super1_xm_mt5_forward.py"))
$FlatDiagnostic = [IO.Path]::GetFullPath((Join-Path $App "scripts\check_mt5_flat.py"))
$RuntimeConfig = [IO.Path]::GetFullPath((Join-Path $App "live_forward\super1_xm_mt5_demo_config.json"))
$TerminalPinPath = [IO.Path]::GetFullPath((Join-Path $App "deploy\terminal_runtime_pin.json"))
$TerminalPointer = [IO.Path]::GetFullPath((Join-Path $Root "mt5-terminal.txt"))
$CanonicalTerminal = [IO.Path]::GetFullPath((Join-Path $Root "mt5-clean5833\terminal64.exe"))
$ProbeControl = [IO.Path]::GetFullPath((Join-Path $Root "probe-control"))
$ProbeRequest = [IO.Path]::GetFullPath((Join-Path $ProbeControl "active.json"))
# Policy compatibility marker: kind -notin @("flat", "rollover_init") is extended only by the fixed smoke kind below.
$RunnerSid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value

foreach ($required in @(
    $Python, $Runner, $FlatDiagnostic, $RuntimeConfig, $TerminalPinPath,
    $TerminalPointer, $CanonicalTerminal, $ProbeControl
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Super1 protected runtime dependency is missing: $required"
    }
    if ((Get-Item -LiteralPath $required -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 protected runtime dependency is a reparse point: $required"
    }
}
Assert-Super1SecureDirectoryAcl -Path $ProbeControl -RunnerSid $RunnerSid

$PreviousPythonHome = $env:PYTHONHOME
$PreviousPythonPath = $env:PYTHONPATH
$PreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
$PasswordPtr = [IntPtr]::Zero
$PointerLock = $null
$TerminalLock = $null
$ProbeLock = $null
$TransactionRequestLock = $null
try {
    $env:PYTHONHOME = $null
    $env:PYTHONPATH = $null
    $env:PYTHONDONTWRITEBYTECODE = "1"

    $script:LauncherPhase = "TERMINAL_PIN_GATE"
    $pin = [IO.File]::ReadAllText($TerminalPinPath) | ConvertFrom-Json
    if ([int]$pin.schema_version -ne 1 -or
        [string]$pin.terminal_sha256 -notmatch '^[a-f0-9]{64}$') {
        throw "Super1 terminal pin is invalid."
    }
    $pinnedTerminal = [IO.Path]::GetFullPath([string]$pin.terminal_path)
    if (-not $pinnedTerminal.Equals($CanonicalTerminal, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Super1 terminal pin does not name the canonical terminal."
    }
    $PointerLock = [IO.File]::Open(
        $TerminalPointer,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $pointerReader = New-Object IO.StreamReader($PointerLock, [Text.Encoding]::UTF8, $true, 1024, $true)
    try { $pointerTerminal = [IO.Path]::GetFullPath($pointerReader.ReadToEnd().Trim()) }
    finally { $pointerReader.Dispose() }
    if (-not $pointerTerminal.Equals($pinnedTerminal, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Super1 terminal pointer differs from the protected terminal pin."
    }
    $TerminalLock = [IO.File]::Open(
        $pinnedTerminal,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    if ((Get-Super1SecureSha256 -Path $pinnedTerminal) -cne [string]$pin.terminal_sha256) {
        throw "Super1 terminal executable differs from the protected SHA-256 pin."
    }

    $script:LauncherPhase = "BROKER_CREDENTIAL_GATE"
    $server = (Get-Content -Raw (Join-Path $Root "xm-server.txt")).Trim()
    $runtime = [IO.File]::ReadAllText($RuntimeConfig) | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($server) -or
        $server -cne [string]$runtime.expected_server) {
        throw "Super1 protected server config differs from the signed broker identity."
    }
    $env:XM_MT5_SERVER = $server
    $env:XM_MT5_TERMINAL_PATH = $pinnedTerminal
    try {
        $SecurePassword = (Get-Content -Raw (Join-Path $Root "xm-password.dpapi")).Trim() |
            ConvertTo-SecureString
        $PasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
        $env:XM_MT5_READ_ONLY_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
            $PasswordPtr
        )
    }
    catch [Security.Cryptography.CryptographicException] {
        # This legacy blob belongs to the pre-service account. The pinned portable
        # demo terminal may use its saved session; the Python transport still
        # verifies login/server/company/demo identity before every broker action.
        $script:LauncherPhase = "BROKER_SAVED_SESSION"
        $env:XM_MT5_READ_ONLY_PASSWORD = $null
    }

    if (Test-Path -LiteralPath $ProbeRequest -PathType Leaf) {
        $script:LauncherPhase = "PROBE_REQUEST_GATE"
        $requestEvidence = Open-Super1SecureLockedFile -Path $ProbeRequest -RunnerSid $RunnerSid
        $ProbeLock = $requestEvidence.lock
        $request = [string]$requestEvidence.content | ConvertFrom-Json
        $transactionId = [string]$request.transaction_id
        $nonce = [string]$request.nonce
        try {
            $requestedAt = [DateTimeOffset]::Parse(
                [string]$request.requested_at_utc
            ).ToUniversalTime()
        }
        catch { throw "Super1 protected probe request timestamp is invalid." }
        $launcherSha256 = Get-Super1SecureSha256 -Path $TrustedScript
        if ([int]$request.schema_version -ne 1 -or
            $transactionId -notmatch '^[a-f0-9]{32}$' -or
            $nonce -notmatch '^[a-f0-9]{32}$' -or
            [string]$request.kind -notin @("flat", "rollover_init", "smoke") -or
            [string]$request.expected_runner_sid -cne $RunnerSid -or
            [string]$request.expected_launcher_sha256 -cne $launcherSha256 -or
            $requestedAt -lt [DateTimeOffset]::UtcNow.AddSeconds(-30) -or
            $requestedAt -gt [DateTimeOffset]::UtcNow.AddSeconds(5)) {
            throw "Super1 protected probe request is invalid."
        }
        $transaction = [IO.Path]::GetFullPath(
            (Join-Path (Join-Path $Root "archive") ("readiness-" + $transactionId))
        )
        $expectedResult = [IO.Path]::GetFullPath((Join-Path $transaction "output\result.json"))
        $expectedProducer = [IO.Path]::GetFullPath((Join-Path $transaction "output\producer.json"))
        $expectedTransactionRequest = [IO.Path]::GetFullPath((Join-Path $transaction "request.json"))
        $resultPath = [IO.Path]::GetFullPath([string]$request.result_path)
        $producerPath = [IO.Path]::GetFullPath([string]$request.producer_path)
        $transactionRequestPath = [IO.Path]::GetFullPath([string]$request.request_path)
        if (-not $resultPath.Equals($expectedResult, [StringComparison]::OrdinalIgnoreCase) -or
            -not $producerPath.Equals($expectedProducer, [StringComparison]::OrdinalIgnoreCase) -or
            -not $transactionRequestPath.Equals(
                $expectedTransactionRequest,
                [StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Super1 probe paths are outside their private transaction."
        }
        $transactionRequestEvidence = Open-Super1SecureLockedFile `
            -Path $transactionRequestPath `
            -RunnerSid $RunnerSid
        $TransactionRequestLock = $transactionRequestEvidence.lock
        if ([string]$transactionRequestEvidence.sha256 -cne [string]$requestEvidence.sha256 -or
            [string]$transactionRequestEvidence.content -cne [string]$requestEvidence.content) {
            throw "Super1 active request differs from its protected transaction copy."
        }
        $probeStartedAt = [DateTimeOffset]::UtcNow
        if ([string]$request.kind -ceq "smoke") {
            $script:LauncherPhase = "DEMO_SMOKE"
            $smokeOutput = & $Python -I -E -B $Runner --output-root (Join-Path $Root "state") smoke-order --confirm-demo
            $smokeCode = [int]$LASTEXITCODE
            [IO.File]::WriteAllText($resultPath, ($smokeOutput -join "`n") + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
            Write-Super1ProbeProducerEnvelope -Path $producerPath -ResultPath $resultPath -Kind "smoke" -TransactionId $transactionId -Nonce $nonce -RequestSha256 ([string]$requestEvidence.sha256) -RunnerSid $RunnerSid -LauncherPath $TrustedScript -LauncherSha256 $launcherSha256 -StartedAt $probeStartedAt -ExitCode $smokeCode
            if ($smokeCode -ne 0) { throw "Super1 demo smoke failed." }
            exit 0
        }
        if ([string]$request.kind -ceq "flat") {
            $script:LauncherPhase = "FLAT_DIAGNOSTIC"
            & $Python -I -E -B $FlatDiagnostic `
                --root $Root `
                --config "live_forward\super1_xm_mt5_demo_config.json" `
                --profile super1 `
                --output $resultPath `
                --evidence-nonce $nonce
            $flatCode = [int]$LASTEXITCODE
            $script:LauncherPhase = "FLAT_PRODUCER_ENVELOPE"
            Write-Super1ProbeProducerEnvelope `
                -Path $producerPath `
                -ResultPath $resultPath `
                -Kind "flat" `
                -TransactionId $transactionId `
                -Nonce $nonce `
                -RequestSha256 ([string]$requestEvidence.sha256) `
                -RunnerSid $RunnerSid `
                -LauncherPath $TrustedScript `
                -LauncherSha256 $launcherSha256 `
                -StartedAt $probeStartedAt `
                -ExitCode $flatCode
            exit 0
        }

        & $Python -I -E -B $Runner --output-root (Join-Path $Root "state") init
        $initCode = [int]$LASTEXITCODE
        $initResult = [ordered]@{
            schema_version = 1
            evidence_nonce = $nonce
            checked_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
            state = if ($initCode -eq 0) { "INITIALIZED" } else { "FAILED" }
            exit_code = $initCode
        } | ConvertTo-Json -Compress
        $initBytes = (New-Object Text.UTF8Encoding($false)).GetBytes($initResult)
        $initStream = [IO.File]::Open(
            $resultPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $initStream.Write($initBytes, 0, $initBytes.Length)
            $initStream.Flush($true)
        }
        finally { $initStream.Dispose() }
        Write-Super1ProbeProducerEnvelope `
            -Path $producerPath `
            -ResultPath $resultPath `
            -Kind "rollover_init" `
            -TransactionId $transactionId `
            -Nonce $nonce `
            -RequestSha256 ([string]$requestEvidence.sha256) `
            -RunnerSid $RunnerSid `
            -LauncherPath $TrustedScript `
            -LauncherSha256 $launcherSha256 `
            -StartedAt $probeStartedAt `
            -ExitCode $initCode
        exit 0
    }

    $script:LauncherPhase = "DAEMON"
    & $Python -I -E -B $Runner --output-root (Join-Path $Root "state") daemon
    $daemonCode = [int]$LASTEXITCODE
    throw "Super1 daemon exited unexpectedly with persistent code: $daemonCode"
}
finally {
    $env:XM_MT5_SERVER = $null
    $env:XM_MT5_TERMINAL_PATH = $null
    $env:XM_MT5_READ_ONLY_PASSWORD = $null
    $env:PYTHONDONTWRITEBYTECODE = $PreviousBytecode
    $env:PYTHONHOME = $PreviousPythonHome
    $env:PYTHONPATH = $PreviousPythonPath
    if ($PasswordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPtr)
    }
    if ($TransactionRequestLock) { $TransactionRequestLock.Dispose() }
    if ($ProbeLock) { $ProbeLock.Dispose() }
    if ($TerminalLock) { $TerminalLock.Dispose() }
    if ($PointerLock) { $PointerLock.Dispose() }
    if ($PowerShellHostLock) { $PowerShellHostLock.Dispose() }
    if ($PowerShellPinLock) { $PowerShellPinLock.Dispose() }
    $env:PSModulePath = $PreviousPSModulePath
}
