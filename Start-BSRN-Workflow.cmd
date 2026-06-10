@echo off
setlocal
set "PROJECT_DIR=%~dp0"
set "BUNDLED_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%BUNDLED_PY%" (
  "%BUNDLED_PY%" "%PROJECT_DIR%scripts\bsrn_launch_dashboard.py"
  goto :done
)

py -3 "%PROJECT_DIR%scripts\bsrn_launch_dashboard.py"
if %ERRORLEVEL% EQU 0 goto :done

python "%PROJECT_DIR%scripts\bsrn_launch_dashboard.py"

:done
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo Could not start the BSRN Workflow Dashboard.
  echo Install Python 3.10 or newer, or update this launcher to point to the Python executable on this PC.
  pause
)
exit /b %ERRORLEVEL%
