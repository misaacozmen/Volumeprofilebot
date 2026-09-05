$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$Contract = Assert-Super1RuntimeContract

$Root = [string]$Contract.root
$App = [string]$Contract.app
$Python = [string]$Contract.python
$Wheelhouse = Join-Path $Root "wheelhouse"
$Mt5Installer = Join-Path $Root "xm.com5setup.exe"
$ExpectedMt5Sha256 = "FD8CA7875A13DED372492BC8C06B2DDDDEBE6B522BEA62E81BA203436B012320"
$Terminal = [string]$Contract.terminal

foreach ($RequiredPath in @($App, $Python, $Wheelhouse, $Mt5Installer)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        throw "Missing recovery path: $RequiredPath"
    }
}

& $Python -m pip install --no-index --find-links $Wheelhouse pandas MetaTrader5
if ($LASTEXITCODE -ne 0) {
    throw "Offline dependency installation failed."
}
& $Python -m pip install --no-deps $App
if ($LASTEXITCODE -ne 0) {
    throw "Super1 package installation failed."
}

$Mt5Signature = Get-AuthenticodeSignature -LiteralPath $Mt5Installer
if ((Get-FileHash -LiteralPath $Mt5Installer -Algorithm SHA256).Hash -ne $ExpectedMt5Sha256) {
    throw "XM MT5 installer SHA-256 validation failed."
}
if ($Mt5Signature.Status -ne "Valid") {
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

& $Python --version
& $Python -m pip show MetaTrader5 pandas
Get-Item -LiteralPath $Terminal | Select-Object FullName, Length, LastWriteTime
