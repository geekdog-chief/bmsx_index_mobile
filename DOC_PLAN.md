# 文档修订计划（核对结果 2026-11-25 深夜）

## 已有且与代码一致（本轮刚生成，验收者可抽验）
PROJECT_ARCHITECTURE.md / MODULE_MAP.md / DATA_FLOW.md / METRICS_SPEC.md / KNOWN_ISSUES.md / RESEARCH_PLAN.md / ARCHITECTURE_VERIFIED.md / RISK_AUDIT.md / REMEDIATION_PLAN.md / RELEASE_PROCESS.md

## 待补（按价值排序，都是简版）
| 文档 | 内容 | 优先级 |
|---|---|---|
| DATA_SCHEMA.md | 关键数据文件字段级 schema：forum_cache 帖子字段、STORE 评分条目字段、study_emotion.csv 七列、snapshot 的 signals/sectors 键全集 | 高——新窗口 AI 的查表手册 |
| TEST_PLAN.md | 66 用例的分层清单（单元/集成/E2E 占位）+ 每层验收标准 + 如何本地跑 | 中 |
| ROLLBACK.md | 给用户的 3 步回滚图文（GitHub Desktop revert 到 tag） | 中 |
| RELEASE_LOG.md | 从 2026-11-25 起每次推送一行（日期/tag/变更摘要）；2026-11-25 之前的历史用 git log 追 | 低 |
| CHANGELOG.md | 与 RELEASE_LOG 合并即可，不单独建 | 不建 |

## 废弃/合并
- HANDOVER.md / 交接_羊驼接管版.md / 发给Kimi的指令.md —— 历史交接材料，信息已全部沉淀进上述文档；建议移入 archive/ 或删除（用户裁）
- llm_package_*.json / llm_scores_*.json（根目录实验残渣）—— 合规清理已将其移出版本控制，本地文件用户自决
