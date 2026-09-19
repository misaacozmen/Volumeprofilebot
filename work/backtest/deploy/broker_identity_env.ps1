function Get-BrokerIdentityLogin {
    [CmdletBinding()]
    param(
        [string]$VariableName = "XM_MT5_ACCOUNT_LOGIN"
    )

    $raw = [Environment]::GetEnvironmentVariable($VariableName, "Process")
    if ($null -eq $raw) {
        throw "$VariableName is required before broker identity checks."
    }
    $value = $raw.Trim()
    if ([string]::IsNullOrEmpty($value) -or $value -cnotmatch '^[0-9]+$') {
        throw "$VariableName must contain only positive ASCII decimal digits."
    }
    try {
        $login = [long]::Parse(
            $value,
            [Globalization.NumberStyles]::None,
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch {
        throw "$VariableName must be within the signed 64-bit range."
    }
    if ($login -le 0) {
        throw "$VariableName must be positive."
    }
    return [long]$login
}

function Assert-BrokerIdentityLogin {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Actual,
        [Parameter(Mandatory = $true)][long]$Expected,
        [string]$Label = "broker account"
    )

    try { $actualLogin = [long]$Actual }
    catch { throw "$Label is not a valid signed 64-bit account login." }
    if ($actualLogin -ne [long]$Expected) {
        throw "$Label does not match the validated XM_MT5_ACCOUNT_LOGIN identity."
    }
    return [long]$actualLogin
}
