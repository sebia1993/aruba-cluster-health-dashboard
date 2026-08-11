param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
if (-not $PythonExe) { $PythonExe = Join-Path $repo ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python virtual environment not found." }

$env:PYTHONUTF8 = "1"
$sourcePath = Join-Path $repo "src"
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $sourcePath
} else {
    $env:PYTHONPATH = "$sourcePath;$env:PYTHONPATH"
}
& $PythonExe -m aruba_mini_dashboard.main --demo
exit $LASTEXITCODE
