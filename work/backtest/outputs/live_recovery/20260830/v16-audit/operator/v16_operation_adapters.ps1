Set-StrictMode -Version Latest

function Test-V16HasProperty {
    param([AllowNull()][object]$Object, [Parameter(Mandatory = $true)][string]$Name)
    return $null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]
}

function Get-V16Field {
    param([AllowNull()][object]$Object, [Parameter(Mandatory = $true)][string]$Name)
    if (Test-V16HasProperty $Object $Name) { return $Object.PSObject.Properties[$Name].Value }
    return $null
}

function Get-V16NestedField {
    param([AllowNull()][object]$Object, [Parameter(Mandatory = $true)][string[]]$Path)
    $current = $Object
    foreach ($name in $Path) {
        if (-not (Test-V16HasProperty $current $name)) { return $null }
        $current = $current.PSObject.Properties[$name].Value
    }
    return $current
}

function Convert-V16ExactJsonDocument {
    param([Parameter(Mandatory = $true)][string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { throw 'JSON_EMPTY' }
    $trimmed = $Text.Trim()
    if ($trimmed[0] -ne '{' -or $trimmed[$trimmed.Length - 1] -ne '}') { throw 'JSON_NOT_OBJECT' }
    try { return $trimmed | ConvertFrom-Json -ErrorAction Stop } catch { throw "JSON_INVALID:$($_.Exception.Message)" }
}

function New-V16Condition {
    param([string]$Id, [AllowNull()][object]$Actual, [AllowNull()][object]$Expected, [AllowNull()][object]$Rejected, [string]$SourceFileSha256)
    [pscustomobject]@{ condition_id=$Id; actual=if($null -eq $Actual){'UNKNOWN'}else{$Actual}; expected=if($null -eq $Expected){'UNKNOWN'}else{$Expected}; rejected=$Rejected; source_file_sha256=if([string]::IsNullOrWhiteSpace($SourceFileSha256)){'UNKNOWN'}else{$SourceFileSha256} }
}

function Test-V16TypedEqual {
    param([AllowNull()][object]$Actual, [AllowNull()][object]$Expected, [ValidateSet('string','bool','number','any')][string]$Type)
    if ($null -eq $Actual -or $null -eq $Expected) { return 'UNKNOWN' }
    switch ($Type) {
        'string' { if ($Actual -isnot [string]) { return $true }; return (-not ([string]$Actual).Equals([string]$Expected, [StringComparison]::Ordinal)) }
        'bool' { if ($Actual -isnot [bool] -or $Expected -isnot [bool]) { return $true }; return ([bool]$Actual -ne [bool]$Expected) }
        'number' { if ($Actual -isnot [byte] -and $Actual -isnot [int16] -and $Actual -isnot [int32] -and $Actual -isnot [int64] -and $Actual -isnot [decimal] -and $Actual -isnot [double]) { return $true }; return ([decimal]$Actual -ne [decimal]$Expected) }
        default { return ($Actual -ne $Expected) }
    }
}

function Add-V16FieldCondition {
    param([System.Collections.Generic.List[object]]$List, [object]$Object, [string]$Property, [object]$Expected, [string]$Type, [string]$SourceHash)
    $actual = Get-V16Field $Object $Property
    $rejected = Test-V16TypedEqual $actual $Expected $Type
    $List.Add((New-V16Condition $Property $actual $Expected $rejected $SourceHash))
}

function Test-V16CanonicalPathEqual {
    param([AllowNull()][object]$Left, [AllowNull()][object]$Right)
    if ($Left -isnot [string] -or $Right -isnot [string]) { return $false }
    try {
        return [IO.Path]::GetFullPath([string]$Left).Equals(
            [IO.Path]::GetFullPath([string]$Right),
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch { return $false }
}

function Get-V16ValidDeferredState {
    param([AllowNull()][object]$Preflight, [AllowNull()][object]$Schedule)
    if ($null -eq $Preflight -or $null -eq $Schedule) { return 'UNKNOWN' }
    $state = Get-V16Field $Preflight 'state'
    $reason = Get-V16Field $Preflight 'reason'
    $opensAt = Get-V16Field $Preflight 'opens_at'
    $retcode = Get-V16Field $Preflight 'retcode'
    $weekend = Get-V16Field $Schedule 'is_weekend'
    $beforeWindow = Get-V16Field $Schedule 'before_preflight_window'
    $closed = Get-V16Field $Schedule 'scheduled_closed'
    if ($weekend -isnot [bool] -or $beforeWindow -isnot [bool] -or $closed -isnot [bool]) { return 'UNKNOWN' }
    $valid = (
        ([string]$state -ceq 'NON_TRADING_DAY' -and [string]$reason -ceq 'WEEKEND' -and [bool]$weekend -and [bool]$closed) -or
        ([string]$state -ceq 'WAITING_PREFLIGHT_WINDOW' -and [string]$opensAt -ceq '09:30' -and [bool]$beforeWindow) -or
        ([string]$state -ceq 'WAITING_ORDER_CHECK' -and $retcode -is [ValueType] -and [int]$retcode -eq 10018 -and [bool]$closed)
    )
    return [bool]$valid
}

function Split-V16JsonDocuments {
    param([Parameter(Mandatory = $true)][string]$Text)
    $docs = [System.Collections.Generic.List[object]]::new()
    $start = -1; $depth = 0; $inString = $false; $escaped = $false
    for ($i = 0; $i -lt $Text.Length; $i++) {
        $c = $Text[$i]
        if ($start -lt 0) {
            if ([char]::IsWhiteSpace($c)) { continue }
            if ($c -ne '{') { throw "JSON_DOCUMENT_START_INVALID:$i" }
            $start = $i; $depth = 1; continue
        }
        if ($inString) {
            if ($escaped) { $escaped = $false; continue }
            if ($c -eq [char]0x5C) { $escaped = $true; continue }
            if ($c -eq '"') { $inString = $false }
            continue
        }
        if ($c -eq '"') { $inString = $true; continue }
        if ($c -eq '{') { $depth++ }
        elseif ($c -eq '}') { $depth-- }
        if ($depth -lt 0) { throw 'JSON_DOCUMENT_DEPTH_INVALID' }
        if ($depth -eq 0) {
            $raw = $Text.Substring($start, $i - $start + 1)
            try { $docs.Add(($raw | ConvertFrom-Json -ErrorAction Stop)) } catch { throw "JSON_DOCUMENT_INVALID:$($_.Exception.Message)" }
            $start = -1
        }
    }
    if ($start -ge 0 -or $inString -or $depth -ne 0) { throw 'JSON_DOCUMENT_TRUNCATED' }
    return $docs.ToArray()
}

function Test-V16FlatOutput {
    param([Parameter(Mandatory = $true)][string]$StdoutText, [Parameter(Mandatory = $true)][hashtable]$Expected, [string]$SourceFileSha256 = 'UNKNOWN')
    $doc = Convert-V16ExactJsonDocument $StdoutText
    $conditions = [System.Collections.Generic.List[object]]::new()
    foreach ($p in @(
        @{n='state';e='READY_FLAT_SEALED';t='string'},
        @{n='readiness_evidence';e=$null;t='string'}, @{n='readiness_sha256';e=$null;t='string'},
        @{n='evidence_transaction';e=$null;t='string'}, @{n='checked_at_utc';e=$null;t='string'},
        @{n='request_sha256';e=$null;t='string'}, @{n='producer_sha256';e=$null;t='string'},
        @{n='producer_process_id';e=$null;t='number'}, @{n='account_login';e=$Expected.account_login;t='number'},
        @{n='server';e=$Expected.server;t='string'}, @{n='company';e=$Expected.company;t='string'},
        @{n='terminal_path';e=$Expected.terminal_path;t='string'}, @{n='terminal_data_path';e=$Expected.terminal_data_path;t='string'},
        @{n='open_orders';e=0;t='number'}, @{n='open_positions';e=0;t='number'}, @{n='task_xml_unchanged';e=$true;t='bool'}
    )) {
        if ($null -eq $p.e) {
            $actual = Get-V16Field $doc $p.n
            $rejected = if ($null -eq $actual) {'UNKNOWN'} elseif ($p.n -match 'sha256$' -and ([string]$actual -notmatch '^[a-fA-F0-9]{64}$')) {$true} elseif ($actual -isnot [string] -and $p.t -eq 'string') {$true} else {$false}
            $conditions.Add((New-V16Condition $p.n $actual 'present/valid' $rejected $SourceFileSha256))
        } else { Add-V16FieldCondition $conditions $doc $p.n $p.e $p.t $SourceFileSha256 }
    }
    $failed = @($conditions | Where-Object { $_.rejected -eq $true })
    $unknown = @($conditions | Where-Object { $_.rejected -eq 'UNKNOWN' })
    [pscustomobject]@{operation='flat';document=$doc;conditions=$conditions.ToArray();failed_conditions=$failed;unknown_conditions=$unknown;operation_accepted=($failed.Count -eq 0 -and $unknown.Count -eq 0);status=if($failed.Count -eq 0 -and $unknown.Count -eq 0){'PASS'}elseif($failed.Count -gt 0){'FAIL'}else{'UNKNOWN'} }
}

function Get-V16ReadinessConditions {
    param([Parameter(Mandatory = $true)][object]$Flat, [Parameter(Mandatory = $true)][hashtable]$Expected, [Parameter(Mandatory = $true)][DateTimeOffset]$Now, [AllowNull()][object]$Producer, [string]$SourceFileSha256='UNKNOWN', [AllowNull()][object]$TerminalPin, [AllowNull()][object]$MarketScheduleState, [string]$ExpectedEvidenceRoot='')
    $c = [System.Collections.Generic.List[object]]::new()
    foreach ($p in @(
        @{id='profile';path=@('profile');exp='super1';type='string'}, @{id='ready';path=@('ready');exp=$true;type='bool'},
        @{id='flat';path=@('flat');exp=$true;type='bool'}, @{id='identity_ready';path=@('identity_ready');exp=$true;type='bool'},
        @{id='transport_ready';path=@('transport_ready');exp=$true;type='bool'}, @{id='permission_state';path=@('permission','state');exp='READY';type='string'},
        @{id='login';path=@('account_login');exp=$Expected.account_login;type='number'}, @{id='server';path=@('server');exp=$Expected.server;type='string'},
        @{id='company';path=@('company');exp=$Expected.company;type='string'}, @{id='open_orders';path=@('open_orders');exp=0;type='number'},
        @{id='open_positions';path=@('open_positions');exp=0;type='number'},
        @{id='order_send_called';path=@('order_transport_preflight','order_send_called');exp=$false;type='bool'}
    )) {
        $actual = Get-V16NestedField $Flat $p.path
        $rejected = Test-V16TypedEqual $actual $p.exp $p.type
        $c.Add((New-V16Condition $p.id $actual $p.exp $rejected $SourceFileSha256))
    }
    $windowsProfile = Get-V16NestedField $Flat @('identity_checks','windows_profile')
    $windowsProfileRejected = if($null -eq $windowsProfile){$true}else{Test-V16TypedEqual $windowsProfile $true 'bool'}
    $c.Add((New-V16Condition 'identity_windows_profile' $windowsProfile $true $windowsProfileRejected $SourceFileSha256))
    $nonce=Get-V16Field $Flat 'evidence_nonce'; $c.Add((New-V16Condition 'evidence_nonce' $nonce '32 lowercase hex' $(if($null -eq $nonce){'UNKNOWN'}else{([string]$nonce -cnotmatch '^[a-f0-9]{32}$')}) $SourceFileSha256))
    foreach($market in @('nq','spx')) { $v=Get-V16NestedField $Flat @('markets',$market,'type'); $c.Add((New-V16Condition "market_${market}_type" $v 'INDICES' $(if($null -eq $v){'UNKNOWN'}else{[string]$v -cne 'INDICES'}) $SourceFileSha256)) }
    foreach ($group in @('identity_checks','permission_checks')) {
        $obj = Get-V16Field $Flat $group
        $props = if ($null -eq $obj) {@()} else {@($obj.PSObject.Properties)}
        $bad = @($props | Where-Object { $_.Value -isnot [bool] -or -not [bool]$_.Value })
        $c.Add((New-V16Condition "${group}_count" $props.Count 5 ($props.Count -lt 5) $SourceFileSha256))
        $c.Add((New-V16Condition "${group}_all_pass" $(if($props.Count -eq 0){$null}else{$bad.Count -eq 0}) $true $(if($props.Count -eq 0){'UNKNOWN'}else{$bad.Count -ne 0}) $SourceFileSha256))
    }
    $preflight = Get-V16Field $Flat 'order_transport_preflight'
    $checks = if($null -eq $preflight){@()}elseif(Test-V16HasProperty $preflight 'checks'){@($preflight.checks)}else{@()}
    $preflightPass = if($null -eq $preflight){$null}else{(Get-V16Field $preflight 'state') -is [string] -and [string](Get-V16Field $preflight 'state') -ceq 'PASS' -and $checks.Count -ge 4}
    $deferred=Get-V16Field $Flat 'transport_preflight_deferred'
    $validDeferred = Get-V16ValidDeferredState -Preflight $preflight -Schedule $MarketScheduleState
    $preflightRejected=if($null -eq $preflight -or $deferred -isnot [bool]){'UNKNOWN'}elseif($deferred -eq $false){$preflightPass -ne $true}elseif($validDeferred -eq 'UNKNOWN'){'UNKNOWN'}else{-not [bool]$validDeferred}
    $c.Add((New-V16Condition 'preflight_pass_or_valid_deferred' @{preflight_pass=$preflightPass;deferred=$deferred;valid_deferred=$validDeferred;schedule=$MarketScheduleState} 'PASS or one signed deferred state' $preflightRejected $SourceFileSha256))
    $checked = Get-V16Field $Flat 'checked_at_utc'; $age = $null
    try { if($checked -is [string]){$age=($Now-[DateTimeOffset]::Parse($checked).ToUniversalTime()).TotalSeconds} } catch { $age=$null }
    $ageRejected = if($null -eq $age){'UNKNOWN'}else{$age -lt -5 -or $age -gt 90}
    $c.Add((New-V16Condition 'broker_age_seconds' $age '[-5,90]' $ageRejected $SourceFileSha256))
    $producerAge = $null
    try { if($Producer -and (Get-V16Field $Producer 'produced_at_utc') -is [string]){$producerAge=($Now-[DateTimeOffset]::Parse([string](Get-V16Field $Producer 'produced_at_utc')).ToUniversalTime()).TotalSeconds} } catch { $producerAge=$null }
    $c.Add((New-V16Condition 'producer_age_seconds' $producerAge '[-5,90]' $(if($null -eq $producerAge){'UNKNOWN'}else{$producerAge -lt -5 -or $producerAge -gt 90}) $SourceFileSha256))
    $pinPath = if($TerminalPin){Get-V16Field $TerminalPin 'terminal_path'}else{$null}
    $terminalPath = Get-V16Field $Flat 'terminal_path'; $terminalData = Get-V16Field $Flat 'terminal_data_path'
    $c.Add((New-V16Condition 'terminal_pin' $terminalPath $pinPath $(if($null -eq $pinPath -or $null -eq $terminalPath){'UNKNOWN'}else{-not (Test-V16CanonicalPathEqual $terminalPath $pinPath)}) $SourceFileSha256))
    $c.Add((New-V16Condition 'terminal_data_pin' $terminalData $(if($pinPath){Split-Path -Parent ([string]$pinPath)}else{$null}) $(if($null -eq $pinPath -or $null -eq $terminalData){'UNKNOWN'}else{-not (Test-V16CanonicalPathEqual $terminalData (Split-Path -Parent ([string]$pinPath)))}) $SourceFileSha256))
    $evidenceRoot = Get-V16Field $Flat 'evidence_root'
    $evidenceRejected = if($evidenceRoot -isnot [string] -or [string]::IsNullOrWhiteSpace($ExpectedEvidenceRoot)){'UNKNOWN'}else{-not ([IO.Path]::GetFullPath([string]$evidenceRoot)).StartsWith(([IO.Path]::GetFullPath($ExpectedEvidenceRoot)+[IO.Path]::DirectorySeparatorChar),[StringComparison]::OrdinalIgnoreCase)}
    $c.Add((New-V16Condition 'evidence_root' $evidenceRoot $ExpectedEvidenceRoot $evidenceRejected $SourceFileSha256))
    $failed=@($c|Where-Object {$_.rejected -eq $true}); $unknown=@($c|Where-Object {$_.rejected -eq 'UNKNOWN'})
    [pscustomobject]@{operation='sealed_readiness';conditions=$c.ToArray();failed_conditions=$failed;unknown_conditions=$unknown;operation_accepted=($failed.Count -eq 0 -and $unknown.Count -eq 0);broker_age_seconds=$age;producer_age_seconds=$producerAge;status=if($failed.Count -eq 0 -and $unknown.Count -eq 0){'PASS'}elseif($failed.Count -gt 0){'FAIL'}else{'UNKNOWN'}}
}

function Test-V16RolloverOutput {
    param([Parameter(Mandatory = $true)][string]$StdoutText, [Parameter(Mandatory = $true)][string]$ReadinessPath, [Parameter(Mandatory = $true)][string]$ReadinessSha256, [string]$SourceFileSha256='UNKNOWN', [string]$MainTask='Super1XM')
    $docs = @(Split-V16JsonDocuments $StdoutText)
    if ($docs.Count -ne 3) { throw "ROLLOVER_DOCUMENT_COUNT:$($docs.Count)" }
    $summary=$docs[0]; $health=$docs[1]; $watchdog=$docs[2]; $c=[System.Collections.Generic.List[object]]::new()
    foreach($x in @(
        @{id='external_readiness_path';a=Get-V16Field $summary 'readiness_evidence';e=$ReadinessPath;t='string'},
        @{id='external_readiness_sha256';a=Get-V16Field $summary 'readiness_sha256';e=$ReadinessSha256;t='string'},
        @{id='internal_readiness_path';a=Get-V16Field $summary 'internal_readiness_evidence';e=$null;t='string'},
        @{id='internal_readiness_sha256';a=Get-V16Field $summary 'internal_readiness_sha256';e=$null;t='string'},
        @{id='initialization_evidence';a=Get-V16Field $summary 'initialization_evidence';e=$null;t='string'},
        @{id='initialization_sha256';a=Get-V16Field $summary 'initialization_sha256';e=$null;t='string'},
        @{id='task_xml_unchanged';a=Get-V16Field $summary 'task_xml_unchanged';e=$true;t='bool'},
        @{id='health_state';a=Get-V16Field $health 'state';e='RUNNING';t='string'},
        @{id='watchdog_state';a=Get-V16Field $watchdog 'state';e='HEALTHY';t='string'},
        @{id='watchdog_main_task';a=Get-V16Field $watchdog 'main_task';e=$MainTask;t='string'}
    )) { $rej=if($null -eq $x.e){if($null -eq $x.a){'UNKNOWN'}else{$false}}else{Test-V16TypedEqual $x.a $x.e $x.t}; $c.Add((New-V16Condition $x.id $x.a $(if($null -eq $x.e){'present'}else{$x.e}) $rej $SourceFileSha256)) }
    $failed=@($c|Where-Object {$_.rejected -eq $true});$unknown=@($c|Where-Object {$_.rejected -eq 'UNKNOWN'})
    [pscustomobject]@{operation='rollover';documents=$docs;summary=$summary;health=$health;watchdog=$watchdog;conditions=$c.ToArray();failed_conditions=$failed;unknown_conditions=$unknown;operation_accepted=($failed.Count -eq 0 -and $unknown.Count -eq 0);status=if($failed.Count -eq 0 -and $unknown.Count -eq 0){'PASS'}elseif($failed.Count -gt 0){'FAIL'}else{'UNKNOWN'}}
}

function Test-V16AgeWindow {
    param([Parameter(Mandatory = $true)][double]$AgeSeconds)
    [pscustomobject]@{age_seconds=$AgeSeconds;expected='[-5,90]';accepted=($AgeSeconds -ge -5 -and $AgeSeconds -le 90)}
}
