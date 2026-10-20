[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$buildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "build\synology"))
$allowedBuildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "build"))
$stageRoot = Join-Path $buildRoot "OneDriveBackup"
$distRoot = Join-Path $projectRoot "dist"
$releaseFile = "OneDriveBackup-0.13.0-synology-source.zip"
$zipPath = Join-Path $distRoot $releaseFile

if (-not $buildRoot.StartsWith($allowedBuildRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe staging path: $buildRoot"
}

if (Test-Path -LiteralPath $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $projectRoot "onedrive_backup") -Destination $stageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "synology") -Destination $stageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "tools") -Destination $stageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "config.example.json") -Destination $stageRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Destination $stageRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $stageRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "SYNOLOGY.md") -Destination $stageRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "SCALABILITY.md") -Destination $stageRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "PERFORMANCE.md") -Destination $stageRoot

Get-ChildItem -LiteralPath $stageRoot -Filter "__pycache__" -Directory -Recurse |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stageRoot -Filter "*.pyc" -File -Recurse |
    Remove-Item -Force

if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Compress-Archive -Path $stageRoot -DestinationPath $zipPath -CompressionLevel Optimal
$checksum = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($zipPath + ".sha256") -Value ($checksum + "  " + $releaseFile) -Encoding ascii
Write-Host "Synology source release created: $zipPath"
