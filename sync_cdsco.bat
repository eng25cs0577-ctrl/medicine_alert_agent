@echo off
rem MedAlert laptop sync: double-click to download new CDSCO lists and upload them
rem to your MedAlert website. Task Scheduler runs it with --quiet.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py sync.py %*
) else (
    python sync.py %*
)
if "%~1"=="" pause
