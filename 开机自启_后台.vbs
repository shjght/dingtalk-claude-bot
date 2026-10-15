' 开机自启：后台无窗口运行钉钉遥控机器人
' 看实时日志双击 bot_log.bat；想取消自启就删掉本文件
Set sh = CreateObject("WScript.Shell")
sh.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
sh.CurrentDirectory = "D:\研\claude-remote-bot"
sh.Run """D:\python\pythonw.exe"" bot.py", 0, False
