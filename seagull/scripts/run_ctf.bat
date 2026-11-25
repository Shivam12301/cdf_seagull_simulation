@echo off
REM ==============================================================================
REM Run Seagull CTF Client (Windows Batch)
REM Usage: run_ctf.bat [session|event]
REM ==============================================================================
set SCRIPT_DIR=%~dp0
set BASE_DIR=%SCRIPT_DIR%..
set MODE=%1
if "%MODE%"=="" set MODE=session
if not exist "%BASE_DIR%\logs" mkdir "%BASE_DIR%\logs"

where seagull >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [CTF] Launching native Seagull CTF Client [%MODE%]...
    if "%MODE%"=="event" (
        set SCEN=%BASE_DIR%\scenario\ctf_event_client.xml
    ) else (
        set SCEN=%BASE_DIR%\scenario\ctf_session_client.xml
    )
    seagull -conf "%BASE_DIR%\config\conf_ctf_client.xml" -dico "%BASE_DIR%\config\base_rf.xml" -scen "%SCEN%" -log "%BASE_DIR%\logs\ctf_client.log" -llevel E
) else (
    echo [CTF] Native seagull not found. Falling back to Python CTF simulation...
    python "%BASE_DIR%\..\python\ctf_client.py" --mode %MODE%
)
pause
