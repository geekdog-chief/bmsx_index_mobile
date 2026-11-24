@echo off
cd /d "%~dp0"
title 导出LLM帖子包
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )
if not defined PY ( echo [错误] 找不到 Python & pause & exit /b 1 )
%PY% llm_export.py
pause
