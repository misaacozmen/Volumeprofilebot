$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$RuntimeContract = Assert-Super1RuntimeContract

$Root = [IO.Path]::GetFullPath([string]$RuntimeContract.root)
$App = [IO.Path]::GetFullPath([string]$RuntimeContract.app)
$script:LauncherPhase = "BOOTSTRAP_START"
trap {
    $failure = $_.Exception
    $sanitizedMessage = if ($null -eq $failure) { "Unknown launcher failure." } else { [string]$failure.Message }
    $sanitizedMessage = $sanitizedMessage -replace "(?i)(password|secret|token)=?[^ ;,}]+", '$1=***'
    try {
        $trapContract = Get-Variable -Name RuntimeContract -ValueOnly -ErrorAction SilentlyContinue
        # The literal is the emergency copy of the contract value used only if
        # the contract itself failed before the trap could resolve it.
        $fatalState = if ($null -ne $trapContract) { [IO.Path]::GetFullPath([string]$trapContract.health) } else { [IO.Path]::GetFullPath((Join-Path $Root "state\health.json")) }
        $launcherFailure = if ($null -ne $trapContract) { [IO.Path]::GetFullPath([string]$trapContract.launcher_failure) } else { [IO.Path]::GetFullPath((Join-Path $Root "state\launcher_failure.json")) }
        $fatalDirectory = Split-Path -Parent $fatalState
        [void][IO.Directory]::CreateDirectory($fatalDirectory)
        $fatalStamp = [DateTimeOffset]::UtcNow.ToString("o")
        $failureJson = [ordered]@{
            schema_version = 1
            state = "LAUNCHER_FATAL_NO_SEND"
            phase = $script:LauncherPhase
            exception_type = if ($null -eq $failure) { "Unknown" } else { $failure.GetType().FullName }
            error = $sanitizedMessage
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
        $trapContract = Get-Variable -Name RuntimeContract -ValueOnly -ErrorAction SilentlyContinue
        $fatalState = if ($null -ne $trapContract) { [IO.Path]::GetFullPath([string]$trapContract.health) } else { [IO.Path]::GetFullPath((Join-Path $Root "state\health.json")) }
        $fatalDirectory = Split-Path -Parent $fatalState
        [void][IO.Directory]::CreateDirectory($fatalDirectory)
        $fatalStamp = [DateTimeOffset]::UtcNow.ToString("o")
        $fatalJson = [ordered]@{
            schema_version = 1
            state = "CRITICAL_STOP"
            error = $sanitizedMessage
            updated_at = $fatalStamp
            markets = [ordered]@{
                nq = [ordered]@{ streaming = $false }
                spx = [ordered]@{ streaming = $false }
            }
            last_cycle = [ordered]@{
                execution = [ordered]@{ state = "LAUNCHER_FATAL_NO_SEND" }
                daily_report = [ordered]@{ path = "" }
                prefix = [ordered]@{ path = "" }
                fetches = [ordered]@{
                    nq = [ordered]@{ latest_bar_utc = ""; conflicts = 0; rejected = 0 }
                    spx = [ordered]@{ latest_bar_utc = ""; conflicts = 0; rejected = 0 }
                }
            }
        } | ConvertTo-Json -Depth 12 -Compress
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
        # A fatal launcher failure must remain observable as a nonzero task result.
    }
    exit 78
}

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
    (Join-Path ([string]$RuntimeContract.runtime_trust) "powershell_runtime_pin.json")
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

$Python = [IO.Path]::GetFullPath([string]$RuntimeContract.python)
$Runner = [IO.Path]::GetFullPath((Join-Path $App "scripts\run_super1_xm_mt5_forward.py"))
$FlatDiagnostic = [IO.Path]::GetFullPath((Join-Path $App "scripts\check_mt5_flat.py"))
$RuntimeConfig = [IO.Path]::GetFullPath((Join-Path $App ([string]$RuntimeContract.runtime_config)))
$CredentialPath = Get-Super1RuntimeCredentialPath
$TerminalPinPath = [IO.Path]::GetFullPath((Join-Path ([string]$RuntimeContract.runtime_trust) "terminal_runtime_pin.json"))
$CanonicalTerminal = [IO.Path]::GetFullPath([string]$RuntimeContract.terminal)
$ProbeControl = [IO.Path]::GetFullPath([string]$RuntimeContract.control)
$ProbeRequest = [IO.Path]::GetFullPath((Join-Path $ProbeControl "active.json"))
# Policy compatibility marker: kind -notin @("flat", "rollover_init") is extended only by the fixed smoke kind below.
$RunnerSid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$LauncherSha256 = Get-Super1SecureSha256 -Path $TrustedScript

foreach ($required in @(
    $Python, $Runner, $FlatDiagnostic, $RuntimeConfig, $TerminalPinPath, $CredentialPath,
    $CanonicalTerminal, $ProbeControl, [string]$RuntimeContract.runtime_trust
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Super1 protected runtime dependency is missing: $required"
    }
    if ((Get-Item -LiteralPath $required -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Super1 protected runtime dependency is a reparse point: $required"
    }
}
Assert-Super1SecureDirectoryAcl -Path $ProbeControl -RunnerSid $RunnerSid

$script:Super1PasswordPlain = $null
$script:Super1PythonExitCode = -1
$script:Super1PythonStderr = ""
function Invoke-Super1Python {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$ProvideCredential
    )
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Python
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Arguments = (($Arguments | ForEach-Object {
        $value = [string]$_
        if ($value -match '[\s"]') { '"' + $value.Replace('"', '\\"') + '"' } else { $value }
    }) -join " ")
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Could not start the pinned Super1 Python runtime." }
    try {
        if ($ProvideCredential) {
            if ([string]::IsNullOrEmpty($script:Super1PasswordPlain)) {
                throw "Transient Super1 credential is unavailable."
            }
            $process.StandardInput.WriteLine($script:Super1PasswordPlain)
        }
        $process.StandardInput.Close()
        $stdout = $process.StandardOutput.ReadToEnd()
        $script:Super1PythonStderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        $script:Super1PythonExitCode = [int]$process.ExitCode
        return $stdout
    }
    finally { $process.Dispose() }
}

function Write-Super1SmokeStopHealth {
    param(
        [Parameter(Mandatory = $true)][object]$Result,
        [Parameter(Mandatory = $true)][object]$StopRequest
    )
    $openOrders = [int]$Result.open_orders_after
    $openPositions = [int]$Result.open_positions_after
    $unknown = [int]$Result.unknown_exposure_after
    $safe = [string]$Result.state -ceq "PASS" -and $openOrders -eq 0 -and
        $openPositions -eq 0 -and $unknown -eq 0
    $state = if ($safe) { "STOPPED" } else { "UNSAFE_STOP_NO_SEND" }
    $payload = [ordered]@{
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        state = $state
        request_id = [string]$StopRequest.request_id
        lease_id = [string]$StopRequest.lease_id
        safe_stop = if ($safe) { "PASS" } else { "FAIL" }
        cancelled = $Result.cancelled
        owned_pending = $openOrders
        open_positions = $openPositions
        owned_positions = $openPositions
        protected_open = 0
        foreign_exposure = 0
        unknown = $unknown
        last_cycle = [ordered]@{ execution = [ordered]@{ state = "DEMO_SMOKE" } }
    }
    $healthPath = [IO.Path]::GetFullPath([string]$RuntimeContract.health)
    $temp = "$healthPath.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText(
            $temp,
            (($payload | ConvertTo-Json -Depth 12 -Compress) + [Environment]::NewLine),
            (New-Object Text.UTF8Encoding($false))
        )
        if ([IO.File]::Exists($healthPath)) { [IO.File]::Replace($temp, $healthPath, $null) }
        else { [IO.File]::Move($temp, $healthPath) }
    }
    finally { if ([IO.File]::Exists($temp)) { Remove-Item -LiteralPath $temp -Force } }
}

function Wait-Super1SmokeStopRequest {
    $path = Join-Path $ProbeControl "stop-request.json"
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
    do {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            try {
                $request = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
                if ([string]$request.request_id -and [string]$request.reason) { return $request }
            }
            catch { }
        }
        Start-Sleep -Seconds 1
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "Smoke completed without a protected stop request; refusing to claim safe shutdown."
}

$PreviousPythonHome = $env:PYTHONHOME
$PreviousPythonPath = $env:PYTHONPATH
$PreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
$PasswordPtr = [IntPtr]::Zero
$SecurePassword = $null
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
    $runtime = [IO.File]::ReadAllText($RuntimeConfig) | ConvertFrom-Json
    try {
        $SecurePassword = (Get-Content -Raw $CredentialPath).Trim() |
            ConvertTo-SecureString
        $PasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
        $script:Super1PasswordPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPtr)
    }
    catch {
        $script:LauncherPhase = "BROKER_CREDENTIAL_FATAL"
        $script:Super1PasswordPlain = $null
        throw "Super1 broker credential is unavailable or cannot be decrypted."
    }

    if (Test-Path -LiteralPath $ProbeRequest -PathType Leaf) {
        $script:LauncherPhase = "PROBE_REQUEST_GATE"
        $requestEvidence = Open-Super1SecureLockedFile -Path $ProbeRequest -RunnerSid $RunnerSid
        $ProbeLock = $requestEvidence.lock
        $request = [string]$requestEvidence.content | ConvertFrom-Json
        $transactionId = [string]$request.transaction_id
        $nonce = [string]$request.nonce
        $env:SUPER1_INVOCATION_NONCE = $nonce
        $env:SUPER1_RUNNER_SID = $RunnerSid
        $env:SUPER1_LAUNCHER_SHA256 = $LauncherSha256
        $env:SUPER1_INVOCATION_STARTED_AT = [DateTimeOffset]::UtcNow.ToString("o")
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
            [string]$request.kind -notin @("flat", "rollover_init", "binding_readiness", "smoke") -or
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
            $smokeOutput = Invoke-Super1Python -Arguments @(
                "-I", "-E", "-B", $Runner, "--output-root", (Join-Path $Root "state"),
                "--credential-stdin", "smoke-order", "--confirm-demo"
            ) -ProvideCredential
            $smokeCode = [int]$script:Super1PythonExitCode
            [IO.File]::WriteAllText($resultPath, ($smokeOutput -join "`n") + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
            Write-Super1ProbeProducerEnvelope -Path $producerPath -ResultPath $resultPath -Kind "smoke" -TransactionId $transactionId -Nonce $nonce -RequestSha256 ([string]$requestEvidence.sha256) -RunnerSid $RunnerSid -LauncherPath $TrustedScript -LauncherSha256 $launcherSha256 -StartedAt $probeStartedAt -ExitCode $smokeCode
            if ($smokeCode -ne 0) { throw "Super1 demo smoke failed." }
            $smokeResult = ($smokeOutput -join "`n") | ConvertFrom-Json
            $stopRequest = Wait-Super1SmokeStopRequest
            Write-Super1SmokeStopHealth -Result $smokeResult -StopRequest $stopRequest
            exit 0
        }
        if ([string]$request.kind -ceq "flat") {
            $script:LauncherPhase = "FLAT_DIAGNOSTIC"
            $null = Invoke-Super1Python -Arguments @(
                "-I", "-E", "-B", $FlatDiagnostic,
                "--root", $Root,
                "--config", ([string]$RuntimeContract.runtime_config),
                "--profile", "super1",
                "--output", $resultPath,
                "--evidence-nonce", $nonce,
                "--credential-stdin"
            ) -ProvideCredential
            $flatCode = [int]$script:Super1PythonExitCode
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

        if ([string]$request.kind -ceq "binding_readiness") {
            $script:LauncherPhase = "BINDING_READINESS"
            $null = Invoke-Super1Python -Arguments @(
                "-I", "-E", "-B", $FlatDiagnostic,
                "--root", $Root,
                "--config", ([string]$RuntimeContract.runtime_config),
                "--profile", "super1",
                "--output", $resultPath,
                "--evidence-nonce", $nonce,
                "--binding-proof",
                "--credential-stdin"
            ) -ProvideCredential
            $bindingCode = [int]$script:Super1PythonExitCode
            Write-Super1ProbeProducerEnvelope `
                -Path $producerPath `
                -ResultPath $resultPath `
                -Kind "binding_readiness" `
                -TransactionId $transactionId `
                -Nonce $nonce `
                -RequestSha256 ([string]$requestEvidence.sha256) `
                -RunnerSid $RunnerSid `
                -LauncherPath $TrustedScript `
                -LauncherSha256 $launcherSha256 `
                -StartedAt $probeStartedAt `
                -ExitCode $bindingCode
            exit $bindingCode
        }

        $null = Invoke-Super1Python -Arguments @(
            "-I", "-E", "-B", $Runner, "--output-root", (Join-Path $Root "state"),
            "--credential-stdin", "init"
        ) -ProvideCredential
        $initCode = [int]$script:Super1PythonExitCode
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
    $leasePath = [IO.Path]::GetFullPath((Join-Path ([string]$RuntimeContract.control) "session-lease.json"))
    if (-not (Test-Path -LiteralPath $leasePath -PathType Leaf)) {
        throw "Super1 daemon has no active manual lease."
    }
    $daemonLease = Get-Content -Raw -LiteralPath $leasePath | ConvertFrom-Json
    if ([string]$daemonLease.state -cne "ACTIVE" -or [string]$daemonLease.lease_id -eq "") {
        throw "Super1 daemon lease is not active."
    }
    $env:SUPER1_INVOCATION_NONCE = [string]$daemonLease.invocation_nonce
    $env:SUPER1_RUNNER_SID = $RunnerSid
    $env:SUPER1_LAUNCHER_SHA256 = $LauncherSha256
    $env:SUPER1_INVOCATION_STARTED_AT = [DateTimeOffset]::UtcNow.ToString("o")
    $null = Invoke-Super1Python -Arguments @(
        "-I", "-E", "-B", $Runner, "--output-root", (Join-Path $Root "state"),
        "--credential-stdin", "daemon"
    ) -ProvideCredential
    $daemonCode = [int]$script:Super1PythonExitCode
    if ($daemonCode -eq 0) {
        $healthPath = [string]$RuntimeContract.health
        if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
            $health = Get-Content -Raw -LiteralPath $healthPath | ConvertFrom-Json
            if ([string]$health.state -ceq "STOPPED" -and [string]$health.safe_stop -ceq "PASS") {
                exit 0
            }
        }
    }
    throw "Super1 daemon exited unexpectedly with persistent code: $daemonCode"
}
finally {
    $script:Super1PasswordPlain = $null
    $env:SUPER1_INVOCATION_NONCE = $null
    $env:SUPER1_RUNNER_SID = $null
    $env:SUPER1_LAUNCHER_SHA256 = $null
    $env:SUPER1_INVOCATION_STARTED_AT = $null
    $env:PYTHONDONTWRITEBYTECODE = $PreviousBytecode
    $env:PYTHONHOME = $PreviousPythonHome
    $env:PYTHONPATH = $PreviousPythonPath
    if ($PasswordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPtr)
    }
    if ($SecurePassword) {
        $SecurePassword.Dispose()
        $SecurePassword = $null
    }
    if ($TransactionRequestLock) { $TransactionRequestLock.Dispose() }
    if ($ProbeLock) { $ProbeLock.Dispose() }
    if ($TerminalLock) { $TerminalLock.Dispose() }
    if ($PowerShellHostLock) { $PowerShellHostLock.Dispose() }
    if ($PowerShellPinLock) { $PowerShellPinLock.Dispose() }
    $env:PSModulePath = $PreviousPSModulePath
}
