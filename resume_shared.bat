@echo off
chcp 65001 >nul
cd /d "D:\研"
rem 用法：resume_shared.bat [会话名]
rem   带参数：打开指定会话，如 resume_shared.bat 求职
rem   不带参数：打开手机当前所在的会话（current_session.txt）
set NAME=%1
if "%NAME%"=="" (
    if exist "%~dp0current_session.txt" (
        set /p NAME=<"%~dp0current_session.txt"
    ) else (
        set NAME=默认
    )
)
if not exist "%~dp0sessions\%NAME%.txt" (
    echo 会话【%NAME%】还不存在。手机上用 /s %NAME% 先创建，或直接双击本脚本打开手机当前会话。
    pause
    exit /b
)
set /p SID=<"%~dp0sessions\%NAME%.txt"
if "%SID%"=="" (
    echo 会话【%NAME%】还是空的，先在手机上给它发个任务。
    pause
    exit /b
)
title Claude会话 - %NAME%
echo 正在打开会话【%NAME%】（%SID%）...
echo 提示：电脑上聊完后请 /exit 退出，避免和手机同时写同一个会话。
claude --resume %SID%
