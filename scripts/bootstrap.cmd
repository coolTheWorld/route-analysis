@echo off
setlocal
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
exit /b %ERRORLEVEL%
