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
$LeaseCli = [string](Join-Path ([string]$RuntimeContract.app) "scripts\super1_lease_cli.py")
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
$notificationDeadLetterPath = Join-Path $stateDirectory "watchdog_telegram_outbox.deadletter.jsonl"
$notificationCorruptionLatchPath = Join-Path $stateDirectory "watchdog_telegram_outbox.corrupt.json"
$decisionNotificationStatePath = Join-Path $stateDirectory "watchdog_decision_telegram_state.json"
$premarketAuditStatePath = Join-Path $stateDirectory "watchdog_premarket_audit_state.json"
$restartStatePath = Join-Path $stateDirectory "watchdog_restart_budget.json"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Legacy schema was $TelegramRuntimeStateSchema = 2; version 3 adds durable
# event_id/dedupe_key/state/attempt and Telegram acknowledgement fields.
$TelegramRuntimeStateSchema = 3
$TelegramAlertStates = @(
    "RESTARTED",
    "DEGRADED_RETRYING",
    "WATCHDOG_ERROR",
    "ALARM_CRITICAL",
    "ALARM_CREDENTIALS",
    "ALARM_BROKER_UNKNOWN",
    "ALARM_DATA_INVALID", "UNKNOWN_NO_SEND",
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

function Write-NoSendSentinel {
    param([string]$Reason, [object]$Details = @{})
    $path = Join-Path $CanonicalState "runtime\no_send.sentinel.json"
    $payload = [ordered]@{ schema_version = 1; state = "UNKNOWN_NO_SEND"; reason = $Reason; updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o") }
    if ($Details -is [hashtable]) { foreach ($key in $Details.Keys) { $payload[$key] = $Details[$key] } }
    Write-JsonAtomically -Path $path -Value $payload
}

function Write-TelegramOutbox {
    param([object[]]$Entries)
    $seenKeys = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $seenEventIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $requiredProperties = @(
        "key", "event_id", "dedupe_key", "state", "message", "queued_at_utc",
        "attempt_count", "attempted_at_utc", "last_attempt_utc", "next_attempt_utc",
        "telegram_message_id"
    )
    foreach ($entry in @($Entries)) {
        if ($null -eq $entry -or $null -eq $entry.PSObject) {
            throw "Telegram outbox contains a null or non-object entry."
        }
        foreach ($property in $requiredProperties) {
            if ($null -eq $entry.PSObject.Properties[$property]) {
                throw "Telegram outbox entry is missing required property: $property"
            }
        }
        $key = [string]$entry.key
        $eventId = [string]$entry.event_id
        $dedupeKey = [string]$entry.dedupe_key
        $attemptCount = 0
        $telegramMessageId = 0
        try {
            if ($entry.attempt_count -is [bool]) { throw "attempt_count must be an integer" }
            $attemptCount = [int]$entry.attempt_count
            if ($entry.telegram_message_id -is [bool]) { throw "telegram_message_id must be an integer" }
            $telegramMessageId = [int]$entry.telegram_message_id
        }
        catch { throw "Telegram outbox numeric schema is invalid." }
        if ([string]::IsNullOrWhiteSpace($key) -or
            [string]::IsNullOrWhiteSpace($eventId) -or
            $dedupeKey -cne $key -or
            -not $seenKeys.Add($key) -or
            -not $seenEventIds.Add($eventId) -or
            [string]$entry.state -notin @("PENDING", "IN_FLIGHT", "ACKED") -or
            [string]::IsNullOrWhiteSpace([string]$entry.message) -or
            $attemptCount -lt 0 -or
            $telegramMessageId -lt 0) {
            throw "Telegram outbox contains an invalid or duplicate durable entry."
        }
        if ([string]$entry.state -eq "ACKED" -and $telegramMessageId -le 0) {
            throw "Telegram outbox ACKED entry has no confirmed Telegram message id."
        }
    }
    Write-JsonAtomically -Path $notificationOutboxPath -Value ([ordered]@{ schema_version = 3; entries = @($Entries) })
}

function Test-SignedMarketSessionOpen {
    param([DateTimeOffset]$Observed)
    try {
        $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
        $calendarBinding = $config.rth_session_calendar
        $relative = [string]$calendarBinding.path
        $calendarPath = Join-Path ([string]$RuntimeContract.app) $relative
        if (-not (Test-Path -LiteralPath $calendarPath -PathType Leaf)) { throw "signed market calendar is missing" }
        $actualHash = (Get-FileHash -LiteralPath $calendarPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -cne ([string]$calendarBinding.sha256).ToLowerInvariant()) { throw "signed market calendar hash mismatch" }
        $calendar = Get-Content -LiteralPath $calendarPath -Raw | ConvertFrom-Json
        if ([string]$calendar.calendar_id -cne [string]$calendarBinding.calendar_id -or
            [string]$calendar.timezone -cne "America/New_York" -or
            @($calendar.source_records).Count -eq 0) { throw "signed market calendar identity or provenance is invalid" }
        $local = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($Observed, "Eastern Standard Time")
        $coverageStart = [DateTime]::ParseExact([string]$calendar.coverage.start, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
        $coverageEnd = [DateTime]::ParseExact([string]$calendar.coverage.end, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
        if ($local.Date -lt $coverageStart -or $local.Date -gt $coverageEnd) { throw "observed time is outside signed calendar coverage" }
        $session = @($calendar.sessions | Where-Object { [string]$_.date -eq $local.ToString("yyyy-MM-dd") })
        if ($session.Count -gt 1) { throw "signed market calendar has duplicate session rows" }
        if ($session.Count -eq 1) {
            if ([string]$session[0].state -eq "CLOSED") { return [pscustomobject]@{ state = "CLOSED"; detail = "exchange closed" } }
            $sessionStart = [string]$session[0].start
            $sessionEnd = [string]$session[0].end
        }
        else {
            if ($local.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday) -or
                @($calendar.closed_dates) -contains $local.ToString("yyyy-MM-dd")) {
                return [pscustomobject]@{ state = "CLOSED"; detail = "exchange closed" }
            }
            $sessionStart = [string]$calendar.regular_session.open
            $sessionEnd = if (@($calendar.early_close_dates) -contains $local.ToString("yyyy-MM-dd")) {
                [string]$calendar.early_close_time
            } else { [string]$calendar.regular_session.close }
        }
        $zone = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
        $format = "yyyy-MM-dd HH:mm"
        $culture = [Globalization.CultureInfo]::InvariantCulture
        $styles = [Globalization.DateTimeStyles]::AssumeUnspecified
        $localStart = [DateTime]::ParseExact(
            ("{0} {1}" -f $local.ToString("yyyy-MM-dd"), $sessionStart),
            $format, $culture, $styles
        )
        $localEnd = [DateTime]::ParseExact(
            ("{0} {1}" -f $local.ToString("yyyy-MM-dd"), $sessionEnd),
            $format, $culture, $styles
        )
        $start = [DateTimeOffset]([TimeZoneInfo]::ConvertTimeToUtc($localStart, $zone))
        $end = [DateTimeOffset]([TimeZoneInfo]::ConvertTimeToUtc($localEnd, $zone))
        $observedUtc = $Observed.ToUniversalTime()
        return [pscustomobject]@{
            state = if ($observedUtc -ge $start.ToUniversalTime() -and $observedUtc -lt $end.ToUniversalTime()) { "OPEN" } else { "CLOSED" }
            detail = "signed market calendar verified"
        }
    }
    catch { return [pscustomobject]@{ state = "UNKNOWN_NO_SEND"; detail = $_.Exception.Message } }
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
        $response = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot{0}/sendMessage" -f $credential.token) `
            -ContentType "application/json" -Body $body -TimeoutSec 15
        if ($null -eq $response -or $response.ok -ne $true -or [int]$response.result.message_id -le 0) {
            throw "Telegram response is not a confirmed successful message acknowledgement."
        }
        return $response
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
        if ($parsed -is [array]) {
            throw "Telegram outbox legacy array schema is not accepted; preserving it for operator recovery."
        }
        if ([int]$parsed.schema_version -ne 3 -or $null -eq $parsed.entries) {
            throw "Telegram outbox schema is corrupt; preserving it for operator recovery."
        }
        $entries = @($parsed.entries | Where-Object { $null -ne $_ })
        Write-TelegramOutbox -Entries $entries
        return $entries
    }
    catch {
        $line = "{0} unreadable Telegram outbox: {1}" -f [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
        [IO.File]::AppendAllText($notificationErrorPath, $line + [Environment]::NewLine)
        try { Write-JsonAtomically -Path $notificationCorruptionLatchPath -Value @{ schema_version = 1; state = "OUTBOX_CORRUPT"; detected_at_utc = [DateTimeOffset]::UtcNow.ToString("o"); error = $_.Exception.Message } } catch {}
        try { Write-NoSendSentinel -Reason "TELEGRAM_OUTBOX_CORRUPT" -Details @{ error = $_.Exception.Message } } catch {}
        throw "Telegram outbox is unreadable; preserving it for operator recovery."
    }
}

function Read-TelegramRuntimeState {
    $empty = [pscustomobject]@{
        schema_version = $TelegramRuntimeStateSchema
        active_alert_categories = @()
        active_alert_episodes = [ordered]@{}
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
        $episodes = [ordered]@{}
        if ($null -ne $saved.PSObject.Properties["active_alert_episodes"] -and $null -ne $saved.active_alert_episodes) {
            foreach ($property in $saved.active_alert_episodes.PSObject.Properties) {
                if (-not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
                    $episodes[[string]$property.Name] = [string]$property.Value
                }
            }
        }
        return [pscustomobject]@{
            schema_version = $TelegramRuntimeStateSchema
            active_alert_categories = $categories
            active_alert_episodes = $episodes
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
        [string]$LastRuntimeState,
        [object]$ActiveAlertEpisodes = @{}
    )
    $episodes = [ordered]@{}
    if ($ActiveAlertEpisodes -is [System.Collections.IDictionary]) {
        foreach ($key in $ActiveAlertEpisodes.Keys) {
            if (-not [string]::IsNullOrWhiteSpace([string]$ActiveAlertEpisodes[$key])) {
                $episodes[[string]$key] = [string]$ActiveAlertEpisodes[$key]
            }
        }
    }
    elseif ($null -ne $ActiveAlertEpisodes -and $null -ne $ActiveAlertEpisodes.PSObject) {
        foreach ($property in $ActiveAlertEpisodes.PSObject.Properties) {
            if (-not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
                $episodes[[string]$property.Name] = [string]$property.Value
            }
        }
    }
    Write-JsonAtomically -Path $notificationStatePath -Value @{
        schema_version = $TelegramRuntimeStateSchema
        active_alert_categories = @($ActiveAlertCategories)
        active_alert_episodes = $episodes
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
        $label = [string]$parts[$parts.Count - 2]
        $category = [string]$parts[$parts.Count - 1]
        $state = Read-TelegramRuntimeState
        $categories = @($state.active_alert_categories)
        if ($label -eq "RECOVERED") {
            $categories = @()
            if ($state.active_alert_episodes -is [System.Collections.IDictionary]) {
                $state.active_alert_episodes.Remove($category)
            }
        }
        elseif ($TelegramAlertStates -contains $category -and
            $categories -notcontains $category) {
            $categories += $category
        }
        Write-TelegramRuntimeState `
            -ActiveAlertCategories $categories `
            -LastRuntimeState $category `
            -ActiveAlertEpisodes $state.active_alert_episodes
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
    $entries = @(Read-TelegramOutbox)
    $now = [DateTimeOffset]::UtcNow
    $selectedIndex = -1
    for ($index = 0; $index -lt $entries.Count; $index++) {
        $item = $entries[$index]
        if ($null -eq $item) {
            throw "Telegram outbox contains a null entry; operator recovery is required."
        }
        $key = if ($item.PSObject.Properties["dedupe_key"]) { [string]$item.dedupe_key } else { [string]$item.key }
        $eventId = if ($item.PSObject.Properties["event_id"] -and [string]$item.event_id) { [string]$item.event_id } else { $key }
        $message = [string]$item.message
        if ([string]::IsNullOrWhiteSpace($key) -or
            [string]::IsNullOrWhiteSpace($message)) {
            $line = "{0} discarded invalid Telegram outbox entry" -f `
                $now.ToString("o")
            [IO.File]::AppendAllText(
                $notificationErrorPath,
                $line + [Environment]::NewLine
            )
            [IO.File]::AppendAllText($notificationDeadLetterPath, (($item | ConvertTo-Json -Depth 8 -Compress) + [Environment]::NewLine))
            try { Write-JsonAtomically -Path $notificationCorruptionLatchPath -Value @{ schema_version = 1; state = "OUTBOX_ENTRY_INVALID"; detected_at_utc = [DateTimeOffset]::UtcNow.ToString("o") } } catch {}
            try { Write-NoSendSentinel -Reason "TELEGRAM_OUTBOX_ENTRY_INVALID" } catch {}
            throw "Telegram outbox contains an invalid entry; operator recovery is required."
        }
        if ($item.PSObject.Properties["state"] -and [string]$item.state -eq "ACKED") {
            [void]$script:deliveredTelegramKeys.Add($key)
            continue
        }
        if ($script:deliveredTelegramKeys.Contains($key)) {
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
            continue
        }
        if ($RequestedKey -and $key -ne $RequestedKey) { continue }
        $selectedIndex = $index
        break
    }
    if ($selectedIndex -lt 0) {
        return [bool]($RequestedKey -and $script:deliveredTelegramKeys.Contains($RequestedKey))
    }

    # One invocation owns one entry.  The complete array is rewritten after
    # each state change, so every prefix/suffix item survives unchanged.
    $item = $entries[$selectedIndex]
    $key = [string]$item.key
    $eventId = [string]$item.event_id
    $message = if ([string]$item.message -match '\[event_id=') { [string]$item.message } else { "$($item.message)`n[event_id=$eventId]" }
    $attemptCount = [int]$item.attempt_count + 1
    $item.state = "IN_FLIGHT"
    $item.message = $message
    $item.last_attempt_utc = $now.ToString("o")
    $item.attempted_at_utc = $now.ToString("o")
    $item.attempt_count = $attemptCount
    $entries[$selectedIndex] = $item
    Write-TelegramOutbox -Entries $entries
    try {
        $telegramResponse = Send-TelegramText -Message $message
        $item.state = "ACKED"
        $item.telegram_message_id = [int]$telegramResponse.result.message_id
        $entries[$selectedIndex] = $item
        Write-TelegramOutbox -Entries $entries
        [void]$script:deliveredTelegramKeys.Add($key)
        try { Update-TelegramDeliveryState -Key $key }
        catch {
            $line = "{0} Telegram delivery-state update: {1}" -f [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
            [IO.File]::AppendAllText($notificationErrorPath, $line + [Environment]::NewLine)
        }
        return [bool]($RequestedKey -and $key -eq $RequestedKey)
    }
    catch {
        $delay = Get-TelegramRetryDelayMinutes -AttemptCount $attemptCount
        $item.state = "PENDING"
        $item.next_attempt_utc = $now.AddMinutes($delay).ToString("o")
        $item.telegram_message_id = 0
        $entries[$selectedIndex] = $item
        Write-TelegramOutbox -Entries $entries
        $line = "{0} Telegram outbox delivery: {1}" -f [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
        [IO.File]::AppendAllText($notificationErrorPath, $line + [Environment]::NewLine)
        return $false
    }
}

function Send-TelegramReliable {
    param(
        [string]$Key,
        [string]$Message,
        [string]$AlertEpisodeId = "",
        [string]$AlertCategory = ""
    )

    if ($script:deliveredTelegramKeys.Contains($Key)) {
        return $true
    }
    $entries = @(Read-TelegramOutbox)
    if (-not @($entries | Where-Object { [string]$_.key -eq $Key }).Count) {
        $entries += [pscustomobject]@{
            key = $Key
            event_id = $Key
            dedupe_key = $Key
            state = "PENDING"
            message = $Message
            queued_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
            attempt_count = 0
            attempted_at_utc = ""
            last_attempt_utc = ""
            next_attempt_utc = [DateTimeOffset]::UtcNow.ToString("o")
            telegram_message_id = 0
            alert_episode_id = $AlertEpisodeId
            alert_category = $AlertCategory
        }
        Write-TelegramOutbox -Entries $entries
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
    $episodeMap = @{}
    if ($notificationState.active_alert_episodes -is [System.Collections.IDictionary]) {
        foreach ($key in $notificationState.active_alert_episodes.Keys) {
            $episodeMap[[string]$key] = [string]$notificationState.active_alert_episodes[$key]
        }
    }
    $episodeCategory = if ($isRecovery) { [string]$notifiedCategories[0] } else { $category }
    $episodeId = [string]$episodeMap[$episodeCategory]
    if ([string]::IsNullOrWhiteSpace($episodeId)) {
        try {
            $queuedEpisode = @(Read-TelegramOutbox | Where-Object {
                [string]$_.alert_category -ceq $episodeCategory -and
                -not [string]::IsNullOrWhiteSpace([string]$_.alert_episode_id)
            } | Select-Object -Last 1)
            if ($queuedEpisode.Count -eq 1) { $episodeId = [string]$queuedEpisode[0].alert_episode_id }
        }
        catch { throw }
    }
    if ([string]::IsNullOrWhiteSpace($episodeId)) {
        $episodeId = [Guid]::NewGuid().ToString("N")
    }
    $episodeMap[$episodeCategory] = $episodeId
    $message = "[$label] $env:COMPUTERNAME / $MainTaskName`n$($Record.detail)`n$($Record.observed_at_utc)`n[alert_episode_id=$episodeId]"
    $key = "runtime|$episodeId|$label|$episodeCategory"
    $delivered = Send-TelegramReliable -Key $key -Message $message -AlertEpisodeId $episodeId -AlertCategory $episodeCategory
    if ($delivered) {
        if ($isRecovery) {
            $notifiedCategories = @()
            $episodeMap.Remove($episodeCategory)
        }
        elseif ($notifiedCategories -notcontains $category) {
            $notifiedCategories += $category
        }
    }
    Write-TelegramRuntimeState `
        -ActiveAlertCategories $notifiedCategories `
        -LastRuntimeState $currentState `
        -ActiveAlertEpisodes $episodeMap
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

function Get-Super1ReadinessEvidence {
    $archiveRoot = Join-Path ([string]$RuntimeContract.root) "archive"
    $best = $null
    if (-not (Test-Path -LiteralPath $archiveRoot -PathType Container)) { return $null }
    foreach ($transaction in @(Get-ChildItem -LiteralPath $archiveRoot -Directory -Filter "readiness-*" -Force -ErrorAction SilentlyContinue)) {
        $producerPath = Join-Path $transaction.FullName "output\producer.json"
        if (-not (Test-Path -LiteralPath $producerPath -PathType Leaf)) { continue }
        try {
            $producer = Get-Content -LiteralPath $producerPath -Raw | ConvertFrom-Json
            $produced = [DateTimeOffset]::Parse([string]$producer.produced_at_utc).ToUniversalTime()
            $now = [DateTimeOffset]::UtcNow
            if ([string]$producer.kind -cne "binding_readiness" -or
                ($now - $produced).TotalSeconds -gt 180 -or
                ($null -ne $best -and $produced -le $best.produced_at_utc) -or
                [int]$producer.producer_process_id -le 4) { continue }
            $best = [pscustomobject]@{
                transaction_id = [string]$producer.transaction_id
                nonce = [string]$producer.nonce
                request_sha256 = [string]$producer.request_sha256
                producer_process_id = [int]$producer.producer_process_id
                producer_runner_sid = [string]$producer.producer_runner_sid
                produced_at_utc = $produced
            }
        }
        catch { }
    }
    return $best
}

function Read-Super1LeaseState {
    $stdoutPath = "$LeasePath.$PID.lease-cli.stdout"
    $stderrPath = "$LeasePath.$PID.lease-cli.stderr"
    try {
        if (-not (Test-Path -LiteralPath $LeaseCli -PathType Leaf)) {
            return [pscustomobject]@{ State = "INVALID"; Lease = $null; LeaseId = ""; Detail = "canonical lease CLI is missing" }
        }
        $process = Start-Process -FilePath $CanonicalRunner -ArgumentList @(
            "-I", "-E", "-B", $LeaseCli, "--root", ([string]$RuntimeContract.root)
        ) -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $stdout = if (Test-Path -LiteralPath $stdoutPath) { [IO.File]::ReadAllText($stdoutPath) } else { "" }
        $stderr = if (Test-Path -LiteralPath $stderrPath) { [IO.File]::ReadAllText($stderrPath) } else { "" }
        $lines = @($stdout -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($lines.Count -ne 1 -or $process.ExitCode -notin @(0, 2)) {
            return [pscustomobject]@{ State = "INVALID"; Lease = $null; LeaseId = ""; Detail = "canonical lease CLI failed: exit=$($process.ExitCode) $stderr" }
        }
        $payload = $lines[0] | ConvertFrom-Json -ErrorAction Stop
        $state = [string]$payload.state
        if ($state -notin @("ACTIVE", "MISSING", "REVOKED", "EXPIRED", "INVALID")) {
            return [pscustomobject]@{ State = "INVALID"; Lease = $null; LeaseId = [string]$payload.lease_id; Detail = "canonical lease CLI returned an unknown state" }
        }
        return [pscustomobject]@{
            State = $state
            Lease = $payload.lease
            LeaseId = [string]$payload.lease_id
            Detail = [string]$payload.detail
        }
    }
    catch {
        return [pscustomobject]@{
            State = "INVALID"
            Lease = $null
            LeaseId = ""
            Detail = "lease is unreadable or invalid: $($_.Exception.Message)"
        }
    }
    finally {
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
        }
    }
}

function Read-Super1ActiveLease {
    $evidence = Read-Super1LeaseState
    if ([string]$evidence.State -eq "ACTIVE") { return $evidence.Lease }
    return $null
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
    # Telegram/outbox is best-effort; local JSON status and Windows Event Log
    # must remain available even when the notification channel is corrupt or down.
    try {
        Send-TelegramNotification -Record $Record
    }
    catch {
        $notificationLine = "{0} local-status notification failure: {1}" -f `
            [DateTimeOffset]::UtcNow.ToString("o"), $_.Exception.Message
        try {
            [IO.File]::AppendAllText(
                $notificationErrorPath,
                $notificationLine + [Environment]::NewLine
            )
        }
        catch {
            # Never replace the authoritative local watchdog status with a
            # failure in the optional notification error channel.
        }
    }

    $signature = "{0}|{1}|{2}" -f $Record.state, $Record.action, $Record.detail
    if ($signature -ne $script:lastEventSignature) {
        $compact = $Record | ConvertTo-Json -Depth 6 -Compress
        [IO.File]::AppendAllText($eventPath, $compact + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        $script:lastEventSignature = $signature
        if ([string]$Record.state -like "ALARM_*" -or [string]$Record.state -eq "UNKNOWN_NO_SEND") {
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
    $mutex = [Threading.Mutex]::new($false, [string]$RuntimeContract.order_mutex)
    $held = $false
    try {
        $held = $mutex.WaitOne(30000)
        if (-not $held) { return [pscustomobject]@{ Restarted = $false; State = "UNKNOWN_NO_SEND"; Detail = "Could not acquire order transport mutex." } }
        $leaseEvidence = Read-Super1LeaseState
        if ([string]$leaseEvidence.State -ne "ACTIVE") {
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
            return [pscustomobject]@{ Restarted = $false; State = "ALARM_RESTART_LOOP"; Detail = "$Reason; restart limit reached" }
        }
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
    $leaseHash = if (Test-Path -LiteralPath $LeasePath -PathType Leaf) {
        (Get-FileHash -LiteralPath $LeasePath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { "" }
    $leaseId = if ($null -eq $Lease) { "" } else { [string]$Lease.lease_id }
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try {
            $existing = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
            $requested = [DateTimeOffset]::Parse([string]$existing.requested_at_utc).ToUniversalTime()
            if ([string]$existing.request_id -and
                [string]$existing.lease_id -ceq $leaseId -and
                [string]$existing.lease_sha256 -ceq $leaseHash -and
                [string]$existing.reason -ceq $Reason -and
                ([DateTimeOffset]::UtcNow - $requested).TotalSeconds -le 90) { return $existing }
            $archive = Join-Path ([string]$RuntimeContract.control) ("stop-archive-" + [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssfffZ") + ".json")
            Move-Item -LiteralPath $path -Destination $archive -Force
        }
        catch { }
    }
    $request = [ordered]@{
        schema_version = 1
        request_id = [Guid]::NewGuid().ToString()
        lease_id = $leaseId
        lease_sha256 = $leaseHash
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
        $leaseEvidence = Read-Super1LeaseState
        $lease = $leaseEvidence.Lease
        $task = Get-ScheduledTask -TaskName $MainTaskName
        $taskInfo = Get-ScheduledTaskInfo -TaskName $MainTaskName
        $taskState = $task.State.ToString()
        $processes = @(Get-Super1ExactRunnerProcesses)
        $health = Read-HealthRecord
        $readinessEvidence = Get-Super1ReadinessEvidence
        $healthState = if ($null -eq $health) { "MISSING" } else { [string]$health.state }
        $executionState = if ($null -eq $health) { "" } else { [string]$health.last_cycle.execution.state }
        $healthAge = $null
        if ($null -ne $health -and -not [string]::IsNullOrWhiteSpace([string]$health.updated_at)) {
            $updated = [DateTimeOffset]::Parse([string]$health.updated_at).ToUniversalTime()
            $healthAge = [math]::Round(($observed - $updated).TotalSeconds, 1)
        }
        $componentHeartbeatIssues = @()
        $calendarEvidence = Test-SignedMarketSessionOpen -Observed $observed
        $marketFreshnessRequired = [string]$calendarEvidence.state -eq "OPEN"
        foreach ($component in @("last_market_data", "last_signal_cycle", "last_risk_cycle", "last_reconciliation", "last_audit_anchor")) {
            if ($component -eq "last_market_data" -and -not $marketFreshnessRequired) { continue }
            $stamp = if ($null -eq $health) { "" } else { [string]$health.$component }
            if ([string]::IsNullOrWhiteSpace($stamp)) {
                $componentHeartbeatIssues += "$component missing"
            }
            else {
                try {
                    $age = ($observed - [DateTimeOffset]::Parse($stamp).ToUniversalTime()).TotalSeconds
                    if ($age -gt $StaleSeconds) { $componentHeartbeatIssues += "$component stale=$([math]::Round($age, 1))s" }
                }
                catch { $componentHeartbeatIssues += "$component timestamp invalid" }
            }
        }

        $state = "HEALTHY"
        $action = "NONE"
        $detail = "main task and heartbeat are healthy"
        if ($healthState -eq "MISSING") {
            Write-NoSendSentinel -Reason "HEALTH_MISSING"
            if ([string]$leaseEvidence.State -ne "ACTIVE") {
                $state = switch ([string]$leaseEvidence.State) {
                    "EXPIRED" { "LEASE_EXPIRED_SAFE_STOP_PENDING"; break }
                    "REVOKED" { "LEASE_REVOKED_SAFE_STOP_PENDING"; break }
                    "INVALID" { "LEASE_INVALID_SAFE_STOP_PENDING"; break }
                    default { "WAITING_MANUAL_LEASE"; break }
                }
                if ($processes.Count -gt 0) {
                    $null = Ensure-Super1SafeStopRequest -Lease $lease -Reason "watchdog observed missing main health with lease state $($leaseEvidence.State)"
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
            Write-NoSendSentinel -Reason "HEALTH_UNREADABLE"
            $state = "ALARM_CRITICAL"
            $action = "NONE"
            $detail = "Main health is unreadable; broker state is not proven."
        }
        elseif ([string]$leaseEvidence.State -ne "ACTIVE") {
            Write-NoSendSentinel -Reason "LEASE_NOT_ACTIVE" -Details @{ lease_state = [string]$leaseEvidence.State }
            $state = switch ([string]$leaseEvidence.State) {
                "EXPIRED" { "LEASE_EXPIRED_SAFE_STOP_PENDING"; break }
                "REVOKED" { "LEASE_REVOKED_SAFE_STOP_PENDING"; break }
                "INVALID" { "LEASE_INVALID_SAFE_STOP_PENDING"; break }
                default { "WAITING_MANUAL_LEASE"; break }
            }
            if ($processes.Count -gt 0) {
                $null = Ensure-Super1SafeStopRequest -Lease $lease -Reason "watchdog observed a running Super1 process with lease state $($leaseEvidence.State)"
                $action = "REQUEST_SAFE_STOP"
                $detail = "Lease state $($leaseEvidence.State); automatic restart is disabled and safe stop is requested."
            }
            else {
                $action = "NONE"
                $detail = "Lease state $($leaseEvidence.State) and no exact Super1 process; automatic restart is disabled."
            }
        }
        elseif ([string]$calendarEvidence.state -eq "UNKNOWN_NO_SEND") {
            Write-NoSendSentinel -Reason "MARKET_CALENDAR_UNKNOWN_NO_SEND" -Details @{ detail = [string]$calendarEvidence.detail }
            $state = "UNKNOWN_NO_SEND"
            $action = "NONE"
            $detail = "Signed market calendar could not be proven; automatic restart and sends are disabled."
        }
        elseif ($healthState -in @("CRITICAL_STOP", "UNSAFE_OPEN_ORDERS", "UNSAFE_STOP_NO_SEND")) {
            Write-NoSendSentinel -Reason "HEALTH_CRITICAL_STOP" -Details @{ health_state = $healthState }
            $state = "ALARM_CRITICAL"
            $detail = [string]$health.error
        }
        elseif ($executionState -eq "UNKNOWN_NO_SEND") {
            Write-NoSendSentinel -Reason "BROKER_UNKNOWN_NO_SEND"
            $state = "ALARM_BROKER_UNKNOWN"
            $action = "NONE"
            $detail = "broker order state is unknown; automatic restart is disabled and transmission remains fail-closed"
        }
        elseif ($componentHeartbeatIssues.Count -gt 0) {
            Write-NoSendSentinel -Reason "HEARTBEAT_STALE_OR_MISSING" -Details @{ issues = @($componentHeartbeatIssues) }
            $state = "UNKNOWN_NO_SEND"
            $action = "NONE"
            $detail = "A required runtime component heartbeat is stale; new sends are blocked: $($componentHeartbeatIssues -join '; ')"
        }
        elseif ($healthState -and $healthState -notin $AllowedMainHealthStates) {
            Write-NoSendSentinel -Reason "HEALTH_STATE_NOT_ALLOWED" -Details @{ health_state = $healthState }
            $state = "UNKNOWN_NO_SEND"
            $action = "NONE"
            $detail = "Main health state is outside the allowlist: $healthState"
        }
        elseif ($taskState -ne "Running" -or $processes.Count -eq 0) {
            $restart = Restart-MainTask "main task or Python process is not running"
            $state = $restart.State
            $action = if ($restart.Restarted) { "START_MAIN_TASK" } else { "NONE" }
            $detail = $restart.Detail
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
            lease_id = [string]$leaseEvidence.LeaseId
            invocation_nonce = if ($null -eq $lease) { "" } else { [string]$lease.invocation_nonce }
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
            last_market_data = if ($null -eq $health) { "" } else { [string]$health.last_market_data }
            last_signal_cycle = if ($null -eq $health) { "" } else { [string]$health.last_signal_cycle }
            last_risk_cycle = if ($null -eq $health) { "" } else { [string]$health.last_risk_cycle }
            last_reconciliation = if ($null -eq $health) { "" } else { [string]$health.last_reconciliation }
            last_audit_anchor = if ($null -eq $health) { "" } else { [string]$health.last_audit_anchor }
            health_age_seconds = $healthAge
            restart_count_in_window = $restartHistory.Count
            restart_window_minutes = $RestartWindowMinutes
            watchdog_sid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            readiness_transaction_id = if ($null -eq $readinessEvidence) { "" } else { [string]$readinessEvidence.transaction_id }
            readiness_nonce = if ($null -eq $readinessEvidence) { "" } else { [string]$readinessEvidence.nonce }
            readiness_request_sha256 = if ($null -eq $readinessEvidence) { "" } else { [string]$readinessEvidence.request_sha256 }
            readiness_producer_pid = if ($null -eq $readinessEvidence) { 0 } else { [int]$readinessEvidence.producer_process_id }
            readiness_producer_sid = if ($null -eq $readinessEvidence) { "" } else { [string]$readinessEvidence.producer_runner_sid }
        }
        Send-DecisionNotifications -Health $health
        Invoke-PremarketAudit -Health $health -TaskState $taskState -ProcessCount $processes.Count -Observed $observed
    }
    catch {
        try { Write-NoSendSentinel -Reason "WATCHDOG_ERROR" -Details @{ error = $_.Exception.Message } } catch {}
        Write-WatchdogRecord @{
            schema_version = 1
            observed_at_utc = $observed.ToString("o")
            state = "WATCHDOG_ERROR"
            action = "NONE"
            detail = $_.Exception.Message
            invocation_nonce = ""
            watchdog_sid = [string][Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            readiness_transaction_id = ""
            readiness_nonce = ""
            readiness_request_sha256 = ""
            readiness_producer_pid = 0
            readiness_producer_sid = ""
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
