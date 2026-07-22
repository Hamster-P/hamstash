# Build release installer: PyInstaller backend -> NSSM -> Tauri client (NSIS/MSI installer)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$serverDir = Join-Path $root "server"
$clientDir = Join-Path $root "client"
$backendOutDir = Join-Path $clientDir "src-tauri\backend"

Write-Host "== 1/3 Building backend (PyInstaller) ==" -ForegroundColor Cyan
Push-Location $serverDir
try {
    & python -m pip show pyinstaller *> $null
    if ($LASTEXITCODE -ne 0) {
        python -m pip install pyinstaller
    }

    $backendAppDir = Join-Path $backendOutDir "hamstash-server"
    if (Test-Path $backendAppDir) {
        Remove-Item -Recurse -Force $backendAppDir
    }

    python -m PyInstaller --onedir --name hamstash-server `
        --distpath "$backendOutDir" --workpath build --specpath . --noconfirm `
        run_service.py
} finally {
    Pop-Location
}

Write-Host "== 2/3 Preparing NSSM ==" -ForegroundColor Cyan
$nssmExe = Join-Path $backendOutDir "nssm.exe"
if (-not (Test-Path $nssmExe)) {
    $nssmZip = Join-Path $env:TEMP "nssm-release.zip"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip
    $nssmExtractDir = Join-Path $env:TEMP "nssm-extract"
    if (Test-Path $nssmExtractDir) { Remove-Item -Recurse -Force $nssmExtractDir }
    Expand-Archive -Path $nssmZip -DestinationPath $nssmExtractDir -Force
    Copy-Item (Join-Path $nssmExtractDir "nssm-2.24\win64\nssm.exe") $nssmExe
    Write-Host "NSSM downloaded to $nssmExe"
} else {
    Write-Host "NSSM already present, skipping download"
}

Write-Host "== 3/3 Building client installer (Tauri) ==" -ForegroundColor Cyan
Push-Location $clientDir
try {
    npx tauri build
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Done. Installers are under client\src-tauri\target\release\bundle\ (nsis and msi folders)." -ForegroundColor Green
