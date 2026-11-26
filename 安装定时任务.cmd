@echo off
chcp 936 >nul
cd /d "%~dp0"
rem 2026-11-27 整改：全仓库唯一安装入口，一次装齐全部定时任务。
rem 原安装LLM自动任务.cmd 与 安装价格任务.cmd 已删除（任务名互踩+入口重复）；勿再建第二个安装器。
schtasks /Create /TN "宝妈指数每日更新" /TR "\"%~dp0推送数据含LLM.cmd\"" /SC DAILY /ST 15:40 /F
if errorlevel 1 (echo [失败] 宝妈指数每日更新 安装失败 & pause & exit /b 1)
schtasks /Create /TN "bmsx_fetch_price" /TR "\"%~dp0价格抓取.cmd\"" /SC DAILY /MO 3 /ST 16:20 /F
if errorlevel 1 (echo [失败] bmsx_fetch_price 安装失败 & pause & exit /b 1)
echo 安装成功：宝妈指数每日更新 15:40（完整版含LLM）
echo             bmsx_fetch_price 每3日16:20（价格抓取）
echo 断更检测：study\price\_last_run.txt 超过6日没更新 = akshare/东财挂了
pause
