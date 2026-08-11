param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [string]$OutputDirectory = "dist\release",

    [switch]$VerifyOnly,

    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$repo = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $repo

$productName = "ArubaMiniDashboard"
$versionPattern = '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Operation
    )

    & $FilePath @Arguments | ForEach-Object { Write-Host $_ }
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Operation failed with exit code $exitCode."
    }
}

function Resolve-RepositoryChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) {
        [System.IO.Path]::GetFullPath($Path)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $repo $Path))
    }
    $repoPrefix = $repo.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Purpose must stay inside the repository: $candidate"
    }
    if ($candidate.Equals($repo, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Purpose cannot be the repository root."
    }
    return $candidate
}

function Get-SourceVersion {
    $pyprojectPath = Join-Path $repo "pyproject.toml"
    $initPath = Join-Path $repo "src\aruba_mini_dashboard\__init__.py"
    $pyprojectText = Get-Content -LiteralPath $pyprojectPath -Raw -Encoding UTF8
    $initText = Get-Content -LiteralPath $initPath -Raw -Encoding UTF8
    $pyprojectMatches = [regex]::Matches($pyprojectText, '(?m)^version\s*=\s*"([^"]+)"\s*$')
    $initMatches = [regex]::Matches($initText, '(?m)^__version__\s*=\s*"([^"]+)"\s*$')
    if ($pyprojectMatches.Count -ne 1) {
        throw "Expected exactly one project version in pyproject.toml."
    }
    if ($initMatches.Count -ne 1) {
        throw "Expected exactly one __version__ value in aruba_mini_dashboard/__init__.py."
    }
    $pyprojectVersion = $pyprojectMatches[0].Groups[1].Value
    $packageVersion = $initMatches[0].Groups[1].Value
    if ($pyprojectVersion -ne $packageVersion) {
        throw "Source version mismatch: pyproject.toml=$pyprojectVersion package=$packageVersion"
    }
    return $pyprojectVersion
}

function Resolve-Python {
    $releasePython = Join-Path $repo ".release-venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $releasePython -PathType Leaf) {
        return $releasePython
    }
    $venvPython = Join-Path $repo ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }
    $pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python is required to run the package verifier."
    }
    return $pythonCommand.Source
}

function Resolve-BootstrapPython {
    $venvPython = Join-Path $repo ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }
    $pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python 3.11.9 is required to create the clean release environment."
    }
    return $pythonCommand.Source
}

function Get-ReproducibleTimestamp {
    $epochText = (& git -C $repo show -s --format=%ct HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $epochText -notmatch '^\d+$') {
        throw "Could not determine the source commit timestamp."
    }
    $timestamp = [System.DateTimeOffset]::FromUnixTimeSeconds([int64]$epochText).ToUniversalTime()
    $minimum = [System.DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [System.TimeSpan]::Zero)
    $maximum = [System.DateTimeOffset]::new(2107, 12, 31, 23, 59, 58, [System.TimeSpan]::Zero)
    if ($timestamp -lt $minimum) { return $minimum }
    if ($timestamp -gt $maximum) { return $maximum }
    return $timestamp
}

function New-OnedirZip {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$RootName
    )

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $sourceFull = [System.IO.Path]::GetFullPath($SourceRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $files = @(Get-ChildItem -LiteralPath $sourceFull -Recurse -File | Sort-Object FullName)
    if ($files.Count -eq 0) {
        throw "Release directory is empty: $sourceFull"
    }
    $reparseFiles = @($files | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 })
    if ($reparseFiles.Count -gt 0) {
        throw "Release directory contains reparse points; packaging is refused."
    }

    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Force
    }
    $timestamp = Get-ReproducibleTimestamp
    $zipStream = [System.IO.File]::Open(
        $Destination,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    try {
        $archive = New-Object System.IO.Compression.ZipArchive(
            $zipStream,
            [System.IO.Compression.ZipArchiveMode]::Create,
            $false
        )
        try {
            foreach ($file in $files) {
                $relative = $file.FullName.Substring($sourceFull.Length).TrimStart('\', '/')
                $entryName = "$RootName/" + ($relative -replace '\\', '/')
                $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
                $entry.LastWriteTime = $timestamp
                $inputStream = [System.IO.File]::Open(
                    $file.FullName,
                    [System.IO.FileMode]::Open,
                    [System.IO.FileAccess]::Read,
                    [System.IO.FileShare]::Read
                )
                try {
                    $entryStream = $entry.Open()
                    try {
                        $inputStream.CopyTo($entryStream)
                    }
                    finally {
                        $entryStream.Dispose()
                    }
                }
                finally {
                    $inputStream.Dispose()
                }
            }
        }
        finally {
            $archive.Dispose()
        }
    }
    finally {
        $zipStream.Dispose()
    }
}

function Get-FileInventory {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $inventory = @{}
    foreach ($file in Get-ChildItem -LiteralPath $rootFull -Recurse -File) {
        $relative = $file.FullName.Substring($rootFull.Length).TrimStart('\', '/') -replace '\\', '/'
        $key = $relative.ToLowerInvariant()
        if ($inventory.ContainsKey($key)) {
            throw "Case-insensitive file collision found: $relative"
        }
        $inventory[$key] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $inventory
}

function Assert-MatchingInventories {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ActualRoot
    )

    $expected = Get-FileInventory -Root $ExpectedRoot
    $actual = Get-FileInventory -Root $ActualRoot
    $expectedKeys = @($expected.Keys | Sort-Object)
    $actualKeys = @($actual.Keys | Sort-Object)
    if (($expectedKeys -join "`n") -ne ($actualKeys -join "`n")) {
        throw "ZIP file inventory differs from the built onedir directory."
    }
    foreach ($key in $expectedKeys) {
        if ($expected[$key] -ne $actual[$key]) {
            throw "ZIP content hash mismatch: $key"
        }
    }
}

function Assert-PackageAssetSet {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string[]]$ExpectedNames
    )

    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        throw "Release asset directory not found: $Directory"
    }
    $items = @(Get-ChildItem -LiteralPath $Directory -Force)
    $actualNames = @($items | ForEach-Object { $_.Name } | Sort-Object)
    $expectedSorted = @($ExpectedNames | Sort-Object)
    if (($actualNames -join "|") -ne ($expectedSorted -join "|")) {
        throw "Release asset directory must contain exactly: $($expectedSorted -join ', ')"
    }
    $files = @($items | Where-Object { -not $_.PSIsContainer })
    if ($items.Count -ne $files.Count) {
        throw "Release asset directory contains an unexpected subdirectory."
    }
}

function Assert-ZipEntryContract {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$RootName
    )

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        if ($archive.Entries.Count -eq 0) {
            throw "Release ZIP is empty."
        }
        $names = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $archive.Entries) {
            $entryName = $entry.FullName
            if ([string]::IsNullOrWhiteSpace($entryName) -or $entryName.Contains('\')) {
                throw "ZIP contains an invalid entry name: $entryName"
            }
            if ($entryName.StartsWith('/') -or -not $entryName.StartsWith("$RootName/", [System.StringComparison]::Ordinal)) {
                throw "ZIP entry is outside the expected root directory: $entryName"
            }
            $segments = @($entryName.Split('/'))
            $unsafeSegments = @($segments | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' })
            if ($segments.Count -lt 2 -or $unsafeSegments.Count -gt 0) {
                throw "ZIP contains an unsafe entry path: $entryName"
            }
            if (-not $names.Add($entryName)) {
                throw "ZIP contains a case-insensitive duplicate entry: $entryName"
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Assert-ReleaseArchive {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$ChecksumPath,
        [Parameter(Mandatory = $true)][string]$RootName,
        [string]$ExpectedSourceRoot = ""
    )

    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw "Release ZIP not found: $ZipPath"
    }
    if (-not (Test-Path -LiteralPath $ChecksumPath -PathType Leaf)) {
        throw "Release checksum not found: $ChecksumPath"
    }
    $checksumText = (Get-Content -LiteralPath $ChecksumPath -Raw -Encoding ASCII).Trim()
    $expectedChecksumLine = '^([0-9a-f]{64})  ([A-Za-z0-9._-]+\.zip)$'
    if ($checksumText -notmatch $expectedChecksumLine) {
        throw "Checksum file does not use the required '<sha256>  <zip-name>' format."
    }
    $expectedHash = $Matches[1]
    $expectedName = $Matches[2]
    if ($expectedName -ne (Split-Path -Leaf $ZipPath)) {
        throw "Checksum filename does not match the release ZIP."
    }
    $actualHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Release ZIP SHA-256 mismatch."
    }

    Assert-ZipEntryContract -ZipPath $ZipPath -RootName $RootName

    $python = Resolve-Python
    Invoke-Checked -FilePath $python -Arguments @(
        (Join-Path $PSScriptRoot "verify_release_package.py"),
        "--zip",
        $ZipPath,
        "--name",
        $RootName,
        "--expected-version",
        $Version,
        "--sha256-file",
        $ChecksumPath
    ) -Operation "Release ZIP verification"

    if (-not $ExpectedSourceRoot) {
        return $actualHash
    }

    $tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $tempRoot = [System.IO.Path]::GetFullPath((Join-Path $tempBase ("ArubaMiniDashboard-release-verify-" + [guid]::NewGuid().ToString('N'))))
    $tempPrefix = $tempBase + [System.IO.Path]::DirectorySeparatorChar + "ArubaMiniDashboard-release-verify-"
    if (-not $tempRoot.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe temporary verification path: $tempRoot"
    }
    New-Item -ItemType Directory -Path $tempRoot | Out-Null
    try {
        [System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $tempRoot)
        $topLevelItems = @(Get-ChildItem -LiteralPath $tempRoot -Force)
        if ($topLevelItems.Count -ne 1 -or -not $topLevelItems[0].PSIsContainer -or $topLevelItems[0].Name -ne $RootName) {
            throw "Release ZIP must extract to exactly one $RootName directory."
        }
        $extractedRoot = $topLevelItems[0].FullName
        Assert-MatchingInventories -ExpectedRoot $ExpectedSourceRoot -ActualRoot $extractedRoot
    }
    finally {
        $resolvedTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
        if (-not $resolvedTempRoot.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove an unsafe temporary path: $resolvedTempRoot"
        }
        if (Test-Path -LiteralPath $resolvedTempRoot) {
            Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force
        }
    }
    return $actualHash
}

if ($Version -notmatch $versionPattern) {
    throw "Invalid release version '$Version'. Use semantic version form such as 0.2.0 or 0.2.0-rc.1."
}
$sourceVersion = Get-SourceVersion
if ($sourceVersion -ne $Version) {
    throw "Requested release version $Version does not match source version $sourceVersion."
}
$sourceCommit = (& git -C $repo rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Could not determine the source commit for release packaging."
}
$initialStatus = @(& git -C $repo status --porcelain)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the release working tree."
}
if (-not $VerifyOnly -and $initialStatus.Count -gt 0 -and -not $AllowDirty) {
    throw "Release packaging requires a clean working tree. Commit the intended source first."
}

$outputFull = Resolve-RepositoryChildPath -Path $OutputDirectory -Purpose "Release output directory"
$assetBaseName = "$productName-v$Version-windows-x64"
$zipName = "$assetBaseName.zip"
$checksumName = "$zipName.sha256"
$zipPath = Join-Path $outputFull $zipName
$checksumPath = Join-Path $outputFull $checksumName

if (-not $VerifyOnly) {
    $bootstrapPython = Resolve-BootstrapPython
    $buildScript = Join-Path $PSScriptRoot "build.ps1"
    & $buildScript -CleanReleaseEnvironment -PythonLauncher $bootstrapPython |
        ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        throw "Windows onedir build failed with exit code $LASTEXITCODE."
    }
    $releaseRoot = Join-Path $repo "dist\$productName"
    if (-not (Test-Path -LiteralPath $releaseRoot -PathType Container)) {
        throw "Expected onedir build output is missing: $releaseRoot"
    }
    $currentSourceVersion = Get-SourceVersion
    if ($currentSourceVersion -ne $Version) {
        throw "Source version changed during the build: requested=$Version current=$currentSourceVersion"
    }
    $currentSourceCommit = (& git -C $repo rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $currentSourceCommit -ne $sourceCommit) {
        throw "Source commit changed during the build; packaging is refused."
    }
    if (-not $AllowDirty) {
        $currentStatus = @(& git -C $repo status --porcelain)
        if ($LASTEXITCODE -ne 0 -or $currentStatus.Count -gt 0) {
            throw "Release working tree changed during the build; packaging is refused."
        }
    }
    $python = Resolve-Python
    Invoke-Checked -FilePath $python -Arguments @(
        (Join-Path $PSScriptRoot "verify_release_package.py"),
        "--path",
        $releaseRoot,
        "--name",
        $productName
    ) -Operation "Built onedir verification"

    New-Item -ItemType Directory -Path $outputFull -Force | Out-Null
    New-OnedirZip -SourceRoot $releaseRoot -Destination $zipPath -RootName $productName
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText(
        $checksumPath,
        "$zipHash  $zipName`r`n",
        [System.Text.Encoding]::ASCII
    )
    Assert-PackageAssetSet -Directory $outputFull -ExpectedNames @($zipName, $checksumName)
    $verifiedHash = Assert-ReleaseArchive `
        -ZipPath $zipPath `
        -ChecksumPath $checksumPath `
        -RootName $productName `
        -ExpectedSourceRoot $releaseRoot
}
else {
    Assert-PackageAssetSet -Directory $outputFull -ExpectedNames @($zipName, $checksumName)
    $verifiedHash = Assert-ReleaseArchive `
        -ZipPath $zipPath `
        -ChecksumPath $checksumPath `
        -RootName $productName
}

if ($env:GITHUB_OUTPUT) {
    $outputLines = @(
        "version=$Version"
        "tag=v$Version"
        "zip_path=$zipPath"
        "zip_name=$zipName"
        "checksum_path=$checksumPath"
        "checksum_name=$checksumName"
        "sha256=$verifiedHash"
    ) -join "`n"
    [System.IO.File]::AppendAllText(
        $env:GITHUB_OUTPUT,
        $outputLines + "`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
}

Write-Host "ARUBA_MINI_DASHBOARD_RELEASE_PACKAGE_OK"
Write-Host "Version: $Version"
Write-Host "ZIP: $zipPath"
Write-Host "SHA-256: $verifiedHash"
if ($AllowDirty) {
    Write-Warning "Package was created with -AllowDirty for local validation and must not be published."
}
