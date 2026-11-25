# DATA_FLOW.md —— 数据流图

## 主链（每日 15:40 自动）
```
东财/新浪/同花顺/淘股吧 ──collectors.py──▶ data/forum_cache.json（2万+帖，含质量字段）
                                                │
llm_overlay.py ◀── Ollama gemma4:12b ◀── 未评分帖（最新300/轮，B=7/包，temp=0）
   │  norm/validate_batch 硬校验（非法重试/拆包，错标永不入库）
   ▼
data/llm_scores_ollama.json（每批7条原子落盘，按id查重）
   │
update_index_llm.run_update()
   ├─ apply_overlays：NOW刷新 / load_llm_scores 多维索引（id→(标题,源)→唯一标题→歧义词典兜底）
   │                / dedup_cache（按id去重） / sentiment_score、contains_nonnegated 猴补丁
   ├─ build_snapshot（冻结层）：3交易日窗口帖子 → 方向/强度/panic → signals + sectors + community
   ├─ compute_llm_emotion：15天窗口(30/60/全期兜底≥30条) → 平台均值→25%×4→÷3×100 → llmFear/llmGreed+覆盖字段
   ├─ compute_llm_sector_emotion：板块追溯(15/30/60/全期≥3条) → 温度=50+(贪-恐)/2 → 热度=80%活动度+20%情绪 → 重排名
   ├─ collect_llm_sample：近30天抽24条（强方向8/强情绪8/营销4/平静4）→ llmSample
   ├─ _publish_snapshot：暂存→四文件校验→依序替换（data.js最后）→ snapshotVersion
   └─ append_study_log：按交易日 upsert → data/study_emotion.csv（含窗口/平台覆盖列）
   ▼
git push data.js → GitHub Pages → sw.js(缓存壳) → 手机 PWA
```
## 研究链
- 价格：16:20 计划任务 → study/fetch_price.py → study/price/*.csv（增量+心跳）
- 对齐：情绪 15:40 快照 vs ETF 次日开盘收益（RESEARCH_PLAN §2.3）；180 交易日（约 2027-08-04）正式检验
## 关键不变量
- STORE 是唯一评分事实源；换代码不伤 STORE；同时只许一个评分进程（.score.lock）和一个更新进程（.update.lock）
- 页面任何时刻只见完整旧版或完整新版 data.js
