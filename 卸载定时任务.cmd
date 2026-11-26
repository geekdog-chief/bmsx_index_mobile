@echo off
rem 2026-11-27 整改：补删 bmsx_fetch_price（原只删每日更新任务，价格任务成漏网之鱼）
schtasks /delete /tn "宝妈指数每日更新" /f
schtasks /delete /tn "bmsx_fetch_price" /f
pause >nul
