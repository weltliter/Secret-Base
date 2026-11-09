@echo off
setlocal
title WeChat Vault Console
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0console.ps1" -Action Open %*
if errorlevel 1 (
    echo.
    echo Could not open the management page. See the error above.
    pause
    exit /b 1
)
