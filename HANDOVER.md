> **本文档已被《单文件交接_最终版_20261125.md》取代（2026-11-27 核对）。**
> 方法论细节（n/(n+12) 平滑、persona 互斥等）在 METRICS_SPEC.md 均有同款且为现行口径；
> 本文保留仅供历史回溯，新窗口请勿以本文为准。

# HANDOVER — 任务交接（2026-11-24）

## 一、项目是什么
「宝妈·小白·散户」A股社区情绪指数 v6。从东方财富股吧、新浪股吧、同花顺板块社区、淘股吧
四个平台的公开讨论文本，按固定方法论（四渠道各25%席位、日权重50/30/20、n/(n+12)样本收缩、
persona互斥分类、板块热度不造区分度）合成三个分指数+综合温度。
不使用任何股价/成交量数据，观察情绪不预测涨跌。

## 二、当前状态：重写已完成并交付（不要再动核心方法论）
- 项目目录即本文件所在目录，全部代码/测试/前端在此，自包含
- 99个测试全绿：自有27个(tests/test_*.py) + 原版兼容72个(tests/compat_*.py)
- 真网验证：308源全通，17,397条文本样本，产出综合52/热度51.6
- 用户Windows真机已跑通（中文GBK系统）
- 交付包：/mnt/agents/output/bmsx_index_v6.zip

### 文件结构
- update_index.py  主入口：调度+评分编排+原子写+45min缓存续用+写入门槛+sqlite归档
- collectors.py    四平台采集（信号量限流10/5/3/6，无嵌套池；平台特判全在里面）
- sentiment.py     文本层：词典/否定与切换语境/persona互斥遮蔽/草根权重/营销过滤
- channel_fairness_model.py  纯函数四渠道聚合（evidence与正式分双口径）
- trading_calendar.py  2025/2026官方休市表硬编码+未知年份weekday回退+calendarMode暴露
- sector_heat.py / history_engine.py / fairness_model.py / sector_catalog.py / community_archive.py  原版保留
- dashboard.html/dashboard.js/styles.css  现有网页前端（用户认可其视觉，勿推翻重设计）
- tests/           测试即规格，改任何评分行为前先看对应测试
- data.example.js  dashboard字段样例

## 三、新任务：桌面GUI + 后台自动更新
把 dashboard.html 用 pywebview 套壳成桌面窗口；启动即显示旧数据，后台线程自动更新，
三态状态灯。这是唯一待办任务。

### 设计约束（用户已确认，不可妥协）
1. 可靠性优先：绿=已更新 / 黄=正在后台更新(带进度) / 红=更新失败但保留旧数据。
   任何情况下界面不留白、不闪退。
2. 五个选项卡，每页只答一个问题，禁止满屏信息：
   - 总览：综合温度环 + 三个分指数 + 热度/方向（只给"现在情绪怎样"）
   - 板块热度：30板块横截面榜（只给"钱在聊什么"，含"数据不足不硬排名"态）
   - 六信号：乐观/追涨/恐慌/新手/强度/关注 六条（只给"哪种情绪在动"）
   - 样本帖子：抽样原话+情绪标签（审计入口，只给"原话是什么"）
   - 系统状态：308源覆盖/写入门槛/缓存命中/失败源/日历模式（只给"这数能不能信"）
3. dashboard.html 视觉风格沿用现有，不重设计；草稿见 docs/ui_draft.html（可交互）

### 已否决的方案（用户同意过，勿再提）
- PySide6/Tkinter重写界面（工作量2-3倍且更丑）
- PyInstaller打包exe（60MB+、杀软误报、用户唯一无需自动更新器）
- 开机自启默认开（做成可选脚本，用户自己定）

### 落地步骤（按序执行）
1. update_index.build_snapshot 增加 progress_callback 参数：
   collect_sources 每完成一个源回调 {key, platform, done, total}；
   状态变化回调 {state:'run'|'ok'|'bad', message}。写盘逻辑保持现状（save_json/save_text原子写）。
   注意：cmd的main()流程必须完全不受影响。
2. 新建 gui.py：pywebview 窗口(1200x800) 加载 dashboard.html；
   后台 threading.Thread 调 build_snapshot→写盘→window.evaluate_js 推送状态；
   WebView2缺失时兜底 webbrowser.open 退回浏览器方案。
3. dashboard.js 增加 window.receiveGuiStatus(payload) 钩子：
   三态灯、进度条(平台x/N)、失败时显示"保留 xx:xx 旧数据"。
4. 新建 启动民间温度计.cmd：探测python(py/python3/py launcher) → 启动 gui.py。
5. 验收：99测试仍全绿；真跑一次；人为断网验证红态保留旧数据。

### 技术要点
- pywebview: pip install pywebview（唯一新增依赖，纯Python包小）；Win10/11自带WebView2
- 进度推送格式: {"state":"run","done":142,"total":308,"platform":"东方财富股吧"}
- 采集与UI线程分离，写盘走既有uuid原子写，无竞争
- 45分钟缓存续用意味着一天内第二次打开秒开，进度条多数时间直接跳完成
- 用户机器环境：中文Windows GBK代码页；Python经python-manager(msix)装好，py launcher可用；
  cmd必须GBK编码+CRLF+无BOM（UTF-8/BOM/LF都会导致闪退或乱码，已踩过坑）

## 四、改代码前必读
- tests/ 是规格书。四席位25%/日权重50-30-20/收缩不越界/persona互斥/热度不造区分度/
  并发写不崩/2025-2026日历，每条都有对应测试文件
- 不要引入除pywebview外的新依赖
- 不要改评分/采集/聚合逻辑，GUI任务只加表现层
- 2027年休市表：等上交所公布后在 trading_calendar.py 的 _OFFICIAL_HOLIDAY_RANGES 补区间，
  并把该年加进 OFFICIAL_YEARS


---

## 追加（2026-11-24 GUI任务会话进展）

**已完成：**
- update_index.py 表现层钩子：`collect_sources(..., on_source=None)`、`build_snapshot(progress_callback=None)`（内部拆 `_build_snapshot_core`），progress事件每源一条、state事件run/ok/bad，回调故障一律吞掉。main()不传参，行为零变化。99测试全绿。
- gui.py 完成：pywebview 1200x800、后台线程+节流推送（150ms泵）、js_api `update_now()`、失败红态读 data/latest.json 旧时间、WebView2缺失退回 webbrowser。
- UI草稿经19轮迭代定稿：`docs/ui_v8_draft.html`（v2–v7是过程版，以v8为准；曲线在参考、走势只留地图；地图标签四方向贪心防重叠+白描边；panel化后补了h2间距规则）。

**v4定稿结构（用户逐条确认过）：**
- 3主选项卡：**总览**（宝妈指数温度环→贪婪恐惧→宝妈热度[最热/最冷子切换]）；**走势**（仅宝妈地图，独占窗口）；**参考**（原帖抽样→指数参考→宝妈曲线）。状态条全局置顶：三态灯+最近交易日+更新于+进度条+立即更新按钮。
- 品牌名「宝妈指数」；单指数展示，三子指数（宝妈/小白/散户）压到底部「指数参考」参考位，各带原版口径小字。
- 宝妈地图=原版四象限散点画法（横轴方向35-65、纵轴热度20-90、气泡=三交易日样本量、四象限计数卡），非瓦片。
- 贪婪恐惧：贪婪=六项50中性信号均值经Sigmoid放大（中心50、坡度4，约52→64）；恐惧=恐慌原始分。同量程0-100、0为基准。注解写明"仅显示端合成"。
- 社区热度与方向信号节已废，其panic注解小字并入贪婪恐惧；posts不显示persona标签。

**已完成（GUI任务交付，2026-11-24）：**
1. dashboard.html / dashboard.js / styles.css 已按v8定稿重写接真实字段（假DOM验证：30地图点/12排行/曲线/三卡全渲染，贪婪=Sigmoid六项合成、恐惧=panic原始分）
2. 启动民间温度计.cmd 已交付：GBK+CRLF+无BOM，py→python→python3探测，缺pywebview自动pip install
3. 99测试全绿；py_compile gui.py/update_index.py 通过
4. docs/ui_v8_draft.html 保留作视觉对照；v2–v7过程稿已清理
5. 地图坐标改为数据自适应（实际分布±25%padding，保底跨度8，50中性线随动；修复真数据全挤中心的问题）
6. 修复：receiveGuiStatus的ok分支原先不切绿灯只等刷新，状态卡在"正在后台更新"；现补setState绿+时间戳+400ms刷新。黄态文案带平台名（正在读取：东方财富股吧（142/308））
7. 真机验收项（用户执行）：双击cmd三态切换、断网红态"保留xx:xx旧数据"、45分钟缓存秒开、立即更新按钮


---

## 追加（2026-11-24 手机版任务交付）

**方案（用户已确认）：电脑当采集机，GitHub Pages 当展示端。放弃 GitHub Actions 采集（美国IP可能被东财封）。**

**新增文件：**
- `index.html` — 跳转页，让 GitHub Pages 根网址（用户名.github.io/仓库名/）可直接打开；桌面 gui.py 仍加载 dashboard.html，零影响
- `manifest.webmanifest` + `icon-192.png`/`icon-512.png` — PWA 配置与图标（程序生成的仪表盘图标）
- `sw.js` — Service Worker：界面缓存优先秒开，**data.js 网络优先**（保证指数永远最新，断网才回落缓存）
- `推送数据.cmd` — 一键：跑 update_index.py → git add/commit/push data.js。git 探测顺序：PATH → GitHub Desktop 内置 git
- `安装定时任务.cmd` — schtasks 注册每天 15:40 自动跑推送数据.cmd（A股15:00收盘+40分钟采集）
- `卸载定时任务.cmd` — 删除该计划任务
- 三个 cmd 均 GBK+CRLF+无BOM，不带 chcp（沿用已验证写法）

**改动文件（纯表现层）：**
- `dashboard.html` — head 加 theme-color/apple-mobile-web-app/ manifest 链接/图标；body 尾加 sw.js 注册脚本（8行）
- `styles.css` — 替换末尾原有的一条 @media(max-width:760px) 为完整手机适配块（状态条、温度环、地图高度64vh、排行、三卡单列），桌面断点以上零变化

**用户待办（真机验收）：**
1. 装 GitHub Desktop 并登录，建公开仓库 bmsx-index，用 Desktop 把项目 push 上去
2. Settings → Pages → Deploy from a branch → main/(root) → Save
3. 手机访问 https://用户名.github.io/bmsx-index/ → 添加到主屏幕
4. 双击 安装定时任务.cmd；当天手动跑一次 推送数据.cmd 验证
5. 验收：三态正常、手机三页可滑、断网显示旧数据、第二天15:40后时间戳自动更新

**注意：本zip仍不含 tests/ 目录，99个unittest未在本地验证（改动均为新增文件+纯表现层，未碰任何.py）。**


---

## 追加（2026-11-24 LLM评分实验交付）

**方案（用户已确认）：先半自动——用户每天把帖子包拖给免费Kimi打分，对比两周后决定要不要上 Ollama 本地模型或 Gemini API（用户已有：本机装了Ollama、有Google账号）。**

**新增文件（纯新增，13个原有.py与GUI迁移包字节级一致，未动任何采集/评分/聚合逻辑）：**
- `llm_export.py` — 从 data/community_archive/ 既有档案只读取样（近3交易日，每源×日1帖，总上限200帖），用 sentiment/update_index 的现成纯函数算词典分数作基准，输出 llm_package_N.json（每文件<=50帖，防Kimi输出截断）+ llm_package_with_dict.json（基准）
- `llm_compare.py` — 严格校验 llm_scores_*.json（JSON数组/id唯一/direction限-3~3/未知id拒绝），脏数据不出报告；输出 llm对比报告.md（方向一致率、信号级差值表、分歧Top10、口径说明）
- `发给Kimi的指令.md` — 固定prompt（方向分-3~3、五个0/1标记、persona四选一、讽刺/AI文few-shot、纯JSON输出纪律），用户一字不改照贴
- `导出帖子包.cmd` / `对比LLM结果.cmd` — GBK+CRLF+无BOM

**用户操作流程：** 更新指数（定时任务照旧）→ 双击导出帖子包.cmd → 按指令.md把 llm_package_N.json 逐个拖给Kimi → 回复逐个保存为 llm_scores_N.json → 双击对比LLM结果.cmd 看报告。

**测试情况（zip仍不含tests目录，以下为本会话手工验证）：**
- llm_export.py 假档案全链路通过：8帖导出、词典分数与主流程同口径（讽刺帖"又要涨了呵呵"词典判+1——正是LLM要解决的失败案例）
- llm_compare.py 正例出报告；脏数据（越界direction/未知id/非JSON）全部正确拒绝
- 已知口径差异写进报告：词典信号经tanh饱和、LLM标记率为线性，只看差的方向和量级
- 踩坑记录：previous_trading_days 直接返回 date 对象不可 fromisoformat；post_age_days 的 as_of 必须带时区（CN_TZ）


---

## 追加（2026-11-24 测试恢复）

用户补传 tests.zip（12个文件：5个test_*.py + 7个compat_test_*.py），已并入 tests/。
99个unittest全绿（0.075s）。注意：compat_test_*.py 不以 test 开头，发现命令必须用
`python -m unittest discover -s tests -p "*test*.py"`，用默认 pattern 只会跑到27个。
