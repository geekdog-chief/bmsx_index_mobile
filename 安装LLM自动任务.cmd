@echo off
title 安装LLM自动任务

echo 将创建/覆盖计划任务：每天 15:40 自动 更新+LLM打分+推送
echo （覆盖原"宝妈指数每日更新"任务，卸载仍用 卸载定时任务.cmd）
echo.
schtasks /create /tn "宝妈指数每日更新" /tr "\"%~dp0推送数据含LLM.cmd\"" /sc daily /st 15:40 /f
if errorlevel 1 (
    echo.
    echo [错误] 创建失败，请右键本文件"以管理员身份运行"
) else (
    echo.
    echo 安装成功！每天 15:40 自动运行。
)
pause >nul
