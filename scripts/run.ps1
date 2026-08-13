$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw 'Virtual environment not found. Run scripts\bootstrap.cmd first.'
}

Push-Location $ProjectRoot
try {
    & $VenvPython -m route_analysis
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
