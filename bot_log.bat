@echo off
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File bot_log_status.ps1
echo.
powershell -NoProfile -Command "Get-Content -Path bot.log -Tail 30 -Wait -Encoding UTF8"
