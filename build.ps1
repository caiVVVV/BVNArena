param(
    [string]$Python = 'python',
    [string]$Haxe = 'haxe',
    [string]$Output = (Join-Path $PSScriptRoot 'dist'),
    [string]$Entrants,
    [string]$CoreManager
)
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$Output = [IO.Path]::GetFullPath($Output)
if ([bool]$Entrants -ne [bool]$CoreManager) {
    throw 'Entrants and CoreManager must be supplied together'
}
$buildName = 'BVNArena-Setup'
$competitionPayload = @()
if ($Entrants) {
    $Entrants = (Resolve-Path -LiteralPath $Entrants).Path
    $CoreManager = (Resolve-Path -LiteralPath $CoreManager).Path
    if (!(Test-Path -LiteralPath $Entrants -PathType Container) -or
        !(Test-Path -LiteralPath $CoreManager -PathType Leaf) -or
        (Split-Path -Leaf $CoreManager) -ne 'BVNArena-Setup.exe') {
        throw 'Provide an entrants directory and the standard BVNArena-Setup.exe'
    }
    $buildName = 'BVNArena-Competition-Setup'
    $competitionPayload = @('--add-data', "$Entrants;Entrants", '--add-data', "$CoreManager;core")
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null

Push-Location (Join-Path $source 'bridge')
try {
    & $Haxe build.hxml
    if ($LASTEXITCODE -ne 0) { throw 'ArenaBridge compile failed' }
} finally { Pop-Location }

$csc = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$audioExe = Join-Path $Output 'ProcessAudio.exe'
& $csc /nologo /target:exe /optimize+ ("/out:"+ $audioExe) (Join-Path $source 'replay_assets/ProcessAudio.cs')
if ($LASTEXITCODE -ne 0) { throw 'ProcessAudio compile failed' }

$assets = Join-Path $Output 'payload'
$sdk = Join-Path $assets 'SDK'
New-Item -ItemType Directory -Force -Path $sdk | Out-Null
Get-ChildItem -LiteralPath (Join-Path $source 'SDK') -Recurse -File |
    Where-Object { $_.Extension -ne '.pyc' } |
    ForEach-Object {
        $relative = [IO.Path]::GetRelativePath((Join-Path $source 'SDK'), $_.FullName)
        $dest = Join-Path $sdk $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
    }
& $Python -m PyInstaller --noconfirm --onefile --windowed --noupx --name $buildName `
    --distpath $Output --workpath (Join-Path $Output 'build') --specpath $Output `
    --add-data ((Join-Path $source 'bridge/ArenaBridge.swf') + ';bridge') `
    --add-data ($audioExe + ';replay_assets') `
    --add-data ($sdk + ';SDK') @competitionPayload (Join-Path $source 'arena_app.py')
if ($LASTEXITCODE -ne 0) { throw 'Windows packaging failed' }
