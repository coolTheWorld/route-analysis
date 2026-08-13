$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    $Python = Get-Command py -ErrorAction SilentlyContinue
    if ($null -eq $Python) {
        throw 'Python launcher not found. Install Python 3.12 x64 first.'
    }
    & py -3.12 -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create the Python 3.12 virtual environment.'
    }
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip.' }

& $VenvPython -m pip install -r (Join-Path $ProjectRoot 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies.' }

& $VenvPython -m pip install --no-deps -e $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Failed to install the editable route-analysis package.' }

Write-Host 'Environment ready. Run scripts\run.cmd to start.'
