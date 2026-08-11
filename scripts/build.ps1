param(
    [switch]$Console,
    [switch]$OneFile,
    [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$repo = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location $repo

$venv = Join-Path $repo ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    & $PythonLauncher -3.11 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Python 3.11 virtual environment." }
}

$actual = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($actual.Trim() -ne "3.11.9") {
    throw "Build requires CPython 3.11.9; found $actual"
}

& $python -m pip install --disable-pip-version-check --require-hashes -r (Join-Path $repo "requirements-lock.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

& (Join-Path $PSScriptRoot "run_tests.ps1") -PythonExe $python
if ($LASTEXITCODE -ne 0) { throw "Tests failed; build stopped." }

$buildDir = [System.IO.Path]::GetFullPath((Join-Path $repo "build"))
$distDir = [System.IO.Path]::GetFullPath((Join-Path $repo "dist"))
if (-not $buildDir.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe build path." }
if (-not $distDir.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe dist path." }
if (Test-Path -LiteralPath $buildDir) { Remove-Item -LiteralPath $buildDir -Recurse -Force }
if (Test-Path -LiteralPath $distDir) { Remove-Item -LiteralPath $distDir -Recurse -Force }

$env:ARUBA_BUILD_CONSOLE = if ($Console) { "1" } else { "0" }
$env:ARUBA_BUILD_ONEFILE = if ($OneFile) { "1" } else { "0" }
& $python -m PyInstaller --noconfirm --clean (Join-Path $repo "ArubaMiniDashboard.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$name = if ($Console) { "ArubaMiniDashboardConsole" } else { "ArubaMiniDashboard" }
$releaseRoot = if ($OneFile) { $distDir } else { Join-Path $distDir $name }
if (-not (Test-Path -LiteralPath $releaseRoot)) { throw "Build output missing: $releaseRoot" }

if (-not $OneFile) {
    Copy-Item -LiteralPath (Join-Path $repo "config.example.json") -Destination (Join-Path $releaseRoot "config.example.json")
    Copy-Item -LiteralPath (Join-Path $repo "docs\README.txt") -Destination (Join-Path $releaseRoot "README.txt")
    Copy-Item -LiteralPath (Join-Path $repo "docs\WINDOWS11_QA_CHECKLIST_KO.md") -Destination (Join-Path $releaseRoot "WINDOWS11_QA_CHECKLIST_KO.md")
}

$verifyArgs = @((Join-Path $PSScriptRoot "verify_release_package.py"), "--path", $releaseRoot, "--name", $name)
if ($OneFile) { $verifyArgs += "--one-file" }
& $python @verifyArgs
if ($LASTEXITCODE -ne 0) { throw "Release verification failed." }

$exe = if ($OneFile) { Join-Path $distDir "$name.exe" } else { Join-Path $releaseRoot "$name.exe" }
$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
Set-Content -LiteralPath (Join-Path $distDir "$name.sha256.txt") -Value "$hash  $name.exe" -Encoding ASCII
Write-Host "Build completed: $releaseRoot"
Write-Host "SHA-256: $hash"
