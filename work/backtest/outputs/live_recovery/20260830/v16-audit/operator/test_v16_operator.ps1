$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:AssertionCount = 0
. (Join-Path $here 'v16_operator_guard.ps1')
. (Join-Path $here 'v16_child_executor.ps1')

function Assert-Equal($actual, $expected, [string]$name) {
    $script:AssertionCount++
    if ($actual -ne $expected) { throw "$name expected [$expected], got [$actual]" }
}
function Assert-True($value, [string]$name) { $script:AssertionCount++; if (-not $value) { throw "$name expected true" } }
function Test-GuardSddl([string]$Sddl, [string]$ExpectedStatus, [hashtable]$ExpectedDecisions = @{}) {
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($Sddl)
    $rows = @(Get-V16RawAceReport -Descriptor $raw)
    foreach ($key in $ExpectedDecisions.Keys) {
        $row = $rows | Where-Object { $_.index -eq [int]$key }
        Assert-Equal $row.decision $ExpectedDecisions[$key] "ACE $key decision"
    }
    $fails = @($rows | Where-Object decision -eq 'FAIL').Count
    if ($ExpectedStatus -eq 'PASS') { Assert-Equal $fails 0 'guard failure count' }
    if ($ExpectedStatus -eq 'FAIL') { Assert-True ($fails -gt 0) 'guard failure count' }
}

# Parent guard matrix: InheritOnly is ignored from raw AceFlags, not from PropagationFlags.
Test-GuardSddl 'O:SYG:SYD:P(A;OICIIO;GA;;;CO)(A;OICIIO;0x2;;;BU)(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)' 'PASS' @{0='IGNORED_INHERIT_ONLY';1='IGNORED_INHERIT_ONLY'}
Test-GuardSddl 'O:SYG:SYD:P(A;OICI;0x2;;;BU)' 'FAIL' @{0='FAIL'}
Test-GuardSddl 'O:SYG:SYD:(A;OICI;0x00010000;;;BU)' 'FAIL' @{0='FAIL'}
Test-GuardSddl 'O:SYG:SYD:(A;OICI;0x001200AD;;;BU)' 'PASS'
Test-GuardSddl 'O:SYG:SYD:(A;OICI;0x00010000;;;BU)' 'FAIL'
Test-GuardSddl 'O:SYG:SYD:(A;OICI;0x10000000;;;BU)' 'FAIL'
Test-GuardSddl 'O:SYG:SYD:(A;OICI;0x80000000;;;BU)' 'PASS'
$emptyRows = @(Get-V16RawAceReport -Descriptor (New-Object System.Security.AccessControl.RawSecurityDescriptor('O:SYG:SYD:P')))
Assert-Equal $emptyRows[0].decision 'EMPTY_DACL_NO_CREATE_ACCESS' 'empty DACL decision'
$nullDacl = New-Object System.Security.AccessControl.RawSecurityDescriptor('O:SYG:SY')
try { [void](Get-V16RawAceReport -Descriptor $nullDacl); throw 'null DACL was accepted' } catch { if ($_.Exception.Message -ne 'NULL_DACL') { throw } }
$badOwner = New-Object System.Security.AccessControl.RawSecurityDescriptor('O:BU G:SY D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'.Replace(' ',''))
$badOwnerThrown = $false
try { [void](Assert-V16AuditRootDescriptor -Descriptor $badOwner) } catch { $badOwnerThrown = $true }
Assert-True $badOwnerThrown 'invalid audit owner rejection'

# Audit-root validator is independent: InheritOnly does not make an extra ACE acceptable.
$auditSddl = 'O:SYG:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
Assert-Equal (@(Get-V16RawAceReport -Descriptor (New-Object System.Security.AccessControl.RawSecurityDescriptor($auditSddl))).Count) 2 'audit-root source rows'

# Child capture: exit 0/23 twenty times, plus acceptance/rejection semantics.
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('v16-operator-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    foreach ($code in @(0, 23)) {
        for ($i = 1; $i -le 20; $i++) {
            $r = Invoke-V16Child -ScriptBody "Write-Output ('PASS-$code-$i'); exit $code" -LogDirectory $tmp -RunId "fast-$code-$i" -TimeoutSeconds 30
            Assert-True $r.capture_complete "capture $code/$i"
            Assert-Equal ([int]$r.exit_code) $code "exit $code/$i"
            Assert-Equal $r.process_succeeded ($code -eq 0) "process success $code/$i"
        }
    }
    $passFailJson = Invoke-V16Child -ScriptBody 'Write-Output ''{"state":"PASS"}''; exit 23' -LogDirectory $tmp -RunId 'pass-json-nonzero' -TimeoutSeconds 30
    Assert-Equal ([int]$passFailJson.exit_code) 23 'PASS JSON nonzero'
    Assert-True (-not $passFailJson.process_succeeded) 'PASS JSON nonzero process success'
    $failZero = Invoke-V16Child -ScriptBody 'Write-Output ''{"state":"FAIL"}''; exit 0' -LogDirectory $tmp -RunId 'fail-json-zero' -TimeoutSeconds 30
    Assert-True $failZero.process_succeeded 'executor only reports process success'
    Assert-True ((Get-Content -LiteralPath $failZero.stdout -Raw).Contains('FAIL')) 'FAIL JSON captured'
    $large = Invoke-V16Child -ScriptBody "`$x='x'*1048576; [Console]::Out.Write(`$x); [Console]::Error.Write(`$x); exit 0" -LogDirectory $tmp -RunId 'large-streams' -TimeoutSeconds 30
    Assert-True $large.capture_complete 'large capture'
    Assert-Equal (Get-Item $large.stdout).Length 1048576 'stdout length'
    Assert-Equal (Get-Item $large.stderr).Length 1048576 'stderr length'
    $largeEvents=@(Get-Content -LiteralPath (Join-Path $tmp 'events.jsonl') | ForEach-Object {$_|ConvertFrom-Json} | Where-Object run_id -eq 'large-streams' | ForEach-Object event)
    foreach($requiredEvent in @('launch_intent','native_started','exit_observed','stream_drain_complete','flush_readback_hash','capture_complete','child_end')){Assert-True ($largeEvents -contains $requiredEvent) ("lifecycle_event_"+$requiredEvent)}
    $timeoutPidPath = Join-Path $tmp 'timeout.pid'
    $timeoutBody = "[IO.File]::WriteAllText('$($timeoutPidPath.Replace("'","''"))', [string][Diagnostics.Process]::GetCurrentProcess().Id); Start-Sleep -Seconds 30"
    $timeout = Invoke-V16Child -ScriptBody $timeoutBody -LogDirectory $tmp -RunId 'timeout' -TimeoutSeconds 1
    Assert-True (-not $timeout.capture_complete) 'timeout capture state'
    Assert-True ($timeout.primary_error -like '*CHILD_TIMEOUT*') 'timeout error'
    Assert-True $timeout.active_child 'timeout active child'
    Assert-True $timeout.native_started 'timeout native started'
    Assert-True (-not $timeout.exit_observed) 'timeout no exit observed'
    Assert-True (Test-Path -LiteralPath $timeoutPidPath -PathType Leaf) 'timeout synthetic pid captured'
    $syntheticPid = [int](Get-Content -LiteralPath $timeoutPidPath -Raw)
    Stop-Process -Id $syntheticPid -Force -ErrorAction Stop
    Start-Sleep -Milliseconds 300
    $stillThere = $true; try { Get-Process -Id $syntheticPid -ErrorAction Stop | Out-Null } catch { $stillThere = $false }
    Assert-True (-not $stillThere) 'timeout synthetic child terminated'
} finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }

Write-Output ("V16_OPERATOR_TESTS_PASS assertions=$script:AssertionCount guard_sha256=" + (Get-FileHash -LiteralPath (Join-Path $here 'v16_operator_guard.ps1') -Algorithm SHA256).Hash.ToLowerInvariant() + " executor_sha256=" + (Get-FileHash -LiteralPath (Join-Path $here 'v16_child_executor.ps1') -Algorithm SHA256).Hash.ToLowerInvariant())
