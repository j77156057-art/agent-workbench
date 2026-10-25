# 双击本脚本即可在「桌面」生成 DocMind 快捷方式。
# （也可在资源管理器右键 dist\DocMind.exe → 发送到 → 桌面快捷方式）
$W = New-Object -ComObject WScript.Shell
$base = Split-Path -Parent $MyInvocation.MyCommand.Definition
$exe  = Join-Path $base 'dist\DocMind.exe'
$lnkPath = Join-Path $env:USERPROFILE 'Desktop\DocMind.lnk'
$l = $W.CreateShortcut($lnkPath)
$l.TargetPath = $exe
$l.WorkingDirectory = Join-Path $base 'dist'
$l.Description = 'DocMind - 本地 RAG 代码问答'
$l.IconLocation = $exe
$l.Save()
Write-Host "已创建桌面快捷方式：$lnkPath"
Read-Host "按 Enter 关闭"
