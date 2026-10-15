@echo off
cd /d %~dp0
echo ============ DEBUG CONSOLE ============
echo  Closing this window STOPS the bot.
echo =======================================
"D:\python\python.exe" bot.py
echo.
echo Bot exited.
pause
