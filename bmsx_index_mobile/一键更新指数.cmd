@echo off
cd /d "%~dp0"
title 宝妈·小白·散户指数 - 一键更新

rem ====== 探测真实可用的 Python（自动跳过微软商店占位符）======
set "PYEXE="
python -c "import sys" >nul 2>nul
if %errorlevel%==0 set "PYEXE=python"
if not defined PYEXE (
    py -3 -c "import sys" >nul 2>nul
    if %errorlevel%==0 set "PYEXE=py -3"
)
if not defined PYEXE (
    python3 -c "import sys" >nul 2>nul
    if %errorlevel%==0 set "PYEXE=python3"
)

if not defined PYEXE (
    echo ============================================
    echo  [错误] 未找到可用的 Python 3.9+
    echo ============================================
    echo.
    echo  本机 where python 的结果：
    where python 2>nul
    echo.
    echo  如果路径里有 Microsoft\WindowsApps\python.exe，
    echo  那只是微软商店占位符，不是真正的 Python。
    echo.
    echo  解决方法：
    echo   1. 打开 https://www.python.org/downloads/
    echo   2. 安装 3.9 或更高版本，第一页务必勾选
    echo      "Add python.exe to PATH"
    echo   3. 装完后重新双击本文件
    echo.
    echo  或者：设置 - 应用 - 高级应用设置 - 应用执行别名，
    echo  把 python.exe / python3.exe 两项关掉。
    pause
    exit /b 1
)

%PYEXE% -c "import sys; print('使用 Python', sys.version.split()[0])"
echo.
echo ============================================
echo   宝妈·小白·散户社区情绪指数 - 一键更新
echo ============================================
echo.

%PYEXE% update_index.py
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo [失败] 更新未完成（错误码 %RC%），已保留上一次结果。详见 data\update.log
    pause
    exit /b 1
)
echo.
echo 正在打开最新结果...
start "" "%~dp0dashboard.html"
pause
