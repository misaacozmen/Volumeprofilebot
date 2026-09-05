param(
    [Parameter(Mandatory = $true)]
    [Security.SecureString]$SecurePassword
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract

$Root = [string]$Contract.root
$App = [string]$Contract.app
$Terminal = [string]$Contract.terminal
$Python = [string]$Contract.python
$RunnerUser = [string]$Contract.runner_account
$TaskName = [string]$Contract.main_task
$ExistingTaskName = "ForwardShadowXM"
$Worker = Join-Path $Root "bootstrap_super1_user.ps1"

function Get-StringSha256([string]$Value) {
    $Sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString(
            $Sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))
        ).Replace("-", "")
    }
    finally {
        $Sha256.Dispose()
    }
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "Run this script from an elevated PowerShell."
}
foreach ($Required in @($App, $Terminal, $Python)) {
    if (-not (Test-Path -LiteralPath $Required)) {
        throw "Missing Super1 component: $Required"
    }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Super1 scheduled task already exists; refusing to overwrite it."
}

$ExistingTaskXml = Export-ScheduledTask -TaskName $ExistingTaskName
$ExistingTaskHash = Get-StringSha256 $ExistingTaskXml
$ExistingTerminalProcess = Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
    Where-Object { $_.ExecutablePath -eq $Terminal }
if (-not $ExistingTerminalProcess) {
    throw "Existing XM terminal was not running before isolation; refusing to continue."
}

$Alphabet = (48..57) + (65..90) + (97..122)
$RunnerPlain = "Aa1!" + (-join (1..28 | ForEach-Object { [char]($Alphabet | Get-Random) }))
$RunnerSecure = ConvertTo-SecureString $RunnerPlain -AsPlainText -Force
$ExistingRunner = Get-LocalUser -Name $RunnerUser -ErrorAction SilentlyContinue
if ($ExistingRunner) {
    if ($ExistingRunner.Description -ne "Isolated local runtime for Super1 XM demo") {
        throw "Local user name collision: $RunnerUser"
    }
    Set-LocalUser -Name $RunnerUser -Password $RunnerSecure -AccountNeverExpires -PasswordNeverExpires $true
    Enable-LocalUser -Name $RunnerUser
}
else {
    New-LocalUser -Name $RunnerUser -Password $RunnerSecure -AccountNeverExpires -PasswordNeverExpires `
        -Description "Isolated local runtime for Super1 XM demo" | Out-Null
}

$RunnerIdentity = "$env:COMPUTERNAME\$RunnerUser"
$RunnerSid = ([Security.Principal.NTAccount]::new($RunnerIdentity)).Translate(
    [Security.Principal.SecurityIdentifier]
).Value
$RunnerProfile = Get-CimInstance Win32_UserProfile | Where-Object {
    [string]$_.SID -ceq $RunnerSid
} | Select-Object -First 1
if ($null -eq $RunnerProfile -or [string]::IsNullOrWhiteSpace([string]$RunnerProfile.LocalPath)) {
    throw "Super1Runner profile is unavailable; cannot provision CurrentUser DPAPI."
}
$RunnerCredential = Join-Path ([string]$RunnerProfile.LocalPath) "AppData\Local\Super1\xm-password.dpapi"
$StateRoot = Join-Path $Root "state"
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
& icacls.exe $Root /grant:r "${RunnerIdentity}:(RX)" /Q | Out-Null
& icacls.exe $App /grant:r "${RunnerIdentity}:(OI)(CI)(RX)" /T /C /Q | Out-Null
& icacls.exe (Join-Path $Root "venv311") /grant:r "${RunnerIdentity}:(OI)(CI)(RX)" /T /C /Q | Out-Null
& icacls.exe $StateRoot /grant:r "${RunnerIdentity}:(OI)(CI)(M)" /T /C /Q | Out-Null
& icacls.exe (Join-Path $Root "mt5") /grant:r "${RunnerIdentity}:(OI)(CI)(M)" /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not apply isolated runner ACLs." }

# Stop only the failed Super1 terminal launched in the Administrator session.
Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
    Where-Object { $_.ExecutablePath -eq $Terminal } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }

$WorkerSource = @'
$ErrorActionPreference = "Stop"
$Root = "%SUPER1_ROOT%"
$Config = Join-Path $Root "app\live_forward\super1_xm_mt5_demo_config.json"
$Password = [Console]::In.ReadLine()
if ([string]::IsNullOrWhiteSpace($Password)) { throw "Missing XM password on stdin." }
$Secure = ConvertTo-SecureString $Password -AsPlainText -Force
try {
    $Json = $Password | & "%SUPER1_PYTHON%" `
        "%SUPER1_APP%\scripts\discover_super1_xm_account.py" --config $Config --credential-stdin
    if ($LASTEXITCODE -ne 0 -or -not $Json) { throw "Isolated discovery failed." }
    $Discovery = $Json | ConvertFrom-Json
    $Runtime = Get-Content -Raw $Config | ConvertFrom-Json
    if ($Discovery.state -ne "FOUND" -or -not $Discovery.demo_verified -or
        [int]$Discovery.login -ne [int]$Runtime.account_login -or
        [string]$Discovery.server -cne [string]$Runtime.expected_server -or
        [string]$Discovery.company -cne [string]$Runtime.expected_company) {
        throw "Isolated discovery did not verify a demo account."
    }
    $target = Join-Path $env:LOCALAPPDATA "Super1\xm-password.dpapi"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    $Secure | ConvertFrom-SecureString | Set-Content -LiteralPath $target -Encoding ascii
    $Json
}
finally {
    $Password = $null
}
'@
$WorkerSource = $WorkerSource.Replace("%SUPER1_ROOT%", $Root).Replace("%SUPER1_PYTHON%", $Python).Replace("%SUPER1_APP%", $App)
Set-Content -LiteralPath $Worker -Value $WorkerSource -Encoding UTF8

$PasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $XmPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPtr)
    $StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe"
    $StartInfo.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Worker`""
    $StartInfo.WorkingDirectory = $Root
    $StartInfo.UserName = $RunnerUser
    $StartInfo.Domain = "."
    $StartInfo.Password = $RunnerSecure
    $StartInfo.LoadUserProfile = $true
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Process = [Diagnostics.Process]::Start($StartInfo)
    $Process.StandardInput.WriteLine($XmPlain)
    $Process.StandardInput.Close()
    if (-not $Process.WaitForExit(120000)) {
        $Process.Kill()
        throw "Isolated XM discovery timed out."
    }
    $DiscoveryJson = $Process.StandardOutput.ReadToEnd().Trim()
    $DiscoveryError = $Process.StandardError.ReadToEnd().Trim()
    if ($Process.ExitCode -ne 0) {
        throw "Isolated XM discovery failed: $DiscoveryError"
    }
    $Discovery = $DiscoveryJson | ConvertFrom-Json
    if (-not (Test-Path -LiteralPath $RunnerCredential)) {
        throw "Runner-bound XM credential was not created."
    }
    & icacls.exe $RunnerCredential /inheritance:r /grant:r `
        "SYSTEM:(F)" "BUILTIN\Administrators:(F)" "${RunnerIdentity}:(R)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect runner-bound credential ACLs." }
}
finally {
    if ($PasswordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPtr)
    }
    $XmPlain = $null
}

$Launcher = Join-Path $App "deploy\run_super1_windows.ps1"
$Action = New-ScheduledTaskAction -Execute (Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe") `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 0 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Settings $Settings `
    -User ".\$RunnerUser" -Password $RunnerPlain -RunLevel Limited | Out-Null

# Recovery provisions the stopped manual task only; the daily lease start is
# deliberately left to start_super1_local_windows.ps1.

$ExistingTaskXmlAfter = Export-ScheduledTask -TaskName $ExistingTaskName
$ExistingTaskHashAfter = Get-StringSha256 $ExistingTaskXmlAfter
if ($ExistingTaskHashAfter -ne $ExistingTaskHash) {
    throw "Existing ForwardShadowXM task changed unexpectedly."
}
$ExistingTerminalAfter = Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
    Where-Object { $_.ExecutablePath -eq $Terminal }
if (-not $ExistingTerminalAfter) {
    throw "Existing XM terminal changed unexpectedly."
}
$Super1Process = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*run_super1_xm_mt5_forward.py*" }
$Task = Get-ScheduledTask -TaskName $TaskName
$TaskInfo = Get-ScheduledTaskInfo -TaskName $TaskName

[ordered]@{
    state = "INSTALLED"
    runner = $RunnerUser
    login = $Discovery.login
    server = $Discovery.server
    company = $Discovery.company
    demo_verified = $Discovery.demo_verified
    nq_symbol = $Discovery.symbols.nq
    spx_symbol = $Discovery.symbols.spx
    task_state = [string]$Task.State
    last_task_result = $TaskInfo.LastTaskResult
    super1_python_processes = @($Super1Process).Count
    existing_task_hash_unchanged = $true
    existing_terminal_unchanged = $true
} | ConvertTo-Json -Depth 5

$RunnerPlain = $null
$RunnerSecure.Dispose()
