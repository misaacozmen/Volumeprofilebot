[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract

function Assert-Super1Administrator {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell."
    }
}
function Get-Super1Hash([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}
function Invoke-Super1Python {
    param([Parameter(Mandatory = $true)][string]$Python, [Parameter(Mandatory = $true)][string[]]$Arguments, [Parameter(Mandatory = $true)][string]$Password)
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Python; $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true; $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
    $info.Arguments = (($Arguments | ForEach-Object { $value = [string]$_; if ($value -match '[\s"]') { '"' + $value.Replace('"', '\\"') + '"' } else { $value } }) -join " ")
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $info
    if (-not $process.Start()) { throw "Could not start the pinned Super1 Python runtime." }
    try {
        $process.StandardInput.WriteLine($Password); $process.StandardInput.Close()
        $stdout = $process.StandardOutput.ReadToEnd(); $stderr = $process.StandardError.ReadToEnd(); $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "Super1 Python command failed ($($process.ExitCode)): $stderr" }
        return $stdout
    }
    finally { $process.Dispose() }
}

Assert-Super1Administrator
$root = [string]$Contract.root
$app = [string]$Contract.app
$python = [string]$Contract.python
$terminal = [string]$Contract.terminal
$configPath = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.runtime_config)
$launcher = Get-Super1RuntimeAppPath -RelativePath ([string]$Contract.launcher)
foreach ($path in @($app, $python, $terminal, $configPath, $launcher)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction SilentlyContinue) -and $path -ne $app) {
        throw "Missing Super1 finalization dependency: $path"
    }
}
$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
if ([string]$config.environment -cne "XM_MT5_DEMO_ORDER" -or
    [string]$config.account_mode -cne "DEMO_ORDER" -or
    [string]$config.expected_server -eq "" -or
    [string]$config.expected_company -eq "" -or
    [bool]$config.real_money_execution_allowed) {
    throw "Signed Super1 config is not an exact XM demo-only profile."
}
$secureBrokerPassword = Read-Host "Super1 XM MetaTrader parolasi" -AsSecureString
$brokerPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureBrokerPassword)
$brokerPassword = $null
try {
    $brokerPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($brokerPointer)
    $discovery = Invoke-Super1Python `
        -Python $python `
        -Arguments @("-I", "-E", "-B", (Join-Path $app "scripts\discover_super1_xm_account.py"), "--config", $configPath, "--credential-stdin") `
        -Password $brokerPassword | ConvertFrom-Json
    if ([string]$discovery.state -cne "FOUND" -or
        [int]$discovery.login -ne [int]$config.account_login -or
        [string]$discovery.server -cne [string]$config.expected_server -or
        [string]$discovery.company -cne [string]$config.expected_company -or
        -not [bool]$discovery.demo_verified) {
        throw "Super1 discovery failed the signed exact demo binding."
    }
}
finally {
    if ($brokerPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($brokerPointer) }
    $secureBrokerPassword.Dispose()
}

$runnerPassword = Read-Host "Super1Runner Windows parolasi" -AsSecureString
if (-not (Get-LocalUser -Name ([string]$Contract.runner_account) -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name ([string]$Contract.runner_account) -Password $runnerPassword -Description "Super1 local non-administrator runner" -AccountNeverExpires -PasswordNeverExpires | Out-Null
}
$runnerUser = "$env:COMPUTERNAME\$([string]$Contract.runner_account)"
$runnerPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($runnerPassword)
try {
    $runnerPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($runnerPointer)
    $runnerSid = ([Security.Principal.NTAccount]::new($runnerUser)).Translate([Security.Principal.SecurityIdentifier]).Value
    $runnerProfile = Get-CimInstance Win32_UserProfile | Where-Object { [string]$_.SID -ceq $runnerSid } | Select-Object -First 1
    if ($null -eq $runnerProfile -or [string]::IsNullOrWhiteSpace([string]$runnerProfile.LocalPath)) { throw "Super1Runner profile is unavailable." }
    $credentialPath = Join-Path ([string]$runnerProfile.LocalPath) "AppData\Local\Super1\xm-password.dpapi"
    $credentialWorker = Join-Path $root "control\write_runner_credential.ps1"
    $credentialWorkerSource = @'
$ErrorActionPreference = "Stop"
$plain = [Console]::In.ReadLine()
if ([string]::IsNullOrWhiteSpace($plain)) { throw "Missing transient broker credential." }
$secure = ConvertTo-SecureString $plain -AsPlainText -Force
$target = Join-Path $env:LOCALAPPDATA "Super1\xm-password.dpapi"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
$secure | ConvertFrom-SecureString | Set-Content -LiteralPath $target -Encoding ascii
$plain = $null
'@
    Set-Content -LiteralPath $credentialWorker -Value $credentialWorkerSource -Encoding UTF8
    $credentialInfo = [Diagnostics.ProcessStartInfo]::new()
    $credentialInfo.FileName = Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe"
    $credentialInfo.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$credentialWorker`""
    $credentialInfo.UserName = [string]$Contract.runner_account; $credentialInfo.Domain = "."; $credentialInfo.Password = $runnerPassword; $credentialInfo.LoadUserProfile = $true
    $credentialInfo.UseShellExecute = $false; $credentialInfo.RedirectStandardInput = $true; $credentialInfo.RedirectStandardOutput = $true; $credentialInfo.RedirectStandardError = $true
    $credentialProcess = [Diagnostics.Process]::Start($credentialInfo)
    $credentialProcess.StandardInput.WriteLine($brokerPassword); $credentialProcess.StandardInput.Close()
    $credentialError = $credentialProcess.StandardError.ReadToEnd(); $credentialProcess.WaitForExit()
    if ($credentialProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) { throw "Runner-bound CurrentUser DPAPI provisioning failed: $credentialError" }
    $credentialProcess.Dispose(); Remove-Item -LiteralPath $credentialWorker -Force
    $icacls = Join-Path ([Environment]::SystemDirectory) "icacls.exe"
    & $icacls $credentialPath /inheritance:r /grant:r "SYSTEM:(F)" "BUILTIN\Administrators:(F)" "${runnerUser}:(R)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect the Runner-bound credential metadata ACL." }
    $taskAction = New-ScheduledTaskAction -Execute (Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe") -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
    $taskSettings = New-ScheduledTaskSettingsSet -RestartCount 0 -RestartInterval (New-TimeSpan -Minutes 15) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask `
        -TaskName ([string]$Contract.main_task) `
        -Action $taskAction `
        -Settings $taskSettings `
        -User $runnerUser `
        -Password $runnerPlain `
        -RunLevel Limited `
        -Force | Out-Null
}
finally {
    if ($runnerPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($runnerPointer) }
    $brokerPassword = $null
    $runnerPassword.Dispose()
}

& (Join-Path $app "deploy\install_super1_watchdog_windows.ps1")
Write-Host "Super1 finalization registered the password/no-trigger main task and watchdog." -ForegroundColor Green
