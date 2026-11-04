param(
    [Parameter(Mandatory=$true)][string]$StateRoot,
    [Parameter(Mandatory=$true)][string]$Database,
    [Parameter(Mandatory=$true)][string]$KeyFile,
    [Parameter(Mandatory=$true)][string]$ClientExe,
    [string]$MediaRoot,
    [string]$AccountAlias='account',
    [string]$ApplicationRoot=$PSScriptRoot
)
$ErrorActionPreference='Stop'
function Full([string]$Path){[IO.Path]::GetFullPath($Path).TrimEnd('\')}
function Overlap([string]$First,[string]$Second){
    $a=(Full $First)+'\';$b=(Full $Second)+'\'
    $a.StartsWith($b,[StringComparison]::OrdinalIgnoreCase) -or $b.StartsWith($a,[StringComparison]::OrdinalIgnoreCase)
}
function Reject-ReparseParents([string]$Path){
    $current=Full $Path
    while($current){
        if((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Use a local path without junctions or symbolic links for initial configuration.'}
        $next=Split-Path -Parent $current;if($next -eq $current){break};$current=$next
    }
}
$state=Full $StateRoot
if(Test-Path -LiteralPath $state){throw 'Choose a new state directory. Existing files are never overwritten.'}
$databasePath=(Resolve-Path -LiteralPath $Database).Path
$keyPath=(Resolve-Path -LiteralPath $KeyFile).Path
$clientPath=(Resolve-Path -LiteralPath $ClientExe).Path
$applicationPath=(Resolve-Path -LiteralPath $ApplicationRoot).Path
foreach($path in @($state,$databasePath,$keyPath,$applicationPath)){Reject-ReparseParents $path}
if($AccountAlias -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$'){throw 'AccountAlias must be a simple local identifier.'}
if([Diagnostics.FileVersionInfo]::GetVersionInfo($clientPath).FileVersion -ne '9.9.19.34467'){throw 'This release only validates QQ 9.9.19.34467. Check compatibility before creating an archive.'}
$savedKey=[IO.File]::ReadAllBytes($keyPath)
if($savedKey.Length -ne 16){throw 'KeyFile must contain the authorized raw 16-byte source key. No key is discovered or printed.'}
if((Overlap $state (Split-Path -Parent $databasePath)) -or (Overlap $state $applicationPath) -or $state -eq (Full $keyPath)){throw 'Source, program and new state directories must be separate.'}
if($MediaRoot){$mediaPath=(Resolve-Path -LiteralPath $MediaRoot).Path;Reject-ReparseParents $mediaPath;if(Overlap $state $mediaPath){throw 'Media and state directories overlap.'}}
$manifestPath=Join-Path $applicationPath 'release_verification.json'
$manifest=Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if($manifest.schema -ne 'qq-next-release/1'){throw 'Use the complete verified portable program.'}
$program=Join-Path $state 'application'
[void][IO.Directory]::CreateDirectory($program)
foreach($item in $manifest.files){
    $source=Full (Join-Path $applicationPath $item.path);$target=Full (Join-Path $program $item.path)
    if(-not $source.StartsWith((Full $applicationPath)+'\',[StringComparison]::OrdinalIgnoreCase) -or -not $target.StartsWith((Full $program)+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe program manifest path.'}
    if((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256){throw 'Program hash mismatch.'}
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $target));[IO.File]::Copy($source,$target,$false)
    if((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256){throw 'Program copy hash mismatch.'}
}
[IO.File]::Copy($manifestPath,(Join-Path $program 'release_verification.json'),$false)
$private=Join-Path $state 'private';[void][IO.Directory]::CreateDirectory($private)
[IO.File]::WriteAllBytes((Join-Path $private 'source.key'),$savedKey);[Array]::Clear($savedKey,0,$savedKey.Length)
$identity=[Guid]::NewGuid().ToString()
$source=@{id='source-main';account_key=$AccountAlias;account_label=$AccountAlias;database=$databasePath;key_file='private/source.key'}
if($MediaRoot){$source.media_root=$mediaPath}
$config=@{schema='qq-next-config/2';state_id=$identity;application_directory='application';sources=@($source);legacy_sources=@();history_packages=@();recovery_packages=@();
    auto_capture=$false;auto_backup=$true;poll_seconds=3;capture_seconds=60;media_seconds=1800;full_verify_seconds=86400;backup_seconds=3600;client_exe=$clientPath;client_version='9.9.19.34467'}
$utf8=[Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $state 'OWNER.json'),(@{schema='qq-next-state/1';id=$identity}|ConvertTo-Json),$utf8)
[IO.File]::WriteAllText((Join-Path $state 'config.private.json'),($config|ConvertTo-Json -Depth 12),$utf8)
$core=Join-Path $program 'backend/QQArchiveCore/QQArchiveCore.exe'
$report=Join-Path $state 'setup-result.private.json'
$captureOutput=& $core --state $state --report $report capture --no-media 2>&1
if($LASTEXITCODE -ne 0){throw 'Initial capture was not confirmed. Existing sources are unchanged. Inspect setup-result.private.json locally; this new state is kept for diagnosis.'}
Write-Output 'Archive created and first capture verified. Open application/QQArchiveNext.exe inside the new state directory. Enable continuous capture in the GUI when ready.'
