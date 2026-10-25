param(
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$resolved = [System.IO.Path]::GetFullPath($InstallDir)
$root = [System.IO.Path]::GetPathRoot($resolved)
if ($resolved.TrimEnd('\') -eq $root.TrimEnd('\')) {
    throw "拒绝把磁盘根目录作为卸载目标"
}
if (-not (Test-Path -LiteralPath (Join-Path $resolved "DocMind.exe"))) {
    throw "卸载目标不是 DocMind 安装目录：$resolved"
}

Get-Process DocMind -ErrorAction SilentlyContinue | Stop-Process -Force
if (-not $Force) {
    $confirm = Read-Host "确认删除 $resolved 及其全部安装文件？输入 YES"
    if ($confirm -ne "YES") { Write-Host "已取消"; exit 1 }
}

# The script is copied into the directory being removed. Schedule deletion
# after this PowerShell process exits so the file is not locked on Windows.
# Portable uninstall removes the whole install directory; users who need to
# preserve indexes or sessions should set DOCMIND_STATE_ROOT outside it first.
$marker = Join-Path $resolved ".docmind-uninstalling"
Set-Content -LiteralPath $marker -Value (Get-Date -Format o) -Encoding ASCII
$escaped = $resolved.Replace('"', '""')
$cmd = Join-Path $env:TEMP ("DocMind-uninstall-{0}.cmd" -f ([guid]::NewGuid().ToString("N")))
@"
@echo off
timeout /t 1 /nobreak >nul
rmdir /s /q "$escaped"
del "%~f0" >nul 2>&1
"@ | Set-Content -LiteralPath $cmd -Encoding ASCII
Start-Process -FilePath "cmd.exe" -ArgumentList "/d", "/c", "`"$cmd`"" -WindowStyle Hidden
Write-Host "DocMind 卸载已启动，安装目录将在程序退出后删除。"
