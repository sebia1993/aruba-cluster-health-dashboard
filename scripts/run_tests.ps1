param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (-not $PythonExe) {
    $PythonExe = Join-Path $repo ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python virtual environment not found: $PythonExe"
}

$env:PYTHONUTF8 = "1"
$env:QT_QPA_PLATFORM = if ($env:QT_QPA_PLATFORM) { $env:QT_QPA_PLATFORM } else { "offscreen" }
& $PythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed with exit code $LASTEXITCODE" }
& $PythonExe -m compileall -q src tests scripts
if ($LASTEXITCODE -ne 0) { throw "compileall failed with exit code $LASTEXITCODE" }
& $PythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed with exit code $LASTEXITCODE" }

Write-Host "ARUBA_MINI_DASHBOARD_TESTS_OK"
