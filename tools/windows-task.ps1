# Own task only; no production task or completion-hook changes.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('install','pause','resume','uninstall','status')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Python,
    [ValidatePattern('^HomeMediaFollow-[A-Za-z0-9_-]{1,64}$')][string]$TaskName='HomeMediaFollow-Worker',
    [switch]$DryRun
)
$ErrorActionPreference='Stop'
$Root=[IO.Path]::GetFullPath($Root)
$Python=[IO.Path]::GetFullPath($Python)
function Assert-NoReparse($Path) {
    $cursor=[IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item=Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse paths are not allowed for task installation or control' }
        }
        $parent=Split-Path -Parent $cursor
        if ($parent -eq $cursor) { break }
        $cursor=$parent
    }
}
Assert-NoReparse $Root
Assert-NoReparse $Python
Assert-NoReparse (Join-Path $Root 'config\shows.json')
Assert-NoReparse (Join-Path $Root 'secrets')
Assert-NoReparse (Join-Path $Root '.home-media-follow-instance')
foreach ($name in @('state','requests','reports')) { Assert-NoReparse (Join-Path $Root $name) }

if ($Root.Contains('"') -or $Python.Contains('"')) { throw 'Unsupported quoted path' }
$Owner='HomeMediaFollow isolated task v1 | '+$Root
$Args='-m home_media_follow --root "'+$Root+'" run'
$existing=Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Action -eq 'install') {
    if ($existing) { throw 'Task already exists; refusing overwrite' }
    if (-not (Test-Path -LiteralPath ($Root+'\.home-media-follow-instance') -PathType Leaf)) { throw 'Instance ownership marker missing' }
    if (-not (Test-Path -LiteralPath ($Root+'\config\shows.json') -PathType Leaf)) { throw 'Instance configuration missing' }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python executable missing' }
    $config=Get-Content -LiteralPath (Join-Path $Root 'config\shows.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $dataPaths=@()
    foreach ($key in @('media_root','staging','recovery_root','publisher_lock')) {
        $candidate=[IO.Path]::GetFullPath([string]$config.$key)
        if (-not $candidate.StartsWith($Root.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Automatic task installation requires instance-contained data paths' }
        Assert-NoReparse $candidate
        $protected=@('config','secrets','login','.venv','runtime')
        if ($key -ne 'publisher_lock') { $protected+=@('state','requests','reports') }
        foreach ($reserved in $protected) {
            $reservedPath=(Join-Path $Root $reserved).TrimEnd('\')
            if ($candidate.Equals($reservedPath,[StringComparison]::OrdinalIgnoreCase) -or $candidate.StartsWith($reservedPath+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Data path overlaps a protected instance directory' }
        }
        if ($key -eq 'publisher_lock') {
            $lockParent=Split-Path -Parent $candidate
            if ($lockParent.TrimEnd('\').Equals($Root.TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase)) { throw 'Publisher lock requires a dedicated subdirectory; instance root cannot be writable' }
            $dataPaths+=$lockParent
        } else { $dataPaths+=$candidate }
        if ($key -ne 'publisher_lock' -and (Test-Path -LiteralPath $candidate) -and -not (Test-Path -LiteralPath $candidate -PathType Container)) { throw 'Data directory is not a directory' }


    }

} else {
    if (-not $existing) { throw 'Task missing' }
    if ($existing.Description -cne $Owner -or @($existing.Actions).Count -ne 1 -or $existing.Actions[0].Execute -ine $Python -or $existing.Actions[0].Arguments -cne $Args) { throw 'Task ownership mismatch; refusing action' }
}
if ($DryRun) { @{ok=$true;dry_run=$true;action=$Action;task=$TaskName;root=$Root}|ConvertTo-Json -Compress; exit 0 }
if ($Action -eq 'install') {
    # Only dedicated instance paths receive service permissions. No recursive grants
    # on parent drives, user profiles or unrelated service installations.
    foreach ($item in @($Root)) {
        & icacls.exe $item /grant '*S-1-5-19:(OI)(CI)RX' /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Read ACL update failed' }
    }
    $writePaths=@((Join-Path $Root 'state'),(Join-Path $Root 'requests'),(Join-Path $Root 'reports'))+$dataPaths
    foreach ($path in $writePaths | Select-Object -Unique) {
        Assert-NoReparse $path
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Assert-NoReparse $path
        & icacls.exe $path /grant '*S-1-5-19:(OI)(CI)M' /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Writer ACL update failed' }
    }
    $secretPath=Join-Path $Root 'secrets'
    & icacls.exe $secretPath /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M' /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Secret ACL update failed' }
    $esc=[Security.SecurityElement]
    $start=(Get-Date).AddMinutes(1).ToString('s')
    $hour=(Get-Date).Date.AddHours((Get-Date).Hour+1).ToString('s')
    $xml=@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>$($esc::Escape($Owner))</Description></RegistrationInfo>
<Triggers><TimeTrigger><Repetition><Interval>PT15M</Interval></Repetition><StartBoundary>$start</StartBoundary><Enabled>true</Enabled></TimeTrigger><TimeTrigger><Repetition><Interval>PT1H</Interval></Repetition><StartBoundary>$hour</StartBoundary><Enabled>true</Enabled></TimeTrigger><BootTrigger><Enabled>true</Enabled><Delay>PT2M</Delay></BootTrigger></Triggers>
<Principals><Principal id="Worker"><UserId>S-1-5-19</UserId><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT6H</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
<Actions Context="Worker"><Exec><Command>$($esc::Escape($Python))</Command><Arguments>$($esc::Escape($Args))</Arguments><WorkingDirectory>$($esc::Escape($Root))</WorkingDirectory></Exec></Actions>
</Task>
"@
    Register-ScheduledTask -TaskName $TaskName -Xml $xml | Out-Null
} elseif ($Action -eq 'pause') { Disable-ScheduledTask -TaskName $TaskName | Out-Null
} elseif ($Action -eq 'resume') { Enable-ScheduledTask -TaskName $TaskName | Out-Null
} elseif ($Action -eq 'uninstall') { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
} else { $existing | Select-Object TaskName,State,Description | ConvertTo-Json -Compress; exit 0 }
@{ok=$true;action=$Action;task=$TaskName;media_deleted=$false}|ConvertTo-Json -Compress
