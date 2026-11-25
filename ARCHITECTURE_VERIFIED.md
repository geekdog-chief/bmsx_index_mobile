# 真实系统架构图（已核对 main 代码的 import 与调用关系）

## 模块依赖图（箭头=实际 import/调用）
```
collectors.py ──▶ forum_cache.json
trading_calendar.py ──▶ (纯工具, 被 update_index/llm_overlay 使用)
sentiment.py ──▶ (纯工具词典, 被 update_index 使用; 被 update_index_llm 猴补丁替换其入口)
sector_catalog.py ──▶ (纯数据, 被 sector_heat/update_index_llm 使用)
channel_fairness_model.py ──▶ (被 update_index 使用)
community_archive.py ──▶ data/community_archive/
history_engine.py ──▶ data/history*.json
sector_heat.py ──▶ sectorIndices (被 update_index 使用)

llm_overlay.py ──读 forum_cache──▶ Ollama(gemma4:12b) ──▶ data/llm_scores_ollama.json
                                    ▲ 并发锁 .score.lock（整改包b已修, 待推送）

update_index.py (总控, import 以上全部8个模块)
   ▲ 猴补丁: update_index_llm.apply_overlays() 替换其 sentiment_score / contains_nonnegated /
     load_forum_cache / fetch_source 四个入口
update_index_llm.py ──▶ run_update() 统一入口
   ├─ data/study_emotion.csv (研究瘦日志, 按日 upsert)
   └─ _publish_snapshot() ──▶ history/history_meta/latest/data.js (四文件, 版本号+更新锁)

gui.py ──▶ update_index_llm.run_update()   （桌面唯一入口, 不重复实现业务）
推送数据含LLM.cmd ──▶ llm_overlay → update_index_llm → git push data.js
backfill_all.py ──▶ 循环 llm_overlay → 推送数据含LLM.cmd
fetch_price.py(独立) ──▶ study/price/*.csv （与主链零耦合）

前端: index.html → dashboard.html → dashboard.js ← data.js (sw.js 缓存壳 v18)
```
## 关键调用事实（验收者复核锚点）
- build_snapshot 内 source_analysis 逐帖调 sentiment_score —— 这是猴补丁生效点，指标口径的分水岭
- 历史追加发生在下一轮 build_snapshot 读 history.json 时 —— 四文件短暂不一致的自愈合点
- GUI/定时/命令行三入口全部汇于 run_update —— 业务逻辑只有一份
