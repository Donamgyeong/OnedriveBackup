@echo off
setlocal
cd /d "%~dp0"
if not exist "config.json" (
  echo config.json is missing. Copy config.example.json to config.json and edit it first.
  pause
  exit /b 2
)
OneDriveBackup.exe --config config.json status
set "BACKUP_EXIT=%ERRORLEVEL%"
echo.
echo Exit code: %BACKUP_EXIT%
pause
exit /b %BACKUP_EXIT%
