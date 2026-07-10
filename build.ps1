# Build script for the Simple COLLADA Importer extension.
# Produces simple_collada_importer-<version>.zip in the dist folder.

$ErrorActionPreference = "Stop"

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$pkgDir  = Join-Path $here "simple_collada_importer"
$manifest = Join-Path $pkgDir "blender_manifest.toml"

if (-not (Test-Path $manifest)) {
    throw "blender_manifest.toml not found at $manifest"
}

$versionLine = (Get-Content $manifest | Where-Object { $_ -match '^\s*version\s*=' } | Select-Object -First 1)
if (-not $versionLine) { throw "Could not find version in manifest" }
$version = ([regex]::Match($versionLine, '"([^"]+)"')).Groups[1].Value

$distDir = Join-Path $here "dist"
if (-not (Test-Path $distDir)) {
    New-Item -ItemType Directory -Path $distDir | Out-Null
}

$zipName = "simple_collada_importer-$version.zip"
$zipPath = Join-Path $distDir $zipName
$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("simple_collada-importer-" + [guid]::NewGuid())
$stagePkg = Join-Path $stageRoot "simple_collada_importer"

if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

try {
    New-Item -ItemType Directory -Path $stageRoot | Out-Null
    Copy-Item -LiteralPath $pkgDir -Destination $stagePkg -Recurse

    Get-ChildItem -LiteralPath $stagePkg -Directory -Filter "__pycache__" -Recurse |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $stagePkg -File -Recurse |
        Where-Object { $_.Extension -in ".pyc", ".pyo" -or $_.Name -eq ".DS_Store" } |
        Remove-Item -Force

    Compress-Archive -Path (Join-Path $stagePkg "*") -DestinationPath $zipPath -Force
}
finally {
    if (Test-Path $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}

Write-Host "Built dist/$zipName"
