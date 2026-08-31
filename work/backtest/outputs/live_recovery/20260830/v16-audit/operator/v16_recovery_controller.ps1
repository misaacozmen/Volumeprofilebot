[CmdletBinding()]
param([switch]$Execute,[string]$AuditAttempt='',[string]$AuditRoot='')

Set-StrictMode -Version Latest
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here 'v16_operator_guard.ps1')
. (Join-Path $here 'v16_child_executor.ps1')
. (Join-Path $here 'v16_operation_adapters.ps1')

function Write-V16JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][object]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 20))
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw "AUDIT_WRITE_PARENT_MISSING:$parent" }
    $stream = [IO.File]::Open([IO.Path]::GetFullPath($Path), [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    $readback = [IO.File]::ReadAllBytes($Path)
    if (-not ([Linq.Enumerable]::SequenceEqual($bytes, $readback))) { throw "AUDIT_WRITE_READBACK_MISMATCH:$Path" }
}

function New-V16AuditAttempt {
    param([Parameter(Mandatory = $true)][string]$AuditRoot)
    [void](Assert-V16AuditRoot -Path $AuditRoot)
    $root = [IO.Path]::GetFullPath($AuditRoot)
    $name = 'attempt-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N')
    $path = [IO.Path]::GetFullPath((Join-Path $root $name))
    if (-not $path.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $path)) { throw 'AUDIT_ATTEMPT_PATH_INVALID_OR_EXISTS' }
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetSecurityDescriptorSddlForm('D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)', [System.Security.AccessControl.AccessControlSections]::Access)
    [void][IO.Directory]::CreateDirectory($path, $security)
    [void](Assert-V16AuditRoot -Path $path)
    foreach ($child in @('operator','calls','proof','state')) { [void][IO.Directory]::CreateDirectory((Join-Path $path $child)) }
    return [string]$path
}

function Copy-V16ProofFile {
    param([Parameter(Mandatory = $true)][string]$Source, [Parameter(Mandatory = $true)][string]$Destination)
    $src = [IO.Path]::GetFullPath($Source); $dst = [IO.Path]::GetFullPath($Destination)
    if (-not (Test-Path -LiteralPath $src -PathType Leaf) -or (Test-Path -LiteralPath $dst)) { throw "PROOF_COPY_SOURCE_OR_DEST_INVALID:$src" }
    [IO.File]::Copy($src, $dst, $false)
    $sourceBytes = [IO.File]::ReadAllBytes($src); $destBytes = [IO.File]::ReadAllBytes($dst)
    $sourceHash = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLowerInvariant()
    $destHash = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sourceBytes.Length -ne $destBytes.Length -or $sourceHash -cne $destHash -or -not ([Linq.Enumerable]::SequenceEqual($sourceBytes, $destBytes))) { throw "PROOF_COPY_MISMATCH:$src" }
    [pscustomobject]@{source=$src;destination=$dst;source_bytes=$sourceBytes.Length;destination_bytes=$destBytes.Length;source_sha256=$sourceHash;destination_sha256=$destHash}
}

function Get-V16PathSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath ([IO.Path]::GetFullPath($Path)) -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-V16AclEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
        $mask = [int64][int]$_.FileSystemRights
        if ($mask -lt 0) { $mask += 0x100000000L }
        [pscustomobject]@{
            sid = [string]$_.IdentityReference.Value
            access_type = [string]$_.AccessControlType
            mask_decimal = $mask
            mask_hex = ('0x{0:X8}' -f ([uint64]$mask))
            is_inherited = [bool]$_.IsInherited
            inheritance_flags = [string]$_.InheritanceFlags
            propagation_flags = [string]$_.PropagationFlags
        }
    })
    [pscustomobject]@{
        owner_sid = [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        sddl = [string]$acl.Sddl
        are_access_rules_protected = [bool]$acl.AreAccessRulesProtected
        ace_count = $rules.Count
        aces = $rules
    }
}

function Assert-V16RuntimePinAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $acl = Get-Acl -LiteralPath $full -ErrorAction Stop
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($acl.Sddl)
    if ($null -eq $raw.Owner) { throw "RUNTIME_PIN_OWNER_MISSING:$full" }
    if ($raw.Owner.Value -cne 'S-1-5-18') { throw "RUNTIME_PIN_OWNER_INVALID:${full}:$($raw.Owner.Value)" }
    if ($null -eq $raw.DiscretionaryAcl) { throw "RUNTIME_PIN_NULL_DACL:${full}" }
    $aces = @(Get-V16RawAceReport -Descriptor $raw)
    $failures = @($aces | Where-Object decision -eq 'FAIL')
    if ($failures.Count -ne 0) { throw "RUNTIME_PIN_EFFECTIVE_ACL_INVALID:${full}:$($failures | ConvertTo-Json -Depth 8 -Compress)" }
    [pscustomobject]@{status='PASS';owner_sid=$raw.Owner.Value;sddl=$acl.Sddl;protected=[bool]$acl.AreAccessRulesProtected;ace_decisions=$aces;failure_count=0}
}

function Get-V16RuntimePinEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('powershell','terminal')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$ExpectedPowerShell,
        [Parameter(Mandatory = $true)][string]$ExpectedTerminal,
        [Parameter(Mandatory = $true)][string]$ExpectedTerminalData
    )
    $full = [IO.Path]::GetFullPath($Path)
    $out = [ordered]@{kind=$Kind;path=$full;status='UNKNOWN';get_item=$null;read_only_open=$null;bytes=$null;sha256=$null;raw_json=$null;owner_sid=$null;sddl=$null;acl=$null;acl_contract=$null;binary=$null;error=$null}
    try {
        $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
        $out.get_item = [pscustomobject]@{full_name=$item.FullName;length=$item.Length;attributes=[string]$item.Attributes;is_file=(-not $item.PSIsContainer);is_directory=[bool]$item.PSIsContainer;is_reparse=[bool](($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)}
        $out.acl = Get-V16AclEvidence -Path $full
        $out.owner_sid = $out.acl.owner_sid; $out.sddl = $out.acl.sddl
        if ($item.PSIsContainer) { $out.status='WRONG_NODE_TYPE'; return [pscustomobject]$out }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { $out.status='REPARSE_POINT'; return [pscustomobject]$out }
        try { $out.acl_contract = Assert-V16RuntimePinAcl -Path $full } catch { $out.status='ACL_CONTRACT_INVALID'; $out.error=[pscustomobject]@{stage='acl_contract';exception_type=$_.Exception.GetType().FullName;hresult=$_.Exception.HResult;message=$_.Exception.Message}; return [pscustomobject]$out }
        $stream = [IO.File]::Open($full,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        try { $out.read_only_open=[pscustomobject]@{status='PASS';length=$stream.Length}; $reader=New-Object IO.StreamReader($stream,[Text.Encoding]::UTF8,$true,4096,$true); try { $out.raw_json=$reader.ReadToEnd() } finally { $reader.Dispose() } }
        finally { $stream.Dispose() }
        $out.bytes=[IO.File]::ReadAllBytes($full).Length; $out.sha256=Get-V16PathSha256 $full
        try { $pin=$out.raw_json | ConvertFrom-Json -ErrorAction Stop } catch { $out.status='INVALID_JSON'; $out.error=[pscustomobject]@{stage='json_parse';exception_type=$_.Exception.GetType().FullName;hresult=$_.Exception.HResult;message=$_.Exception.Message}; return [pscustomobject]$out }
        if ([int]$pin.schema_version -ne 1) { $out.status='INVALID_PIN_CONTENT'; $out.error='schema_version_must_be_1'; return [pscustomobject]$out }
        if ($Kind -eq 'powershell') {
            $expected = [IO.Path]::GetFullPath($ExpectedPowerShell); $pathOk = ([string]$pin.powershell_path) -and [IO.Path]::GetFullPath([string]$pin.powershell_path).Equals($expected,[StringComparison]::OrdinalIgnoreCase)
            $sigOk = [string]$pin.signer_subject -match '^CN=Microsoft Windows, O=Microsoft Corporation,' -and [string]$pin.signer_thumbprint -match '^[A-Fa-f0-9]{40}$'
            $binary = Get-Item -LiteralPath $expected -Force -ErrorAction Stop
            $binaryHash=Get-V16PathSha256 $expected; $sig=Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $expected
            $binarySubject=if($sig.SignerCertificate){[string]$sig.SignerCertificate.Subject}else{$null}; $binaryThumbprint=if($sig.SignerCertificate){[string]$sig.SignerCertificate.Thumbprint}else{$null}
            $out.binary=[pscustomobject]@{path=$expected;sha256=$binaryHash;is_reparse=[bool](($binary.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0);signature_status=[string]$sig.Status;signer_subject=$binarySubject;signer_thumbprint=$binaryThumbprint;pin_signer_subject=[string]$pin.signer_subject;pin_signer_thumbprint=[string]$pin.signer_thumbprint}
            $sigOk = $sigOk -and [string]$pin.signer_subject -ceq $binarySubject -and [string]$pin.signer_thumbprint -ceq $binaryThumbprint
            if (-not $pathOk -or [string]$pin.powershell_sha256 -notmatch '^[a-f0-9]{64}$' -or [string]$pin.powershell_sha256 -cne $binaryHash -or -not $sigOk -or [string]$sig.Status -cne 'Valid' -or $out.binary.is_reparse) { $out.status='PIN_BINARY_MISMATCH'; return [pscustomobject]$out }
        }
        else {
            $expected = [IO.Path]::GetFullPath($ExpectedTerminal); $terminal = Get-Item -LiteralPath $expected -Force -ErrorAction Stop; $binaryHash=Get-V16PathSha256 $expected
            $pointer = Join-Path (Split-Path -Parent $ExpectedTerminalData) 'mt5-terminal.txt'
            $pointerValue=$null; $pointerItem=$null; if(Test-Path -LiteralPath $pointer -PathType Leaf){$pointerItem=Get-Item -LiteralPath $pointer -Force; if(($pointerItem.Attributes -band [IO.FileAttributes]::ReparsePoint)-eq 0){$pointerValue=[IO.Path]::GetFullPath(([IO.File]::ReadAllText($pointer)).Trim())}}
            $pathOk=([string]$pin.terminal_path) -and [IO.Path]::GetFullPath([string]$pin.terminal_path).Equals($expected,[StringComparison]::OrdinalIgnoreCase)
            $pointerOk=($null -ne $pointerItem -and $pointerItem.PSIsContainer -eq $false -and ($pointerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and $null -ne $pointerValue -and $pointerValue.Equals($expected,[StringComparison]::OrdinalIgnoreCase))
            $out.binary=[pscustomobject]@{path=$expected;sha256=$binaryHash;pointer=$pointer;pointer_value=$pointerValue;pointer_present=($null -ne $pointerItem);pointer_is_reparse=if($pointerItem){[bool](($pointerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)}else{$null};is_reparse=[bool](($terminal.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0);data_root=$ExpectedTerminalData}
            if (-not $pathOk -or [string]$pin.terminal_sha256 -notmatch '^[a-f0-9]{64}$' -or [string]$pin.terminal_sha256 -cne $binaryHash -or -not $pointerOk -or $out.binary.is_reparse) { $out.status='PIN_BINARY_MISMATCH'; return [pscustomobject]$out }
        }
        $out.status='PRESENT_VALID'; return [pscustomobject]$out
    }
    catch {
        $exceptionType=$_.Exception.GetType().FullName; $hresult=$_.Exception.HResult; $status=if($exceptionType -match 'Unauthorized|Security'){'ACCESS_DENIED'}elseif($exceptionType -match 'FileNotFound|DirectoryNotFound'){'MISSING_ON_DISK'}else{'UNKNOWN'}
        $out.status=$status; $out.error=[pscustomobject]@{stage='get_item_or_read';exception_type=$exceptionType;hresult=$hresult;message=$_.Exception.Message}; return [pscustomobject]$out
    }
}

function Get-V16SignedArchiveEvidence {
    param([Parameter(Mandatory = $true)][string]$Root,[Parameter(Mandatory = $true)][string]$ExpectedRelease,[Parameter(Mandatory = $true)][string]$ExpectedCommit,[Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,[Parameter(Mandatory = $true)][string]$ExpectedArchiveSha256,[Parameter(Mandatory = $true)][string]$IntegrityScriptExpectedSha256)
    $integrity=Join-Path (Join-Path $Root 'app') 'deploy\release_integrity.ps1'
    if(-not(Test-Path -LiteralPath $integrity -PathType Leaf)){throw 'RELEASE_INTEGRITY_SCRIPT_MISSING'}
    $integrityActual=Get-V16PathSha256 $integrity
    if($integrityActual -cne $IntegrityScriptExpectedSha256){throw "RELEASE_INTEGRITY_SCRIPT_HASH_MISMATCH:expected=$IntegrityScriptExpectedSha256 actual=$integrityActual"}
    . $integrity
    $archiveRoot=Join-Path $Root 'archive'; $attempts=[System.Collections.Generic.List[object]]::new(); $valid=[System.Collections.Generic.List[object]]::new()
    foreach($archive in @(Get-ChildItem -LiteralPath $archiveRoot -Recurse -File -Filter '*.zip' -ErrorAction Stop | Sort-Object FullName)) {
        $manifestPath=[IO.Path]::ChangeExtension($archive.FullName,'.manifest.json'); $signaturePath=[IO.Path]::ChangeExtension($archive.FullName,'.manifest.sig')
        if(-not(Test-Path -LiteralPath $manifestPath -PathType Leaf) -or -not(Test-Path -LiteralPath $signaturePath -PathType Leaf)){continue}
        $row=[ordered]@{archive=$archive.FullName;archive_sha256=Get-V16PathSha256 $archive.FullName;manifest_sha256=Get-V16PathSha256 $manifestPath;status='FAIL';error=$null;verified=$null}
        try {
            $m=[IO.File]::ReadAllText($manifestPath)|ConvertFrom-Json
            if([string]$m.release_id -cne $ExpectedRelease -or $row.archive_sha256 -cne $ExpectedArchiveSha256 -or $row.manifest_sha256 -cne $ExpectedManifestSha256){throw 'ARCHIVE_ID_HASH_NOT_EXPECTED'}
            $v=Assert-SignedReleaseArchive -Archive $archive.FullName -ExpectedProfile 'super1' -RequireProvenance
            if([string]$v.release_id -cne $ExpectedRelease -or [string]$v.git_commit -cne $ExpectedCommit -or @($v.files).Count -ne 196){throw 'SIGNED_ARCHIVE_PROVENANCE_MISMATCH'}
            $row.status='PASS';$row.verified=$v;$valid.Add([pscustomobject]$row)
        } catch { $row.error=$_.Exception.ToString() }
        $attempts.Add([pscustomobject]$row)
    }
    if($valid.Count -eq 0){throw ('SIGNED_ARCHIVE_GATE_FAILED:' + (($attempts|ConvertTo-Json -Depth 8 -Compress)))}
    $selected=$valid|Sort-Object archive|Select-Object -First 1
    [pscustomobject]@{status='PASS';integrity_script=[pscustomobject]@{path=$integrity;sha256=$integrityActual};selected_archive=$selected.archive;archive_sha256=$selected.archive_sha256;manifest_sha256=$selected.manifest_sha256;release_id=$ExpectedRelease;git_commit=$ExpectedCommit;file_count=@($selected.verified.files).Count;manifest=$selected.verified;valid_copies=@($valid|ForEach-Object archive);other_verified_copies=@($valid|Where-Object archive -ne $selected.archive|ForEach-Object archive);candidates=$attempts.ToArray()}
}

function Get-V16InstalledManifestEvidence {
    param([Parameter(Mandatory = $true)][string]$App,[Parameter(Mandatory = $true)][object]$Manifest)
    $appFull=[IO.Path]::GetFullPath($App); $rows=[System.Collections.Generic.List[object]]::new()
    foreach($entry in @($Manifest.files|Sort-Object path)) {
        $rel=[string]$entry.path; $full=[IO.Path]::GetFullPath((Join-Path $appFull ($rel.Replace('/',[IO.Path]::DirectorySeparatorChar))))
        $row=[ordered]@{path=$rel;full_path=$full;expected_sha256=([string]$entry.sha256).ToLowerInvariant();status='ERROR';actual_sha256=$null;bytes=$null;is_reparse=$null}
        try {
            if(-not $full.StartsWith($appFull+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'OUTSIDE_APP'}
            $item=Get-Item -LiteralPath $full -Force -ErrorAction Stop
            $row.is_reparse=[bool](($item.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0);$row.bytes=$item.Length
            if($item.PSIsContainer){$row.status='WRONG_NODE_TYPE'}elseif($row.is_reparse){$row.status='REPARSE_POINT'}else{$row.actual_sha256=Get-V16PathSha256 $full;$row.status=if($row.actual_sha256 -ceq $row.expected_sha256){'PASS'}else{'HASH_MISMATCH'}}
        } catch { $row.status=if($_.Exception -is [System.Management.Automation.ItemNotFoundException]){'MISSING'}else{'ERROR'};$row.error=$_.Exception.ToString() }
        $rows.Add([pscustomobject]$row)
    }
    $bad=@($rows|Where-Object status -ne 'PASS'); [pscustomobject]@{status=if($bad.Count -eq 0){'PASS'}else{'FAIL'};manifest_count=$rows.Count;matched_count=@($rows|Where-Object status -eq 'PASS').Count;rows=$rows.ToArray();failures=$bad}
}

function Invoke-V16RecoveryStateMachine {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Preflight,
        [Parameter(Mandatory = $true)][scriptblock]$Flat,
        [Parameter(Mandatory = $true)][scriptblock]$FlatAdapter,
        [Parameter(Mandatory = $true)][scriptblock]$ProofCopy,
        [Parameter(Mandatory = $true)][scriptblock]$ReadinessGate,
        [Parameter(Mandatory = $true)][scriptblock]$Rollover,
        [Parameter(Mandatory = $true)][scriptblock]$StopAssert,
        [Parameter(Mandatory = $true)][scriptblock]$PostFlat
    )
    $counts=[ordered]@{flat_calls=0;rollover_launch_intents=0;rollover_calls=0;post_flat_calls=0;smoke_calls=0;natural_observation_calls=0}
    $out=[ordered]@{counts=$counts;stage='START';first_failure=$null;first_failure_detail=$null;rollover_result=$null;post_flat_result=$null;active_child=$false;active_or_unknown_children=@();final_runtime_stopped=$false;stop_assertions=0;automatic_shutdown_attempted=$false;automatic_shutdown_verified=$false}
    $flatResult=$null; $rolloverResult=$null; $stopResult=$null; $runtimeAction='none'; $stopChildActive=$false; $stopSinceAction=$false
    try {
        $pre=&$Preflight; if ($null -eq $pre -or $pre.status -ne 'PASS') { throw 'PREFLIGHT_GATE_FAILED' }
        $counts.flat_calls++; $out.stage='FLAT'; $flatResult=&$Flat
        if($flatResult -and (Test-V16HasProperty $flatResult 'active_child') -and $flatResult.active_child){$out.active_or_unknown_children+=@($flatResult);$out.active_child=$true}
        $flatExited=$false; if($flatResult -and (Test-V16HasProperty $flatResult 'exit_observed')){if($flatResult.exit_observed -eq $true){$flatExited=$true}}elseif($flatResult -and (Test-V16HasProperty $flatResult 'child_exited')){if($flatResult.child_exited -eq $true){$flatExited=$true}}; if($flatExited){$runtimeAction='flat';$stopSinceAction=$false}
        $flatHasPrimaryError=($flatResult -and (Test-V16HasProperty $flatResult 'primary_error') -and $null -ne $flatResult.primary_error);$flatHasCleanupError=($flatResult -and (Test-V16HasProperty $flatResult 'cleanup_error') -and $null -ne $flatResult.cleanup_error); if($flatResult -and (Test-V16HasProperty $flatResult 'capture_complete') -and $flatResult.capture_complete -eq $true -and ($flatHasPrimaryError -or $flatHasCleanupError)){throw 'FLAT_CAPTURE_ERROR_CONTRADICTION'}
        if ($null -eq $flatResult -or -not (Test-V16HasProperty $flatResult 'capture_complete') -or -not (Test-V16HasProperty $flatResult 'process_succeeded') -or $flatResult.capture_complete -ne $true -or $flatResult.process_succeeded -ne $true) { throw 'FLAT_PROCESS_OR_CAPTURE_FAILED' }
        $out.stage='FLAT_ADAPTER'; $flatAccepted=&$FlatAdapter $flatResult
        if ($null -eq $flatAccepted -or -not (Test-V16HasProperty $flatAccepted 'operation_accepted') -or $flatAccepted.operation_accepted -ne $true) { throw 'FLAT_OPERATION_REJECTED' }
        $out.stage='PROOF_COPY'; $proof=&$ProofCopy $flatAccepted
        if ($null -eq $proof -or -not (Test-V16HasProperty $proof 'status') -or $proof.status -ne 'PASS') { throw 'PROOF_COPY_FAILED' }
        $out.stage='READINESS_GATE'; $gate=&$ReadinessGate $flatAccepted $proof
        if ($null -eq $gate -or -not (Test-V16HasProperty $gate 'status') -or $gate.status -ne 'PASS') { throw 'READINESS_GATE_FAILED' }
        $counts.rollover_launch_intents++; $out.stage='ROLLOVER_START_INTENT'; $counts.rollover_calls++; $runtimeAction='rollover'; $stopSinceAction=$false; $rolloverResult=&$Rollover $flatAccepted $proof
        $out.rollover_result=$rolloverResult
        if ($rolloverResult -and (Test-V16HasProperty $rolloverResult 'active_child') -and $rolloverResult.active_child){$out.active_or_unknown_children+=@($rolloverResult);$out.active_child=$true}
        if ($null -eq $rolloverResult -or -not (Test-V16HasProperty $rolloverResult 'child_exited') -or $rolloverResult.child_exited -ne $true) { $out.active_child=$true; throw 'ROLLOVER_CHILD_EXIT_UNKNOWN' }
        $stopSinceAction=$true; $stopResult=&$StopAssert; $out.stop_assertions++; if ($stopResult -and (Test-V16HasProperty $stopResult 'active_child') -and $stopResult.active_child){$out.active_or_unknown_children+=@($stopResult);$out.active_child=$true;$stopChildActive=$true}; if ($null -eq $stopResult -or -not (Test-V16HasProperty $stopResult 'status') -or $stopResult.status -ne 'PASS') { throw 'POST_ROLLOVER_STOP_ASSERT_FAILED' }
        $out.final_runtime_stopped=$true; $runtimeAction='stopped'
        if (-not (Test-V16HasProperty $rolloverResult 'operation_accepted') -or $rolloverResult.operation_accepted -ne $true) { throw 'ROLLOVER_OPERATION_REJECTED' }
        $counts.post_flat_calls++; $out.stage='POST_FLAT'; $runtimeAction='post_flat'; $stopSinceAction=$false; $out.final_runtime_stopped=$false; $out.post_flat_result=&$PostFlat; if($out.post_flat_result -and (Test-V16HasProperty $out.post_flat_result 'active_child') -and $out.post_flat_result.active_child){$out.active_or_unknown_children+=@($out.post_flat_result);$out.active_child=$true}
        if ($null -eq $out.post_flat_result -or -not (Test-V16HasProperty $out.post_flat_result 'operation_accepted') -or $out.post_flat_result.operation_accepted -ne $true) { throw 'POST_FLAT_REJECTED' }
        $stopSinceAction=$true; $stopResult=&$StopAssert; $out.stop_assertions++; if ($stopResult -and (Test-V16HasProperty $stopResult 'active_child') -and $stopResult.active_child){$out.active_or_unknown_children+=@($stopResult);$out.active_child=$true;$stopChildActive=$true}; if ($null -eq $stopResult -or -not (Test-V16HasProperty $stopResult 'status') -or $stopResult.status -ne 'PASS') { throw 'FINAL_STOP_ASSERT_FAILED' }
        $out.final_runtime_stopped=$true; $out.stage='SUCCESS'
        $runtimeAction='stopped'
    } catch {
        if ($null -eq $out.first_failure) { $out.first_failure=$_.Exception.Message; $out.first_failure_detail=$_.Exception.ToString() }
        $canSafeStop = ($runtimeAction -in @('flat','rollover','post_flat')) -and $out.active_child -ne $true -and -not $stopChildActive -and -not $stopSinceAction
        if ($out.stage -ne 'SUCCESS' -and $canSafeStop) {
            $out.automatic_shutdown_attempted=$true
            try { $stopSinceAction=$true; $stopResult=&$StopAssert; $out.stop_assertions++; if($stopResult -and (Test-V16HasProperty $stopResult 'active_child') -and $stopResult.active_child){$out.active_or_unknown_children+=@($stopResult);$out.active_child=$true;$stopChildActive=$true}; $out.final_runtime_stopped=($null -ne $stopResult -and $stopResult.status -eq 'PASS' -and (-not (Test-V16HasProperty $stopResult 'active_child') -or $stopResult.active_child -ne $true)); $out.automatic_shutdown_verified=$out.final_runtime_stopped; if($out.final_runtime_stopped){$runtimeAction='stopped'} } catch { $out.final_runtime_stopped=$false }
        }
        if ($out.stage -ne 'SUCCESS') { $out.stage='FAILED' }
    }
    if($out.active_or_unknown_children.Count -eq 0){$out.active_or_unknown_children=@()}
    [pscustomobject]$out
}

function Assert-V16InstalledRelease {
    param([string]$Root,[string]$App,[string]$ExpectedRelease,[string]$ExpectedCommit,[string]$ExpectedManifestSha256)
    $expectedArchive='7629122eec3d587146bc6fce53bc684b495dc410f58a186264873357b01073bf'
    $signed=Get-V16SignedArchiveEvidence -Root $Root -ExpectedRelease $ExpectedRelease -ExpectedCommit $ExpectedCommit -ExpectedManifestSha256 $ExpectedManifestSha256 -ExpectedArchiveSha256 $expectedArchive -IntegrityScriptExpectedSha256 'd9f1aac04394dea1b11c8273fa68b732ce14e8c7f2ae96f1c698c26fae05b0b3'
    $installed=Get-V16InstalledManifestEvidence -App $App -Manifest $signed.manifest
    if($installed.status -ne 'PASS'){throw ('INSTALLED_MANIFEST_GATE_FAILED:' + ($installed.failures|ConvertTo-Json -Depth 8 -Compress))}
    [pscustomobject]@{signed_archive=$signed;installed_manifest=$installed;signed_archive_verified=($signed.status -eq 'PASS');installed_manifest_files_verified=($installed.status -eq 'PASS');generated_runtime_pins_verified=$null;installed_release_verified=($signed.status -eq 'PASS' -and $installed.status -eq 'PASS')}
}

function Assert-V16ManifestHelperHash {
    param([Parameter(Mandatory = $true)][object]$Manifest,[Parameter(Mandatory = $true)][string]$App,[Parameter(Mandatory = $true)][string]$RelativePath)
    $entry=@($Manifest.files|Where-Object { [string]$_.path -ceq $RelativePath }); if($entry.Count -ne 1){throw "SIGNED_HELPER_NOT_IN_MANIFEST:$RelativePath"}
    $full=[IO.Path]::GetFullPath((Join-Path $App ($RelativePath.Replace('/',[IO.Path]::DirectorySeparatorChar))))
    if(-not(Test-Path -LiteralPath $full -PathType Leaf)){throw "SIGNED_HELPER_MISSING:$RelativePath"}
    $item=Get-Item -LiteralPath $full -Force; if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0){throw "SIGNED_HELPER_REPARSE:$RelativePath"}
    $actual=Get-V16PathSha256 $full; if($actual -cne ([string]$entry[0].sha256).ToLowerInvariant()){throw "SIGNED_HELPER_HASH_MISMATCH:$RelativePath expected=$($entry[0].sha256) actual=$actual"}
    [pscustomobject]@{path=$full;relative_path=$RelativePath;sha256=$actual;expected_sha256=([string]$entry[0].sha256).ToLowerInvariant();status='PASS'}
}

function Get-V16TransactionIdentity {
    param([Parameter(Mandatory = $true)][string]$ReadinessPath,[Parameter(Mandatory = $true)][string]$Root)
    $result=[IO.Path]::GetFullPath($ReadinessPath);$archive=[IO.Path]::GetFullPath((Join-Path $Root 'archive'));$tx=[IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $result)));$txName=Split-Path -Leaf $tx
    if(-not $tx.StartsWith($archive+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase) -or $txName -notmatch '^readiness-([a-f0-9]{32})$'){throw 'READINESS_TRANSACTION_PATH_INVALID'}
    $transactionId=[string]$Matches[1]
    if(-not $result.Equals([IO.Path]::GetFullPath((Join-Path $tx 'output\result.json')),[StringComparison]::OrdinalIgnoreCase)){throw 'READINESS_RESULT_PATH_INVALID'}
    $items=@(Get-Item -LiteralPath $tx -Force -ErrorAction Stop)+@(Get-ChildItem -LiteralPath $tx -Recurse -Force -ErrorAction Stop)
    if(@($items|Where-Object {($_.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0}).Count -ne 0){throw 'READINESS_SEALED_TREE_REPARSE'}
    [pscustomobject]@{transaction_id=$transactionId;transaction_path=$tx;readiness_path=$result;archive_root=$archive;status='PASS'}
}

function New-V16CoordinationLock {
    param([Parameter(Mandatory = $true)][string]$AuditRoot,[Parameter(Mandatory = $true)][string]$Attempt,[Parameter(Mandatory = $true)][string]$RunId)
    $dir=Join-Path ([IO.Path]::GetFullPath($AuditRoot)) 'coordination-v16'; [void][IO.Directory]::CreateDirectory($dir)
    $unresolved=Join-Path $dir 'unresolved.json'; $lockPath=Join-Path $dir 'controller.lock'; $historyDir=Join-Path $dir 'history'; [void][IO.Directory]::CreateDirectory($historyDir); $lock=$null
    try {
        $lock=[IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $previous=$null; $previousSha256=$null; $previousHistory=$null
        if(Test-Path -LiteralPath $unresolved -PathType Leaf){
            $previousBytes=[IO.File]::ReadAllBytes($unresolved); $sha=[Security.Cryptography.SHA256]::Create(); try{$previousSha256=([BitConverter]::ToString($sha.ComputeHash($previousBytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
            try{$previous=([Text.Encoding]::UTF8.GetString($previousBytes))|ConvertFrom-Json -ErrorAction Stop}catch{throw 'COORDINATION_PREVIOUS_RECORD_INVALID'}
            if(-not (Test-V16HasProperty $previous 'schema_version') -or [int]$previous.schema_version -ne 2 -or -not (Test-V16HasProperty $previous 'resolved') -or $previous.resolved -isnot [bool]){throw 'COORDINATION_PREVIOUS_RECORD_SCHEMA_INVALID'}
            if($previous.resolved -ne $true){throw 'UNRESOLVED_V16_OPERATION_EXISTS'}
            $previousHistory=Join-Path $historyDir ('previous-'+$previousSha256+'.json'); if(-not (Test-Path -LiteralPath $previousHistory -PathType Leaf)){[IO.File]::WriteAllBytes($previousHistory,$previousBytes)}
            if(-not ([Linq.Enumerable]::SequenceEqual($previousBytes,[IO.File]::ReadAllBytes($previousHistory)))){throw 'COORDINATION_HISTORY_COPY_MISMATCH'}
        }
        $record=[ordered]@{schema_version=2;run_id=$RunId;attempt=[IO.Path]::GetFullPath($Attempt);created_at_utc=[DateTimeOffset]::UtcNow.ToString('o');process_records=@();active_or_unknown_children=$false;resolved=$false;status='ACTIVE';previous_record_sha256=$previousSha256}
        Write-V16JsonFile -Path $unresolved -Value $record
        [pscustomobject]@{lock=$lock;lock_path=$lockPath;unresolved_path=$unresolved;history_dir=$historyDir;previous=$previous;previous_record_sha256=$previousSha256}
    } catch { if($lock){$lock.Dispose()}; throw }
}

function Write-V16LifecycleRecord {
    param([Parameter(Mandatory = $true)][string]$Path,[Parameter(Mandatory = $true)][object]$Record)
    Write-V16JsonFile -Path $Path -Value $Record
}

function Get-V16InitialInventory {
    param([string]$Root,[string]$App,[string]$MainTask,[string]$WatchdogTask,[string]$Attempt,[string]$ExpectedRelease,[string]$ExpectedCommit,[string]$ExpectedManifestSha256)
    $expectedPowerShell=[IO.Path]::GetFullPath((Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'))
    $expectedTerminal=[IO.Path]::GetFullPath((Join-Path $Root 'mt5-clean5833\terminal64.exe'))
    $expectedTerminalData=[IO.Path]::GetFullPath((Join-Path $Root 'mt5-clean5833'))
    $release=[ordered]@{signed_archive_verified=$false;installed_manifest_files_verified=$false;generated_runtime_pins_verified=$false;installed_release_verified=$false;error=$null;signed_archive=$null;installed_manifest=$null}
    try {
        $signed=Get-V16SignedArchiveEvidence -Root $Root -ExpectedRelease $ExpectedRelease -ExpectedCommit $ExpectedCommit -ExpectedManifestSha256 $ExpectedManifestSha256 -ExpectedArchiveSha256 '7629122eec3d587146bc6fce53bc684b495dc410f58a186264873357b01073bf' -IntegrityScriptExpectedSha256 'd9f1aac04394dea1b11c8273fa68b732ce14e8c7f2ae96f1c698c26fae05b0b3'
        $release.signed_archive_verified=$true;$release.signed_archive=$signed
        $installed=Get-V16InstalledManifestEvidence -App $App -Manifest $signed.manifest
        $release.installed_manifest_files_verified=($installed.status -eq 'PASS');$release.installed_manifest=$installed
        if(-not $release.installed_manifest_files_verified){$release.error='INSTALLED_MANIFEST_GATE_FAILED'}
    } catch { $release.error=$_.Exception.ToString() }
    $pinEvidence=[ordered]@{powershell=Get-V16RuntimePinEvidence -Path (Join-Path $App 'deploy\powershell_runtime_pin.json') -Kind powershell -ExpectedPowerShell $expectedPowerShell -ExpectedTerminal $expectedTerminal -ExpectedTerminalData $expectedTerminalData;terminal=Get-V16RuntimePinEvidence -Path (Join-Path $App 'deploy\terminal_runtime_pin.json') -Kind terminal -ExpectedPowerShell $expectedPowerShell -ExpectedTerminal $expectedTerminal -ExpectedTerminalData $expectedTerminalData}
    $release.generated_runtime_pins_verified=($pinEvidence.powershell.status -eq 'PRESENT_VALID' -and $pinEvidence.terminal.status -eq 'PRESENT_VALID')
    $release.installed_release_verified=($release.signed_archive_verified -and $release.installed_manifest_files_verified -and $release.generated_runtime_pins_verified)
    $helperEvidence=@();$runnerSid=$null;$taskXml=@();$stopped=$false;$probeActive=$null;$activeRecovery=@();$candidateRows=@();$stateFiles=@();$helperError=$null
    if($release.signed_archive_verified -and $release.installed_manifest_files_verified){
        try {
            $helperEvidence=@(Assert-V16ManifestHelperHash -Manifest $release.signed_archive.manifest -App $App -RelativePath 'deploy/super1_secure_task.ps1'; Assert-V16ManifestHelperHash -Manifest $release.signed_archive.manifest -App $App -RelativePath 'deploy/run_super1_windows.ps1'; Assert-V16ManifestHelperHash -Manifest $release.signed_archive.manifest -App $App -RelativePath 'deploy/check_super1_flat_windows.ps1'; Assert-V16ManifestHelperHash -Manifest $release.signed_archive.manifest -App $App -RelativePath 'deploy/rollover_super1_campaign_windows.ps1'; . (Join-Path $App 'deploy\super1_secure_task.ps1'))
            $pairs=@(
                @{name='capital';candidate=(Join-Path $Root 'run_capital_forward.py.candidate');target=(Join-Path $Root 'app\scripts\run_capital_forward.py')},
                @{name='xm';candidate=(Join-Path $Root 'run_xm_mt5_forward.py.candidate');target=(Join-Path $Root 'app\scripts\run_xm_mt5_forward.py')},
                @{name='super1';candidate=(Join-Path $Root 'run_super1_xm_mt5_forward.py.candidate');target=(Join-Path $Root 'app\scripts\run_super1_xm_mt5_forward.py')},
                @{name='runtime';candidate=(Join-Path $Root 'super1_xm_mt5_demo_config.json.candidate');target=(Join-Path $Root 'app\live_forward\super1_xm_mt5_demo_config.json')},
                @{name='manifest';candidate=(Join-Path $Root 'super1_manifest.json.candidate');target=(Join-Path $Root 'app\research_candidates\super1\super1_manifest.json')},
                @{name='contract';candidate=(Join-Path $Root 'super1_signal_contract.json.candidate');target=(Join-Path $Root 'app\research_candidates\super1\super1_signal_contract.json')}
            )
            foreach($p in $pairs){$ch=Get-V16PathSha256 $p.candidate;$th=Get-V16PathSha256 $p.target;$candidateRows+= [pscustomobject]@{name=$p.name;candidate=$p.candidate;target=$p.target;candidate_sha256=$ch;target_sha256=$th;equal=($ch -ceq $th)};if($ch -cne $th){throw "CANDIDATE_TARGET_MISMATCH:$($p.name)"}}
            foreach($task in @($MainTask,$WatchdogTask)){$xml=[string](Export-ScheduledTask -TaskName $task -ErrorAction Stop);$taskXml+=[pscustomobject]@{task=$task;sha256=(Get-FileHash -InputStream ([IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($xml))) -Algorithm SHA256).Hash.ToLowerInvariant();xml=$xml}}
            $runnerSid=[string](Assert-Super1SecureTaskBindings -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask);Assert-Super1SecureStopped -Root $Root -MainTask $MainTask -WatchdogTask $WatchdogTask;$stopped=$true
            $probe=Join-Path $Root 'probe-control\active.json';$probeActive=Test-Path -LiteralPath $probe -PathType Leaf;if($probeActive){throw 'ACTIVE_PROBE_PRESENT'}
            $stateFiles=@(Get-ChildItem -LiteralPath (Join-Path $Root 'state') -Recurse -File -Force -ErrorAction Stop|ForEach-Object{[pscustomobject]@{path=$_.FullName;bytes=$_.Length;sha256=Get-V16PathSha256 $_.FullName}})
            $activeRecovery=@(Get-CimInstance Win32_Process -ErrorAction Stop|Where-Object{$_.ProcessId -ne [Diagnostics.Process]::GetCurrentProcess().Id -and [string]$_.CommandLine -match '(?i)v16_recovery_controller\.ps1|v16_child_executor\.ps1'}|Select-Object ProcessId,CommandLine)
            if($activeRecovery.Count -gt 0){throw 'ACTIVE_OR_UNKNOWN_CONTROLLER_CHILD'}
        } catch {$helperError=$_.Exception.ToString()}
    }
    [pscustomobject]@{root=$Root;app=$App;runner_sid=$runnerSid;stopped=$stopped;probe_active=$probeActive;active_recovery_processes=$activeRecovery;candidate_pairs=$candidateRows;task_xml=$taskXml;state_files=$stateFiles;runtime_pins=$pinEvidence;release=$release;helper_evidence=$helperEvidence;helper_error=$helperError;captured_at_utc=[DateTimeOffset]::UtcNow.ToString('o')}
}

function Invoke-V16RecoveryController {
    param([Parameter(Mandatory = $true)][string]$AuditRoot,[string]$AuditAttempt='')
    $Root='C:\Super1';$App=Join-Path $Root 'app';$MainTask='Super1XM';$WatchdogTask='Super1Watchdog';$FlatScript=Join-Path $App 'deploy\check_super1_flat_windows.ps1';$RolloverScript=Join-Path $App 'deploy\rollover_super1_campaign_windows.ps1';$SecureHelper=Join-Path $App 'deploy\super1_secure_task.ps1';$ExpectedRelease='super1-20260830T075916Z-96decc74b6dc-v16';$ExpectedCommit='96decc74b6dcbb28bfb8b86bb838db254dcb69cd';$ExpectedManifestSha='d58ac65cb0ca15f88e24a254fd52b9cae73f2f41bf07b187c5fa204994dc3039';$Expected=@{account_login=1301910045;server='XMGlobal-MT5 6';company='XM Global Limited';terminal_path='C:\Super1\mt5-clean5833\terminal64.exe';terminal_data_path='C:\Super1\mt5-clean5833'}
    if([string]::IsNullOrWhiteSpace($AuditAttempt)){throw 'AUDIT_ATTEMPT_REQUIRED'}
    $attempt=[IO.Path]::GetFullPath($AuditAttempt); [void](Assert-V16AuditRoot -Path $attempt); if(-not $attempt.StartsWith(([IO.Path]::GetFullPath($AuditRoot)+[IO.Path]::DirectorySeparatorChar),[StringComparison]::OrdinalIgnoreCase)){throw 'AUDIT_ATTEMPT_OUTSIDE_ROOT'}; $runId=[Guid]::NewGuid().ToString('N'); $lock=$null; $coordination=$null; $result=$null; $processRecords=@(); $errorFields=@()
    try {
        $lock=New-Object System.IO.FileStream -ArgumentList @((Join-Path $attempt 'operator\recovery.lock'),[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None); [IO.File]::WriteAllText((Join-Path $attempt 'operator\run_id.txt'),$runId,(New-Object Text.UTF8Encoding($false))); $coordination=New-V16CoordinationLock -AuditRoot $AuditRoot -Attempt $attempt -RunId $runId
        $inv=Get-V16InitialInventory -Root $Root -App $App -MainTask $MainTask -WatchdogTask $WatchdogTask -Attempt $attempt -ExpectedRelease $ExpectedRelease -ExpectedCommit $ExpectedCommit -ExpectedManifestSha256 $ExpectedManifestSha
        Write-V16JsonFile -Path (Join-Path $attempt 'state\inventory-before.json') -Value $inv
        if($inv.release.installed_release_verified -and [string]::IsNullOrWhiteSpace([string]$inv.helper_error)){
            . $SecureHelper
        }
        $launcherEvidence=@($inv.helper_evidence|Where-Object {$_.relative_path -ceq 'deploy/run_super1_windows.ps1'}); if($launcherEvidence.Count -ne 1){throw 'SIGNED_LAUNCHER_EVIDENCE_MISSING'}
        $secureHelperEvidence=@($inv.helper_evidence|Where-Object {$_.relative_path -ceq 'deploy/super1_secure_task.ps1'}); if($secureHelperEvidence.Count -ne 1){throw 'SIGNED_SECURE_HELPER_EVIDENCE_MISSING'}
        $helperTrace=[pscustomobject]@{secure_helper=[pscustomobject]@{path=$secureHelperEvidence[0].path;sha256=$secureHelperEvidence[0].sha256};launcher=[pscustomobject]@{path=$launcherEvidence[0].path;sha256=$launcherEvidence[0].sha256}}
        $preflight={ [pscustomobject]@{status=if($inv.release.installed_release_verified -and [string]::IsNullOrWhiteSpace([string]$inv.helper_error)){'PASS'}else{'FAIL'};inventory=$inv} }.GetNewClosure()
        $callDir=Join-Path $attempt 'calls'
        $flatOp={ $callId=$runId+'-flat'; Write-V16JsonFile -Path (Join-Path $callDir ($callId+'.launch_intent.json')) -Value ([ordered]@{operation='flat';run_id=$callId;intent_at_utc=[DateTimeOffset]::UtcNow.ToString('o')}); $body="& '$($FlatScript.Replace("'","''"))' -KeepStopped"; $exec=Invoke-V16Child -ScriptBody $body -LogDirectory $callDir -RunId $callId -TimeoutSeconds 300; $exec }.GetNewClosure()
        $flatAdapter={ param($exec); if($exec.capture_complete -ne $true -or $exec.process_succeeded -ne $true){return [pscustomobject]@{operation_accepted=$false;status='FAIL';executor=$exec}}; $text=[IO.File]::ReadAllText($exec.stdout); $outer=Test-V16FlatOutput -StdoutText $text -Expected $Expected -SourceFileSha256 ((Get-FileHash -LiteralPath $exec.stdout -Algorithm SHA256).Hash.ToLowerInvariant()); [pscustomobject]@{operation_accepted=$outer.operation_accepted;status=$outer.status;executor=$exec;outer=$outer;readiness_path=[string](Get-V16Field $outer.document 'readiness_evidence');readiness_sha256=[string](Get-V16Field $outer.document 'readiness_sha256')} }.GetNewClosure()
        $copyProofForFlat={ param($flat,$label); . $SecureHelper; $rp=$flat.readiness_path; if(-not(Test-Path -LiteralPath $rp -PathType Leaf)){throw 'READINESS_SOURCE_MISSING'}; $txInfo=Get-V16TransactionIdentity -ReadinessPath $rp -Root $Root; $tx=$txInfo.transaction_path; $sealedCommand=Get-Command -Name Assert-Super1SecureSealedTree -CommandType Function -ErrorAction Stop; $producerCommand=Get-Command -Name Get-Super1SecureProducerEnvelope -CommandType Function -ErrorAction Stop; & $sealedCommand -Path $tx; $dstRoot=Join-Path $attempt ('proof\sealed-transaction-'+$label); [void][IO.Directory]::CreateDirectory($dstRoot); $rows=@();$rows+=Copy-V16ProofFile -Source (Join-Path $tx 'request.json') -Destination (Join-Path $dstRoot 'request.json');$rows+=Copy-V16ProofFile -Source (Join-Path $tx 'output\producer.json') -Destination (Join-Path $dstRoot 'producer.json');$rows+=Copy-V16ProofFile -Source $rp -Destination (Join-Path $dstRoot 'result.json'); $result=[IO.File]::ReadAllText($rp)|ConvertFrom-Json;$request=[IO.File]::ReadAllText((Join-Path $tx 'request.json'))|ConvertFrom-Json;$producer=[IO.File]::ReadAllText((Join-Path $tx 'output\producer.json'))|ConvertFrom-Json; $flatDoc=$flat.outer.document; $expectedLauncherSha=[string]$launcherEvidence[0].sha256; if([string]$request.expected_launcher_sha256 -cne $expectedLauncherSha){throw 'REQUEST_LAUNCHER_HASH_NOT_INSTALLED_HASH'}; $notBefore=[DateTimeOffset]::Parse([string]$flat.executor.started_at_utc); $producerBinding=& $producerCommand -ProducerPath (Join-Path $tx 'output\producer.json') -RequestPath (Join-Path $tx 'request.json') -ResultPath $rp -TransactionId $txInfo.transaction_id -Nonce ([string]$request.nonce) -Kind 'flat' -RunnerSid $inv.runner_sid -ExpectedLauncherPath ([string]$launcherEvidence[0].path) -ExpectedLauncherSha256 $expectedLauncherSha -ExpectedExitCode 0 -NotBefore $notBefore; if($rows[2].source_sha256 -cne $flat.readiness_sha256 -or $rows[0].source_sha256 -cne [string](Get-V16Field $flatDoc 'request_sha256') -or $rows[1].source_sha256 -cne [string](Get-V16Field $flatDoc 'producer_sha256')){throw 'FLAT_PROOF_HASH_BINDING_FAILED'}; [pscustomobject]@{status='PASS';source_transaction=$tx;transaction_id=$txInfo.transaction_id;source_result=$rp;source_result_sha256=$rows[2].source_sha256;source_request_sha256=$rows[0].source_sha256;source_producer_sha256=$rows[1].source_sha256;request=$request;producer=$producer;producer_binding=$producerBinding;result=$result;result_payload=$producerBinding.result_payload;copy_rows=$rows;flat=$flatDoc;not_before_utc=$notBefore.ToString('o');source_file_trace=$helperTrace} }.GetNewClosure()
        $proofCopy={ param($flat); & $copyProofForFlat $flat 'pre-flat' }.GetNewClosure()
        $readinessGate={ param($flat,$proof); . $SecureHelper; $checked=[DateTimeOffset]::Parse([string](Get-V16Field $proof.result_payload 'checked_at_utc')).ToUniversalTime(); $runtime=[IO.File]::ReadAllText((Join-Path $App 'live_forward\super1_xm_mt5_demo_config.json'))|ConvertFrom-Json; $scheduleCommand=Get-Command -Name Get-Super1SecureMarketScheduleState -CommandType Function -ErrorAction Stop; $schedule=& $scheduleCommand -CheckedAt $checked -Runtime $runtime; $pin=$inv.runtime_pins.terminal.raw_json|ConvertFrom-Json; $rc=Get-V16ReadinessConditions -Flat $proof.result_payload -Expected $Expected -Now ([DateTimeOffset]::UtcNow) -Producer $proof.producer -SourceFileSha256 $proof.source_result_sha256 -TerminalPin $pin -MarketScheduleState $schedule -ExpectedEvidenceRoot (Split-Path -Parent $proof.source_result); $rc }.GetNewClosure()
        $rollover={ param($flat,$proof); $callId=$runId+'-rollover'; Write-V16JsonFile -Path (Join-Path $callDir ($callId+'.launch_intent.json')) -Value ([ordered]@{operation='rollover';run_id=$callId;intent_at_utc=[DateTimeOffset]::UtcNow.ToString('o');readiness_path=$flat.readiness_path;readiness_sha256=$flat.readiness_sha256}); $body="& '$($RolloverScript.Replace("'","''"))' -ReadinessEvidence '$($flat.readiness_path.Replace("'","''"))' -ExpectedReadinessSha256 '$($flat.readiness_sha256)'"; $exec=Invoke-V16Child -ScriptBody $body -LogDirectory $callDir -RunId $callId -TimeoutSeconds 900; $childExited=($exec.exit_observed -eq $true); if($exec.active_child -eq $true){return [pscustomobject]@{operation_accepted=$false;status='UNKNOWN';child_exited=$false;active_child=$true;executor=$exec}}; if($exec.capture_complete -ne $true -or $exec.process_succeeded -ne $true){return [pscustomobject]@{operation_accepted=$false;status='FAIL';child_exited=$childExited;executor=$exec}}; try { $adapter=Test-V16RolloverOutput -StdoutText ([IO.File]::ReadAllText($exec.stdout)) -ReadinessPath $flat.readiness_path -ReadinessSha256 $flat.readiness_sha256 -SourceFileSha256 ((Get-FileHash -LiteralPath $exec.stdout -Algorithm SHA256).Hash.ToLowerInvariant()) -MainTask $MainTask; $summary=$adapter.summary; $archive=[string](Get-V16Field $summary 'archive'); $state=Join-Path $Root 'state'; $stateGate=(Test-Path -LiteralPath (Join-Path $state 'campaign_lock.json') -PathType Leaf) -and -not(Test-Path -LiteralPath (Join-Path $state 'fatal_latch.json') -PathType Leaf) -and ($archive -and (Test-Path -LiteralPath $archive -PathType Container)); [pscustomobject]@{operation_accepted=($adapter.operation_accepted -and $stateGate);status=if($adapter.operation_accepted -and $stateGate){'PASS'}else{'FAIL'};child_exited=$childExited;executor=$exec;adapter=$adapter;state_gate=$stateGate;archive=$archive} } catch { [pscustomobject]@{operation_accepted=$false;status='FAIL';child_exited=$childExited;executor=$exec;adapter_error=$_.Exception.ToString()} } }.GetNewClosure()
        $stop={ try { $callId=$runId+'-stop-'+[Guid]::NewGuid().ToString('N'); Write-V16JsonFile -Path (Join-Path $callDir ($callId+'.launch_intent.json')) -Value ([ordered]@{operation='stop_assert';run_id=$callId;intent_at_utc=[DateTimeOffset]::UtcNow.ToString('o')}); $body=". '$($SecureHelper.Replace("'","''"))'; Stop-Super1SecureRuntime -Root '$Root' -MainTask '$MainTask' -WatchdogTask '$WatchdogTask'; Assert-Super1SecureStopped -Root '$Root' -MainTask '$MainTask' -WatchdogTask '$WatchdogTask'"; $exec=Invoke-V16Child -ScriptBody $body -LogDirectory $callDir -RunId $callId -TimeoutSeconds 120; [pscustomobject]@{status=if($exec.capture_complete -and $exec.process_succeeded){'PASS'}else{'FAIL'};active_child=$exec.active_child;executor=$exec} } catch { [pscustomobject]@{status='FAIL';error=$_.Exception.ToString()} } }.GetNewClosure()
        $post={ $callId=$runId+'-postflat'; Write-V16JsonFile -Path (Join-Path $callDir ($callId+'.launch_intent.json')) -Value ([ordered]@{operation='post_flat';run_id=$callId;intent_at_utc=[DateTimeOffset]::UtcNow.ToString('o')}); $body="& '$($FlatScript.Replace("'","''"))' -KeepStopped"; $exec=Invoke-V16Child -ScriptBody $body -LogDirectory $callDir -RunId $callId -TimeoutSeconds 300; if($exec.capture_complete -ne $true -or $exec.process_succeeded -ne $true){return [pscustomobject]@{operation_accepted=$false;status='FAIL';active_child=$exec.active_child;executor=$exec}}; $a=Test-V16FlatOutput -StdoutText ([IO.File]::ReadAllText($exec.stdout)) -Expected $Expected -SourceFileSha256 ((Get-FileHash -LiteralPath $exec.stdout -Algorithm SHA256).Hash.ToLowerInvariant()); $accepted=[pscustomobject]@{outer=$a;readiness_path=[string](Get-V16Field $a.document 'readiness_evidence');readiness_sha256=[string](Get-V16Field $a.document 'readiness_sha256');executor=$exec}; if(-not $a.operation_accepted){return [pscustomobject]@{operation_accepted=$false;status=$a.status;executor=$exec;adapter=$a}}; try{$postProof=& $copyProofForFlat $accepted 'post-flat'}catch{return [pscustomobject]@{operation_accepted=$false;status='FAIL';executor=$exec;adapter=$a;proof_error=$_.Exception.ToString()}}; [pscustomobject]@{operation_accepted=$true;status='PASS';executor=$exec;adapter=$a;proof=$postProof} }.GetNewClosure()
        $result=Invoke-V16RecoveryStateMachine -Preflight $preflight -Flat $flatOp -FlatAdapter $flatAdapter -ProofCopy $proofCopy -ReadinessGate $readinessGate -Rollover $rollover -StopAssert $stop -PostFlat $post
        Write-V16JsonFile -Path (Join-Path $attempt 'operator\controller-result.json') -Value $result
        $result | Add-Member NoteProperty audit_attempt $attempt
        return $result
    } finally {
        if($coordination){
            $active=($null -ne $result -and $result.active_child -eq $true)
            $countsForRecord=if($null -ne $result){$result.counts}else{[ordered]@{flat_calls=0;rollover_calls=0;post_flat_calls=0}}
            $resolved=($null -ne $result -and -not $active -and ([int]$countsForRecord.flat_calls -eq 0 -or $result.final_runtime_stopped -eq $true))
            $processRecords=@(); if($null -ne $result){foreach($candidate in @($flatResult,$result.rollover_result,$result.post_flat_result)){if($candidate -and (Test-V16HasProperty $candidate 'executor')){$processRecords+=@($candidate.executor)}}}
            $record=[ordered]@{schema_version=2;run_id=$runId;attempt=$attempt;completed_at_utc=[DateTimeOffset]::UtcNow.ToString('o');active_or_unknown_children=$active;resolved=$resolved;status=[string](if($null -ne $result){$result.stage}else{'FAILED_BEFORE_RESULT'});counts=$countsForRecord;process_records=$processRecords;result_summary=if($null -ne $result){[pscustomobject]@{stage=$result.stage;first_failure=$result.first_failure;final_runtime_stopped=$result.final_runtime_stopped}}else{$null}}
            try { Write-V16JsonFile -Path $coordination.unresolved_path -Value $record; $recordBytes=[IO.File]::ReadAllBytes($coordination.unresolved_path); $sha=[Security.Cryptography.SHA256]::Create(); try{$recordSha=([BitConverter]::ToString($sha.ComputeHash($recordBytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}; $historyPath=Join-Path $coordination.history_dir ('final-'+$recordSha+'.json'); if(-not(Test-Path -LiteralPath $historyPath -PathType Leaf)){[IO.File]::WriteAllBytes($historyPath,$recordBytes)}; if(-not([Linq.Enumerable]::SequenceEqual($recordBytes,[IO.File]::ReadAllBytes($historyPath)))){throw 'COORDINATION_FINAL_HISTORY_MISMATCH'} } finally { $coordination.lock.Dispose() }
        }
        if($lock){$lock.Dispose()}
    }
}

if ($Execute) {
    $root=if([string]::IsNullOrWhiteSpace($AuditRoot)){'C:\Super1AuditRecovery-V16-20260830T173056Z-c263b6ace527414da18a356d9b770aa0'}else{[IO.Path]::GetFullPath($AuditRoot)}
    try {
        if([string]::IsNullOrWhiteSpace($AuditAttempt)){throw 'AUDIT_ATTEMPT_REQUIRED'}
        $final=Invoke-V16RecoveryController -AuditRoot $root -AuditAttempt $AuditAttempt
        $final | ConvertTo-Json -Depth 20
        if($final.stage -ne 'SUCCESS' -or $final.final_runtime_stopped -ne $true){exit 1}
    } catch {
        [ordered]@{stage='FAILED';first_failure=$_.Exception.ToString();active_child=$false;final_runtime_stopped=$false;counts=[ordered]@{flat_calls=0;rollover_calls=0;post_flat_calls=0;smoke_calls=0;natural_observation_calls=0}} | ConvertTo-Json -Depth 20
        exit 1
    }
}
