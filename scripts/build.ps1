param(
    [switch]$Console,
    [switch]$OneFile,
    [switch]$CleanReleaseEnvironment,
    [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$repo = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location $repo

if ($OneFile) {
    throw "One-file builds are not a supported distribution: LGPL runtime replacement requires the persistent onedir _internal tree."
}

$venv = if ($CleanReleaseEnvironment) {
    Join-Path $repo ".release-venv"
}
else {
    Join-Path $repo ".venv"
}
$venvFull = [System.IO.Path]::GetFullPath($venv)
$expectedVenv = [System.IO.Path]::GetFullPath((Join-Path $repo $(if ($CleanReleaseEnvironment) { ".release-venv" } else { ".venv" })))
if (-not $venvFull.Equals($expectedVenv, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe virtual environment path: $venvFull"
}
if ($CleanReleaseEnvironment -and (Test-Path -LiteralPath $venvFull)) {
    Remove-Item -LiteralPath $venvFull -Recurse -Force
}
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $launcherName = [System.IO.Path]::GetFileName([string]$PythonLauncher)
    if ($launcherName -in @("py", "py.exe")) {
        & $PythonLauncher -3.11 -m venv $venv
    }
    else {
        & $PythonLauncher -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Python 3.11 virtual environment." }
}

$actual = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($actual.Trim() -ne "3.11.9") {
    throw "Build requires CPython 3.11.9; found $actual"
}
$architecture = & $python -c "import platform,struct; print(str(struct.calcsize(chr(80)) * 8) + chr(58) + platform.machine())"
if ($LASTEXITCODE -ne 0 -or -not $architecture.Trim().StartsWith("64:")) {
    throw "Build requires 64-bit CPython for the windows-x64 package; found $architecture"
}

& $python -m pip install --disable-pip-version-check --require-hashes -r (Join-Path $repo "requirements-lock.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

& $python -c "import importlib.metadata as m; n={d.metadata['Name'].lower().replace('_','-') for d in m.distributions()}; b=sorted(n & {'pyside6','pyside6-addons'}); assert not b, f'Unapproved Qt distributions installed: {b}'"
if ($LASTEXITCODE -ne 0) { throw "Unapproved Qt distributions are installed in the build environment." }

& $python (Join-Path $repo "scripts\collect_third_party_licenses.py") --check
if ($LASTEXITCODE -ne 0) { throw "Third-party license inventory is stale or incomplete." }

& $python (Join-Path $repo "scripts\collect_qt_runtime_notices.py") --check-notice
if ($LASTEXITCODE -ne 0) { throw "Qt third-party notice inventory is stale or incomplete." }

& $python (Join-Path $repo "scripts\collect_lgpl_runtime_sources.py") --check-manifest
if ($LASTEXITCODE -ne 0) { throw "Replaceable LGPL runtime manifest is stale or incomplete." }

& (Join-Path $PSScriptRoot "run_tests.ps1") -PythonExe $python
if ($LASTEXITCODE -ne 0) { throw "Tests failed; build stopped." }

$buildDir = [System.IO.Path]::GetFullPath((Join-Path $repo "build"))
$distDir = [System.IO.Path]::GetFullPath((Join-Path $repo "dist"))
if (-not $buildDir.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe build path." }
if (-not $distDir.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe dist path." }
if (Test-Path -LiteralPath $buildDir) { Remove-Item -LiteralPath $buildDir -Recurse -Force }
if (Test-Path -LiteralPath $distDir) { Remove-Item -LiteralPath $distDir -Recurse -Force }

$name = if ($Console) { "ArubaMiniDashboardConsole" } else { "ArubaMiniDashboard" }
$version = & $python -c "import pathlib, tomllib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
    throw "Failed to read project version from pyproject.toml."
}
$version = $version.Trim()
$versionFile = Join-Path $buildDir "windows-version-info.txt"
& $python (Join-Path $repo "scripts\generate_windows_version_info.py") `
    --output $versionFile `
    --version $version `
    --description "Aruba MM and WLC Mini Dashboard" `
    --original-filename "$name.exe"
if ($LASTEXITCODE -ne 0) { throw "Windows version resource generation failed." }

$env:ARUBA_BUILD_CONSOLE = if ($Console) { "1" } else { "0" }
$env:ARUBA_BUILD_ONEFILE = if ($OneFile) { "1" } else { "0" }
$env:ARUBA_BUILD_VERSION_FILE = $versionFile
& $python -m PyInstaller --noconfirm --clean (Join-Path $repo "ArubaMiniDashboard.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$releaseRoot = if ($OneFile) { $distDir } else { Join-Path $distDir $name }
if (-not (Test-Path -LiteralPath $releaseRoot)) { throw "Build output missing: $releaseRoot" }

Copy-Item -LiteralPath (Join-Path $repo "config.example.json") -Destination (Join-Path $releaseRoot "config.example.json")
Copy-Item -LiteralPath (Join-Path $repo "LICENSE") -Destination (Join-Path $releaseRoot "LICENSE.txt")
Copy-Item -LiteralPath (Join-Path $repo "docs\README.txt") -Destination (Join-Path $releaseRoot "README.txt")
Copy-Item -LiteralPath (Join-Path $repo "docs\WINDOWS11_QA_CHECKLIST_KO.md") -Destination (Join-Path $releaseRoot "WINDOWS11_QA_CHECKLIST_KO.md")
Copy-Item -LiteralPath (Join-Path $repo "docs\THIRD_PARTY_NOTICES.txt") -Destination (Join-Path $releaseRoot "THIRD_PARTY_NOTICES.txt")
Copy-Item -LiteralPath (Join-Path $repo "docs\LGPL_RUNTIME_REPLACEMENT_KO_EN.md") -Destination (Join-Path $releaseRoot "LGPL_RUNTIME_REPLACEMENT_KO_EN.md")

& $python (Join-Path $repo "scripts\collect_qt_runtime_notices.py") `
    --package-root $releaseRoot `
    --write-package-files `
    --check-package-files
if ($LASTEXITCODE -ne 0) { throw "Packaged Qt runtime inventory verification failed." }

& $python (Join-Path $repo "scripts\collect_lgpl_runtime_sources.py") `
    --package-root $releaseRoot `
    --executable "$name.exe" `
    --write-package-files `
    --check-package-files
if ($LASTEXITCODE -ne 0) { throw "Packaged LGPL runtime source verification failed." }

$verifyArgs = @(
    (Join-Path $PSScriptRoot "verify_release_package.py"),
    "--path", $releaseRoot,
    "--name", $name,
    "--expected-version", $version
)
if ($OneFile) { $verifyArgs += "--one-file" }
& $python @verifyArgs
if ($LASTEXITCODE -ne 0) { throw "Release verification failed." }

$exe = if ($OneFile) { Join-Path $distDir "$name.exe" } else { Join-Path $releaseRoot "$name.exe" }
$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
Set-Content -LiteralPath (Join-Path $distDir "$name.sha256.txt") -Value "$hash  $name.exe" -Encoding ASCII
Write-Host "Build completed: $releaseRoot"
Write-Host "SHA-256: $hash"
