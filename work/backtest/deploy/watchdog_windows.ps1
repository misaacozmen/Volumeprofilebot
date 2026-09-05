[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MainTaskName,
    [Parameter(Mandatory = $true)]
    [string]$HealthPath,
    [Parameter(Mandatory = $true)]
    [string]$ProcessPattern = "",
    [Parameter(Mandatory = $true)]
    [string]$StatusPath,
    [int]$PollSeconds = 60,
    [int]$StaleSeconds = 180,
    [int]$MaximumRestarts = 3,
    [int]$RestartWindowMinutes = 15,
    [switch]$ForcePremarketAudit,
    [switch]$OneShot,
    [switch]$LibraryOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "super1_runtime_contract.ps1")
$RuntimeContract = Assert-Super1RuntimeContract
$CanonicalRunner = [string]$RuntimeContract.python
$CanonicalHarness = [string](Join-Path ([string]$RuntimeContract.app) "scripts\run_super1_xm_mt5_forward.py")
$CanonicalTerminal = [string]$RuntimeContract.terminal
$LeasePath = Join-Path ([string]$RuntimeContract.control) "session-lease.json"
$ConfigPath = Join-Path ([string]$RuntimeContract.app) ([string]$RuntimeContract.runtime_config)
$ManifestPath = Join-Path ([string]$RuntimeContract.app) ([string]$RuntimeContract.manifest)
$CanonicalState = [string]$RuntimeContract.state
$RestartBudgetLatchPath = Join-Path ([string]$RuntimeContract.state) "RESTART_BUDGET_EXHAUSTED_NO_SEND.json"
$RunnerAccount = [string]$RuntimeContract.runner_account
Add-Type -AssemblyName System.Security
$restartHistory = [System.Collections.Generic.List[DateTimeOffset]]::new()
$deliveredTelegramKeys = [System.Collections.Generic.HashSet[string]]::new(
    [StringComparer]::Ordinal
)
$lastEventSignature = ""
$eventPath = [IO.Path]::ChangeExtension($StatusPath, ".events.jsonl")
$stateDirectory = Split-Path -Parent $StatusPath
$notificationCredentialPath = Join-Path $stateDirectory "watchdog_telegram.dat"
$notificationErrorPath = Join-Path $stateDirectory "watchdog_telegram_error.log"
$notificationStatePath = Join-Path $stateDirectory "watchdog_telegram_state.json"
$notificationOutboxPath = Join-Path $stateDirectory "watchdog_telegram_outbox.json"
$decisionNotificationStatePath = Join-Path $stateDirectory "watchdog_decision_telegram_state.json"
$premarketAuditStatePath = Join-Path $stateDirectory "watchdog_premarket_audit_state.json"
$restartStatePath = Join-Path $stateDirectory "watchdog_restart_budget.json"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$TelegramRuntimeStateSchema = 2
$TelegramAlertStates = @(
    "RESTARTED",
    "DEGRADED_RETRYING",
    "WATCHDOG_ERROR",
    "ALARM_CRITICAL",
    "ALARM_CREDENTIALS",
    "ALARM_BROKER_UNKNOWN",
    "ALARM_DATA_INVALID",
    "ALARM_RESTART_LOOP"
)
$AllowedMainHealthStates = @(
    "RUNNING", "RETRYING", "WAITING_CREDENTIALS", "STOPPED", "INITIALIZED",
    "CAMPAIGN_WARMUP", "CRITICAL_STOP", "UNSAFE_OPEN_ORDERS", "UNSAFE_STOP_NO_SEND",
    "UNKNOWN_NO_SEND", "READY_WAITING_WINDOW", "WAITING_MANUAL_LEASE"
)

function Write-JsonAtomically {
    param(
        [string]$Path,
        $Value,
        [int]$Depth = 8
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temp = "$Path.$PID.tmp"
    $json = ConvertTo-Json -InputObject $Value -Depth $Depth -Compress
    [IO.File]::WriteAllText($temp, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

if (Test-Path -LiteralPath $restartStatePath) {
    try {
        $savedRestartState = Get-Content -LiteralPath $restartStatePath -Raw | ConvertFrom-Json
        foreach ($stamp in @($savedRestartState.restart_times_utc)) {
            $restartHistory.Add([DateTimeOffset]::Parse([string]$stamp).ToUniversalTime())
        }
    }
    catch {
        $restartHistory.Clear()
    }
}

function Send-TelegramText {
    param([string]$Message)

    $protected = [Convert]::FromBase64String((Get-Content -LiteralPath $notificationCredentialPath -Raw).Trim())
    $plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $protected,
        [Text.Encoding]::UTF8.GetBytes("CodexWatchdogTelegramV1"),
        [System.Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    try {
        $credential = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
        $body = @{ chat_id = [string]$credential.chat_id; text = $Message } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot{0}/sendMessage" -f $credential.token) `
            -ContentType "application/json" -Body $body -TimeoutSec 15 | Out-Null
    }
    finally {
        [Array]::Clear($plain, 0, $plain.Length)
    }
}

function Read-TelegramOutbox {
    if (-not (Test-Path -LiteralPath $notificationOutboxPath)) {
        return @()
    }
    try {
        $parsed = Get-Content -LiteralPath $notificationOutboxPath -Raw |
            ConvertFrom-Json
        if ($null -eq $parsed) {
            return
        }
        return @($parsed | Where-Object { $null -ne $_ })
    }
    catch {
        $line = "{0} unreadable Telegram outbox: {1}" -f [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
        [IO.File]::AppendAllText($notificationErrorPath, $line + [Environment]::NewLine)
        throw "Telegram outbox is unreadable; preserving it for operator recovery."
    }
}

function Read-TelegramRuntimeState {
    $empty = [pscustomobject]@{
        schema_version = $TelegramRuntimeStateSchema
        active_alert_categories = @()
        last_runtime_state = ""
        updated_at_utc = ""
    }
    if (-not (Test-Path -LiteralPath $notificationStatePath)) {
        return $empty
    }
    try {
        $saved = Get-Content -LiteralPath $notificationStatePath -Raw |
            ConvertFrom-Json
        if ([int]$saved.schema_version -ne $TelegramRuntimeStateSchema) {
            return $empty
        }
        $categories = @(
            @($saved.active_alert_categories) |
                ForEach-Object { [string]$_ } |
                Where-Object { $TelegramAlertStates -contains $_ } |
                Select-Object -Unique
        )
        return [pscustomobject]@{
            schema_version = $TelegramRuntimeStateSchema
            active_alert_categories = $categories
            last_runtime_state = [string]$saved.last_runtime_state
            updated_at_utc = [string]$saved.updated_at_utc
        }
    }
    catch {
        return $empty
    }
}

function Write-TelegramRuntimeState {
    param(
        [string[]]$ActiveAlertCategories,
        [string]$LastRuntimeState
    )
    Write-JsonAtomically -Path $notificationStatePath -Value @{
        schema_version = $TelegramRuntimeStateSchema
        active_alert_categories = @($ActiveAlertCategories)
        last_runtime_state = $LastRuntimeState
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    }
}

function Get-TelegramRetryDelayMinutes {
    param([int]$AttemptCount)
    if ($AttemptCount -le 1) { return 5 }
    if ($AttemptCount -eq 2) { return 15 }
    if ($AttemptCount -eq 3) { return 60 }
    return 360
}

function Update-TelegramDeliveryState {
    param([string]$Key)

    if ($Key -like "runtime|*") {
        $parts = $Key.Split("|")
        $label = [string]$parts[1]
        $category = [string]$parts[2]
        $state = Read-TelegramRuntimeState
        $categories = @($state.active_alert_categories)
        if ($label -eq "RECOVERED") {
            $categories = @()
        }
        elseif ($TelegramAlertStates -contains $category -and
            $categories -notcontains $category) {
            $categories += $category
        }
        Write-TelegramRuntimeState `
            -ActiveAlertCategories $categories `
            -LastRuntimeState $category
        return
    }
    if ($Key -like "decision|*" -or $Key -like "order|*") {
        $savedDecision = ""
        $savedOrder = ""
        if (Test-Path -LiteralPath $decisionNotificationStatePath) {
            try {
                $state = Get-Content -LiteralPath $decisionNotificationStatePath -Raw | ConvertFrom-Json
                $savedDecision = [string]$state.decision_signature
                $savedOrder = [string]$state.order_signature
            }
            catch {
                $savedDecision = ""
                $savedOrder = ""
            }
        }
        if ($Key -like "decision|*") {
            $savedDecision = $Key.Substring("decision|".Length)
        }
        else {
            $savedOrder = $Key.Substring("order|".Length)
        }
        Write-JsonAtomically -Path $decisionNotificationStatePath -Value @{
            decision_signature = $savedDecision
            order_signature = $savedOrder
            updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        }
    }
}

function Invoke-TelegramOutboxDelivery {
    param([string]$RequestedKey = "")

    if (-not (Test-Path -LiteralPath $notificationCredentialPath)) {
        return $false
    }
    $remaining = [System.Collections.Generic.List[object]]::new()
    $requestedDelivered = $false
    $now = [DateTimeOffset]::UtcNow
    foreach ($item in @(Read-TelegramOutbox)) {
        if ($null -eq $item) { continue }
        $key = [string]$item.key
        $message = [string]$item.message
        if ([string]::IsNullOrWhiteSpace($key) -or
            [string]::IsNullOrWhiteSpace($message)) {
            $line = "{0} discarded invalid Telegram outbox entry" -f `
                $now.ToString("o")
            [IO.File]::AppendAllText(
                $notificationErrorPath,
                $line + [Environment]::NewLine
            )
            continue
        }
        if ($script:deliveredTelegramKeys.Contains($key)) {
            if ($key -eq $RequestedKey) { $requestedDelivered = $true }
            continue
        }
        $nextAttempt = [DateTimeOffset]::MinValue
        if ($null -ne $item.PSObject.Properties["next_attempt_utc"] -and
            -not [string]::IsNullOrWhiteSpace([string]$item.next_attempt_utc)) {
            try {
                $nextAttempt = [DateTimeOffset]::Parse(
                    [string]$item.next_attempt_utc
                ).ToUniversalTime()
            }
            catch { $nextAttempt = [DateTimeOffset]::MinValue }
        }
        if ($nextAttempt -gt $now) {
            $remaining.Add($item)
            continue
        }
        try {
            Send-TelegramText -Message $message
            [void]$script:deliveredTelegramKeys.Add($key)
            try { Update-TelegramDeliveryState -Key $key }
            catch {
                $line = "{0} Telegram delivery-state update: {1}" -f `
                    [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
                [IO.File]::AppendAllText(
                    $notificationErrorPath,
                    $line + [Environment]::NewLine
                )
            }
            if ($key -eq $RequestedKey) {
                $requestedDelivered = $true
            }
        }
        catch {
            $attemptCount = 1
            if ($null -ne $item.PSObject.Properties["attempt_count"]) {
                $attemptCount = [int]$item.attempt_count + 1
            }
            $delay = Get-TelegramRetryDelayMinutes -AttemptCount $attemptCount
            $remaining.Add([pscustomobject]@{
                key = $key
                message = $message
                queued_at_utc = [string]$item.queued_at_utc
                attempt_count = $attemptCount
                last_attempt_utc = $now.ToString("o")
                next_attempt_utc = $now.AddMinutes($delay).ToString("o")
            })
            $line = "{0} Telegram outbox delivery: {1}" -f [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
            [IO.File]::AppendAllText($notificationErrorPath, $line + [Environment]::NewLine)
        }
    }
    Write-JsonAtomically -Path $notificationOutboxPath -Value @($remaining)
    return $requestedDelivered
}

function Send-TelegramReliable {
    param(
        [string]$Key,
        [string]$Message
    )

    if ($script:deliveredTelegramKeys.Contains($Key)) {
        return $true
    }
    $entries = @(Read-TelegramOutbox)
    if (-not @($entries | Where-Object { [string]$_.key -eq $Key }).Count) {
        $entries += [pscustomobject]@{
            key = $Key
            message = $Message
            queued_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
            attempt_count = 0
            last_attempt_utc = ""
            next_attempt_utc = [DateTimeOffset]::UtcNow.ToString("o")
        }
        Write-JsonAtomically -Path $notificationOutboxPath -Value @($entries)
    }
    return Invoke-TelegramOutboxDelivery -RequestedKey $Key
}

function Send-TelegramNotification {
    param([hashtable]$Record)

    $currentState = [string]$Record.state
    $shouldSend = $TelegramAlertStates -contains $currentState
    $category = $currentState
    $notificationState = Read-TelegramRuntimeState
    $notifiedCategories = @($notificationState.active_alert_categories)
    $isRecovery = $currentState -eq "HEALTHY" -and $notifiedCategories.Count -gt 0
    if ((-not $shouldSend -and -not $isRecovery) -or
        ($shouldSend -and $notifiedCategories -contains $category)) {
        if ($currentState -eq "HEALTHY" -and
            [string]$notificationState.last_runtime_state -cne "HEALTHY") {
            Write-TelegramRuntimeState `
                -ActiveAlertCategories @() `
                -LastRuntimeState "HEALTHY"
        }
        return
    }
    $label = if ($isRecovery) { "RECOVERED" } else { $currentState }
    $message = "[$label] $env:COMPUTERNAME / $MainTaskName`n$($Record.detail)`n$($Record.observed_at_utc)"
    $key = "runtime|$label|$category"
    $delivered = Send-TelegramReliable -Key $key -Message $message
    if ($delivered) {
        if ($isRecovery) {
            $notifiedCategories = @()
        }
        elseif ($notifiedCategories -notcontains $category) {
            $notifiedCategories += $category
        }
        Write-TelegramRuntimeState `
            -ActiveAlertCategories $notifiedCategories `
            -LastRuntimeState $currentState
    }
}

function Send-DecisionNotifications {
    param($Health)

    if ($null -eq $Health) {
        return
    }
    $reportPath = [string]$Health.last_cycle.daily_report.path
    if ([string]::IsNullOrWhiteSpace($reportPath) -or -not (Test-Path -LiteralPath $reportPath)) {
        return
    }
    try {
        $report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
        $date = [string]$report.date
        $take = [int]$report.decisions.TAKE
        $skip = [int]$report.decisions.SKIP
        $invalid = [int]$report.decisions.DATA_INVALID
        $submitted = [int]$report.orders.submitted
        $pending = [int]$report.orders.open_pending
        $opened = [int]$report.orders.opened_deals
        $closed = [int]$report.orders.closed_deals
        $tp = [int]$report.orders.TP
        $sl = [int]$report.orders.SL
        $saved = $null
        if (Test-Path -LiteralPath $decisionNotificationStatePath) {
            try { $saved = Get-Content -LiteralPath $decisionNotificationStatePath -Raw | ConvertFrom-Json } catch { $saved = $null }
        }

        $decisionSignature = "$date|$take|$skip|$invalid"
        $orderSignature = "$date|$submitted|$pending|$opened|$closed|$tp|$sl"
        $decisionChanged = ($take + $skip + $invalid -gt 0) -and $decisionSignature -ne [string]$saved.decision_signature
        $orderChanged = ($submitted + $opened + $closed -gt 0) -and $orderSignature -ne [string]$saved.order_signature

        $savedDecisionSignature = [string]$saved.decision_signature
        $savedOrderSignature = [string]$saved.order_signature

        if ($decisionChanged) {
            $message = "[DECISION] $env:COMPUTERNAME / $MainTaskName`n$date | TAKE=$take SKIP=$skip DATA_INVALID=$invalid"
            if (Send-TelegramReliable -Key "decision|$decisionSignature" -Message $message) {
                $savedDecisionSignature = $decisionSignature
                Write-JsonAtomically -Path $decisionNotificationStatePath -Value @{
                    decision_signature = $savedDecisionSignature
                    order_signature = $savedOrderSignature
                    updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
                }
            }
        }
        if ($orderChanged) {
            $message = "[ORDER] $env:COMPUTERNAME / $MainTaskName`n$date | submitted=$submitted pending=$pending opened=$opened closed=$closed TP=$tp SL=$sl"
            if (Send-TelegramReliable -Key "order|$orderSignature" -Message $message) {
                $savedOrderSignature = $orderSignature
                Write-JsonAtomically -Path $decisionNotificationStatePath -Value @{
                    decision_signature = $savedDecisionSignature
                    order_signature = $savedOrderSignature
                    updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
                }
            }
        }
    }
    catch {
        $line = "{0} decision notification: {1}" -f [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
        [IO.File]::AppendAllText($notificationErrorPath, $line + [Environment]::NewLine)
    }
}

function Invoke-PremarketAudit {
    param(
        $Health,
        [string]$TaskState,
        [int]$ProcessCount,
        [DateTimeOffset]$Observed
    )

    $newYork = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($Observed, "Eastern Standard Time")
    $tradeDate = $newYork.ToString("yyyy-MM-dd")
    $weekday = $newYork.DayOfWeek -notin @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)
    $auditWindow = $newYork.Hour -eq 9 -and $newYork.Minute -ge 15 -and $newYork.Minute -lt 30
    if (-not $ForcePremarketAudit -and (-not $weekday -or -not $auditWindow)) {
        return
    }
    if (-not $ForcePremarketAudit -and (Test-Path -LiteralPath $premarketAuditStatePath)) {
        try {
            $priorAudit = Get-Content -LiteralPath $premarketAuditStatePath -Raw | ConvertFrom-Json
            if ([string]$priorAudit.trade_date -eq $tradeDate) {
                return
            }
        }
        catch {
            # An unreadable state is safely replaced by today's audit.
        }
    }

    $issues = @()
    if ($TaskState -ne "Running") { $issues += "main task state=$TaskState" }
    if ($ProcessCount -lt 1) { $issues += "Python process missing" }
    if ($null -eq $Health) {
        $issues += "health record missing"
    }
    else {
        if ([string]$Health.state -ne "RUNNING") { $issues += "health state=$([string]$Health.state)" }
        try {
            $heartbeatAge = ($Observed - [DateTimeOffset]::Parse([string]$Health.updated_at).ToUniversalTime()).TotalSeconds
            if ($heartbeatAge -gt $StaleSeconds) { $issues += "heartbeat stale=$([math]::Round($heartbeatAge, 1))s" }
        }
        catch { $issues += "heartbeat timestamp invalid" }
        foreach ($leg in @("nq", "spx")) {
            if ($Health.markets.$leg.streaming -ne $true) { $issues += "$leg market streaming=false" }
            $latestText = [string]$Health.last_cycle.fetches.$leg.latest_bar_utc
            if ([string]::IsNullOrWhiteSpace($latestText)) {
                $issues += "$leg latest candle missing"
            }
            else {
                try {
                    $barAge = ($Observed - [DateTimeOffset]::Parse($latestText).ToUniversalTime()).TotalSeconds
                    if ($barAge -gt 300) { $issues += "$leg latest candle stale=$([math]::Round($barAge / 60, 1))m" }
                }
                catch { $issues += "$leg latest candle timestamp invalid" }
            }
            if ([int]$Health.last_cycle.fetches.$leg.conflicts -gt 0) { $issues += "$leg conflicting candles detected" }
            if ([int]$Health.last_cycle.fetches.$leg.rejected -gt 0) { $issues += "$leg rejected candles detected" }
        }
    }

    $result = if ($issues.Count) { "FAIL" } else { "PASS" }
    if ($issues.Count) {
        try {
            $message = "[PREMARKET_CHECK_FAILED] $env:COMPUTERNAME / $MainTaskName`n$tradeDate 09:15 New York`n$($issues -join '; ')"
            Send-TelegramReliable -Key "premarket|$tradeDate" -Message $message | Out-Null
        }
        catch {
            $line = "{0} premarket audit notification: {1}" -f [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
            [IO.File]::AppendAllText($notificationErrorPath, $line + [Environment]::NewLine)
        }
    }
    $auditState = @{
        trade_date = $tradeDate
        checked_at_utc = $Observed.ToString("o")
        new_york_time = $newYork.ToString("o")
        result = $result
        issues = @($issues)
    } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($premarketAuditStatePath, $auditState + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}

function Read-HealthRecord {
    if (-not (Test-Path -LiteralPath $HealthPath)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $HealthPath -Raw | ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{
            state = "UNREADABLE"
            error = $_.Exception.Message
            updated_at = $null
        }
    }
}

function Read-Super1ActiveLease {
    if (-not (Test-Path -LiteralPath $LeasePath -PathType Leaf)) { return $null }
    try {
        $lease = Get-Content -LiteralPath $LeasePath -Raw | ConvertFrom-Json
        $now = [DateTimeOffset]::UtcNow
        $expires = [DateTimeOffset]::Parse([string]$lease.expires_at_utc).ToUniversalTime()
        if ([string]$lease.state -cne "ACTIVE" -or $expires -le $now) { return $null }
        if ([string]$lease.machine_binding -cne $env:COMPUTERNAME.ToUpperInvariant()) { return $null }
        $configHash = (Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifestHash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ([string]$lease.config_sha256 -cne $configHash -or
            [string]$lease.app_manifest_sha256 -cne $manifestHash -or
            [string]$lease.mode -cne "DEMO_ORDER") { return $null }
        return $lease
    }
    catch { return $null }
}

function Get-Super1ExactRunnerProcesses {
    $runnerSid = $null
    try {
        $runnerSid = (New-Object Security.Principal.NTAccount("$env:COMPUTERNAME\$RunnerAccount")).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch { return @() }
    $matches = @()
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ([string]$process.ExecutablePath -cne $CanonicalRunner -or
            [string]$process.CommandLine -notmatch [regex]::Escape($CanonicalRunner) -or
            [string]$process.CommandLine -notmatch [regex]::Escape($CanonicalHarness) -or
            [string]$process.CommandLine -notmatch [regex]::Escape($CanonicalState)) { continue }
        try {
            $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
            $account = if ([string]$owner.Domain) { "$($owner.Domain)\$($owner.User)" } else { [string]$owner.User }
            $sid = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value
            if ($sid -ceq $runnerSid) { $matches += $process }
        }
        catch { }
    }
    return @($matches)
}

function Write-WatchdogRecord {
    param([hashtable]$Record)

    $parent = Split-Path -Parent $StatusPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $json = $Record | ConvertTo-Json -Depth 6
    $temp = "$StatusPath.tmp"
    [IO.File]::WriteAllText($temp, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $StatusPath -Force
    Send-TelegramNotification -Record $Record

    $signature = "{0}|{1}|{2}" -f $Record.state, $Record.action, $Record.detail
    if ($signature -ne $script:lastEventSignature) {
        $compact = $Record | ConvertTo-Json -Depth 6 -Compress
        [IO.File]::AppendAllText($eventPath, $compact + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        $script:lastEventSignature = $signature
        if ([string]$Record.state -like "ALARM_*") {
            try {
                & eventcreate.exe /T ERROR /ID 100 /L APPLICATION /SO CodexWatchdog /D (
                    "{0}: {1}" -f $MainTaskName, $Record.detail
                ) | Out-Null
            }
            catch {
                # The JSON status/event files remain the authoritative alarm channel.
            }
        }
    }
}

function Save-RestartHistory {
    Write-JsonAtomically -Path $restartStatePath -Value @{
        restart_times_utc = @($restartHistory | ForEach-Object { $_.ToUniversalTime().ToString("o") })
        restart_window_minutes = $RestartWindowMinutes
        maximum_restarts = $MaximumRestarts
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    }
}

function Prune-RestartHistory {
    param([DateTimeOffset]$Now)

    $changed = $false
    for ($index = $restartHistory.Count - 1; $index -ge 0; $index--) {
        if (($Now - $restartHistory[$index]).TotalMinutes -gt $RestartWindowMinutes) {
            $restartHistory.RemoveAt($index)
            $changed = $true
        }
    }
    if ($changed) {
        Save-RestartHistory
    }
}

function Restart-MainTask {
    param([string]$Reason)

    $now = [DateTimeOffset]::UtcNow
    Prune-RestartHistory -Now $now
    if ($null -eq (Read-Super1ActiveLease)) {
        return [pscustomobject]@{ Restarted = $false; State = "WAITING_MANUAL_LEASE"; Detail = "$Reason; no active manual lease" }
    }
    if (Test-Path -LiteralPath $RestartBudgetLatchPath -PathType Leaf) {
        return [pscustomobject]@{ Restarted = $false; State = "RESTART_BUDGET_EXHAUSTED_NO_SEND"; Detail = "Persistent restart-budget latch is set." }
    }
    if ($restartHistory.Count -ge $MaximumRestarts) {
        Write-JsonAtomically -Path $RestartBudgetLatchPath -Value @{
            schema_version = 1
            state = "RESTART_BUDGET_EXHAUSTED_NO_SEND"
            restart_count = $restartHistory.Count
            restart_window_minutes = $RestartWindowMinutes
            updated_at_utc = $now.ToString("o")
            reason = $Reason
        }
        return [pscustomobject]@{
            Restarted = $false
            State = "ALARM_RESTART_LOOP"
            Detail = "$Reason; restart limit reached"
        }
    }

    $mutex = [Threading.Mutex]::new($false, [string]$RuntimeContract.order_mutex)
    $held = $false
    try {
        $held = $mutex.WaitOne(30000)
        if (-not $held) { return [pscustomobject]@{ Restarted = $false; State = "UNKNOWN_NO_SEND"; Detail = "Could not acquire order transport mutex." } }
        Stop-ScheduledTask -TaskName $MainTaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        if (@(Get-Super1ExactRunnerProcesses).Count -ne 0) {
            return [pscustomobject]@{ Restarted = $false; State = "UNKNOWN_NO_SEND"; Detail = "Existing exact Runner process did not stop gracefully." }
        }
        Start-ScheduledTask -TaskName $MainTaskName
    }
    finally {
        if ($held) { [void]$mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    $restartHistory.Add($now)
    Save-RestartHistory
    return [pscustomobject]@{
        Restarted = $true
        State = "RESTARTED"
        Detail = $Reason
    }
}

function Ensure-Super1SafeStopRequest {
    param([object]$Lease, [string]$Reason)
    $path = Join-Path ([string]$RuntimeContract.control) "stop-request.json"
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try {
            $existing = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
            if ([string]$existing.request_id -and [string]$existing.reason) { return $existing }
        }
        catch { }
    }
    $request = [ordered]@{
        schema_version = 1
        request_id = [Guid]::NewGuid().ToString()
        lease_id = if ($null -eq $Lease) { "" } else { [string]$Lease.lease_id }
        reason = $Reason
        requested_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        requested_by = "Super1Watchdog"
    }
    Write-JsonAtomically -Path $path -Value $request
    return [pscustomobject]$request
}

if ($LibraryOnly) {
    return
}

while ($true) {
    $observed = [DateTimeOffset]::UtcNow
    try {
        Prune-RestartHistory -Now $observed
        Invoke-TelegramOutboxDelivery | Out-Null
        $lease = Read-Super1ActiveLease
        $task = Get-ScheduledTask -TaskName $MainTaskName
        $taskInfo = Get-ScheduledTaskInfo -TaskName $MainTaskName
        $taskState = $task.State.ToString()
        $processes = @(Get-Super1ExactRunnerProcesses)
        $health = Read-HealthRecord
        $healthState = if ($null -eq $health) { "MISSING" } else { [string]$health.state }
        $executionState = if ($null -eq $health) { "" } else { [string]$health.last_cycle.execution.state }
        $healthAge = $null
        if ($null -ne $health -and -not [string]::IsNullOrWhiteSpace([string]$health.updated_at)) {
            $updated = [DateTimeOffset]::Parse([string]$health.updated_at).ToUniversalTime()
            $healthAge = [math]::Round(($observed - $updated).TotalSeconds, 1)
        }

        $state = "HEALTHY"
        $action = "NONE"
        $detail = "main task and heartbeat are healthy"
        $leaseExpired = $false
        if ($null -ne $lease) {
            try { $leaseExpired = [DateTimeOffset]::UtcNow -ge ([DateTimeOffset]::Parse([string]$lease.expires_at_utc).ToUniversalTime()) }
            catch { $leaseExpired = $true }
        }
        if ($healthState -eq "MISSING") {
            if ($null -eq $lease) {
                $state = "WAITING_MANUAL_LEASE"
                if ($processes.Count -gt 0) {
                    $null = Ensure-Super1SafeStopRequest -Lease $null -Reason "watchdog observed missing main health without a manual lease"
                    $action = "REQUEST_SAFE_STOP"
                }
                else { $action = "NONE" }
                $detail = "No active manual lease; missing health is fail-closed."
            }
            else {
                $state = "UNKNOWN_NO_SEND"
                $action = "NONE"
                $detail = "Main health is missing; restart is disabled until fresh evidence exists."
            }
        }
        elseif ($healthState -eq "UNREADABLE") {
            $state = "ALARM_CRITICAL"
            $action = "NONE"
            $detail = "Main health is unreadable; broker state is not proven."
        }
        elseif ($null -eq $lease) {
            $state = "WAITING_MANUAL_LEASE"
            if ($processes.Count -gt 0) {
                $null = Ensure-Super1SafeStopRequest -Lease $null -Reason "watchdog observed a running Super1 process without a manual lease"
                $action = "REQUEST_SAFE_STOP"
                $detail = "No active manual lease; automatic restart is disabled and safe stop is requested."
            }
            else {
                $action = "NONE"
                $detail = "No active manual lease and no exact Super1 process; automatic restart is disabled."
            }
        }
        elseif ($leaseExpired) {
            $null = Ensure-Super1SafeStopRequest -Lease $lease -Reason "manual Super1 lease expired"
            $state = "LEASE_EXPIRED_SAFE_STOP_PENDING"
            $action = "REQUEST_SAFE_STOP"
            $detail = "Lease expired; no restart is permitted until broker reconciliation proves safe stop."
        }
        elseif ($healthState -and $healthState -notin $AllowedMainHealthStates) {
            $state = "UNKNOWN_NO_SEND"
            $action = "NONE"
            $detail = "Main health state is outside the allowlist: $healthState"
        }
        elseif ($healthState -in @("CRITICAL_STOP", "UNSAFE_OPEN_ORDERS", "UNSAFE_STOP_NO_SEND")) {
            $state = "ALARM_CRITICAL"
            $detail = [string]$health.error
        }
        elseif ($taskState -ne "Running" -or $processes.Count -eq 0) {
            $restart = Restart-MainTask "main task or Python process is not running"
            $state = $restart.State
            $action = if ($restart.Restarted) { "START_MAIN_TASK" } else { "NONE" }
            $detail = $restart.Detail
        }
        elseif ($healthState -eq "MISSING") {
            $runAge = ($observed - [DateTimeOffset]$taskInfo.LastRunTime).TotalSeconds
            if ($runAge -le $StaleSeconds) {
                $state = "STARTING"
                $detail = "waiting for the first heartbeat"
            }
            else {
                $restart = Restart-MainTask "health file is missing after startup grace period"
                $state = $restart.State
                $action = if ($restart.Restarted) { "RESTART_MAIN_TASK" } else { "NONE" }
                $detail = $restart.Detail
            }
        }
        elseif ($healthState -eq "UNREADABLE") {
            $state = "ALARM_CRITICAL"
            $detail = "health file is unreadable: $($health.error)"
        }
        elseif ($healthState -eq "WAITING_CREDENTIALS") {
            $state = "ALARM_CREDENTIALS"
            $detail = "main daemon is missing required credentials"
        }
        elseif ($null -eq $healthAge -or $healthAge -gt $StaleSeconds) {
            $restart = Restart-MainTask "heartbeat is stale ($healthAge seconds)"
            $state = $restart.State
            $action = if ($restart.Restarted) { "RESTART_MAIN_TASK" } else { "NONE" }
            $detail = $restart.Detail
        }
        elseif ($healthState -eq "TECHNICAL_FAILURE") {
            $state = "DEGRADED_RETRYING"
            $detail = "main daemon is handling a transient technical failure"
        }
        elseif ($healthState -eq "RETRYING") {
            $state = "DEGRADED_RETRYING"
            $detail = if ([string]::IsNullOrWhiteSpace([string]$health.error)) {
                "main daemon is retrying after a runtime failure"
            }
            else {
                "main daemon is retrying: $([string]$health.error)"
            }
        }
        elseif ($executionState -eq "UNKNOWN_NO_SEND") {
            $state = "ALARM_BROKER_UNKNOWN"
            $detail = "broker order state is unknown; transmission is fail-closed"
        }
        elseif ($executionState -like "DATA_INVALID*") {
            $state = "ALARM_DATA_INVALID"
            $sessionPath = [string]$health.last_cycle.prefix.path
            $dataIssues = @()
            if (-not [string]::IsNullOrWhiteSpace($sessionPath) -and (Test-Path -LiteralPath $sessionPath)) {
                try {
                    $prefixRecord = Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
                    foreach ($gate in $prefixRecord.data_gates.PSObject.Properties) {
                        foreach ($issue in @($gate.Value.issues)) {
                            $dataIssues += "{0}: {1} count={2} first={3}" -f `
                                $gate.Name, $issue.code, $issue.count, $issue.first
                        }
                    }
                }
                catch {
                    $dataIssues = @()
                }
            }
            $reason = if ($dataIssues.Count) { $dataIssues -join "; " } else { "see prefix record" }
            $detail = "strategy blocked order transmission: $reason; record=$sessionPath"
        }

        Write-WatchdogRecord @{
            schema_version = 1
             observed_at_utc = $observed.ToString("o")
             updated_at_utc = $observed.ToString("o")
             lease_id = if ($null -eq $lease) { "" } else { [string]$lease.lease_id }
            invocation_nonce = if ($null -eq $lease) { "" } else { [string]$lease.lease_id }
            runner_sid = if ($null -eq $lease) { "" } else { [string]$lease.runner_sid }
            state = $state
            action = $action
            detail = $detail
            main_task = $MainTaskName
            main_task_state = $taskState
            main_task_last_result = $taskInfo.LastTaskResult
            process_count = $processes.Count
            health_state = $healthState
            execution_state = $executionState
            health_age_seconds = $healthAge
            restart_count_in_window = $restartHistory.Count
            restart_window_minutes = $RestartWindowMinutes
        }
        Send-DecisionNotifications -Health $health
        Invoke-PremarketAudit -Health $health -TaskState $taskState -ProcessCount $processes.Count -Observed $observed
    }
    catch {
        Write-WatchdogRecord @{
            schema_version = 1
            observed_at_utc = $observed.ToString("o")
            state = "WATCHDOG_ERROR"
            action = "NONE"
            detail = $_.Exception.Message
            invocation_nonce = ""
            main_task = $MainTaskName
            restart_count_in_window = $restartHistory.Count
            restart_window_minutes = $RestartWindowMinutes
        }
    }

    if ($OneShot) {
        break
    }
    Start-Sleep -Seconds ([math]::Max(10, $PollSeconds))
}
