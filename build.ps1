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

if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

Compress-Archive -Path (Join-Path $pkgDir "*") -DestinationPath $zipPath -Force
Write-Host "Built dist/$zipName"
