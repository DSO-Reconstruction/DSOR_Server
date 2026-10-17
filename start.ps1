param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ServerArgs
)

$ErrorActionPreference = 'Stop'
$ServerDir = $PSScriptRoot
Set-Location -LiteralPath $ServerDir

# Read the simple KEY=VALUE settings used by this project's .env file.
$EnvFile = Join-Path $ServerDir '.env'
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($Line in Get-Content -LiteralPath $EnvFile) {
        $Line = $Line.Trim()
        if (-not $Line -or $Line.StartsWith('#')) { continue }
        $Separator = $Line.IndexOf('=')
        if ($Separator -le 0) { continue }
        $Name = $Line.Substring(0, $Separator).Trim()
        $Value = $Line.Substring($Separator + 1).Trim()
        [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    }
}

if (-not $env:DSO_PUBLIC_HOST) {
    throw "DSO_PUBLIC_HOST is missing. Set it in $EnvFile."
}

$RequiredFiles = @(
    'run.py',
    'data/db/static.db4',
    'data/events.json',
    'data/quests.xml',
    'data/catalogs/chest_contents.json',
    'DSOClient/__root.txt'
)
foreach ($RelativePath in $RequiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $ServerDir $RelativePath) -PathType Leaf)) {
        throw "Required server file is missing: $RelativePath"
    }
}
foreach ($RelativePath in @('data/maps', 'data/navigation')) {
    if (-not (Test-Path -LiteralPath (Join-Path $ServerDir $RelativePath) -PathType Container)) {
        throw "Required server directory is missing: $RelativePath"
    }
}
foreach ($RelativePath in @('data/db', 'data/logs', 'data/diagnostics', 'data/state')) {
    New-Item -ItemType Directory -Path (Join-Path $ServerDir $RelativePath) -Force | Out-Null
}

$PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($env:DSO_PYTHON) {
    $PythonPath = $env:DSO_PYTHON
    & $PythonPath -c 'import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize("P") == 8 else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw "This server requires 64-bit Python 3.12 or newer. Check DSO_PYTHON ($PythonPath)."
    }
    Write-Host "Starting DSO server on $env:COMPUTERNAME and advertising $env:DSO_PUBLIC_HOST."
    & $PythonPath -u run.py @ServerArgs
} elseif ($PythonLauncher) {
    & py -3-64 -c 'import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize("P") == 8 else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw 'This server requires 64-bit Python 3.12 or newer. Install it with the Python launcher or set DSO_PYTHON in .env.'
    }
    Write-Host "Starting DSO server on $env:COMPUTERNAME and advertising $env:DSO_PUBLIC_HOST."
    & py -3-64 -u run.py @ServerArgs
} else {
    throw 'Python was not found. Install 64-bit Python 3.12 or newer, or set DSO_PYTHON in .env.'
}

exit $LASTEXITCODE
