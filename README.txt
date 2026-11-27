K12 撤销包 2026-11-27

背景：K12（cmd 编码漂移）系误报——仓库 cmd 始终为 GBK，
是验证侧抓包工具把 GBK 静默转成 UTF-8 造成的假象。

操作（GitHub Desktop）：
1. 解压 KNOWN_ISSUES.md 覆盖到仓库根
2. 删除 fix_cmd_encoding.ps1 和 一键统一cmd编码.cmd（已无必要）
3. Summary: K12 撤销（编码误报）；删无用工具 → Commit → Push
