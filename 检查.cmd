@echo off
chcp 936 >nul
cd /d "%~dp0"
title 宝妈指数系统体检（只读，不改任何数据）
rem 2026-11-27 建立 v2：一键体检。只读汇总，绝不写入。
rem v2 修复：python 单行调用移出括号块（括号会截断块解析导致闪退），双引号全改 chr(34)。

echo ============================================
echo   宝妈指数系统体检  %date% %time:~0,8%
echo ============================================
echo.

rem ===== 块1：两个定时任务状态 =====
echo [定时任务]
set "T1_LAST=" & set "T1_RC=" & set "T1_NEXT="
schtasks /Query /TN "宝妈指数每日更新" /V /FO LIST > "%TEMP%\qc_t1.txt" 2>nul
for /f "tokens=1,* delims=:" %%a in (%TEMP%\qc_t1.txt) do (
  if "%%a"=="上次运行时间" set "T1_LAST=%%b"
  if "%%a"=="上次结果" set "T1_RC=%%b"
  if "%%a"=="下次运行时间" set "T1_NEXT=%%b"
)
if not defined T1_LAST (echo   [缺失] 宝妈指数每日更新 任务不存在或查询失败) else (goto T1JUDGE)
goto T1DONE
:T1JUDGE
set "T1_RC=%T1_RC: =%"
if "%T1_RC%"=="0" (echo   [OK] 宝妈指数每日更新：上次运行%T1_LAST% 结果成功，下次%T1_NEXT%) else (echo   [注意] 宝妈指数每日更新：上次结果码 %T1_RC%（0=成功；非0要查），下次%T1_NEXT%)
:T1DONE
set "T2_LAST=" & set "T2_RC=" & set "T2_NEXT="
schtasks /Query /TN "bmsx_fetch_price" /V /FO LIST > "%TEMP%\qc_t2.txt" 2>nul
for /f "tokens=1,* delims=:" %%a in (%TEMP%\qc_t2.txt) do (
  if "%%a"=="上次运行时间" set "T2_LAST=%%b"
  if "%%a"=="上次结果" set "T2_RC=%%b"
  if "%%a"=="下次运行时间" set "T2_NEXT=%%b"
)
if not defined T2_LAST (echo   [缺失] bmsx_fetch_price 任务不存在或查询失败) else (goto T2JUDGE)
goto T2DONE
:T2JUDGE
set "T2_RC=%T2_RC: =%"
if "%T2_RC%"=="0" (echo   [OK] bmsx_fetch_price：上次运行%T2_LAST% 结果成功，下次%T2_NEXT%) else (echo   [注意] bmsx_fetch_price：上次结果码 %T2_RC%（0=成功；负数为未登录/锁屏导致没启动；非0要查），下次%T2_NEXT%)
:T2DONE
del "%TEMP%\qc_t1.txt" "%TEMP%\qc_t2.txt" >nul 2>nul
echo.

rem ===== 块2：价格抓取心跳 =====
echo [价格抓取（东财）]
if not exist "study\price\_last_run.txt" (echo   [缺失] study\price\_last_run.txt 不存在（价格抓取从未跑过）) else (goto P2)
goto P3
:P2
type "study\price\_last_run.txt"
findstr /c:"banned: 1" "study\price\_last_run.txt" >nul && echo   [注意] 东财风控熔断中：停手等解封，勿手动连跑
:P3
echo.

rem ===== 块3：主链数据新鲜度 =====
echo [主链数据]
if not exist data.js (echo   [缺失] data.js 不存在) else (goto D2)
goto D4
:D2
forfiles /m data.js /d -1 >nul 2>nul && (echo   [注意] data.js 超过 1 天没更新——主链可能断了，先查 15:40 任务) || (echo   [OK] data.js 24 小时内更新过)
set "PYEXE="
python -c "import sys" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE ( py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3" )
if not defined PYEXE ( python3 -c "import sys" >nul 2>nul && set "PYEXE=python3" )
if not defined PYEXE (goto D3)
%PYEXE% -c "t=open('data.js',encoding='utf-8').read(); q=chr(34); i=t.find(q+'updatedAt'+q); v=t[i+14:i+40].split(q)[0] if i>=0 else ''; print('  data.js 更新于:', v if v else '未找到')" 2>nul
goto D4
:D3
echo   [跳过] 未找到 Python，读不了 data.js 时间戳
:D4
echo.

rem ===== 块4：羊驼打分存量 =====
echo [羊驼打分]
if not exist "data\llm_scores_ollama.json" (echo   [缺失] data\llm_scores_ollama.json 不存在（羊驼从未打分）) else (goto S2)
goto S4
:S2
if not defined PYEXE (goto S3)
%PYEXE% -c "import json; d=json.load(open('data/llm_scores_ollama.json',encoding='utf-8')); xs=d if isinstance(d,list) else list(d.values()); print('  STORE 累计:', len(xs), '条；含 fear 打分:', sum(1 for x in xs if isinstance(x,dict) and 'fear' in x), '条')" 2>nul
goto S4
:S3
echo   [跳过] 未找到 Python，读不了 STORE
:S4
echo.

echo ============================================
echo  体检完。所有项都是只读，没有改动任何数据。
echo  看到 [注意]/[缺失] 先别急，对照各块提示处理；拿不准就截图发给 Kimi。
echo ============================================
pause
