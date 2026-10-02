param(
    [string]$Version = "",
    [switch]$SkipInstaller,
    [switch]$ForceDependencies
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
Set-Location $Root

function Find-HostPython {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @("py", "-3") }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @("python") }
    throw "未找到 Python 3。请先安装 Python 3.11 或更高版本。"
}

$hostPython = Find-HostPython
$hostPythonArgs = @()
if ($hostPython.Count -gt 1) { $hostPythonArgs = $hostPython[1..($hostPython.Count - 1)] }
function Invoke-HostPython {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $hostPython[0] @hostPythonArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python 命令失败：$LASTEXITCODE" }
}
Write-Host "[DocMind] 同步构建依赖..."
if ($ForceDependencies) {
    Invoke-HostPython (Join-Path $Root "dependency_bootstrap.py") --root $Root --force
} else {
    Invoke-HostPython (Join-Path $Root "dependency_bootstrap.py") --root $Root
}

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$stage = Join-Path $Root "dist\release"
$work = Join-Path $Root "build\release"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Write-Host "[DocMind] 构建 PyInstaller onedir 包..."
& $venvPython -m PyInstaller (Join-Path $Root "docmind.spec") --noconfirm `
    --distpath $stage --workpath $work --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

$package = Join-Path $stage "DocMind"
if (-not (Test-Path (Join-Path $package "DocMind.exe"))) {
    throw "发布包缺少 DocMind.exe：$package"
}

if (-not $Version) {
    $git = git -C $Root describe --tags --always --dirty 2>$null
    $Version = if ($git) { $git.Trim() } else { Get-Date -Format "yyyy.MM.dd.HHmm" }
}
$reqHash = (& $hostPython[0] @hostPythonArgs -c `
    "from pathlib import Path; import dependency_bootstrap; print(dependency_bootstrap._sha256(Path('requirements.txt')))" ).Trim()
$lockHash = (& $hostPython[0] @hostPythonArgs -c `
    "from pathlib import Path; import dependency_bootstrap; print(dependency_bootstrap._hash_if_exists(Path('frontend/package-lock.json')))" ).Trim()
$manifest = [ordered]@{
    schema = 1
    name = "DocMind"
    version = $Version
    built_at = (Get-Date).ToString("o")
    requirements_sha256 = $reqHash
    frontend_lock_sha256 = $lockHash
    dependency_policy = "bundled"
    update_command = "installer\upgrade_portable.ps1"
    uninstall_command = "uninstall.bat"
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $package "install-manifest.json") -Encoding UTF8
Copy-Item (Join-Path $Root "installer\uninstall_portable.ps1") (Join-Path $package "uninstall_portable.ps1") -Force
Copy-Item (Join-Path $Root "installer\upgrade_portable.ps1") (Join-Path $package "upgrade_portable.ps1") -Force
Copy-Item (Join-Path $Root "installer\uninstall.bat") (Join-Path $package "uninstall.bat") -Force
Write-Host "[DocMind] 发布包：$package ($Version)"

if (-not $SkipInstaller) {
    $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($iscc) {
        $installerArgs = @("/DAppVersion=$Version", "/DDistDir=$package",
                           "/DInstallerOutput=$(Join-Path $stage 'installer')",
                           (Join-Path $Root "installer\DocMind.iss"))
        & $iscc.Source @installerArgs
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup 构建失败" }
        Write-Host "[DocMind] Inno 安装器已生成：$(Join-Path $stage 'installer')"
    } else {
        Write-Warning "未找到 ISCC.exe，已跳过 Inno Setup；发布包仍可作为便携版使用。"
    }
}
