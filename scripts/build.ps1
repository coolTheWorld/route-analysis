$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$SpecFile = Join-Path $ProjectRoot 'route-analysis.spec'

if (-not [Environment]::Is64BitProcess) {
    throw 'Use 64-bit Python to build the Windows x64 distribution.'
}
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw 'Virtual environment not found. Run scripts\bootstrap.cmd first.'
}
if (-not (Test-Path -LiteralPath $SpecFile -PathType Leaf)) {
    throw 'route-analysis.spec is missing.'
}

Push-Location $ProjectRoot
try {
    & $VenvPython -m pytest
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Build stopped.' }

    & $VenvPython -m ruff check route_analysis tests
    if ($LASTEXITCODE -ne 0) { throw 'Lint failed. Build stopped.' }

    & $VenvPython -m mypy route_analysis
    if ($LASTEXITCODE -ne 0) { throw 'Type checking failed. Build stopped.' }

    & $VenvPython -m PyInstaller --clean --noconfirm $SpecFile
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    $Executable = Join-Path $ProjectRoot 'dist\RouteAnalysis\RouteAnalysis.exe'
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw 'Build finished but RouteAnalysis.exe was not found.'
    }
    & $Executable --smoke-test
    if ($LASTEXITCODE -ne 0) { throw 'Packaged smoke test failed.' }

    Write-Host "Build and smoke test passed: $Executable"
}
finally {
    Pop-Location
}
