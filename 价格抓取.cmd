@echo off
cd /d "%~dp0"
py "%~dp0study\fetch_price.py" >> "%~dp0study\price_sync.log" 2>&1
