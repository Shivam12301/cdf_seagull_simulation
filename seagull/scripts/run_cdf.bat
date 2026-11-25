@echo off
REM ==============================================================================
REM Run Seagull CDF Server (Windows Batch)
REM ==============================================================================
set SCRIPT_DIR=%~dp0
set BASE_DIR=%SCRIPT_DIR%..
if not exist "%BASE_DIR%\logs" mkdir "%BASE_DIR%\logs"

where seagull >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [CDF] Launching native Seagull CDF Server...
    seagull -conf "%BASE_DIR%\config\conf_cdf_server.xml" -dico "%BASE_DIR%\config\base_rf.xml" -scen "%BASE_DIR%\scenario\cdf_server.xml" -log "%BASE_DIR%\logs\cdf_server.log" -llevel E
) else (
    echo [CDF] Native seagull not found. Falling back to Python CDF simulation...
    python "%BASE_DIR%\..\python\cdf_server.py" --port 3868
)
pause
