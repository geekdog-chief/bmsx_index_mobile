# MODULE_MAP.md —— 文件职责地图

## 运行链文件（改动需最谨慎）
| 文件 | 职责 | 改动权限 |
|---|---|---|
| update_index.py | 总控：采集调度（collect_sources）、build_snapshot、signals、sectors、四源数据路径常量 | 冻结只读 |
| update_index_llm.py | 叠加层统一入口：load_llm_scores 索引、多维键防错配、dedup_cache、情绪聚合（15天窗口+兜底）、板块情绪注入、抽检、瘦日志、_publish_snapshot、双锁 | 用户层主战场 |
| llm_overlay.py | 评分器：collect_posts→Ollama→norm/validate_batch 硬校验→STORE；B=7/包、num_ctx=32768、BACKFILL_DONE 哨兵、评分锁 | 用户层 |
| gui.py | Tkinter 桌面窗口；_run_update 调 update_index_llm.run_update | 仅 _run_update 已改 |
| collectors.py | 四平台采集（东财/新浪/同花顺/淘股吧），记录 rawRows/coverageDays/capHit 等质量字段 | 冻结只读（质量字段消费缺位=KNOWN_ISSUES A7） |
| sentiment.py | 词典：FOMO/PANIC/INTENSE 词表、sentiment_score 量纲 -3..+6 | 冻结只读 |
| sector_heat.py | 板块热度 v4：attention/activity/legacy-emotion 双轴、MAD 横截面校准、可靠性收缩、离散度闸门、rankEligible | 冻结只读 |
| sector_catalog.py | 30 板块×3 代表股主归类（板块情绪映射的数据源） | 冻结只读 |
| fairness_model.py / channel_fairness_model.py / history_engine.py / community_archive.py / trading_calendar.py | 原作者：渠道公平/历史引擎/归档/交易日历 | 冻结只读 |

## 前端（GitHub Pages）
| 文件 | 职责 |
|---|---|
| index.html | 壳：加载 dashboard.html |
| dashboard.html | 页面结构：总览/走势/参考三 tab、宝妈情绪卡、热度榜、羊驼抽检板块 |
| dashboard.js | 全部渲染逻辑；renderGreedFear/renderRank/renderLlmaudit；判空先 !=null（缺失显"—"）；emotionTier 双侧分档 |
| sw.js | 缓存壳：SHELL 缓存优先；**改任何壳文件必须升 bmsx-vN** |
| styles.css | 样式含 .gfchip 徽章 |
| data.example.js | 演示数据（941KB，勿删） |

## 脚本与工具
| 文件 | 职责 |
|---|---|
| backfill_all.py | 补跑循环（读 llm_overlay 输出行/BACKFILL_DONE 判完） |
| study/fetch_price.py | 13 只 ETF 日线：增量（_last_date 续抓）+merge_frames 去重+限流重试+心跳 _last_run.txt |
| sync_all.py | 双向同步脚本（用户已弃用自动推送，留档） |
| run_ollama_scoring.py / llm_export.py / llm_compare.py | 实验残渣（含 2018 实验格式），非运行链 |
| tests/test_llm_overlay_fixes.py | 66 用例：键控/校验/窗口/聚合/发布/锁/CSV/合并全专项 |
| RESEARCH_PLAN.md | 研究宪法（含运行日志 §8） |
| 各 .cmd | 任务计划安装/补跑/推送/启动 GUI；批处理只做薄壳，逻辑全在 .py |
