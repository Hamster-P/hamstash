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
    # 必须用项目自己venv里的python,不能用裸"python"——裸命令解析到哪个解释器
    # 完全看当前shell的PATH顺序,如果系统上还装过别的、只装了pyinstaller没装
    # requirements.txt那一整套的"裸python",打包会静默漏掉uvicorn/fastapi等
    # 核心依赖(现象是exe能生成,但一启动就ModuleNotFoundError),排查过一次真事故。
    $venvPython = Join-Path $serverDir "venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        throw "找不到venv: $venvPython -- 请先在server目录下建好venv并安装requirements.txt"
    }

    # 用管道(| Out-Null)只吞stdout,不碰stderr——pip show在包不存在时会往stderr
    # 写一行WARNING,重定向stderr(*>/2>&1)会被PowerShell 5.1包成NativeCommandError,
    # 在$ErrorActionPreference="Stop"下直接把脚本整个终止掉,永远走不到下面的安装分支。
    & $venvPython -m pip show pyinstaller | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & $venvPython -m pip install pyinstaller
    }

    $backendAppDir = Join-Path $backendOutDir "hamstash-server"
    if (Test-Path $backendAppDir) {
        Remove-Item -Recurse -Force $backendAppDir
    }
    # workpath是PyInstaller自己的中间分析缓存,不清理的话下次打包如果用了不同/
    # 不完整的解释器,分析结果会跟上一次干净构建的缓存残留混在一起,拼出一个
    # "看起来装了点东西、实际缺一大半"的坏包,比直接打包失败更难排查,必须每次先清空。
    $workPath = Join-Path $serverDir "build"
    if (Test-Path $workPath) {
        Remove-Item -Recurse -Force $workPath
    }

    & $venvPython -m PyInstaller --onedir --name hamstash-server `
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

# 本地专用的服务账户修复脚本(不进版本库,见.gitignore的scripts/*.local.ps1)——
# hooks.nsh每次装安装包都会把HamStashServer服务重置成默认的LocalSystem账户,
# 这台机器媒体库配置的是网络共享路径,LocalSystem访问不了,必须改回本机账户才能
# 正常启动。这里存在就自动调用一次;如果这时候安装包还没实际装完/服务还没重新
# 注册,脚本内部自己会检测服务存不存在、跳过不报错,不影响这次打包本身的结果。
#$restoreServiceAccountScript = Join-Path $PSScriptRoot "restore-service-account.local.ps1"
#if (Test-Path $restoreServiceAccountScript) {
#    Write-Host ""
#    Write-Host "== 检测到本地服务账户修复脚本,自动运行 ==" -ForegroundColor Cyan
#    & $restoreServiceAccountScript
#}
