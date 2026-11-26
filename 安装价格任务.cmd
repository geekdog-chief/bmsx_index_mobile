@echo off
chcp 936 >nul
cd /d "%~dp0"
schtasks /Create /TN "bmsx_fetch_price" /TR "\"%~dp0价格抓取.cmd\"" /SC DAILY /MO 3 /ST 16:20 /F
if %errorlevel%==0 (
  echo 安装成功：每3日16:20自动抓取ETF价格，日志 study\price_sync.log
  echo 断更检测：study\price\_last_run.txt 超过6日没更新 = akshare/东财挂了
) else (
  echo 安装失败，请尝试以管理员身份运行
)
pause
