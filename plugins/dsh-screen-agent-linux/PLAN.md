# dsh-screen-agent-linux — 重写蓝图

> 目标：给 DSH 装上 Linux 版的"眼睛 + 手"（对标 codex computer use），
> 借鉴 `Arthur303Author/dsh-plugins` 的 `dsh-screen-agent`（Windows 版）**手法**，
> 代码干净重写，不做逐行移植。
>
> 状态：**规划阶段**，尚未写任何插件代码。本机实测数据见下方"依据"。
> 撰写：2026-09-26

---

## 0. 本机实测依据（只读实验，非推测）

| 能力 | 实测结果 | 结论 |
|---|---|---|
| 桌面会话 | Ubuntu 24.04.5，**X11**（`XDG_SESSION_TYPE=x11`，`DISPLAY=:1`，`XAUTHORITY=/run/user/1000/gdm/Xauthority`） | XTest 路线可用 |
| 截图 | Pillow `ImageGrab`（`features.check_feature('xcb')=True`）→ **2560×1600 RGB** 成功 | 零依赖 |
| 视觉链路 | 截图以 **1011×632 = 638,952 px** 进入模型上下文，画面内容可读 | 可用；缩放比 **2.532** |
| 输入注入 | `libXtst.so.6` → `XTestQueryExtension=1`，XTest **2.2** | 零依赖，**不需要 ydotool** |
| 指针查询 | `XQueryPointer` → (1321,662) + 落点窗口 `0x2e00017` | 落点保护可行 |
| 键盘映射 | `XStringToKeysym('Return')=0xff0d` → keycode 36 | 可用 |
| 窗口元数据 | `WM_CLASS="Navigator","firefox_firefox"`、`_NET_WM_NAME`、`_NET_WM_PID=5745`、`_NET_ACTIVE_WINDOW` | 齐全 |
| 元素树 | AT-SPI2 通（14 app 注册），**Firefox 不在列表**、GNOME Terminal 只暴露空 panel、clash-verge(Tauri) 只有外壳；`toolkit-accessibility=false` | **覆盖不足，待探针量化** |
| 已装依赖 | `python3-gi 3.48.2`、`at-spi2-core 2.52.0`、`gir1.2-atspi-2.0`、`libatk-bridge2.0-0`、`Pillow 10.2.0`、`xprop`/`xwininfo`/`xwd` | **零新依赖** |
| 未装但不需要 | `xdotool`、`ydotool`、`scrot`、`tesseract`、`xclip` | — |

**关键反转**：`dsh-ubuntu-kit/README.md` 早年判断"需 `ydotool` 输入"。实测证明
XTest 更优：`ydotool` 要 `/dev/uinput` 权限 + udev 规则；XTest 是 X server 内建扩展，
普通用户即可用。

---

## 1. 参考库手法提炼（重写要继承的 36 条）

### 1.1 Host 半：注册与生命周期

1. **运行时零 import**：手写 ESM，不 import 任何 dsh 包 → 少一个加载失败点，也无构建步骤。
2. **硬依赖 / 软依赖分离**：`inject: ['tools']`（没有 tools 就整体等待）；
   `agents` 用 `ctx.inject(['agents'], cb)` 软取 → 没有 agents 的 profile 仍能用工具。
3. **每个注册都挂 `ctx.effect`**：`ctx.effect(() => ctx.tools.register({...}), label)` →
   热重载 / uninject 时零残留。
4. **`agent.ctx` 上的注册不会随插件卸载自动销毁** → 必须自己存 disposer，插件卸载时统一释放。

### 1.2 Host 半：sidecar 通信

5. **协议**：`execFile(python, [sidecar])` + stdin 一个 JSON + stdout 一个 JSON，
   信封 `{ok: true, ...}` / `{ok: false, error}`。无守护进程、无端口、无临时文件。
6. **子进程参数**：`timeout: 60000`、`maxBuffer: 64MiB`（inline base64 PNG 要余量）、
   `child.stdin.on('error', ()=>{})` 吞 EPIPE、`signal` → `child.kill()`、stderr 仅作错误详情截 500 字。
7. **解释器探测异步化 + 缓存 promise + 在 `apply()` 里预热**：
   `void ensurePython().catch(() => {})`，绝不阻塞宿主事件循环。
8. **候选链**：`DSH_SCREEN_AGENT_PYTHON` env → `python` → `python3`，逐个 `-c "import PIL"` 探测。

### 1.3 Host 半：图像进上下文（本项目最核心的一条链路）

9. **回图三步**：sidecar 返回 base64 → `Buffer.from(b64,'base64')` →
   `ctx.get('attachments').saveImages([{ data: Uint8Array, mediaType:'image/png', name }])`。
10. **`output.render` 决定模型看到什么**：返回
    `[{type:'image', attachment: ref}, {type:'text', text: note}]`；output schema 里
    `image` 用 `additionalProperties: true`（不约束附件 ref 形状）。
11. **双重降级，永不硬失败**：
    - 没有 attachments 服务 → 落盘 + 文本路径（`textFallback`）
    - 当前模型 route 不接受 image（`llm.resolveModelInfo(provider,model).inputModalities`
      不含 `'image'`）→ 同样落盘 + 文本路径
    - 全部包在 try 里，探测失败一律**当作支持图像**（乐观默认，避免误降级）
12. **`note` 是给模型的操作说明书**，不是日志。例：zoom 的 note 直接给出换算公式
    `nx = nx0 + nxLocal*宽`；look 的 note 说明 `0,0` 左上、`1,1` 右下。
13. **动作后自动截图**：`await sleep(350)` → capture → 合并 note →
    一次工具调用闭环 see-think-act；`capture:false` 可关。

### 1.4 Host 半：工具面设计

14. **参数形状合法但值非法 → 交给 sidecar 报错**（裸注册不经 registry 校验），
    代价是自担校验责任，收益是不多一个加载点。
15. **半坐标绝不退化**：只给 `nx` 不给 `ny` → 原样传给 sidecar 报错，
    **绝不**退化成"在原地按一下"（等于朝无人指定的地方开火）。
16. **`move` 与 `click` 拆成两个工具**：真实原因是——"测量→点击"之间的光标移动会让
    浮层菜单重定位，于是测到的坐标在按下时已失效，表现为稳定"点偏一行"。
    正确流程：`screen_move` 就位 → 截图确认 → `screen_click` 原地按（零位移）。
17. **描述里直接写选路哲学**：键盘 > 元素 > 坐标 > 截图；`screen_elements` 的描述
    主动说"能用无障碍树就别读截图，它免疫 DPI/主题/布局漂移且不花图像 token"。

### 1.5 Host 半：分批放出（staging）

18. **三阶段**：0 看（1 个）→ 有了任意 `tool/call` 解锁阶段 1 读（6 个）→
    用过任一阶段 1 工具解锁阶段 2 操作（4 个）。阶段 2 刻意排在一次真实读取之后。
19. **走 `ctx.tools.restrict({ deny })`，不走提示词过滤**。理由（实测）：
    过滤 `system-prompt/assemble` 只改了模型看到的那一份，工具**仍可调用**，
    `Tool.listTools` 也照样报全部 11 个；`restrict` 是注册表**唯一的可见性解析器**，
    schema 展示 / `tools.get()` / 派发三者一致。
20. **空 deny 不能调 `restrict`**（会拒绝空 filter）→ `deny.length === 0` 时直接 return。
21. **读会话历史用 `session.snapshotEvents()`**，`Session` **没有** `.events` 属性
    （照抄脚手架会静默失效：条件恒假、不报错）。
22. **工具名在 `event.data.name`**，不是 `event.name`（写错会静默取到空值，
    表现为永远停在阶段 1）。
23. **覆盖已存在的 agent**：除了 `agent/created`，还要 `ctx.inject(['agents'])` 遍历
    `agents.list()` 补挂 → 否则热重载或中途装配时，staging 对谁都不生效。
24. **`session/event` 按 id 比较而非对象比较**（同一个 session 可能给回不同 wrapper）；
    不确定时宁可重复同步（幂等）也不要漏事件。
25. **逃生口**：`DSH_SCREEN_AGENT_STAGING=off` → 一次调用后全给。

### 1.6 Sidecar（Python）

26. **所有异常转 JSON**：`except Exception as exc: print({ok:false, error})` + `return 0`；
    永远不以栈崩方式失败。`main()` 里 `stdout.reconfigure(encoding='utf-8')`。
27. **错误信息是给模型看的路标**：越界时报"当前只有 N 个窗口，用 0..N-1 或标题子串"；
    无匹配时列出前 8 个窗口标题；多匹配时列出前 6 个让它加长子串。
28. **像素预算收敛 `fit_within`**：超预算则 `scale = sqrt(max/cur)`，
    **截断不四舍五入**（实测 1386×797 四舍五入后 640,385 > 640,000）；
    `MIN_REGION_PX = 8`，更小直接拒绝（放大成模糊没有信息量）。
29. **图像出口双模式**：`inline: true` → base64 塞进 JSON（并发安全、无磁盘残留）；
    否则落盘。并发 8 路截图实测 payload 全部完整可解码。
30. **`make_dpi_aware()` 必须在任何窗口/像素调用之前**（截图像素与光标坐标必须同坐标系）。
    *Linux 无此问题，但 AT-SPI 与 randr 缩放仍需注意。*
31. **指纹稳定检测**：64×40 灰度 digest + `NOISE_FLOOR=12`（单像素）+
    `CHANGE_THRESHOLD=0.02`（变化格子比例）。**精确相等在真实桌面永远不成立**
    （歌词/时钟/光标闪烁），第一版 18 次轮询全是"仍在变"直接超时；
    改成比比例后 896 ms 判定稳定。`stable` 模式要连续 2 次达标。
32. **窗口 spec 三路归一**：z-order index / 标题子串 / hwnd → 同一个 window dict；
    显式拒绝 bool（`True` 在动态语言里会当 int 用）。

### 1.7 无障碍层（UIA，Linux 要换成 AT-SPI）

33. **元素选择用指纹重查找，不用持久句柄**：每次调用是新进程，元素对象活不过去。
    好消息是副作用是好的——UI 变了报 "没找到元素"，而不是拿过期句柄乱操作。
34. **惰性树必须两轮采样**：Chromium 首次 `FindAll` 只返回 49 个外壳，同窗口稍后 414 个
    （页面文字才出现）。→ 采样到**连续两轮指纹一致**才返回，并回报 `stable` / `rounds`；
    不稳时**明确告诉模型"这份列表可能不全"**。
    指纹 = `role \x01 name \x01 aid` 拼接（用分隔符避免歧义）。
35. **缓存属性 / 实时属性分层**：稳定检测只读缓存属性（本地、快）；
    pattern 探测（每元素一次跨进程往返，主要成本）只在稳定的树上做一次。
    *踩过的坑*：`IsInvokePatternAvailable` 类派生属性在 .NET 客户端读出来是空，
    而 PowerShell 访问不存在的成员返回 `$null` 而非抛错 → 静默"看起来没有任何 pattern"。
36. **过滤无意义元素**：无名 + 无 id + 离屏 + 无 pattern 的节点直接丢弃；
    `BoundingRectangle` 可能是 Infinity，**先验证再转 int**（否则异常打断整轮）；
    pattern 名白名单过滤 + 短名化；`ScrollItem` 故意排除（实测 53/60、60/60 都出现，无信号）。

### 1.8 安全

37. **"不许操作自己"是血泪条款**：agent 由浏览器里的对话驱动，点进自己的窗口就是自杀
    （开发中真的发生过）。
38. **两层拦截**：按窗口标题拦 + 按**落点**拦（`WindowFromPoint` / Linux 用 `XQueryPointer`）；
    标记列表用 env 覆盖（`DSH_SCREEN_AGENT_PROTECT="a,b"`，留空即关闭）。
39. **保护校验先于任何真实输入**。
40. **成功的元素操作≠后台操作**：Windows 实测所有 pattern 都会把目标窗口抬到前台
    （与 OpenAI 官方 computer use 文档一致）。脚本会在结束后**归还前台**（`KeepFocus` 可关）。
    → Linux 同样会抢焦点，重写时保留"归还"语义。

### 1.9 工程 / 装配

41. **普通插件 ≠ bundle**：`dev_inject_plugin`（junction + loader.create，不碰 profile 配置）
    vs `dev_install_package`（写 `dsh.profile.bundles`）。后者用在**没有 bundle 声明**的包上，
    会导致 `dsh-app-boot` 断言失败、**整个 DSH 起不来**（真实事故）。
42. **持久化二选一**：保持普通插件 + 在 `cordis.patch.yml` 追加 `insert` 条目；
    或先补 bundle 声明字段、**确认有效后**才进 `bundles` 列表。顺序不能颠倒。
43. **测试三件套**：sidecar 正确性 + 拒绝路径（78 项）／负载·句柄·进程（14 项）／
    分批放出纯函数（23 项）。**测试要先跳过受保护窗口再挑目标**——开发机上最顶层
    常常就是 DSH 自己的浏览器窗口，直接取"窗口 0"会让整套测试莫名其妙全红。
44. **图走 base64 而非固定文件名临时文件**：旧固定名方案在并发时竞态。

---

## 2. Linux 映射表（重写的主体工作）

| 参考实现（Win32） | Linux 对应 | 难度 |
|---|---|---|
| `gdi32!GetDIBits` 截图 | Pillow `ImageGrab`（xcb） | ✅ 已验证 |
| `user32!SendInput` | `libXtst` XTestFakeMotionEvent / ButtonEvent / KeyEvent | ✅ 已验证通路 |
| `KEYEVENTF_UNICODE` 发文本 | `XStringToKeysym` → 失败则 `XChangeKeyboardMapping` 临时映射空闲 keycode 到 `0x01000000+codepoint`（xdotool 同法）；兜底：剪贴板 + Ctrl+V | ⚠️ 待实测 |
| `EnumWindows` | `_NET_CLIENT_LIST`（EWMH，比 `XQueryTree` 准，避开装饰窗口）+ `_NET_WM_NAME` / `WM_CLASS` / `_NET_WM_STATE` | 中 |
| `PrintWindow`（遮挡可见） | **无等价物**。退化为"抬窗 + 截 root + 裁剪 + 还焦点"；也可选 `XComposite` 抓窗口 pixmap（复杂，暂不做） | ⚠️ 能力降级 |
| `SetForegroundWindow` + 四级升级 | EWMH `_NET_ACTIVE_WINDOW` ClientMessage（source=2）+ `XRaiseWindow`；Mutter 有 focus-stealing prevention，需带时间戳 | ⚠️ 待实测 |
| `WindowFromPoint` | `XQueryPointer` 返回 child window → 向上找顶层 | ✅ 已验证 |
| UIA `ControlType` / `Name` / `AutomationId` / patterns | AT-SPI `get_role_name()` / `get_name()` / （**无稳定 id**） / `Action`+`EditableText`+`Value`+`Component` 接口 | 中 |
| UIA pattern → 动作 | `invoke`→Action.do_action(点按) / `set_value`→EditableText.set_text_contents / `toggle`→Action "toggle" / `expand`/`collapse`→Action / `select`→Selection / `focus`→Component.grab_focus | 中 |
| `SetProcessDPIAware` | 无对应（X11 无进程 DPI 感知问题） | — |
| `virtual_desktop` | X11 root window 几何（多屏取并集；当前单屏 2560×1600） | 低 |
| 每元素跨进程往返（PowerShell） | GI `Atspi` 同进程内 DBus 调用，**比 PowerShell 快得多**；但 `Atspi.init()` + 遍历仍有开销 | 低 |
| 惰性树两轮采样 | **同样需要**（Chromium/GTK 都惰性构建） | 低 |
| a11y 开关 | GNOME `toolkit-accessibility` / `GTK_MODULES=gail:atk-bridge` / `QT_ACCESSIBILITY=1` / Firefox `accessibility.force_disabled=-1` / Chromium `--force-renderer-accessibility` | ⚠️ **待探针量化** |

**额外（Linux 独有）**：AT-SPI 有 `Action.get_n_actions()` / `do_action(i)`，
比 UIA 更直白；`Atspi` 还能读 `StateSet`（enabled/visible/focused/checked/expandable）。

---

## 3. 重写架构

```
/home/sir/dsh-ubuntu-kit/plugins/dsh-screen-agent-linux/
├─ package.json              # 普通插件（不是 bundle！）
├─ lib/
│   ├─ index.js              # host 半：工具注册 + staging + 附件回图
│   ├─ screen_tools.py       # sidecar 入口：stdin JSON → stdout JSON
│   └─ linux/
│       ├─ x11_input.py      # ctypes: libX11 + libXtst（注入/指针/keysym）
│       ├─ x11_windows.py    # ctypes: EWMH 窗口枚举/聚焦/保护
│       ├─ atspi_tree.py     # GI Atspi: 元素快照（两轮采样 + 指纹）
│       ├─ atspi_act.py      # GI Atspi: 按指纹重查找 + 动作
│       └─ imaging.py        # 裁剪/预算收敛/PNG 编码（手法照抄）
├─ tests/
│   ├─ test_sidecar.py       # 正确性 + 拒绝路径
│   ├─ test_x11_input.py     # 注入地面真相（见 §4）
│   └─ test_staging.mjs      # 分批放出纯函数
└─ tools/probe/              # P0 探针（独立可跑，不依赖插件）
```

**工具面 11 个**（与参考库同名同序，便于日后对照）：

| 阶段 | 工具 | Linux 实现 |
|---|---|---|
| 0 看 | `screen_look` | ImageGrab 全屏 |
| 1 读 | `screen_zoom` | 原生分辨率裁剪 + `fit_within` |
| 1 | `screen_windows` | EWMH `_NET_CLIENT_LIST` |
| 1 | `screen_window` | 聚焦 + 可选点击/输入 + 截该窗口矩形 |
| 1 | `screen_elements` | AT-SPI 快照（两轮稳定采样） |
| 1 | `screen_wait` | 64×40 指纹比例检测 |
| 1 | `screen_key` | XTest 组合键 |
| 2 操作 | `screen_move` | XTestFakeMotionEvent |
| 2 | `screen_click` | XTestFakeButtonEvent（默认原地按） |
| 2 | `screen_type` | keysym + remap / 剪贴板 |
| 2 | `screen_act` | AT-SPI Action / EditableText |

**协议**（与参考库一致，便于日后共享测试思路）：

```jsonc
// stdin
{ "action": "click", "nx": 0.5, "ny": 0.3, "button": "left", "clicks": 1 }
// stdout
{ "ok": true, "button": "left", "clicks": 1, "cursorX": 1280, "cursorY": 480,
  "movedCursor": true, "pngBase64": "..." }        // inline 模式
{ "ok": false, "error": "ValueError: window index 9 is out of range; ..." }
```

**本机特有参数**：`DISPLAY=:1`、`XAUTHORITY=/run/user/1000/gdm/Xauthority` —
sidecar 要能自己探测（env → `/run/user/$UID/gdm/Xauthority` → `~/.Xauthority`），
否则插件在非同源启动的 DSH 里会连不上 X。

---

## 4. P0 探针（下一步就做这个，不碰插件）

**定位**：独立脚本，几十分钟内跑完，产出硬数据，决定后续范围。
**风险提示**：会短暂夺走本机鼠标/键盘控制 —— 测试窗口用自己开的，全程带超时自动退避，
不碰全屏盲点，**不点击任何 DSH/Firefox 窗口**。

| # | 测什么 | 方法（地面真相） | 判定标准 |
|---|---|---|---|
| 1 | Python sidecar 冷启动开销 | 连续 20 次 `spawn python3 -c "import gi, Atspi"` 计时 | 中位 < 400ms → 每次 spawn；否则做常驻 |
| 2 | 键盘注入真的到达 | 自开 gnome-terminal 跑 `cat -v > /tmp/probe-keys.txt`，注入 `abc` + `ctrl+a` | 文件内容 == 期望，且 xev 无异常 |
| 3 | **中文/Unicode 注入** | 同上注入 `你好中文` + `emoji`；同时观察 fcitx5 是否拦截 | remap 路线成功 or 必须走剪贴板 |
| 4 | 鼠标注入真的生效 | 自开一个 Tkinter 窗口记录 button-press 坐标 | 记录坐标 == 注入坐标（±1px） |
| 5 | **AT-SPI 覆盖率** | 分别用 `GTK_MODULES=gail:atk-bridge` / `QT_ACCESSIBILITY=1` / `ACCESSIBILITY_ENABLED=1` 启动 gnome-terminal、gnome-calculator、Firefox、Chromium，统计元素数 / 有 Action 的元素数 / 是否两轮稳定 | 目标：**有 Action 的可操作元素 ≥ 20 个/应用** 才算元素层值得做 |
| 6 | AT-SPI 动作真的生效 | 对 gnome-calculator 按钮 `do_action` 点 "7"，读回显示值 | 值变化 == 期望（**不移动光标**） |
| 7 | 窗口聚焦 | 对自开窗口发 `_NET_ACTIVE_WINDOW`，轮询 `_NET_ACTIVE_WINDOW` 属性 | 成功；若被 Mutter 拒 → 记录降级方案 |
| 8 | 截图延迟基线 | 连续 30 次全屏 grab + 一次 zoom 裁剪 | 中位 < 250ms，首尾无漂移 |
| 9 | 遮挡窗口裁剪 | 用自开窗口测"被盖住时截图里是什么" | 确认必须"抬窗再截" |

**探针产出**：`tools/probe/REPORT.md` + 原始 JSON 数据，作为是否启用元素层、
是否用剪贴板兜底、是否常驻 sidecar 的决策依据。

---

## 5. 保护与审计（已确认采用 1+3）

- **标题匹配**：默认拦标题含 `DeepSeek Harness` 的窗口（当前 DSH GUI 标题实测为
  `... — DeepSeek Harness — Mozilla Firefox`，`WM_CLASS=firefox_firefox`，
  `_NET_WM_PID=5745`）。
- **落点校验**：`screen_click` 点击前用 `XQueryPointer` 取落点窗口 → 向上找顶层 →
  标题命中即拒绝（这是"点进自己"的最后一关）。
- **禁全屏盲点**：不给坐标的 `click` / 任何 `type`，要求显式确认参数；
  不提供"全屏任意位置点击"的默认行为。
- **审计日志**：每个动作落 JSONL 到 `$DSH_HOME/screen-agent/audit.jsonl`：
  `{ts, action, args, targetWindow{id,title,pid}, cursorBefore/After, result, elapsedMs}`。
  截图**不**入库，只记 sha256 与尺寸（避免审计日志变成敏感内容仓库）。
- **env 逃生口**：`DSH_SCREEN_AGENT_PROTECT="a,b"` 覆盖标记（留空即关闭，危险）。
- **不需要**保护整个 Firefox 进程 —— 保留操作其他 Firefox 窗口的能力。

---

## 6. 风险清单

| 风险 | 影响 | 对策 |
|---|---|---|
| **Wayland** | XTest 整条路线失效（无全局输入注入） | 本机当前是 X11；后端接口抽象成 `input_backend`，Wayland 走 portal + uinput（暂不实现），启动时检测并明确报错 |
| WT-SPI 覆盖不足 | 元素层价值低 | P0 探针量化；不足则元素层降级为"锦上添花"，主打视觉+坐标 |
| 抢焦点 | 用户正在打字被打断 | 保留参考库的"操作完归还前台"语义；`keepFocus` 开关 |
| 中文输入 | remap 路线可能被 IME 拦 | P0 探针测；兜底剪贴板+Ctrl+V |
| 死循环误操作 | agent 反复点错地方 | 审计日志 + 分批放出（操作层后解锁）+ 动作后截图反馈 |
| 屏幕敏感内容 | 截图持久化到 `~/.dsh/attachments/`（参考库同款行为） | 文档明示；可选"不落库仅内存"模式（DSH attachments 机制决定，需验证） |
| 多显示器 | 归一化坐标基准错 | root window 并集几何；当前单屏 |
| NVIDIA 5070 + X11 | 截图/合成异常 | 已实测截图正常 |

---

## 7. 范围边界（明确不做）

- ❌ 不做 Wayland 后端（当前 X11 会话）
- ❌ 不做 OCR（tesseract 未装；视觉模型直读更省事）
- ❌ 不做窗口级 `PrintWindow` 等价物（XComposite 复杂度不划算）
- ❌ 不做 Web GUI 面板（v1 保持纯工具；需要时再加 hybrid UI）
- ❌ 不做"后台操作"幻觉 —— 会抢焦点，如实告诉模型和用户

---

## 8. 下一步

1. 用户确认本蓝图 → 写 P0 探针（`tools/probe/`），跑出 §4 的 9 项数据
2. 按数据定稿元素层策略（全量 / 降级 / 只做特定应用）
3. 写 host 半 `lib/index.js`（照手法 1–25 条重写）
4. 写 sidecar + Linux 层，跑测试三件套
5. `dev_inject_plugin` 注入验证 → `cordis.patch.yml` 持久化

---

## 附：参考库文件与行数（重写时的对照索引）

| 文件 | 行数 | 重写时的角色 |
|---|---|---|
| `lib/index.js` | 940 | host 半范式（本文档 §1.1–1.5） |
| `lib/screen_tools.py` | 1560 | sidecar 范式（§1.6）；Win32 段 194–648、774–830 需替换 |
| `lib/uia_snapshot.ps1` | 252 | 元素快照范式（§1.7） |
| `lib/uia_act.ps1` | 278 | 元素动作范式（指纹重查找） |
| `tests/test_sidecar.py` | 13KB | 拒绝路径测试范式 |
| `tests/test_stability.py` | 9.6KB | 负载/句柄/进程测试范式 |
| `tests/anchor_test.mjs` | 5.5KB | staging 纯函数测试范式 |

许可证：BSD-3-Clause（重写时保留出处说明）。
