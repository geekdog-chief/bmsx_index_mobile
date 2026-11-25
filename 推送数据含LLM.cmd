@echo off
cd /d "%~dp0"
title 宝妈指数 - 更新+LLM打分 并推送

echo ==========================================
echo   宝妈指数 羊驼接管版：LLM打分+更新+推送
echo ==========================================
echo.

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )
if not defined PY ( echo [错误] 找不到 Python，请先安装 Python & pause & exit /b 1 )
echo 使用 Python: %PY%
echo.

REM ---- 1. 先给缓存里的新帖打羊驼分（Ollama 不在会自动跳过）----
%PY% llm_overlay.py
if errorlevel 1 (
    echo.
    echo [错误] LLM 打分失败或评分进程冲突（退出码非0），中止本次更新与推送，旧数据不受影响。
    pause >nul
    exit /b 1
)

REM ---- 2. 生成指数：羊驼接管打分，词典兜底 ----
%PY% update_index_llm.py
if errorlevel 1 (
    echo.
    echo [错误] 指数更新失败，未推送旧数据。
    pause >nul
    exit /b 1
)

REM ---- 3. 探测 git ----
set "GIT="
where git >nul 2>nul && set "GIT=git"
if not defined GIT (
    for /d %%G in ("%LOCALAPPDATA%\GitHubDesktop\app-*") do (
        if exist "%%G\resources\app\git\cmd\git.exe" set "GIT=%%G\resources\app\git\cmd\git.exe"
    )
)
if not defined GIT ( echo [错误] 找不到 git，请安装 GitHub Desktop 并登录 & pause & exit /b 1 )

REM ---- 4. 提交并推送 ----
"%GIT%" add data.js
"%GIT%" commit -m "更新指数 %date% %time%" --allow-empty
"%GIT%" push
if errorlevel 1 (
    echo.
    echo [错误] 推送失败。打开 GitHub Desktop 登录后重试本脚本。
    pause >nul
    exit /b 1
)

echo.
echo 完成！手机刷新即可看到最新指数。
timeout /t 5 >nul
exit /b 0
