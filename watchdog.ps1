# 钉钉遥控机器人守护：没有在跑的 bot.py 就拉活，并留一行看守日志。
$bot = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*bot.py*' }
if (-not $bot) {
  Start-Process -FilePath 'D:\python\pythonw.exe' -ArgumentList 'bot.py' `
    -WorkingDirectory 'D:\研\claude-remote-bot' -WindowStyle Hidden
  $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  $log = 'D:\研\claude-remote-bot\watchdog.log'
  if ((Test-Path $log) -and ((Get-Item $log).Length -gt 64KB)) {
    Move-Item -Force $log "$log.old"
  }
  Add-Content -Path $log -Value "$stamp 检测到机器人不在，已自动拉活"
}
