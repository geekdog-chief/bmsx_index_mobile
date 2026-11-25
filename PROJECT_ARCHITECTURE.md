# PROJECT_ARCHITECTURE.md —— 项目架构总说明

> 任何 AI/开发者修改本仓库前必读。本文档描述 2026-11-25 整改后的真实架构，修改代码后必须同步更新本文档。

## 一、项目一句话

A 股散户情绪指数（宝妈指数）：采集四个股票社区帖子 → 本地 LLM（gemma4:12b，Ollama）逐帖打分 → 聚合为指数/情绪/板块三类指标 → 生成静态 data.js → GitHub Pages 供手机 PWA 查看。另有情绪有效性研究子项目（见 RESEARCH_PLAN.md）。

## 二、分层架构（冻结层 + 叠加层）

```
┌─ 前端层（GitHub Pages 静态）────────────────────────────┐
│ index.html → dashboard.html + dashboard.js + styles.css │
│ sw.js = Service Worker 缓存壳（缓存优先，改壳必升版本号） │
│ 唯一数据入口：data.js（全局 window.INDEX_DATA）           │
├─ 发布层 ────────────────────────────────────────────────┤
│ _publish_snapshot()：暂存→四文件校验→依序替换（data.js 最后）│
│ 版本标识 snapshotVersion；进程锁 data/.update.lock        │
├─ 叠加层（2026-11-25 起，用户自有，可改）──────────────────┤
│ update_index_llm.py  统一入口 run_update()：打猴补丁+构建+发布 │
│ llm_overlay.py       评分器：缓存帖→Ollama→llm_scores_ollama.json│
│ backfill_all.py      全量补跑循环；study/fetch_price.py 价格抓取│
├─ 冻结层（原作者，只读，修复一律叠加层/monkeypatch）─────────┤
│ update_index.py 12万字节总控：采集调度/快照构建/signals/发布主链│
│ sentiment.py    词典情绪；collectors.py 四平台采集器        │
│ sector_heat.py  板块热度模型（v4 公司 Equal）；sector_catalog.py │
│ channel_fairness_model / fairness_model / history_engine /   │
│ community_archive / trading_calendar                       │
├─ 数据层（data/，gitignore 各就各位）──────────────────────┤
│ forum_cache.json 帖子缓存；llm_scores_ollama.json 评分库     │
│ study_emotion.csv 研究瘦日志；history*.json/latest.json      │
└──────────────────────────────────────────────────────────┘
```

## 三、三条流水线

1. **产品链**（定时 15:40）：安装LLM自动任务.cmd 注册的 schtasks → 推送数据含LLM.cmd → llm_overlay.py 打分 → update_index_llm.py（python 入口）→ git push data.js
2. **补跑链**（手动）：补跑全量.cmd → backfill_all.py 循环 llm_overlay 至清空 → 自动调推送数据含LLM.cmd
3. **研究链**（自动）：16:20 价格任务（bmsx_fetch_price）→ study/price/*.csv；瘦日志随每次更新追加

## 四、人机分工（铁律）

AI 出包 → 用户解压覆盖验收 → **推送 Button 永远在用户手里**。指标层（词典/25%权重/-3..+3→-3..+6 不对称映射/板块热度公式/MAX=300/num_predict=16384）冻结，工程层可改。冻结层文件本体只读。

## 五、当前版本

sw 缓存壳 v18；测试 66 用例（tests/test_llm_overlay_fixes.py）；整改状态见 KNOWN_ISSUES.md。
