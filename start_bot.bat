@echo off
schtasks /Change /TN claude_bot_watchdog /ENABLE >nul 2>&1
cd /d %~dp0
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq 'D:\python\pythonw.exe' } | Stop-Process -Force"
start "" "D:\python\pythonw.exe" bot.py
echo 机器人已在后台启动（无窗口，本窗口会自动关闭）。
echo 验证：手机发 /status，或双击 bot_log.bat 看日志。
timeout /t 3 >nul
