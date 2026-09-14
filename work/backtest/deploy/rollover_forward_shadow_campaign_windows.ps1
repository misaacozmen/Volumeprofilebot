[CmdletBinding()]
param([string]$Root = "C:\ForwardShadow")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (
    [string]$PSVersionTable.PSEdition -cne "Desktop" -or
    [int]$PSVersionTable.PSVersion.Major -ne 5
) { throw "ForwardShadow rollover requires Windows PowerShell 5.1." }

$OriginalPSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Process")
$OriginalPythonEnvironment = @{}
foreach ($name in @(
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE",
    "XM_MT5_SERVER"
)) {
    $OriginalPythonEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    [Environment]::SetEnvironmentVariable($name, $null, "Process")
}

$Root = [IO.Path]::GetFullPath($Root)
$App = [IO.Path]::GetFullPath((Join-Path $Root "app"))
$State = [IO.Path]::GetFullPath((Join-Path $Root "state"))
$LegacyState = [IO.Path]::GetFullPath(
    (Join-Path $App "outputs\xm_mt5_forward\nq3m_spx5m")
)
$ArchiveRoot = [IO.Path]::GetFullPath((Join-Path $Root "archive"))
$CapitalCandidate = [IO.Path]::GetFullPath(
    (Join-Path $Root "run_capital_forward.py.candidate")
)
$CapitalTarget = [IO.Path]::GetFullPath((Join-Path $App "scripts\run_capital_forward.py"))
$XmCandidate = [IO.Path]::GetFullPath((Join-Path $Root "run_xm_mt5_forward.py.candidate"))
$XmTarget = [IO.Path]::GetFullPath((Join-Path $App "scripts\run_xm_mt5_forward.py"))
$LauncherCandidate = [IO.Path]::GetFullPath(
    (Join-Path $Root "run_forward_shadow_windows.ps1.candidate")
)
$LauncherTarget = [IO.Path]::GetFullPath(
    (Join-Path $App "deploy\run_forward_shadow_windows.ps1")
)
$RuntimeTarget = [IO.Path]::GetFullPath(
    (Join-Path $App "live_forward\xm_mt5_demo_config.json")
)
$FlatCheckScript = [IO.Path]::GetFullPath(
    (Join-Path $App "deploy\check_forward_flat_windows.ps1")
)
$TrustedScript = [IO.Path]::GetFullPath(
    (Join-Path $App "deploy\rollover_forward_shadow_campaign_windows.ps1")
)
$CurrentScript = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
$Python = [IO.Path]::GetFullPath((Join-Path $Root "venv311\Scripts\python.exe"))
$TerminalFile = [IO.Path]::GetFullPath((Join-Path $Root "mt5-terminal.txt"))
$ServerFile = [IO.Path]::GetFullPath((Join-Path $Root "xm-server.txt"))
$MainTask = "ForwardShadowXM"
$WatchdogTask = "ForwardShadowWatchdog"
$WatchdogStatus = [IO.Path]::GetFullPath((Join-Path $Root "watchdog_status.json"))
$WatchdogHealth = [IO.Path]::GetFullPath((Join-Path $State "health.json"))
$WindowsPowerShellExe = [IO.Path]::GetFullPath((Join-Path $PSHOME "powershell.exe"))
$RunId = [Guid]::NewGuid().ToString("N")
$StateNext = [IO.Path]::GetFullPath((Join-Path $Root "state.next.$RunId"))

if (-not $CurrentScript.Equals($TrustedScript, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run ForwardShadow rollover only from the protected app deploy directory."
}
if (-not (Test-Path -LiteralPath $FlatCheckScript -PathType Leaf)) {
    throw "ForwardShadow sealed readiness script is missing."
}
. $FlatCheckScript -LibraryOnly -Root $Root

# The readiness helper defines globals for its own transaction; restore rollover globals.
$CurrentScript = [IO.Path]::GetFullPath([string]$MyInvocation.MyCommand.Path)
$MainTask = "ForwardShadowXM"
$WatchdogTask = "ForwardShadowWatchdog"
$Terminal = (Get-Content -LiteralPath $TerminalFile -Raw).Trim()
$Server = (Get-Content -LiteralPath $ServerFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($Server)) {
    throw "ForwardShadow XM server setting is missing."
}
$taskBinding = Assert-ForwardCanonicalTaskPair
$mainTaskDefinition = $taskBinding.Main
$watchdogTaskDefinition = $taskBinding.Watchdog
$runnerIdentity = [string]$taskBinding.RunnerIdentity
$runnerSid = [string]$taskBinding.RunnerSid
$originalMainTaskXml = (Export-ScheduledTask -TaskName $MainTask -ErrorAction Stop).Trim()
$originalWatchdogTaskXml = (Export-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop).Trim()

function Get-ForwardSha256Lower {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ForwardTreeFingerprint {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $null }
    $records = New-Object Collections.Generic.List[string]
    $base = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    foreach ($item in Get-ChildItem -LiteralPath $base -Recurse -Force | Sort-Object FullName) {
        $relative = $item.FullName.Substring($base.Length).TrimStart('\')
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "ForwardShadow rollover tree contains a reparse point: $($item.FullName)"
        }
        if ($item.PSIsContainer) {
            $records.Add("D|$relative")
        } else {
            $records.Add("F|$relative|$($item.Length)|$(Get-ForwardSha256Lower -Path $item.FullName)")
        }
    }
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes(
        [string]::Join("`n", $records)
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-ForwardTreeFingerprint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ((Get-ForwardTreeFingerprint -Path $Path) -cne $Expected) {
        throw "$Label fingerprint mismatch."
    }
}

function Copy-ForwardFileCreateNew {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $sourceHash = Get-ForwardSha256Lower -Path $Source
    $input = [IO.File]::Open($Source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $output = [IO.File]::Open(
            $Destination,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try { $input.CopyTo($output); $output.Flush($true) }
        finally { $output.Dispose() }
    }
    finally { $input.Dispose() }
    if ((Get-ForwardSha256Lower -Path $Destination) -cne $sourceHash) {
        throw "ForwardShadow rollover copy hash mismatch: $Destination"
    }
}

function Protect-ForwardStateTreeExact {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RunnerSid
    )
    Assert-NoForwardReparsePoint -Path $Path
    Set-ForwardExactDirectoryAcl -Path $Path -RunnerSid $RunnerSid -RunnerModify
    if (@(Get-ChildItem -LiteralPath $Path -Force).Count -ne 0) {
        $resetOutput = @(& $IcaclsExe (Join-Path $Path "*") /reset /T /C /Q 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "ForwardShadow state ACL inheritance reset failed: $($resetOutput -join ' ')"
        }
    }
    $ownerOutput = @(& $IcaclsExe $Path /setowner "*S-1-5-18" /T /C /Q 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "ForwardShadow state owner seal failed: $($ownerOutput -join ' ')"
    }
    Set-ForwardExactDirectoryAcl -Path $Path -RunnerSid $RunnerSid -RunnerModify
    Assert-ForwardExactAcl -Path $Path -RunnerSid $RunnerSid -RunnerModify -Directory
    foreach ($item in Get-ChildItem -LiteralPath $Path -Recurse -Force) {
        $acl = Get-Acl -LiteralPath $item.FullName
        if (
            $acl.AreAccessRulesProtected -or
            [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne "S-1-5-18" -or
            @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])).Count -ne 0
        ) { throw "ForwardShadow state descendant ACL is not inherited and SYSTEM-owned: $($item.FullName)" }
    }
}

function Assert-ForwardTaskXmlUnchanged {
    if (
        (Export-ScheduledTask -TaskName $MainTask -ErrorAction Stop).Trim() -cne $originalMainTaskXml -or
        (Export-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop).Trim() -cne $originalWatchdogTaskXml
    ) { throw "ForwardShadow task XML changed during rollover." }
}

function Wait-ForwardFreshState {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][DateTimeOffset]$NotBefore,
        [Parameter(Mandatory = $true)][string]$ExpectedState,
        [string]$ExpectedMainTask = ""
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
    do {
        $taskState = [string](Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State
        if (
            $taskState -ceq "Running" -and
            (Test-Path -LiteralPath $Path -PathType Leaf) -and
            (Get-Item -LiteralPath $Path).LastWriteTimeUtc -ge $NotBefore.UtcDateTime
        ) {
            try {
                $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
                if (
                    [string]$record.state -ceq $ExpectedState -and
                    (
                        [string]::IsNullOrWhiteSpace($ExpectedMainTask) -or
                        [string]$record.main_task -ceq $ExpectedMainTask
                    )
                ) { return }
            }
            catch {}
        }
        Start-Sleep -Seconds 2
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "ForwardShadow task $TaskName did not publish fresh $ExpectedState state."
}

foreach ($path in @(
    $App, $State, $LegacyState, $ArchiveRoot, $CapitalCandidate, $CapitalTarget,
    $XmCandidate, $XmTarget, $LauncherCandidate, $LauncherTarget, $RuntimeTarget,
    $FlatCheckScript, $Python, $TerminalFile, $WatchdogStatus, $WatchdogHealth,
    $StateNext
)) {
    if (-not $path.StartsWith(
        ($Root.TrimEnd('\') + '\'),
        [StringComparison]::OrdinalIgnoreCase
    )) { throw "Unsafe ForwardShadow rollover path: $path" }
}
foreach ($path in @(
    $App, $ArchiveRoot, $CapitalCandidate, $CapitalTarget, $XmCandidate, $XmTarget,
    $LauncherCandidate, $LauncherTarget, $RuntimeTarget, $FlatCheckScript, $Python,
    $TerminalFile
)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required ForwardShadow rollover path is missing: $path"
    }
}
if (Test-Path -LiteralPath $StateNext) {
    throw "ForwardShadow unique next-state path already exists."
}
$canonicalLock = Test-Path -LiteralPath (Join-Path $State "campaign_lock.json") -PathType Leaf
$legacyLock = Test-Path -LiteralPath (Join-Path $LegacyState "campaign_lock.json") -PathType Leaf
if ($canonicalLock -and $legacyLock) {
    throw "ForwardShadow has duplicate canonical and legacy campaign state."
}
if (-not $canonicalLock -and -not $legacyLock) {
    throw "ForwardShadow current campaign state was not found."
}
$CurrentState = if ($canonicalLock) { $State } else { $LegacyState }
$CanonicalPlaceholderPresent = $legacyLock -and (Test-Path -LiteralPath $State -PathType Container)
if ($CanonicalPlaceholderPresent -and @(Get-ChildItem -LiteralPath $State -Force).Count -ne 0) {
    throw "ForwardShadow canonical placeholder is not empty."
}
$currentStateFingerprint = Get-ForwardTreeFingerprint -Path $CurrentState

$syntaxValidator = @'
import sys
from pathlib import Path
for name in sys.argv[1:]:
    source = Path(name).read_text(encoding="utf-8")
    compile(source, name, "exec")
'@
$syntaxValidator | & $Python -I -E -B - $CapitalCandidate $XmCandidate
if ($LASTEXITCODE -ne 0) { throw "ForwardShadow candidate syntax validation failed." }
$launcherTokens = $null
$launcherErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $LauncherCandidate,
    [ref]$launcherTokens,
    [ref]$launcherErrors
)
if (@($launcherErrors).Count -ne 0) {
    throw "ForwardShadow launcher candidate has PowerShell syntax errors."
}
$launcherSource = Get-Content -LiteralPath $LauncherCandidate -Raw
foreach ($requiredContract in @(
    'broker-probe-request.json',
    'broker-probe-results',
    '-I -E -B',
    '--output-root (Join-Path $Root "state")'
)) {
    if ($launcherSource.IndexOf($requiredContract, [StringComparison]::Ordinal) -lt 0) {
        throw "ForwardShadow launcher candidate lacks the sealed runtime contract: $requiredContract"
    }
}
$runtime = Get-Content -LiteralPath $RuntimeTarget -Raw | ConvertFrom-Json
if (
    [int]$runtime.account_login -le 0 -or
    [string]::IsNullOrWhiteSpace([string]$runtime.expected_server) -or
    [string]::IsNullOrWhiteSpace([string]$runtime.expected_company)
) { throw "ForwardShadow runtime has an unexpected broker identity." }

$stamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$archive = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot "campaign-$stamp-$RunId"))
$archivedState = [IO.Path]::GetFullPath((Join-Path $archive "state.previous"))
$archivedLegacyState = [IO.Path]::GetFullPath((Join-Path $archive "legacy-state.previous"))
$archivedPlaceholder = [IO.Path]::GetFullPath((Join-Path $archive "canonical-placeholder"))
$candidatePairs = @(
    [pscustomobject]@{ Source = $CapitalCandidate; Staged = (Join-Path $archive "run_capital_forward.py.candidate"); Target = $CapitalTarget; Backup = (Join-Path $archive "run_capital_forward.py.previous"); CandidateMoved = $false; TargetMoved = $false; NewDeployed = $false },
    [pscustomobject]@{ Source = $XmCandidate; Staged = (Join-Path $archive "run_xm_mt5_forward.py.candidate"); Target = $XmTarget; Backup = (Join-Path $archive "run_xm_mt5_forward.py.previous"); CandidateMoved = $false; TargetMoved = $false; NewDeployed = $false },
    [pscustomobject]@{ Source = $LauncherCandidate; Staged = (Join-Path $archive "run_forward_shadow_windows.ps1.candidate"); Target = $LauncherTarget; Backup = (Join-Path $archive "run_forward_shadow_windows.ps1.previous"); CandidateMoved = $false; TargetMoved = $false; NewDeployed = $false }
)
$archiveCreated = $false
$stateActivated = $false
$oldCanonicalArchived = $false
$placeholderArchived = $false
$legacyCommitted = $false
$healthAccepted = $false
$tasksStarted = $false
$readinessLock = $null
$healthRaw = $null
$watchdogRaw = $null

try {
    Stop-ForwardRuntimeExact -RunnerSid $runnerSid -TerminalPath $Terminal
    Assert-ForwardTaskXmlUnchanged
    [void][IO.Directory]::CreateDirectory($archive)
    $archiveCreated = $true
    Protect-ForwardPrivateTree -Path $archive

    foreach ($pair in $candidatePairs) {
        [IO.File]::Move($pair.Source, $pair.Staged)
        $pair.CandidateMoved = $true
        [IO.File]::Move($pair.Target, $pair.Backup)
        $pair.TargetMoved = $true
        Copy-ForwardFileCreateNew -Source $pair.Staged -Destination $pair.Target
        $pair.NewDeployed = $true
        Set-ForwardExactFileAcl -Path $pair.Target -RunnerSid $runnerSid
        Assert-ForwardExactAcl -Path $pair.Target -RunnerSid $runnerSid
    }

    $readinessOutput = @(& $WindowsPowerShellExe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $FlatCheckScript `
        -KeepStopped `
        -Root $Root 2>&1)
    if ($LASTEXITCODE -ne 0 -or $readinessOutput.Count -eq 0) {
        throw "ForwardShadow fresh sealed readiness task failed: $($readinessOutput -join ' ')"
    }
    $readiness = $readinessOutput[-1] | ConvertFrom-Json
    $readinessPath = [IO.Path]::GetFullPath([string]$readiness.evidence_path)
    $expectedReadinessPrefix = [IO.Path]::GetFullPath(
        (Join-Path $Root "broker-probe-results")
    ).TrimEnd('\') + '\'
    if (
        [string]$readiness.state -cne "FORWARD_FLAT_PROVEN" -or
        -not [bool]$readiness.tasks_stopped -or
        [int]$readiness.open_orders -ne 0 -or
        [int]$readiness.open_positions -ne 0 -or
        -not $readinessPath.StartsWith(
            $expectedReadinessPrefix,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not (Test-Path -LiteralPath $readinessPath -PathType Leaf) -or
        (Get-ForwardSha256Lower -Path $readinessPath) -cne [string]$readiness.evidence_sha256
    ) { throw "ForwardShadow rollover rejected the fresh sealed readiness envelope." }
    $readinessLock = [IO.File]::Open(
        $readinessPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    Stop-ForwardRuntimeExact -RunnerSid $runnerSid -TerminalPath $Terminal
    Assert-ForwardTaskXmlUnchanged

    [void][IO.Directory]::CreateDirectory($StateNext)
    Set-ForwardExactDirectoryAcl -Path $StateNext -RunnerSid $runnerSid -RunnerModify
    [Environment]::SetEnvironmentVariable("XM_MT5_SERVER", $Server, "Process")
    & $Python -I -E -B $XmTarget --output-root $StateNext init |
        Out-File -LiteralPath (Join-Path $archive "new_campaign_lock.json") -Encoding utf8
    if (
        $LASTEXITCODE -ne 0 -or
        -not (Test-Path -LiteralPath (Join-Path $StateNext "campaign_lock.json") -PathType Leaf)
    ) { throw "ForwardShadow canonical campaign initialization failed." }
    Protect-ForwardStateTreeExact -Path $StateNext -RunnerSid $runnerSid

    if ($canonicalLock) {
        [IO.Directory]::Move($State, $archivedState)
        $oldCanonicalArchived = $true
        Protect-ForwardPrivateTree -Path $archivedState
    }
    elseif ($CanonicalPlaceholderPresent) {
        [IO.Directory]::Move($State, $archivedPlaceholder)
        $placeholderArchived = $true
        Protect-ForwardPrivateTree -Path $archivedPlaceholder
    }
    [IO.Directory]::Move($StateNext, $State)
    $stateActivated = $true
    Protect-ForwardStateTreeExact -Path $State -RunnerSid $runnerSid
    $readinessLock.Dispose()
    $readinessLock = $null

    $rolloutStarted = [DateTimeOffset]::UtcNow
    Start-ScheduledTask -TaskName $MainTask -ErrorAction Stop
    $tasksStarted = $true
    Wait-ForwardFreshState `
        -TaskName $MainTask `
        -Path $WatchdogHealth `
        -NotBefore $rolloutStarted `
        -ExpectedState "RUNNING"
    Start-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
    Wait-ForwardFreshState `
        -TaskName $WatchdogTask `
        -Path $WatchdogStatus `
        -NotBefore $rolloutStarted `
        -ExpectedState "HEALTHY" `
        -ExpectedMainTask $MainTask
    $healthRaw = Get-Content -LiteralPath $WatchdogHealth -Raw
    $watchdogRaw = Get-Content -LiteralPath $WatchdogStatus -Raw
    if (
        [string]($healthRaw | ConvertFrom-Json).state -cne "RUNNING" -or
        [string]($watchdogRaw | ConvertFrom-Json).state -cne "HEALTHY" -or
        -not (Test-Path -LiteralPath (Join-Path $State "campaign_lock.json") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $State "fatal_latch.json") -PathType Leaf)
    ) { throw "ForwardShadow post-rollover health gate failed." }
    $healthAccepted = $true

    if ($legacyLock) {
        Stop-ForwardRuntimeExact -RunnerSid $runnerSid -TerminalPath $Terminal
        $tasksStarted = $false
        Assert-ForwardTreeFingerprint `
            -Path $LegacyState `
            -Expected $currentStateFingerprint `
            -Label "Legacy ForwardShadow state before final commit"
        [IO.Directory]::Move($LegacyState, $archivedLegacyState)
        $legacyCommitted = $true
        Protect-ForwardPrivateTree -Path $archivedLegacyState
        Assert-ForwardTreeFingerprint `
            -Path $archivedLegacyState `
            -Expected $currentStateFingerprint `
            -Label "Archived legacy ForwardShadow state"
        $restartStarted = [DateTimeOffset]::UtcNow
        Start-ScheduledTask -TaskName $MainTask -ErrorAction Stop
        $tasksStarted = $true
        Wait-ForwardFreshState `
            -TaskName $MainTask `
            -Path $WatchdogHealth `
            -NotBefore $restartStarted `
            -ExpectedState "RUNNING"
        Start-ScheduledTask -TaskName $WatchdogTask -ErrorAction Stop
        Wait-ForwardFreshState `
            -TaskName $WatchdogTask `
            -Path $WatchdogStatus `
            -NotBefore $restartStarted `
            -ExpectedState "HEALTHY" `
            -ExpectedMainTask $MainTask
    }
    Assert-ForwardTaskXmlUnchanged
    Protect-ForwardPrivateTree -Path $archive
}
catch {
    $failure = $_
    $rollbackErrors = New-Object Collections.Generic.List[string]
    try {
        Stop-ForwardRuntimeExact -RunnerSid $runnerSid -TerminalPath $Terminal
        $tasksStarted = $false
    }
    catch { $rollbackErrors.Add("stopped-state gate: $($_.Exception.Message)") }
    if ($readinessLock) {
        try { $readinessLock.Dispose(); $readinessLock = $null }
        catch { $rollbackErrors.Add("readiness lock cleanup: $($_.Exception.Message)") }
    }

    if (-not $healthAccepted -and -not $legacyCommitted -and $rollbackErrors.Count -eq 0) {
        try {
            if ($stateActivated -and (Test-Path -LiteralPath $State -PathType Container)) {
                $failedState = Join-Path $archive "state.failed"
                [IO.Directory]::Move($State, $failedState)
                Protect-ForwardPrivateTree -Path $failedState
                $stateActivated = $false
            }
            if ($oldCanonicalArchived) {
                [IO.Directory]::Move($archivedState, $State)
                Protect-ForwardStateTreeExact -Path $State -RunnerSid $runnerSid
                Assert-ForwardTreeFingerprint `
                    -Path $State `
                    -Expected $currentStateFingerprint `
                    -Label "Rolled-back canonical ForwardShadow state"
                $oldCanonicalArchived = $false
            }
            elseif ($placeholderArchived) {
                [IO.Directory]::Move($archivedPlaceholder, $State)
                Protect-ForwardStateTreeExact -Path $State -RunnerSid $runnerSid
                $placeholderArchived = $false
            }
        }
        catch { $rollbackErrors.Add("state restore: $($_.Exception.Message)") }

        foreach ($pair in @($candidatePairs | Sort-Object { $_.Target.Length } -Descending)) {
            try {
                if ($pair.NewDeployed -and (Test-Path -LiteralPath $pair.Target -PathType Leaf)) {
                    [IO.File]::Delete($pair.Target)
                    $pair.NewDeployed = $false
                }
                if ($pair.TargetMoved -and (Test-Path -LiteralPath $pair.Backup -PathType Leaf)) {
                    [IO.File]::Move($pair.Backup, $pair.Target)
                    $pair.TargetMoved = $false
                }
                if ($pair.CandidateMoved -and (Test-Path -LiteralPath $pair.Staged -PathType Leaf)) {
                    [IO.File]::Move($pair.Staged, $pair.Source)
                    $pair.CandidateMoved = $false
                }
            }
            catch { $rollbackErrors.Add("file restore $($pair.Target): $($_.Exception.Message)") }
        }
        try {
            if (Test-Path -LiteralPath $StateNext -PathType Container) {
                Protect-ForwardPrivateTree -Path $StateNext
                Remove-Item -LiteralPath $StateNext -Recurse -Force
            }
        }
        catch { $rollbackErrors.Add("next-state cleanup: $($_.Exception.Message)") }
    }
    elseif ($healthAccepted -or $legacyCommitted) {
        $rollbackErrors.Add("new campaign passed health and entered final commit; it was retained stopped instead of reverting to the prior campaign")
    }
    try { Assert-ForwardTaskXmlUnchanged }
    catch { $rollbackErrors.Add("task XML verification: $($_.Exception.Message)") }
    $suffix = if ($rollbackErrors.Count) {
        "; rollback/commit status: $($rollbackErrors -join '; ')"
    } else { "; exact file/state rollback completed and tasks remain stopped" }
    throw "ForwardShadow rollover failed: $($failure.Exception.Message)$suffix"
}
finally {
    if ($readinessLock) { $readinessLock.Dispose() }
    [Environment]::SetEnvironmentVariable("PSModulePath", $OriginalPSModulePath, "Process")
    foreach ($name in $OriginalPythonEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name,
            [string]$OriginalPythonEnvironment[$name],
            "Process"
        )
    }
}

[ordered]@{
    state = "ROLLED_OVER_HEALTHY"
    archive = $archive
    canonical_state = $State
    prior_state = $CurrentState
    prior_state_sha256 = $currentStateFingerprint
    readiness_path = [string]$readiness.evidence_path
    readiness_sha256 = [string]$readiness.evidence_sha256
    readiness_nonce = [string]$readiness.evidence_nonce
    main_task_xml_unchanged = $true
    main = [string](Get-ScheduledTask -TaskName $MainTask).State
    watchdog = [string](Get-ScheduledTask -TaskName $WatchdogTask).State
} | ConvertTo-Json -Depth 6 -Compress
$healthRaw
$watchdogRaw
