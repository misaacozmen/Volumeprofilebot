# Run from an elevated PowerShell after forward-shadow.zip is placed in C:\ForwardShadow.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = "C:\ForwardShadow"
$Archive = Join-Path $Root "forward-shadow.zip"
$App = Join-Path $Root "app"
$PythonInstaller = Join-Path $Root "python-3.11.9-amd64.exe"
$PythonExe = "C:\Program Files\Python311\python.exe"
$Mt5Installer = Join-Path $Root "xm.com5setup.exe"
$ExpectedMt5Sha256 = "9125EE0D9CF947C6EF6EFAC392E4410214D39144901159E146D532E45171267A"
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
$ReleaseManifest = Assert-SignedReleaseArchive -Archive $Archive -ExpectedProfile "forward-shadow"

New-Item -ItemType Directory -Force -Path $Root | Out-Null
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $PythonInstaller
    $Signature = Get-AuthenticodeSignature -LiteralPath $PythonInstaller
    if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        throw "Python installer signature validation failed."
    }
    Start-Process -FilePath $PythonInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
}

$Terminal = Get-ChildItem "C:\Program Files" -Filter terminal64.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "(MetaTrader 5|XM.*MT5)" } |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $Terminal) {
    Invoke-WebRequest -UseBasicParsing "https://download.terminal.free/cdn/web/trading.point.of/mt5/xm.com5setup.exe" -OutFile $Mt5Installer
    $InstallerHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Mt5Installer).Hash
    if ($InstallerHash -ne $ExpectedMt5Sha256) {
        throw "MetaTrader installer SHA-256 validation failed."
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Mt5Installer
    if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notmatch "MetaQuotes") {
        throw "MetaTrader installer signature validation failed."
    }
    Start-Process -FilePath $Mt5Installer -ArgumentList "/auto" -Wait
    $Terminal = Get-ChildItem "C:\Program Files" -Filter terminal64.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "(MetaTrader 5|XM.*MT5)" } |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $Terminal) {
    throw "MetaTrader 5 terminal64.exe was not found after installation."
}

if (Test-Path -LiteralPath $App) {
    throw "App directory already exists; refusing to overwrite an initialized campaign."
}
Expand-Archive -LiteralPath $Archive -DestinationPath $App
& $PythonExe -m venv (Join-Path $Root "venv311")
$Python = Join-Path $Root "venv311\Scripts\python.exe"
Install-LockedRelease -Python $Python -App $App
New-Item -ItemType Directory -Force -Path (Join-Path $Root "state") | Out-Null
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Protect-ReleaseApp -App $App -RunnerIdentity $CurrentIdentity

$SecurePassword = Read-Host "XM salt-okunur MetaTrader parolasi" -AsSecureString
$PasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $env:XM_MT5_READ_ONLY_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPtr)
    $env:XM_MT5_TERMINAL_PATH = $Terminal
    $DiscoveryJson = & $Python (Join-Path $App "scripts\discover_xm_mt5_server.py")
    if ($LASTEXITCODE -ne 0 -or -not $DiscoveryJson) {
        throw "XM server discovery failed; verify the exact server name and read-only password."
    }
    $Discovery = $DiscoveryJson | ConvertFrom-Json
    if ($Discovery.state -ne "FOUND") {
        throw "XM server discovery did not return FOUND."
    }
    $Server = [string]$Discovery.server
    Set-Content -LiteralPath (Join-Path $Root "xm-server.txt") -Value $Server -Encoding ascii
    Set-Content -LiteralPath (Join-Path $Root "mt5-terminal.txt") -Value $Terminal -Encoding ascii
    $SecurePassword | ConvertFrom-SecureString |
        Set-Content -LiteralPath (Join-Path $Root "xm-readonly-password.dpapi") -Encoding ascii
    & icacls.exe (Join-Path $Root "xm-readonly-password.dpapi") /inheritance:r /grant:r `
        "SYSTEM:(F)" "BUILTIN\Administrators:(F)" "${CurrentIdentity}:(R)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect XM credential ACL." }

    $env:XM_MT5_SERVER = $Server
    & $Python (Join-Path $App "scripts\run_xm_mt5_forward.py") doctor
}
finally {
    $env:XM_MT5_READ_ONLY_PASSWORD = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPtr)
}

$Launcher = Join-Path $App "deploy\run_forward_shadow_windows.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""
$Triggers = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "ForwardShadowXM" -Action $Action -Trigger $Triggers -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName "ForwardShadowXM"
Start-Sleep -Seconds 5
& (Join-Path $App "deploy\install_watchdog_windows.ps1") `
    -Root $Root `
    -MainTaskName "ForwardShadowXM" `
    -WatchdogTaskName "ForwardShadowWatchdog" `
    -HealthPath (Join-Path $Root "state\health.json") `
    -ProcessPattern "run_xm_mt5_forward.py"
& $Python (Join-Path $App "scripts\run_xm_mt5_forward.py") --output-root (Join-Path $Root "state") status
