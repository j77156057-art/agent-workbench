@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "DOCMIND_ENTRY=desktop.py"
set "DOCMIND_PACKAGED=dist\DocMind\DocMind.exe"
set "DOCMIND_BOOTSTRAP=%~dp0dependency_bootstrap.py"

if exist "%DOCMIND_ENTRY%" goto source_prepare
if exist "%DOCMIND_PACKAGED%" goto packaged
goto missing

:source_prepare
set "DOCMIND_HOST_PYTHON="
where py >nul 2>&1
if not errorlevel 1 set "DOCMIND_HOST_PYTHON=py -3"
if defined DOCMIND_HOST_PYTHON goto bootstrap
where python >nul 2>&1
if not errorlevel 1 set "DOCMIND_HOST_PYTHON=python"
if defined DOCMIND_HOST_PYTHON goto bootstrap
goto no_python

:bootstrap
echo ============================================
echo   Agent Workbench - preparing dependencies
echo ============================================
REM "%~dp0." instead of "%~dp0": %~dp0 expands WITH a trailing backslash, and
REM external programs parse argv by MSVCRT rules -- there \" is an ESCAPED quote,
REM so the closing quote vanishes and --root receives a path with a literal "
REM glued to its end. dependency_bootstrap then reports
REM   missing <root>"\requirements.txt
REM cmd itself does NOT parse by MSVCRT, so this line looks perfectly fine in
REM source and only explodes when double-clicked. Adding a dot makes it expand
REM to "...\." which is correct. (bat_guard rule: quoted_dir_expansion)
%DOCMIND_HOST_PYTHON% "%DOCMIND_BOOTSTRAP%" --root "%~dp0."
if errorlevel 1 goto bootstrap_failed
if not exist ".venv\Scripts\python.exe" goto bootstrap_failed
echo Starting the current source version...
".venv\Scripts\python.exe" "%DOCMIND_ENTRY%"
set "DOCMIND_EXIT=%ERRORLEVEL%"
echo Agent Workbench exited with code %DOCMIND_EXIT%.
exit /b %DOCMIND_EXIT%

:packaged
echo Starting the packaged Agent Workbench version...
start "Agent Workbench" "%DOCMIND_PACKAGED%"
exit /b 0

:missing
echo No Agent Workbench source or packaged installation was found.
pause
exit /b 1

:no_python
if exist "%DOCMIND_PACKAGED%" goto packaged
echo Python 3 was not found. Install Python 3.11+ and run this launcher again.
pause
exit /b 1

:bootstrap_failed
echo Dependency preparation failed. Review the messages above and run again.
pause
exit /b 1
