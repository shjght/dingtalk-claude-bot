$b = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*bot.py*' }
if ($b) {
  Write-Host ('== 机器人正在后台运行 ==  PID ' + $b.ProcessId + ' ==  手机发 /status 或看下方实时日志 ==') -ForegroundColor Green
} else {
  Write-Host '== 机器人未运行！双击 start_bot.bat 启动 ==' -ForegroundColor Red
}
