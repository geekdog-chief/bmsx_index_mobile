@echo off
chcp 936 >nul
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY where python3 >nul 2>nul && set PY=python3
if not defined PY (echo [错误] 未找到 Python，请先安装 Python 3.10+ & pause & exit /b 1)
%PY% -c "import webview" 2>nul
if errorlevel 1 (
  echo 首次运行，安装 pywebview...
  %PY% -m pip install pywebview
)
%PY% gui.py
if errorlevel 1 pause
