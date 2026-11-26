# TEST_PLAN.md —— 测试分层与验收标准

## 运行命令
`python -m unittest discover -s tests -p "test_*.py"` —— 当前 **86 用例（2026-11-27 实跑 55.7s）**，验收线：全过且 ≤90s（含 E2E 全链 62s）。

## 四层结构（对应 RELEASE_PROCESS.md §四）
| 层 | 覆盖 | 现状 |
|---|---|---|
| A 单元 | 键控/校验/窗口/聚合/发布/锁/CSV/合并 | 60+ 用例，tests/test_llm_overlay_fixes.py |
| B 集成 | 评分→指标→发布 各段（TestAuditBatch1/2、Round2） | 已覆盖 |
| C E2E | TestE2E：模拟四平台缓存+注入评分 → run_update 全链 → 断言四文件/四指标/日志/抽检/版本号（不触网不触 Ollama） | 已建（62s/次） |
| D 真实环境 | Windows+Ollama 实测：中断/重跑/并发/坏文件 | 用户侧验收项，见各交付包验收清单 |

## 故障注入清单（已实装）
替换中途磁盘故障（data.js 保旧）/构建异常锁释放/锁原子与死锁接管/CSV 损坏不覆盖/无 akshare 返回 False/锁冲突 UpdateSkipped+exit 2。

## 已知不覆盖（如实声明）
前端渲染（沙箱无浏览器，人工目检项）；东财真实接口（限流策略靠心跳人工监视）；原作者并发写测试 2 例 Windows 固有失败（非运行链）。
