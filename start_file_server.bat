@echo off
chcp 65001 >nul
cd /d %~dp0
set PYTHONIOENCODING=utf-8
title 手机文件服务器
python file_server.py
pause
