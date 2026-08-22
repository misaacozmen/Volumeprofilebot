[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [string]$ChatId
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security
$credentialPath = Join-Path $Root "watchdog_telegram.dat"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$secureToken = Read-Host "BotFather token" -AsSecureString
$tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
    Invoke-RestMethod -Uri ("https://api.telegram.org/bot{0}/getMe" -f $token) -TimeoutSec 15 | Out-Null
    if ([string]::IsNullOrWhiteSpace($ChatId)) {
        $updates = Invoke-RestMethod -Uri ("https://api.telegram.org/bot{0}/getUpdates" -f $token) -TimeoutSec 15
        $messages = @($updates.result | Where-Object { $null -ne $_.message.chat.id })
        if ($messages.Count -eq 0) {
            throw "Telegram'da bota /start gonderin, sonra betigi yeniden calistirin."
        }
        $ChatId = [string]$messages[-1].message.chat.id
    }
    $payload = @{ token = $token; chat_id = $ChatId } | ConvertTo-Json -Compress
    $plain = [Text.Encoding]::UTF8.GetBytes($payload)
    $protected = [System.Security.Cryptography.ProtectedData]::Protect(
        $plain,
        [Text.Encoding]::UTF8.GetBytes("CodexWatchdogTelegramV1"),
        [System.Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    [IO.File]::WriteAllText($credentialPath, [Convert]::ToBase64String($protected), (New-Object Text.UTF8Encoding($false)))
    & icacls.exe $credentialPath /inheritance:r /grant:r "SYSTEM:(F)" "BUILTIN\Administrators:(F)" | Out-Null
    $body = @{
        chat_id = $ChatId
        text = "[TEST] $env:COMPUTERNAME watchdog Telegram bildirimi aktif."
    } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot{0}/sendMessage" -f $token) `
        -ContentType "application/json" -Body $body -TimeoutSec 15 | Out-Null
    Write-Output "Telegram notification configured: $credentialPath"
}
finally {
    if ($null -ne $plain) { [Array]::Clear($plain, 0, $plain.Length) }
    $token = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
}
