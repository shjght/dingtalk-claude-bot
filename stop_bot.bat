@echo off
schtasks /Change /TN claude_bot_watchdog /DISABLE >nul 2>&1
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq 'D:\python\pythonw.exe' } | Stop-Process -Force"
echo 机器人已停止（守护任务同时暂停，不会被自动拉活）。
echo 想再启动：双击 start_bot.bat
timeout /t 3 >nul
