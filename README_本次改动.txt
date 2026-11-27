bmsx 交付包 2026-11-27（sw v23）

一、前端清理（86 测试两轮全绿）
  dashboard.html  删立即更新按钮 + .grow 垫片 + 进度条 div
  dashboard.js    删 btnupd/progwrap/prog 声明、bindUpdate 定义与调用
  styles.css      删 .btn×2/.btn:disabled/.progress×3 六条死规则
  sw.js           CACHE bmsx-v22 → bmsx-v23

二、K11/K12 台账
  KNOWN_ISSUES.md  新增 K11（文档漂移，开放）/ K12（cmd 编码漂移，已修复）

三、K12 修复工具（双击 一键统一cmd编码.cmd）
  全部根 .cmd 转 GBK+CRLF+无 BOM，幂等；报错窗口会停住不闪退

用法：解压覆盖 → 双击 一键统一cmd编码.cmd → push → 手机杀进程重开验收 sw=v23。
