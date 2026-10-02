param(
    [Parameter(Mandatory=$true)][string]$Package,
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = "Stop"
$source = (Resolve-Path $Package).Path
$target = [System.IO.Path]::GetFullPath($InstallDir)
if (-not (Test-Path -LiteralPath (Join-Path $source "DocMind.exe"))) {
    throw "发布包缺少 DocMind.exe：$source"
}
if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
Get-Process DocMind -ErrorAction SilentlyContinue | Stop-Process -Force
# Keep logs, user state and local indexes; the release package owns everything
# else and carries a new install-manifest.json for future update checks.
robocopy $source $target /E /XO /R:2 /W:1 /XF '*.log' '.docmind_state.json' '.docmind_budget.json' '.docmind_traces.jsonl' | Out-Null
if ($LASTEXITCODE -gt 7) { throw "复制发布包失败：$LASTEXITCODE" }
Write-Host "DocMind 便携版升级完成：$target"
