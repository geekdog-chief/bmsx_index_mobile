@echo off
title 安装每日定时任务

echo 将创建计划任务：每天 15:40 自动更新指数并推送
echo （A股 15:00 收盘，留 40 分钟采集时间）
echo.
schtasks /create /tn "宝妈指数每日更新" /tr "\"%~dp0推送数据.cmd\"" /sc daily /st 15:40 /f
if errorlevel 1 (
    echo.
    echo [错误] 创建失败，请右键本文件"以管理员身份运行"
) else (
    echo.
    echo 安装成功！每天 15:40 自动运行。
    echo 卸载请运行 卸载定时任务.cmd
)
pause >nul
