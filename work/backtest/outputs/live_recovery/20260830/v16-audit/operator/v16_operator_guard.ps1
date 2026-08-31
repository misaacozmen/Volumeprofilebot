Set-StrictMode -Version Latest

$script:V16TrustedOwnerSids = @(
    'S-1-5-18',
    'S-1-5-32-544',
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
)
$script:V16AllowedUntrustedMask = [uint64]0x001200AD

function Convert-V16UnsignedMask {
    param([Parameter(Mandatory = $true)][int64]$RawMask)
    if ($RawMask -lt -2147483648 -or $RawMask -gt 4294967295) { throw "MASK_OUT_OF_RANGE:$RawMask" }
    if ($RawMask -lt 0) { return [uint64]($RawMask + 4294967296) }
    return [uint64]$RawMask
}

function Convert-V16GenericMask {
    param([Parameter(Mandatory = $true)][int64]$UnsignedMask)
    [uint64]$u = Convert-V16UnsignedMask ([int64]$UnsignedMask)
    [uint64]$normalized = $u -band [uint64]268435455
    if (($u -band [uint64]2147483648) -ne 0) { $normalized = $normalized -bor [uint64]1179785 }
    if (($u -band [uint64]1073741824) -ne 0) { $normalized = $normalized -bor [uint64]1179926 }
    if (($u -band [uint64]536870912) -ne 0) { $normalized = $normalized -bor [uint64]1179808 }
    if (($u -band [uint64]268435456) -ne 0) { $normalized = $normalized -bor [uint64]2032127 }
    $normalized
}

function Format-V16Hex32 {
    param([Parameter(Mandatory = $true)][int64]$Value)
    '0x{0:X8}' -f ([uint64]($Value -band 4294967295L))
}

function Get-V16RawAceReport {
    param([Parameter(Mandatory = $true)][System.Security.AccessControl.RawSecurityDescriptor]$Descriptor)
    if ($null -eq $Descriptor.DiscretionaryAcl) {
        throw 'NULL_DACL'
    }
    if ($Descriptor.DiscretionaryAcl.Count -eq 0) {
        return @([pscustomobject]@{ decision = 'EMPTY_DACL_NO_CREATE_ACCESS'; reason = 'DACL has zero ACEs' })
    }
    $rows = [Collections.Generic.List[object]]::new()
    for ($i = 0; $i -lt $Descriptor.DiscretionaryAcl.Count; $i++) {
        $ace = $Descriptor.DiscretionaryAcl[$i]
        $rawFlags = [int]$ace.AceFlags
        $inheritOnly = (($rawFlags -band [int][System.Security.AccessControl.AceFlags]::InheritOnly) -ne 0)
        $sid = if ($ace -is [System.Security.AccessControl.CommonAce]) { $ace.SecurityIdentifier.Value } else { 'UNKNOWN' }
        $aceType = [int]$ace.AceType
        $typeName = $ace.AceType.ToString()
        $isInherited = (($rawFlags -band [int][System.Security.AccessControl.AceFlags]::Inherited) -ne 0)
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
        if (($rawFlags -band [int][System.Security.AccessControl.AceFlags]::ContainerInherit) -ne 0) { $inheritance = $inheritance -bor [System.Security.AccessControl.InheritanceFlags]::ContainerInherit }
        if (($rawFlags -band [int][System.Security.AccessControl.AceFlags]::ObjectInherit) -ne 0) { $inheritance = $inheritance -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit }
        $propagation = [System.Security.AccessControl.PropagationFlags]::None
        if ($inheritOnly) { $propagation = $propagation -bor [System.Security.AccessControl.PropagationFlags]::InheritOnly }
        if (($rawFlags -band [int][System.Security.AccessControl.AceFlags]::NoPropagateInherit) -ne 0) { $propagation = $propagation -bor [System.Security.AccessControl.PropagationFlags]::NoPropagateInherit }

        $supportedStandard = $ace -is [System.Security.AccessControl.CommonAce] -and -not $ace.IsCallback -and $aceType -in @(0, 1)
        $knownFlags = (($rawFlags -band 0xE0) -eq 0)
        if (-not $supportedStandard -or -not $knownFlags) {
            $rows.Add([pscustomobject]@{ index=$i; sid=$sid; ace_type=$typeName; raw_flags=$rawFlags; is_inherited=$isInherited; inherit_only=$inheritOnly; raw_mask_hex=if($ace -is [System.Security.AccessControl.CommonAce]){Format-V16Hex32 (Convert-V16UnsignedMask ([int64]$ace.AccessMask))}else{'NOT_APPLICABLE'}; normalized_mask_hex='NOT_APPLICABLE'; outside_bits_hex='NOT_APPLICABLE'; decision='FAIL'; reason='Unsupported, callback, unknown ACE type, or unknown ACE flags'; inheritance=$inheritance.ToString(); propagation=$propagation.ToString() }); continue
        }
        if ($inheritOnly) {
            $rows.Add([pscustomobject]@{ index=$i; sid=$sid; ace_type=$typeName; raw_flags=$rawFlags; is_inherited=$isInherited; inherit_only=$true; raw_mask_hex=(Format-V16Hex32 (Convert-V16UnsignedMask ([int64]$ace.AccessMask))); normalized_mask_hex='NOT_APPLICABLE'; outside_bits_hex='NOT_APPLICABLE'; decision='IGNORED_INHERIT_ONLY'; reason='Raw AceFlags.InheritOnly (8) excludes ACE from parent access'; inheritance=$inheritance.ToString(); propagation=$propagation.ToString() }); continue
        }
        $raw = Convert-V16UnsignedMask ([int64]$ace.AccessMask)
        $normalized = Convert-V16GenericMask $raw
        $outside = $normalized -band [uint64]4293787474
        $trusted = $sid -in $script:V16TrustedOwnerSids
        $decision = 'PASS'; $reason = 'Deny recorded; no allow policy violation'
        if ($aceType -eq 0 -and -not $trusted -and $outside -ne 0) { $decision = 'FAIL'; $reason = 'Untrusted effective Allow has bits outside 0x001200AD' }
        $rows.Add([pscustomobject]@{ index=$i; sid=$sid; ace_type=$typeName; raw_flags=$rawFlags; is_inherited=$isInherited; inherit_only=$false; raw_mask_hex=(Format-V16Hex32 $raw); normalized_mask_hex=(Format-V16Hex32 $normalized); outside_bits_hex=(Format-V16Hex32 $outside); decision=$decision; reason=$reason; inheritance=$inheritance.ToString(); propagation=$propagation.ToString() })
    }
    return $rows.ToArray()
}

function Assert-V16ParentGuard {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $acl = Get-Acl -LiteralPath $full -ErrorAction Stop
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($acl.Sddl)
    if ($null -eq $raw.Owner) { throw 'MISSING_OWNER' }
    $ownerSid = $raw.Owner.Value
    if ($ownerSid -notin $script:V16TrustedOwnerSids) { throw "UNTRUSTED_OWNER:$ownerSid" }
    $aces = @(Get-V16RawAceReport -Descriptor $raw)
    if ($aces.Count -eq 1 -and $aces[0].decision -eq 'EMPTY_DACL_NO_CREATE_ACCESS') { throw 'EMPTY_DACL_NO_CREATE_ACCESS' }
    $failures = @($aces | Where-Object decision -eq 'FAIL')
    [pscustomobject]@{ path=$full; owner_sid=$ownerSid; sddl=$acl.Sddl; protected=$acl.AreAccessRulesProtected; ace_decisions=$aces; status=if($failures.Count -eq 0){'PASS'}else{'FAIL'}; failure_count=$failures.Count }
}

function Assert-V16AuditRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'AUDIT_ROOT_REPARSE' }
    $acl = Get-Acl -LiteralPath $full -ErrorAction Stop
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($acl.Sddl)
    if ($null -eq $raw.Owner -or $raw.Owner.Value -notin @('S-1-5-18','S-1-5-32-544')) { throw 'AUDIT_ROOT_OWNER_INVALID' }
    [void](Assert-V16AuditRootDescriptor -Descriptor $raw)
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 2) { throw 'AUDIT_ROOT_DACL_SHAPE_INVALID' }
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $matches = @($rules | Where-Object { $_.IdentityReference.Value -eq $sid -and $_.AccessControlType -eq 'Allow' -and -not $_.IsInherited -and [uint64]$_.FileSystemRights -eq [uint64]0x001F01FF -and $_.InheritanceFlags -eq ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit) -and $_.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None })
        if ($matches.Count -ne 1) { throw "AUDIT_ROOT_ACE_INVALID:$sid" }
    }
    [pscustomobject]@{path=$full;owner_sid=$raw.Owner.Value;sddl=$acl.Sddl;protected=$acl.AreAccessRulesProtected;ace_count=$rules.Count;status='PASS'}
}

function Assert-V16AuditRootDescriptor {
    param([Parameter(Mandatory = $true)][System.Security.AccessControl.RawSecurityDescriptor]$Descriptor)
    if ($null -eq $Descriptor.Owner -or $Descriptor.Owner.Value -notin @('S-1-5-18','S-1-5-32-544')) { throw 'AUDIT_ROOT_OWNER_INVALID' }
    if ($null -eq $Descriptor.DiscretionaryAcl -or $Descriptor.DiscretionaryAcl.Count -ne 2) { throw 'AUDIT_ROOT_RAW_DACL_SHAPE_INVALID' }
    $seen=@{}
    foreach ($rawAce in $Descriptor.DiscretionaryAcl) {
        if ($rawAce -isnot [System.Security.AccessControl.CommonAce] -or $rawAce.IsCallback -or [int]$rawAce.AceType -ne 0 -or [int]$rawAce.AceFlags -ne 3 -or (Convert-V16UnsignedMask ([int64]$rawAce.AccessMask)) -ne [uint64]2032127) { throw 'AUDIT_ROOT_RAW_ACE_INVALID' }
        $sid=$rawAce.SecurityIdentifier.Value
        if ($sid -notin @('S-1-5-18','S-1-5-32-544') -or $seen.ContainsKey($sid)) { throw 'AUDIT_ROOT_RAW_SID_INVALID' }
        $seen[$sid]=$true
    }
    if (-not $seen.ContainsKey('S-1-5-18') -or -not $seen.ContainsKey('S-1-5-32-544')) { throw 'AUDIT_ROOT_RAW_SID_MISSING' }
    [pscustomobject]@{status='PASS';ace_count=2}
}
