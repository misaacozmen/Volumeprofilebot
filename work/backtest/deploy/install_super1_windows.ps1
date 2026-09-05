$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = "C:\Super1"
$Archive = Join-Path $Root "super1-forward.zip"
$App = Join-Path $Root "app"
$PythonExe = "C:\Program Files\Python311\python.exe"
$DedicatedMt5 = Join-Path $Root "mt5"
$TaskName = "Super1XM"
$IntegrityScript = Join-Path $PSScriptRoot "release_integrity.ps1"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "Run this script from an elevated PowerShell."
}
if (-not (Test-Path -LiteralPath $Archive)) {
    throw "Missing deployment archive: $Archive"
}
if (-not (Test-Path -LiteralPath $IntegrityScript)) {
    throw "Missing release integrity verifier: $IntegrityScript"
}
. $IntegrityScript
$ReleaseManifest = Assert-SignedReleaseArchive -Archive $Archive -ExpectedProfile "super1" -RequireProvenance
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Existing Python 3.11 runtime was not found; refusing to change the working campaign."
}
if (Test-Path -LiteralPath $App) {
    throw "Super1 app already exists; refusing to overwrite an initialized campaign."
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Super1 scheduled task already exists; refusing to overwrite it."
}

$ExistingTerminalFile = "C:\ForwardShadow\mt5-terminal.txt"
if (-not (Test-Path -LiteralPath $ExistingTerminalFile)) {
    throw "Existing XM terminal path evidence was not found."
}
$ExistingTerminal = (Get-Content -Raw $ExistingTerminalFile).Trim()
if (-not (Test-Path -LiteralPath $ExistingTerminal)) {
    throw "Existing XM terminal executable was not found."
}
$ExistingTerminalSignature = Get-AuthenticodeSignature -LiteralPath $ExistingTerminal
if ($ExistingTerminalSignature.Status -ne "Valid" -or `
    $ExistingTerminalSignature.SignerCertificate.Subject -notmatch "MetaQuotes") {
    throw "Existing XM terminal signature validation failed."
}
$ExistingMt5Root = Split-Path -Parent $ExistingTerminal
if (Test-Path -LiteralPath $DedicatedMt5) {
    throw "Dedicated Super1 MT5 directory already exists; refusing to reuse it."
}

New-Item -ItemType Directory -Force -Path $Root | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $App
Copy-Item -LiteralPath $ExistingMt5Root -Destination $DedicatedMt5 -Recurse
$Terminal = Join-Path $DedicatedMt5 "terminal64.exe"
if (-not (Test-Path -LiteralPath $Terminal)) {
    throw "Dedicated Super1 terminal copy is incomplete."
}
Start-Process -FilePath $Terminal -ArgumentList "/portable" -WindowStyle Hidden

& $PythonExe -m venv (Join-Path $Root "venv311")
$Python = Join-Path $Root "venv311\Scripts\python.exe"
Install-LockedRelease -Python $Python -App $App
New-Item -ItemType Directory -Force -Path (Join-Path $Root "state") | Out-Null
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Protect-ReleaseApp -App $App -RunnerIdentity $CurrentIdentity

$SecurePassword = Read-Host "Super1 XM MetaTrader parolasi" -AsSecureString
$PasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $env:XM_MT5_READ_ONLY_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPtr)
    $env:XM_MT5_TERMINAL_PATH = $Terminal
    $env:XM_MT5_SERVER = "XMGlobal-MT5 2"
    $DiscoveryJson = & $Python (Join-Path $App "scripts\discover_super1_xm_account.py")
    if ($LASTEXITCODE -ne 0 -or -not $DiscoveryJson) {
        throw "Super1 XM account discovery failed."
    }
    $Discovery = $DiscoveryJson | ConvertFrom-Json
    if ($Discovery.state -ne "FOUND" -or -not $Discovery.demo_verified) {
        throw "Super1 XM discovery did not verify a demo account."
    }
    if ($Discovery.symbols.nq -ne "US100Cash" -or $Discovery.symbols.spx -ne "US500Cash") {
        throw "Unexpected XM symbol names; deployment stopped before campaign lock."
    }
    Set-Content -LiteralPath (Join-Path $Root "xm-server.txt") -Value $Discovery.server -Encoding ascii
    Set-Content -LiteralPath (Join-Path $Root "mt5-terminal.txt") -Value $Terminal -Encoding ascii
    $SecurePassword | ConvertFrom-SecureString |
        Set-Content -LiteralPath (Join-Path $Root "xm-password.dpapi") -Encoding ascii
    & icacls.exe (Join-Path $Root "xm-password.dpapi") /inheritance:r /grant:r `
        "SYSTEM:(F)" "BUILTIN\Administrators:(F)" "${CurrentIdentity}:(R)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect XM credential ACL." }

    & $Python (Join-Path $App "scripts\run_super1_xm_mt5_forward.py") --output-root (Join-Path $Root "state") doctor
    if ($LASTEXITCODE -ne 0) {
        throw "Super1 doctor gate failed."
    }
}
finally {
    $env:XM_MT5_READ_ONLY_PASSWORD = $null
    $env:XM_MT5_TERMINAL_PATH = $null
    $env:XM_MT5_SERVER = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPtr)
}

$Launcher = Join-Path $App "deploy\run_super1_windows.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""
$Triggers = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Principal $Principal -Settings $Settings | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
& (Join-Path $App "deploy\install_watchdog_windows.ps1") `
    -Root $Root `
    -MainTaskName $TaskName `
    -WatchdogTaskName "Super1Watchdog" `
    -HealthPath (Join-Path $Root "state\health.json") `
    -ProcessPattern "run_super1_xm_mt5_forward.py"

$env:XM_MT5_SERVER = "XMGlobal-MT5 2"
$env:XM_MT5_TERMINAL_PATH = $Terminal
try {
    & $Python (Join-Path $App "scripts\run_super1_xm_mt5_forward.py") --output-root (Join-Path $Root "state") status
}
finally {
    $env:XM_MT5_SERVER = $null
    $env:XM_MT5_TERMINAL_PATH = $null
}
