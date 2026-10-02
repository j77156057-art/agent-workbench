@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Source checkout launcher. Dependencies are created on first launch and
REM upgraded automatically when requirements or the frontend lockfile changes.
call "%~dp0run_desktop.bat"
exit /b %ERRORLEVEL%
