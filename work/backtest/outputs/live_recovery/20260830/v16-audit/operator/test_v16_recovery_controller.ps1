$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$here=Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here 'v16_operator_guard.ps1')
. (Join-Path $here 'v16_child_executor.ps1')
. (Join-Path $here 'v16_operation_adapters.ps1')
. (Join-Path $here 'v16_recovery_controller.ps1')
$script:Assertions=0
function A($Value,[string]$Name){$script:Assertions++;if(-not $Value){throw "ASSERT:$Name"}}
function E($Actual,$Expected,[string]$Name){$script:Assertions++;if($Actual -ne $Expected){throw "ASSERT:$Name expected=$Expected actual=$Actual"}}
function Throws([scriptblock]$Action,[string]$Pattern,[string]$Name){$script:Assertions++;$thrown=$false;$message='';try{&$Action}catch{$thrown=$true;$message=$_.Exception.Message};if(-not $thrown -or $message -notlike $Pattern){throw "ASSERT:$Name message=$message"}}

$controllerSource=[IO.File]::ReadAllText((Join-Path $here 'v16_recovery_controller.ps1'))
A ($controllerSource.Contains('. $SecureHelper')) 'signed_helper_loaded_in_controller_scope'
A ($controllerSource.Contains("run_super1_windows.ps1") -and $controllerSource.Contains('ExpectedLauncherSha256 $expectedLauncherSha')) 'flat_producer_launcher_contract'
A (-not $controllerSource.Contains("ExpectedLauncherPath (Join-Path `$App 'deploy\check_super1_flat_windows.ps1')")) 'flat_producer_not_check_launcher'
A (-not $controllerSource.Contains('ExpectedLauncherSha256 ([string]$request.expected_launcher_sha256)')) 'flat_producer_hash_not_request_claim'

# Generic mappings are exact and execute has no DeleteChild bit.
E (Convert-V16GenericMask -UnsignedMask ([int64]-2147483648)) ([uint64]1179785) 'GENERIC_READ'
E (Convert-V16GenericMask -UnsignedMask ([int64]1073741824)) ([uint64]1179926) 'GENERIC_WRITE'
E (Convert-V16GenericMask -UnsignedMask ([int64]536870912)) ([uint64]1179808) 'GENERIC_EXECUTE'
E (Convert-V16GenericMask -UnsignedMask ([int64]268435456)) ([uint64]2032127) 'GENERIC_ALL'
A (((Convert-V16GenericMask -UnsignedMask ([int64]536870912)) -band [uint64]0x40) -eq 0) 'GENERIC_EXECUTE_NO_DELETE_CHILD'
A (((Convert-V16GenericMask -UnsignedMask ([int64]536870912)) -band [uint64]0x80) -ne 0) 'GENERIC_EXECUTE_READ_ATTRIBUTES'
Throws { Convert-V16UnsignedMask -RawMask ([int64]4294967296) } '*MASK_OUT_OF_RANGE*' 'mask_high_range'
Throws { Convert-V16UnsignedMask -RawMask ([int64]-2147483649) } '*MASK_OUT_OF_RANGE*' 'mask_low_range'

$supportedIo=New-Object System.Security.AccessControl.RawSecurityDescriptor('O:SYG:SYD:P(A;OICIIO;GA;;;BU)(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)')
E (@(Get-V16RawAceReport $supportedIo | Where-Object decision -eq 'IGNORED_INHERIT_ONLY').Count) 1 'supported_inherit_only'
$cbSid=New-Object -TypeName System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-545';$sysSid=New-Object -TypeName System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-18';$cbFlags=[System.Security.AccessControl.AceFlags]([int]11);$cbArgs=@($cbFlags,[System.Security.AccessControl.AceQualifier]::AccessAllowed,[int]-1,$cbSid,$true,[byte[]](1,2,3,4));$cbAce=New-Object -TypeName System.Security.AccessControl.CommonAce -ArgumentList $cbArgs;$cbDacl=New-Object -TypeName System.Security.AccessControl.RawAcl -ArgumentList @(2,1);$cbDacl.InsertAce(0,$cbAce);$unsupportedIo=New-Object -TypeName System.Security.AccessControl.RawSecurityDescriptor -ArgumentList ([System.Security.AccessControl.ControlFlags]::SelfRelative),$sysSid,$sysSid,$null,$cbDacl
E (@(Get-V16RawAceReport $unsupportedIo | Where-Object decision -eq 'FAIL').Count) 1 'unsupported_callback_inherit_only'
$inheritedBad=New-Object System.Security.AccessControl.RawSecurityDescriptor('O:SYG:SYD:P(A;OICIID;GA;;;BU)(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)')
E (@(Get-V16RawAceReport $inheritedBad | Where-Object decision -eq 'FAIL').Count) 1 'effective_inherited_bad'
$thirdAce=New-Object System.Security.AccessControl.RawSecurityDescriptor('O:SYG:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIIO;FA;;;BU)')
Throws { Assert-V16AuditRootDescriptor $thirdAce } '*AUDIT_ROOT_RAW*' 'audit_third_ace'
$nullDacl=New-Object System.Security.AccessControl.RawSecurityDescriptor('O:SYG:SY')
Throws { Get-V16RawAceReport $nullDacl } '*NULL_DACL*' 'null_dacl'
$emptyDacl=New-Object System.Security.AccessControl.RawSecurityDescriptor('O:SYG:SYD:P')
E ((Get-V16RawAceReport $emptyDacl)[0].decision) 'EMPTY_DACL_NO_CREATE_ACCESS' 'empty_dacl'
$badOwner=New-Object System.Security.AccessControl.RawSecurityDescriptor('O:BU G:SY D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'.Replace(' ',''))
Throws { Assert-V16AuditRootDescriptor $badOwner } '*AUDIT_ROOT_OWNER_INVALID*' 'bad_owner'

$hashA=('a'*64);$expected=@{account_login=1301910045;server='XMGlobal-MT5 6';company='XM Global Limited';terminal_path='C:\Super1\mt5-clean5833\terminal64.exe';terminal_data_path='C:\Super1\mt5-clean5833'}
$flat=[ordered]@{state='READY_FLAT_SEALED';readiness_evidence='C:\Super1\archive\readiness-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\output\result.json';readiness_sha256=$hashA;evidence_transaction='C:\Super1\archive\readiness-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';checked_at_utc='2026-08-30T17:37:02.209570+00:00';account_login=1301910045;server='XMGlobal-MT5 6';company='XM Global Limited';terminal_path=$expected.terminal_path;terminal_data_path=$expected.terminal_data_path;open_orders=0;open_positions=0;task_xml_unchanged=$true;request_sha256=$hashA;producer_sha256=$hashA;producer_process_id=364;profile='super1';ready=$true;flat=$true;identity_ready=$true;transport_ready=$true;identity_checks=[ordered]@{windows_profile=$true;a=$true;b=$true;c=$true;d=$true};permission_checks=[ordered]@{a=$true;b=$true;c=$true;d=$true;e=$true};permission=[ordered]@{state='READY'};order_transport_preflight=[ordered]@{state='PASS';checks=@('a','b','c','d');order_send_called=$false};transport_preflight_deferred=$false;evidence_nonce='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';markets=[ordered]@{nq=[ordered]@{type='INDICES'};spx=[ordered]@{type='INDICES'}};evidence_root='C:\Super1\archive\readiness-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\output\evidence'}
$flatText=$flat|ConvertTo-Json -Depth 20
$flatCheck=Test-V16FlatOutput -StdoutText ("`n$flatText`n") -Expected $expected -SourceFileSha256 $hashA
A $flatCheck.operation_accepted 'multiline_flat_fixture'
$flatDoc=$flatText|ConvertFrom-Json
$readiness=Get-V16ReadinessConditions -Flat $flatDoc -Expected $expected -Now ([DateTimeOffset]::Parse('2026-08-30T17:37:20Z')) -Producer ([pscustomobject]@{produced_at_utc='2026-08-30T17:37:05Z'}) -SourceFileSha256 $hashA -TerminalPin ([pscustomobject]@{terminal_path=$expected.terminal_path}) -MarketScheduleState ([pscustomobject]@{is_weekend=$false;before_preflight_window=$false;scheduled_closed=$false}) -ExpectedEvidenceRoot 'C:\Super1\archive\readiness-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\output'
if(-not $readiness.operation_accepted){$readiness.conditions|ConvertTo-Json -Depth 8;throw 'ASSERT:readiness_conditions'}
A $readiness.operation_accepted 'readiness_conditions'
E (@($readiness.conditions).Count -ge 20) $true 'readiness_condition_count'
$badDeferred=$flatDoc|ConvertTo-Json -Depth 20|ConvertFrom-Json;$badDeferred.order_transport_preflight=[pscustomobject]@{state='WAITING_PREFLIGHT_WINDOW';opens_at='08:30';checks=@()};$badDeferred.transport_preflight_deferred=$true
$badDeferredCheck=Get-V16ReadinessConditions -Flat $badDeferred -Expected $expected -Now ([DateTimeOffset]::Parse('2026-08-30T17:37:20Z')) -Producer ([pscustomobject]@{produced_at_utc='2026-08-30T17:37:05Z'}) -SourceFileSha256 $hashA -TerminalPin ([pscustomobject]@{terminal_path=$expected.terminal_path}) -MarketScheduleState ([pscustomobject]@{is_weekend=$false;before_preflight_window=$false;scheduled_closed=$false}) -ExpectedEvidenceRoot 'C:\Super1\archive\readiness-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\output'
A (@($badDeferredCheck.failed_conditions|Where-Object condition_id -eq 'preflight_pass_or_valid_deferred').Count -eq 1) 'invalid_deferred_rejected'
$missingProfile=$flatDoc|ConvertTo-Json -Depth 20|ConvertFrom-Json;$missingProfile.identity_checks.PSObject.Properties.Remove('windows_profile')
$missingProfileCheck=Get-V16ReadinessConditions -Flat $missingProfile -Expected $expected -Now ([DateTimeOffset]::Parse('2026-08-30T17:37:20Z')) -Producer ([pscustomobject]@{produced_at_utc='2026-08-30T17:37:05Z'}) -SourceFileSha256 $hashA -TerminalPin ([pscustomobject]@{terminal_path=$expected.terminal_path}) -MarketScheduleState ([pscustomobject]@{is_weekend=$false;before_preflight_window=$false;scheduled_closed=$false}) -ExpectedEvidenceRoot 'C:\Super1\archive\readiness-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\output'
A (@($missingProfileCheck.failed_conditions|Where-Object condition_id -eq 'identity_windows_profile').Count -eq 1) 'missing_windows_profile_rejected'

$summary=[ordered]@{readiness_evidence=$flat.readiness_evidence;readiness_sha256=$hashA;internal_readiness_evidence='C:\Super1\archive\readiness-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\output\result.json';internal_readiness_sha256=('b'*64);initialization_evidence='C:\Super1\archive\readiness-cccccccccccccccccccccccccccccccc\output\result.json';initialization_sha256=('c'*64);task_xml_unchanged=$true}
$health=[ordered]@{state='RUNNING'};$watchdog=[ordered]@{state='HEALTHY';main_task='Super1XM'}
$rollText=(($summary|ConvertTo-Json -Compress)+"`n"+($health|ConvertTo-Json -Compress)+"`n"+($watchdog|ConvertTo-Json -Compress))
$roll=Test-V16RolloverOutput -StdoutText $rollText -ReadinessPath $flat.readiness_evidence -ReadinessSha256 $hashA -SourceFileSha256 $hashA
A $roll.operation_accepted 'three_document_rollover'
Throws { Test-V16RolloverOutput -StdoutText (($summary|ConvertTo-Json -Compress)+"`n"+($health|ConvertTo-Json -Compress)) -ReadinessPath $flat.readiness_evidence -ReadinessSha256 $hashA } '*ROLLOVER_DOCUMENT_COUNT*' 'rollover_missing_doc'
Throws { Split-V16JsonDocuments '{"x":"unterminated}' } '*JSON_DOCUMENT_TRUNCATED*' 'rollover_truncated'
A ((Split-V16JsonDocuments '{"x":"C:\\temp\\{nested}"}{"x":"escaped \"quote\""}{"x":"done"}').Count -eq 3) 'escaped_backslash_quote_json'

$tmp=Join-Path ([IO.Path]::GetTempPath()) ('v16-recovery-tests-'+[Guid]::NewGuid().ToString('N'));New-Item -ItemType Directory -Path $tmp|Out-Null
try {
    $inner=Join-Path $tmp 'natural.ps1';[IO.File]::WriteAllText($inner,"Write-Output 'NATURAL_RETURN'",(New-Object Text.UTF8Encoding($false)))
    $natural=Invoke-V16Child -ScriptBody "& '$($inner.Replace("'","''"))'" -LogDirectory $tmp -RunId 'natural' -TimeoutSeconds 30
    A $natural.process_succeeded 'natural_ps1_return';A $natural.capture_complete 'natural_capture'
    $failJson=Invoke-V16Child -ScriptBody 'Write-Output ''{"state":"FAIL"}''' -LogDirectory $tmp -RunId 'fail-json' -TimeoutSeconds 30
    A $failJson.process_succeeded 'fail_json_process_success';A (-not (Test-V16FlatOutput -StdoutText ([IO.File]::ReadAllText($failJson.stdout)) -Expected $expected).operation_accepted) 'fail_json_operation_rejected'
    foreach($bad in @('', '{', '{"x":1}x')) { Throws { Test-V16FlatOutput -StdoutText $bad -Expected $expected } '*' ('bad_json_'+$bad.Length) }
    $partial=Invoke-V16Child -ScriptBody "Write-Output 'partial'; [Console]::Error.WriteLine('err-partial'); throw 'synthetic-first'" -LogDirectory $tmp -RunId 'partial' -TimeoutSeconds 30
    A (-not $partial.process_succeeded) 'partial_exception_native_failure';A $partial.capture_complete 'partial_exception_capture';A (([IO.File]::ReadAllText($partial.stderr)).Contains('synthetic-first')) 'partial_exception_error'
    $large=Invoke-V16Child -ScriptBody "`$x='x'*1048576;[Console]::Out.Write(`$x);[Console]::Error.Write(`$x)" -LogDirectory $tmp -RunId 'large' -TimeoutSeconds 30
    $expectedLarge=([Security.Cryptography.SHA256]::Create());try{$largeBytes=[Text.Encoding]::ASCII.GetBytes('x'*1048576);$largeHash=([BitConverter]::ToString($expectedLarge.ComputeHash($largeBytes))).Replace('-','').ToLowerInvariant()}finally{$expectedLarge.Dispose()}
    A $large.capture_complete 'large_capture';E ((Get-FileHash -LiteralPath $large.stdout -Algorithm SHA256).Hash.ToLowerInvariant()) $largeHash 'large_stdout_hash';E ((Get-FileHash -LiteralPath $large.stderr -Algorithm SHA256).Hash.ToLowerInvariant()) $largeHash 'large_stderr_hash'
    $pidFile=Join-Path $tmp 'timeout.pid';$timeout=Invoke-V16Child -ScriptBody "[IO.File]::WriteAllText('$($pidFile.Replace("'","''"))',[string][Diagnostics.Process]::GetCurrentProcess().Id);Start-Sleep -Seconds 30" -LogDirectory $tmp -RunId 'timeout' -TimeoutSeconds 1
    A (-not $timeout.capture_complete) 'timeout_not_capture';$syntheticPid=[int](Get-Content $pidFile -Raw);Stop-Process -Id $syntheticPid -Force -ErrorAction Stop;Start-Sleep -Milliseconds 300;$alive=$true;try{Get-Process -Id $syntheticPid -ErrorAction Stop|Out-Null}catch{$alive=$false};A (-not $alive) 'timeout_child_ended'
    foreach($line in @(Get-Content -LiteralPath (Join-Path $tmp 'events.jsonl'))){[void]($line|ConvertFrom-Json)};A $true 'events_jsonl_roundtrip'
} finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }

$log=[System.Collections.Generic.List[string]]::new();$fakeFlat=[pscustomobject]@{capture_complete=$true;process_succeeded=$true};$fakeAdapter=[pscustomobject]@{operation_accepted=$true};$fakeProof=[pscustomobject]@{status='PASS'};$fakeGate=[pscustomobject]@{status='PASS'};$fakeRoll=[pscustomobject]@{child_exited=$true;operation_accepted=$true};$fakePost=[pscustomobject]@{operation_accepted=$true};
$flow=Invoke-V16RecoveryStateMachine -Preflight { $log.Add('pre');[pscustomobject]@{status='PASS'} } -Flat { $log.Add('flat');$fakeFlat } -FlatAdapter { $log.Add('adapter');$fakeAdapter } -ProofCopy { $log.Add('copy');$fakeProof } -ReadinessGate { $log.Add('gate');$fakeGate } -Rollover { $log.Add('roll');$fakeRoll } -StopAssert { $log.Add('stop');[pscustomobject]@{status='PASS'} } -PostFlat { $log.Add('post');$fakePost }
E $flow.stage 'SUCCESS' 'success_flow';E $flow.counts.flat_calls 1 'success_flat_count';E $flow.counts.rollover_calls 1 'success_rollover_count';E $flow.counts.post_flat_calls 1 'success_post_count';E (@($log|Where-Object {$_ -eq 'roll'}).Count) 1 'success_roll_once'
$amb=Invoke-V16RecoveryStateMachine -Preflight { [pscustomobject]@{status='PASS'} } -Flat { $fakeFlat } -FlatAdapter { $fakeAdapter } -ProofCopy { $fakeProof } -ReadinessGate { $fakeGate } -Rollover { [pscustomobject]@{child_exited=$false;operation_accepted=$false} } -StopAssert { throw 'must-not-stop-active-child' } -PostFlat { throw 'must-not-post' }
E $amb.stage 'FAILED' 'ambiguous_failure';E $amb.counts.rollover_calls 1 'ambiguous_one_call';E $amb.counts.post_flat_calls 0 'ambiguous_no_post';A $amb.active_child 'ambiguous_active_child'
$copyFail=Invoke-V16RecoveryStateMachine -Preflight { [pscustomobject]@{status='PASS'} } -Flat { $fakeFlat } -FlatAdapter { $fakeAdapter } -ProofCopy { [pscustomobject]@{status='FAIL'} } -ReadinessGate { throw 'must-not-gate' } -Rollover { throw 'must-not-roll' } -StopAssert { throw 'must-not-stop' } -PostFlat { throw 'must-not-post' }
E $copyFail.counts.rollover_calls 0 'copy_failure_no_rollover';E $copyFail.counts.post_flat_calls 0 'copy_failure_no_post'
$script:proofFailureStops=0;$proofFailure=Invoke-V16RecoveryStateMachine -Preflight {[pscustomobject]@{status='PASS'}} -Flat{[pscustomobject]@{capture_complete=$true;process_succeeded=$true;exit_observed=$true}} -FlatAdapter{$fakeAdapter} -ProofCopy{[pscustomobject]@{status='FAIL'}} -ReadinessGate{throw 'must-not-gate'} -Rollover{throw 'must-not-roll'} -StopAssert{$script:proofFailureStops++;[pscustomobject]@{status='PASS'}} -PostFlat{throw 'must-not-post'}
E $script:proofFailureStops 1 'proof_failure_auto_stop';A $proofFailure.automatic_shutdown_verified 'proof_failure_auto_stop_verified';A $proofFailure.final_runtime_stopped 'proof_failure_final_stopped'
$script:stopCalls=0;$rollParseFail=Invoke-V16RecoveryStateMachine -Preflight {[pscustomobject]@{status='PASS'}} -Flat{$fakeFlat} -FlatAdapter{$fakeAdapter} -ProofCopy{$fakeProof} -ReadinessGate{$fakeGate} -Rollover{[pscustomobject]@{child_exited=$true;operation_accepted=$false;adapter_error='JSON_PARSE'}} -StopAssert{$script:stopCalls++;[pscustomobject]@{status='PASS'}} -PostFlat{throw 'must-not-post'}
E $rollParseFail.counts.rollover_calls 1 'rollover_parse_call_preserved';E $script:stopCalls 1 'rollover_parse_stop_once';A $rollParseFail.final_runtime_stopped 'rollover_parse_final_stop'
$script:stopFailCalls=0;$stopFail=Invoke-V16RecoveryStateMachine -Preflight {[pscustomobject]@{status='PASS'}} -Flat{$fakeFlat} -FlatAdapter{$fakeAdapter} -ProofCopy{$fakeProof} -ReadinessGate{$fakeGate} -Rollover{$fakeRoll} -StopAssert{$script:stopFailCalls++;[pscustomobject]@{status='FAIL'}} -PostFlat{throw 'must-not-post'}
E $script:stopFailCalls 1 'stop_fail_no_retry';A (-not $stopFail.final_runtime_stopped) 'stop_fail_not_stopped'
$postActive=Invoke-V16RecoveryStateMachine -Preflight {[pscustomobject]@{status='PASS'}} -Flat{$fakeFlat} -FlatAdapter{$fakeAdapter} -ProofCopy{$fakeProof} -ReadinessGate{$fakeGate} -Rollover{$fakeRoll} -StopAssert{[pscustomobject]@{status='PASS'}} -PostFlat{[pscustomobject]@{operation_accepted=$false;active_child=$true}}
A $postActive.active_child 'postflat_active_reported';A (-not $postActive.final_runtime_stopped) 'postflat_active_not_stopped'
foreach($age in @(-5,90)){A (Test-V16AgeWindow $age).accepted ("age_boundary_$age")};foreach($age in @(-5.001,90.001)){A (-not (Test-V16AgeWindow $age).accepted) ("age_outside_$age")}

$txTmp=Join-Path ([IO.Path]::GetTempPath()) ('v16-tx-'+[Guid]::NewGuid().ToString('N'));$txPath=Join-Path $txTmp 'archive\readiness-0123456789abcdef0123456789abcdef\output';New-Item -ItemType Directory -Path $txPath -Force|Out-Null;[IO.File]::WriteAllText((Join-Path $txPath 'result.json'),'{}',(New-Object Text.UTF8Encoding($false)))
try {$txInfo=Get-V16TransactionIdentity -ReadinessPath (Join-Path $txPath 'result.json') -Root $txTmp;E $txInfo.transaction_id '0123456789abcdef0123456789abcdef' 'transaction_guid_exact';Throws {Get-V16TransactionIdentity -ReadinessPath (Join-Path $txPath 'result.json') -Root ($txTmp+'\bad')} '*' 'transaction_archive_boundary'} finally {Remove-Item -LiteralPath $txTmp -Recurse -Force -ErrorAction SilentlyContinue}

$coordTmp=Join-Path ([IO.Path]::GetTempPath()) ('v16-coord-'+[Guid]::NewGuid().ToString('N'));New-Item -ItemType Directory -Path $coordTmp|Out-Null;$coord=$null
try {$coord=New-V16CoordinationLock -AuditRoot $coordTmp -Attempt $coordTmp -RunId 'run-a';$lockBlocked=$false;try{New-V16CoordinationLock -AuditRoot $coordTmp -Attempt $coordTmp -RunId 'run-b'|Out-Null}catch{$lockBlocked=$true};A $lockBlocked 'coordination_exclusive_lock';$coord.lock.Dispose();$coord=$null;Throws {New-V16CoordinationLock -AuditRoot $coordTmp -Attempt $coordTmp -RunId 'run-b'} '*UNRESOLVED_V16_OPERATION_EXISTS*' 'coordination_unresolved_child_guard'} finally {if($coord){$coord.lock.Dispose()};Remove-Item -LiteralPath $coordTmp -Recurse -Force -ErrorAction SilentlyContinue}

Write-Output ("V16_RECOVERY_CONTROLLER_TESTS_PASS assertions=$script:Assertions guard_sha256="+(Get-FileHash (Join-Path $here 'v16_operator_guard.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()+" executor_sha256="+(Get-FileHash (Join-Path $here 'v16_child_executor.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()+" adapters_sha256="+(Get-FileHash (Join-Path $here 'v16_operation_adapters.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()+" controller_sha256="+(Get-FileHash (Join-Path $here 'v16_recovery_controller.ps1') -Algorithm SHA256).Hash.ToLowerInvariant())
