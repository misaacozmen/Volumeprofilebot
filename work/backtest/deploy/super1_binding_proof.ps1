$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")

function Invoke-Super1BindingProof {
    param(
        [Parameter(Mandatory = $true)][object]$RuntimeContract,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$App,
        [Parameter(Mandatory = $true)][string]$State
    )
    $helper = Get-Super1RuntimeAppPath -RelativePath ([string]$RuntimeContract.secure_task_helper)
    . $helper
    $runnerSid = Get-Super1SecurePrincipalSid -Identity "$env:COMPUTERNAME\$([string]$RuntimeContract.runner_account)"
    $currentSid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($currentSid -ceq $runnerSid) {
        throw "Binding proof must run elevated outside Super1Runner so the Runner DPAPI boundary is proven."
    }
    $python = [string]$RuntimeContract.python
    $outputPath = Join-Path $State "readiness-flat.json"
    $workerSource = @'
$ErrorActionPreference = "Stop"
$credential = Join-Path $env:LOCALAPPDATA "Super1\xm-password.dpapi"
$secure = Get-Content -Raw -LiteralPath $credential | ConvertTo-SecureString
$pointer = [IntPtr]::Zero
$plain = $null
$process = $null
try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = "__PYTHON__"
    $info.Arguments = "-I -E -B __FLAT__ --root __ROOT__ --config __CONFIG__ --profile super1 --output __OUTPUT__ --binding-proof --credential-stdin"
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    if (-not $process.Start()) { throw "Could not start the pinned binding-proof runtime." }
    $process.StandardInput.WriteLine($plain)
    $process.StandardInput.Close()
    $null = $process.StandardOutput.ReadToEnd()
    $null = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Pinned binding proof failed with exit code $($process.ExitCode)." }
}
finally {
    if ($process) { $process.Dispose() }
    $plain = $null
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($secure) { $secure.Dispose() }
}
'@
    foreach ($replacement in @{
        "__PYTHON__" = '"' + $python.Replace('"', '\"') + '"'
        "__FLAT__" = '"' + (Join-Path $App "scripts\check_mt5_flat.py").Replace('"', '\"') + '"'
        "__ROOT__" = '"' + $Root.Replace('"', '\"') + '"'
        "__CONFIG__" = '"' + ([string]$RuntimeContract.runtime_config).Replace('"', '\"') + '"'
        "__OUTPUT__" = '"' + $outputPath.Replace('"', '\"') + '"'
    }.GetEnumerator()) {
        $workerSource = $workerSource.Replace($replacement.Key, $replacement.Value)
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($workerSource))
    $runnerPassword = Read-Host "Super1Runner Windows parolasi (binding proof)" -AsSecureString
    $processInfo = [Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = Join-Path ([Environment]::SystemDirectory) "WindowsPowerShell\v1.0\powershell.exe"
    $processInfo.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded"
    $processInfo.UserName = [string]$RuntimeContract.runner_account
    $processInfo.Domain = "."
    $processInfo.Password = $runnerPassword
    $processInfo.LoadUserProfile = $true
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $runnerProcess = [Diagnostics.Process]::new()
    $runnerProcess.StartInfo = $processInfo
    try {
        if (-not $runnerProcess.Start()) { throw "Could not start the Runner-bound binding proof." }
        $null = $runnerProcess.StandardOutput.ReadToEnd()
        $null = $runnerProcess.StandardError.ReadToEnd()
        $runnerProcess.WaitForExit()
        if ($runnerProcess.ExitCode -ne 0) {
            throw "Runner-bound exact demo binding proof failed."
        }
    }
    finally {
        $runnerProcess.Dispose()
        $runnerPassword.Dispose()
    }
    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        throw "Runner-bound binding proof did not publish evidence."
    }
    return Get-Content -Raw -LiteralPath $outputPath | ConvertFrom-Json
}
