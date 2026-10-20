[CmdletBinding()]
param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot $Python

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Build Python was not found: $pythonPath"
}

& $pythonPath -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Run: $Python -m pip install -r requirements-build.txt"
}

Push-Location $projectRoot
try {
    & $pythonPath -m unittest discover -s tests -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }

    & $pythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --console `
        --name OneDriveBackup `
        --distpath dist `
        --workpath build `
        --specpath build `
        packaging\launcher.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    $releaseDir = Join-Path $projectRoot "dist\OneDriveBackup"
    Copy-Item -LiteralPath "config.example.json" -Destination $releaseDir
    Copy-Item -LiteralPath "README.md" -Destination $releaseDir
    Copy-Item -LiteralPath "SCALABILITY.md" -Destination $releaseDir
    Copy-Item -LiteralPath "PERFORMANCE.md" -Destination $releaseDir
    Copy-Item -LiteralPath "DISTRIBUTION.md" -Destination (Join-Path $releaseDir "USER_GUIDE.md")
    Copy-Item -LiteralPath "packaging\RUN_ONCE.cmd" -Destination $releaseDir
    Copy-Item -LiteralPath "packaging\STATUS.cmd" -Destination $releaseDir
    Copy-Item -LiteralPath "packaging\UPDATE_BROWSE.cmd" -Destination $releaseDir

    $releaseFile = "OneDriveBackup-0.13.0-win64.zip"
    $zipPath = Join-Path $projectRoot ("dist\" + $releaseFile)
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
    $checksum = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath ($zipPath + ".sha256") -Value ($checksum + "  " + $releaseFile) -Encoding ascii
    Write-Host "Release created: $zipPath"
}
finally {
    Pop-Location
}
