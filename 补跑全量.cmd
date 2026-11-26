@echo off
cd /d "%~dp0"
rem 2026-11-27 整改：py/python/python3 三级探测，找不到时停屏报错（原硬编码 py 会哑巴闪退）
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )
if not defined PY ( echo [错误] 找不到 Python & pause & exit /b 1 )
%PY% backfill_all.py
pause
