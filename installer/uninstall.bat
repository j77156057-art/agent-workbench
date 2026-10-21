@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0uninstall_portable.ps1" (
  echo Agent Workbench uninstaller is missing.
  pause
  exit /b 1
)
REM -InstallDir "%~dp0." 而不是 "%~dp0"：%~dp0 展开后以反斜杠结尾，powershell.exe
REM 按 MSVCRT 规则解析 argv —— \" 是「转义过的引号」，闭合引号消失，-InstallDir 收到
REM 一个尾部多出字面 " 的目录。与 run_desktop.bat:27 同一个 bug 类（bat_guard 的
REM quoted_dir_expansion 规则）。第 4 行的 if exist 由 cmd 自己解析，不受影响。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_portable.ps1" -InstallDir "%~dp0."
exit /b %ERRORLEVEL%
