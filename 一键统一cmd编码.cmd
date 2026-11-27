@echo off
chcp 936 >nul
echo 正在统一 cmd 编码（GBK+CRLF+无BOM）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_cmd_encoding.ps1"
echo.
echo 处理完毕，窗口不会自动关闭；可直接关窗。
pause
