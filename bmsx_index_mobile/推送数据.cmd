@echo off
cd /d "%~dp0"
title 宝妈指数 - 更新并推送

echo ==========================================
echo   宝妈指数 更新并推送到 GitHub
echo ==========================================
echo.

REM ---- 1. 探测 python ----
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )
if not defined PY ( echo [错误] 找不到 Python，请先安装 Python & pause & exit /b 1 )

echo 使用 Python: %PY%
echo.

REM ---- 2. 采集并生成 data.js ----
%PY% update_index.py
if errorlevel 1 (
    echo.
    echo [错误] 指数更新失败，未推送旧数据。按任意键退出。
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

REM ---- 4. 提交并推送 data.js ----
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
