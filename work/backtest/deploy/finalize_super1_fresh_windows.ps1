$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = "C:\Super1"
$App = Join-Path $Root "app"
$Python = Join-Path $Root "venv311\Scripts\python.exe"
$Terminal = (Get-Content -Raw (Join-Path $Root "mt5-terminal.txt")).Trim()
$TaskName = "Super1XM"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "Run this script from an elevated PowerShell."
}
foreach ($RequiredPath in @($App, $Python, $Terminal)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        throw "Missing required deployment path: $RequiredPath"
    }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Super1 scheduled task already exists; refusing to overwrite it."
}
if (Test-Path -LiteralPath (Join-Path $Root "xm-password.dpapi")) {
    throw "Super1 password state already exists; refusing to overwrite it."
}

Start-Process -FilePath $Terminal -ArgumentList "/portable"
Start-Sleep -Seconds 8
$SecurePassword = Read-Host "Super1 XM MetaTrader parolasi" -AsSecureString
$PasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $env:XM_MT5_READ_ONLY_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPtr)
    $env:XM_MT5_TERMINAL_PATH = $Terminal
    $env:XM_MT5_SERVER = "XMGlobal-MT5 6"
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
    $SecurePassword | ConvertFrom-SecureString |
        Set-Content -LiteralPath (Join-Path $Root "xm-password.dpapi") -Encoding ascii
    $CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe (Join-Path $Root "xm-password.dpapi") /inheritance:r /grant:r `
        "SYSTEM:(F)" "BUILTIN\Administrators:(F)" "${CurrentIdentity}:(R)" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect XM credential ACL." }

    & $Python (Join-Path $App "scripts\run_super1_xm_mt5_forward.py") `
        --output-root (Join-Path $Root "state") doctor
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
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8
& (Join-Path $App "deploy\install_watchdog_windows.ps1") `
    -Root $Root `
    -MainTaskName $TaskName `
    -WatchdogTaskName "Super1Watchdog" `
    -HealthPath (Join-Path $Root "state\health.json") `
    -ProcessPattern "run_super1_xm_mt5_forward.py"

$env:XM_MT5_SERVER = "XMGlobal-MT5 6"
$env:XM_MT5_TERMINAL_PATH = $Terminal
try {
    & $Python (Join-Path $App "scripts\run_super1_xm_mt5_forward.py") `
        --output-root (Join-Path $Root "state") status
}
finally {
    $env:XM_MT5_SERVER = $null
    $env:XM_MT5_TERMINAL_PATH = $null
}
