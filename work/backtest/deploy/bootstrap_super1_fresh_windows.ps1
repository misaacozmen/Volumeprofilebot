$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = "C:\Super1"
$Archive = Join-Path $Root "super1-forward.zip"
$App = Join-Path $Root "app"
$PythonExe = "C:\Program Files\Python311\python.exe"
$PythonInstaller = Join-Path $Root "python-3.11.9-amd64.exe"
$Mt5Installer = Join-Path $Root "xm.com5setup.exe"
$ExpectedMt5Sha256 = "9125EE0D9CF947C6EF6EFAC392E4410214D39144901159E146D532E45171267A"
$Terminal = Join-Path $Root "mt5\terminal64.exe"
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
$ReleaseManifest = Assert-SignedReleaseArchive -Archive $Archive -ExpectedProfile "super1"
if (Test-Path -LiteralPath $App) {
    throw "Super1 app already exists; refusing to overwrite it."
}

New-Item -ItemType Directory -Force -Path $Root | Out-Null
Invoke-WebRequest `
    -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" `
    -OutFile $PythonInstaller
$PythonSignature = Get-AuthenticodeSignature -LiteralPath $PythonInstaller
if ($PythonSignature.Status -ne "Valid" -or `
    $PythonSignature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
    throw "Python installer signature is not valid: $($PythonSignature.Status)"
}
Start-Process -FilePath $PythonInstaller -ArgumentList @(
    "/quiet",
    "InstallAllUsers=1",
    "PrependPath=0",
    "Include_test=0",
    "TargetDir=`"C:\Program Files\Python311`""
) -Wait
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python 3.11 installation failed."
}

Expand-Archive -LiteralPath $Archive -DestinationPath $App
& $PythonExe -m venv (Join-Path $Root "venv311")
$Python = Join-Path $Root "venv311\Scripts\python.exe"
Install-LockedRelease -Python $Python -App $App
New-Item -ItemType Directory -Force -Path (Join-Path $Root "state") | Out-Null
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Protect-ReleaseApp -App $App -RunnerIdentity $CurrentIdentity

if (-not (Test-Path -LiteralPath $Mt5Installer) -or (Get-Item -LiteralPath $Mt5Installer).Length -lt 1MB) {
    Invoke-WebRequest `
        -Uri "https://download.terminal.free/cdn/web/trading.point.of/mt5/xm.com5setup.exe" `
        -OutFile $Mt5Installer
}
$Mt5Signature = Get-AuthenticodeSignature -LiteralPath $Mt5Installer
if ((Get-FileHash -LiteralPath $Mt5Installer -Algorithm SHA256).Hash -ne $ExpectedMt5Sha256) {
    throw "XM MT5 installer SHA-256 validation failed."
}
if ($Mt5Signature.Status -ne "Valid" -or `
    $Mt5Signature.SignerCertificate.Subject -notmatch "MetaQuotes") {
    throw "XM MT5 installer signature is not valid: $($Mt5Signature.Status)"
}
Start-Process -FilePath $Mt5Installer -ArgumentList @(
    "/auto",
    "/path:`"C:\Super1\mt5`""
) -Wait

$Deadline = (Get-Date).AddMinutes(5)
while (-not (Test-Path -LiteralPath $Terminal) -and (Get-Date) -lt $Deadline) {
    Start-Sleep -Seconds 5
}
if (-not (Test-Path -LiteralPath $Terminal)) {
    throw "XM MT5 installation did not produce $Terminal"
}
Get-CimInstance Win32_Process -Filter "Name = 'terminal64.exe'" |
    Where-Object { $_.ExecutablePath -eq $Terminal } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
New-Item -ItemType File -Force -Path (Join-Path (Split-Path -Parent $Terminal) "portable.txt") | Out-Null
Set-Content -LiteralPath (Join-Path $Root "mt5-terminal.txt") -Value $Terminal -Encoding ascii

& $Python --version
& $Python -m pip show MetaTrader5 pandas
Get-Item -LiteralPath $Terminal | Select-Object FullName, Length, LastWriteTime
