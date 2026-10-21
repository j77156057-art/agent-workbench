@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Source checkout launcher. Dependencies are created on first launch and
REM upgraded automatically when requirements or the frontend lockfile changes.
call "%~dp0run_desktop.bat"
rem Keep the window open when the inner launcher reports failure.
rem Without this the console window closes the moment the script ends,
rem so a crash on startup (a missing module, a failed dependency step)
rem flashes and is gone before anyone can read it. Measured on this box:
rem exit code 1 with the error text on screen, then window gone.
set "DOCMIND_EXIT=%ERRORLEVEL%"
if not "%DOCMIND_EXIT%"=="0" echo Agent Workbench exited with code %DOCMIND_EXIT%.
if not "%DOCMIND_EXIT%"=="0" pause
exit /b %DOCMIND_EXIT%
