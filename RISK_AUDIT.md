# 工程风险审计表（2026-11-25 深夜 · 对照 GitHub main 与整改包双层状态）

| 编号 | 模块 | 级别 | 代码证据 | 后果 | 状态 |
|---|---|---|---|---|---|
| P0-1 | 发布链 | P0 | 推送数据含LLM.cmd 第1步 `%PY% llm_overlay.py` 后无 errorlevel 检查；run_update 锁跳过时 return None 但 exit 0 | 评分崩溃/锁冲突仍继续更新+推送；"更新指数"提交可能是空转 | **main 存在，待修**（方案：cmd 加 errorlevel 拦截 + llm_overlay 非零阻断 + 锁跳过 exit 2） |
| P0-2 | 评分层 | P0 | main 版 llm_overlay 锁=先查后写非原子 | 双评分进程竞态丢批次 | **专项包已修 v2**：O_EXCL 原子创建+失败方轮询等胜者写 PID（封空锁窗口）；双进程竞争测试实证恰好一个胜出。待用户覆盖推送，main 仍为旧代码 |
| P0-3 | 评分层 | P0 | 实证两处：update_index.py 的 save_json 原子；但 **llm_overlay.py 一直用裸 write_text 直写 STORE**（真缺口） | 进程中断→半截 JSON 毁整个评分库 | **专项包已修**：_atomic_save_store（临时文件→回读校验→替换→再验）+两路径故障注入测试实证原库字节不动。待推送 |
| P1-1 | 采集层 | P1 | K1：rawRows/coverageDays/capHit/coverageStatus 记而不消费 | 部分采集当完整数据进指数 | 开放，方案待用户批（质量门会改指数输入） |
| P1-2 | 指标层 | P1 | 猴补丁架构：四入口隐式替换 | 长期调试困难、隐式依赖 | 维持现状，调用关系已图化（ARCHITECTURE_VERIFIED）；显式接口化为长期项，不动 |
| P1-3 | 测试 | P1 | 66 用例全专项，无端到端 | 模块间组合故障漏检 | 待建：模拟 cache+STORE 跑通 run_update 全链的 E2E 测试 |
| K2-K10 | 各层 | P2/低 | 见 KNOWN_ISSUES.md | 已披露 | 按台账管理 |

纯文本版：P0-1 真存在待修（退出码链）；P0-2/P0-3 整改包已修待推送；K1 待批；猴补丁维持；E2E 待建。

**已确认 vs 仅风险**：P0-1/P0-2/P1-1 有代码证据=已确认；P0-3 残余竞态为理论风险（写层原子已实证）。
