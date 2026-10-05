# P0 探针报告 — dsh-screen-agent-linux

> 目的：在写任何插件代码之前，用实测数据决定插件的形状。
> 执行：2026-09-26，Ubuntu 24.04.5 / X11 (`:1`) / 单显示器 2560×1600。
> 全部探针脚本可重跑；原始数据在 `out/*.json`，截图在 `out/*.png`。

---

## 0. 一句话结论

**九项全部有结论，零新依赖。** 三块基石（截图 / 输入注入 / 元素树）全部实测可用，
其中**元素层比参考实现的 Windows 版更强**（真正的后台操作），
而**中文输入和遮挡窗口**两处必须换路线——都已找到可用替代。

---

## 1. 结论速览

| # | 项目 | 结果 | 依据 |
|---|---|---|---|
| 1 | sidecar 冷启动 | **每次 spawn 仅 ~30ms** | bare 10.1 / pillow 32.8 / atspi 28.8 / 全流程 194.8 ms |
| 2 | 全屏截图 | ✅ 2560×1600，同进程中位 **14.8ms** | `safe.json` |
| 3 | 裁剪+PNG+base64（640k px） | ✅ 中位 **82.9ms**，payload 567KB | `safe.json` |
| 4 | 键盘注入 ASCII | ✅ `abc` 全部到达窗口 | 窗口日志 `KEY_PRESS a/b/c` |
| 5 | 键盘注入 Unicode | ❌ **XTest 路线不通**（非输入法原因） | 见 §3.2 |
| 6 | Unicode 文本（AT-SPI） | ✅ 中文/emoji 全部成功 | `insert_text` / `set_text_contents` |
| 7 | 鼠标注入 | ✅ 坐标精确匹配 (280,1367) | `BUTTON_PRESS x_root/y_root` |
| 8 | 窗口聚焦（EWMH） | ✅ **0ms** 生效 | `_NET_ACTIVE_WINDOW` |
| 9 | 元素树（AT-SPI） | ✅ GTK/Qt/Tauri 开箱可用，**不需要开全局 a11y** | 见 §3.1 |
| 10 | 元素动作 | ✅ 且**焦点、指针都不变** | 见 §3.3 |
| 11 | 遮挡窗口 | ❌ **截不到**，看到的是覆盖者 | `crop_A_occluded.png` |
| 12 | 坐标对齐 | ✅ 三套坐标系一致（差 37px 标题栏） | `coords.json` |

---

## 2. 性能数据

### 2.1 sidecar 冷启动（spawn python 并把活干完，ms，n=12）

| 场景 | median | p90 | max |
|---|---|---|---|
| `python3 -c pass` | 10.1 | 10.4 | 11.5 |
| `+ import PIL` | 32.8 | 33.3 | 33.4 |
| `+ import gi, Atspi.init()` | 28.8 | 29.1 | 29.3 |
| 全流程（截图+缩放+PNG+base64） | 194.8 | 196.3 | 198.1 |

**决策：不需要常驻 sidecar。** 固定开销约 30ms，而工作本身几十到几百 ms。
参考实现的中位值是 220ms（每次 spawn），我们同量级。
每次 spawn 换来的是：无状态、无泄漏、热重载即净——这个便宜值得占。

### 2.2 同进程截图延迟（ms）

| 操作 | median | p90 | max | 备注 |
|---|---|---|---|---|
| 全屏 grab | 14.8 | 18.5 | 27.0 | 2560×1600 RGB |
| 裁剪 640k px + PNG + base64 | 82.9 | 86.1 | 88.5 | payload 567KB |
| 64×40 灰度指纹 | 17.7 | 18.1 | 18.3 | `screen_wait` 用 |

### 2.3 AT-SPI 遍历耗时

每个应用 **226–487ms**（同进程，含 2 轮稳定采样 + 一次全量 Action 探测）。
稳定采样全部 **`stable=True, rounds=2`** —— 惰性树现象在 Linux 上确实存在，
第一轮与第二轮不同，所以"采样到连续两轮一致"这条设计必须保留。

---

## 3. 三个决定性发现

### 3.1 元素树开箱可用 —— 不需要碰系统无障碍设置

环境里**已经继承**了 `GTK_MODULES=gail:atk-bridge` 和 `QT_ACCESSIBILITY=1`
（来自 GNOME 会话），所以从 DSH 启动的 GTK/Qt 应用自动注册到 AT-SPI。

> 注意：`gsettings get org.gnome.desktop.interface toolkit-accessibility` 返回 `false`，
> 这是个**误导性指标**——真正的开关是那两个环境变量。

实测覆盖率（当前桌面，a11y 无需任何额外开启）：

| 应用 | 节点 | 有名字 | **可操作** | 稳定 |
|---|---|---|---|---|
| gnome-terminal-server | 89 | 53 | **46** | ✅ 2 轮 |
| clash-verge (Tauri/WebKit) | 159 | 44 | **38** | ✅ 2 轮 |
| gnome-shell | 300(截断) | 29 | 1 | ✅ 2 轮 |
| gjs (桌面图标) | 12 | 3 | 0 | ✅ 2 轮 |
| probe_app (GTK3 测试窗口) | 7 | 5 | 3 | ✅ 2 轮 |
| gsd-* × 8 | 0 | 0 | 0 | 已注册无子节点 |

可操作元素长这样（真实输出）：

```
push button  '最小化'      ['click']
push button  '新建标签页'   ['click']
toggle button '菜单'        ['click']
push button  ''            ['press']      <- clash-verge
check box    'PROBE_CHECK' ['click']
```

**决策：元素层值得做**，而且它不是"锦上添花"——对 GTK/Qt 应用它比截图定位更精确、更省 token。

> ⚠️ 与参考实现的关键差异：**AT-SPI 的应用名是程序名，不是窗口标题**。
> 用窗口标题去匹配应用会静默地什么都找不到（我第一版探针就踩了这个）。
> 插件定位自己启动的应用时应**按 pid 匹配**（`get_process_id()`）。

### 3.2 中文输入：XTest 不通，AT-SPI 是正路

对照实验（同一窗口、同一字符串 `你好`）：

| case | 方法 | 输入法 | 键盘事件到达 | 文本进入控件 |
|---|---|---|---|---|
| A | XTest + keysym remap | fcitx（默认） | ✅ `U+4F60 U+597D` | ❌ |
| B | XTest + keysym remap | `gtk-im-context-simple` | ✅ `U+4F60 U+597D` | ❌ |
| C | AT-SPI `insert_text` | fcitx | — | ✅ |
| D | AT-SPI `set_text_contents` | fcitx | — | ✅ |
| E | AT-SPI `insert_text` | simple | — | ✅ |
| F | AT-SPI `insert_text`（ASCII） | fcitx | — | ✅ |
| G | AT-SPI `insert_text`（emoji 🐳） | fcitx | — | ✅ |

两条独立结论：

1. **不是输入法的锅。** A 与 B 都失败，说明 `XChangeKeyboardMapping` 把空闲 keycode
   映射到 Unicode keysym 后，事件**确实到达了窗口且 keyval 正确**，但控件不把它当文本输入。
   这与社区对 `xdotool type` 非 ASCII 的已知抱怨一致。**XTest 只可靠支持当前键盘布局上已有的字符。**
2. **`insert_text` 的 `length` 是字节数。** 传字符数会把非 ASCII 输入**拦腰截断**：
   `abc`（3 字符=3 字节）成功，`你好`（2 字符=6 字节）和 `🐳`（1 字符=4 字节）整个消失。
   改成 `len(payload.encode("utf-8"))` 后全部通过。
   （`set_text_contents` 没有 length 参数，所以它一开始就对。）

**决策：文本输入的选路 = AT-SPI `insert_text`/`set_text_contents` > XTest keysym（仅 ASCII 可靠）。**

### 3.3 元素操作是真正的后台操作（比 Windows 版更强）

把焦点交还给用户的 Firefox 后，对 probe 窗口执行 `do_action`：

```
focus  before/after: 0x2e00017 -> 0x2e00017   unchanged = True
pointer before/after: (1090, 879) -> (1090, 879)   unchanged = True
BUTTON_CLICKED: True
```

参考实现在 Windows 上实测的是反面：**所有** pattern（Invoke/Toggle/Value）都会把目标窗口
**抬到前台**，并为此专门做了"操作完归还前台"的补偿。Linux/AT-SPI **不需要这个补偿**。

**决策：`screen_act` 定位为真正的后台精确操作**，不实现"归还焦点"逻辑
（但保留一个 `keepFocus` 风格的开关以防某些应用例外）。

---

## 4. 四个 Linux 特有的坑（都已解决，必须写进实现）

### 4.1 Xlib 默认错误处理器会**杀掉进程**

探针第一次运行时整个进程被打死：

```
X Error of failed request:  BadWindow (invalid Window parameter)
  Major opcode of failed request:  15 (X_QueryTree)
```

场景：probe 窗口被销毁后 `_NET_ACTIVE_WINDOW` 仍短暂指向它，`XQueryTree` 抛 BadWindow。
**任何枚举窗口的代码都会遇到"窗口在枚举途中消失"**，默认行为是进程级死亡。

修复：`XSetErrorHandler` 安装一个吞掉错误的处理器（`x11.py` 已实现，含 `recent_errors()` 诊断）。
回调对象必须全局持有——被 GC 的 libffi 回调是段错误。

### 4.2 Mutter 的窗口框架不在 `_NET_CLIENT_LIST` 里

`XTranslateCoordinates` 从 root 出发**只返回 root 的直接子窗口**——那是 WM 的装饰框架，
而不是客户端窗口。拿它跟 client id 比较**永远不相等**，于是安全检查会拒绝每一次合法点击。

修复（`client_at`）：
```
1. 从 root 逐层下降到最深窗口（每层 XTranslateCoordinates）
2. 沿父链向上找第一个出现在 _NET_CLIENT_LIST 里的窗口
```
实测修复后：点击 (280,1367) → 窗口记录 `(280,1367)`，精确匹配。

### 4.3 三套坐标系的换算关系

```
X11 client geometry : (40, 1254, 480, 300)     <- 客户区，不含装饰
AT-SPI frame rect   : (40, 1217, 480, 337)     <- 含 37px 标题栏
AT-SPI 子元素 rect  : (52, 1266, 456, 36)      <- 屏幕绝对坐标
GTK border_width=12 : 子元素相对客户区偏移 (+12,+12)
```

**结论：AT-SPI 元素矩形是屏幕绝对坐标，可以直接喂给点击，不需要关心装饰。**
窗口级裁剪要用 client geometry（含装饰会多出 37px 的标题栏区域）。

### 4.4 灰度噪声阈值会漏掉"同亮度异色"的变化

`screen_wait` 沿用参考实现的灰度 + `NOISE_FLOOR=12` 是**对的**（目标是忽略环境噪声）。
但把它用在**差异检测**上会出错：探针把"A 的深灰背景 vs B 的棕色背景"判成了 0.02% 变化，
而实际差异是 76%。**差异检测必须在 RGB 上做**（或至少三通道各自过阈值）。

---

## 5. 遮挡窗口：Linux 的真实能力差距

同矩形裁剪，遮挡前 → 遮挡后 → 抬窗后：

| 状态 | 结果 |
|---|---|
| A 可见 | 正常内容 |
| B 完全盖住 A（B 在 stacking 上层，rect 相同） | **看到的是 B**（`PROBE_LABEL B` + B 的背景色） |
| 抬起 A 后 | 与原始**完全一致（0% 差异）** |

Windows 版用 `PrintWindow` 直接读窗口自己的绘制表面，所以**遮挡物不出现在图里**。
X11 没有等价物。

**决策：`screen_window` 的语义必须是"抬窗 → 截屏 → 裁剪 → 归还焦点"**，
并且在工具描述里**如实告诉模型**这个限制（不能让模型以为能后台偷看被遮挡的窗口）。
归还焦点这一步在 Linux 上是**必要的**（因为抬窗确实抢焦点），
而在 `screen_act` 上**不必要**（§3.3）。

---

## 6. 对重写蓝图的修订

| 蓝图原计划 | 探针后修订 |
|---|---|
| 元素层待定，可能需开 `toolkit-accessibility` | ❌ 不需要开任何系统设置；环境变量已就绪 → **元素层升为一等公民** |
| 可能有"归还前台"补偿逻辑 | 仅 `screen_window` 需要；`screen_act` 不需要 |
| `screen_type` 用 XTest + Unicode remap | 改为 **AT-SPI `insert_text`（字节长度）优先**，XTest 仅 ASCII |
| 落点保护用 `top_level_at` | 必须用 **`client_at`**（逐层下降 + `_NET_CLIENT_LIST` 匹配） |
| 未考虑 X 协议错误 | **必须**安装 `XSetErrorHandler`，否则一次窗口消失就杀死调用 |
| sidecar 可能需常驻 | 每次 spawn（30ms），无需常驻 |
| 元素定位按应用名 | 自己启动的应用**按 pid** 定位；应用名≠窗口标题 |
| 差异检测复用指纹算法 | 指纹（等稳定）用灰度；**差异检测必须 RGB** |

---

## 7. 遗留缺口与未测项

| 项 | 状态 | 影响 |
|---|---|---|
| **非无障碍应用的中文输入** | ⚠️ 缺口 | XTest 对非 ASCII 不通，AT-SPI 又需要元素树。剩下唯一路线是**剪贴板 + Ctrl+V**，而 X11 剪贴板是 owner-based 的——sidecar 进程退出内容就没了，需要一个常驻持有者。**这是目前唯一未解决的硬缺口。** |
| Firefox / Chromium 的 a11y 覆盖率 | 未测 | 测它需要重启 Firefox = 杀掉当前 DSH GUI 会话，**故意跳过**。用 `code`（Electron）替代测试可补，不影响用户。 |
| 多显示器 | 未测 | 当前单屏 DP-0。多屏时归一化坐标要以 root 并集为基准。 |
| Wayland | 未测 | 当前 X11。切过去 XTest 整条路线失效，需 portal + uinput 后端。 |
| 元素操作对有状态控件的副作用 | 部分 | `toggle`/`select` 等未逐一实测，仅验证了 `click` 与 `set_value`。 |
| 长时负载 / 句柄泄漏 | ✅ 已验证 | `stability_test.py` 13/13：30 次连续截图**零漂移**（140ms→140ms）、16 路并发 236ms 且 payload 全部为完整 PNG、8 次元素遍历元素数恒定、无孤儿进程、200 次 X 连接开关无泄漏、最大 payload 300KiB（预算 64MiB）。 |
| 保护窗口与注入面 | ✅ 已验证 | `security_test.py` 27/27：**六个入口**（click/key/type/elements/act/window）全部拒绝受保护窗口；6 次被拒调用在窗口日志里产生 **0 条**事件（拒绝先于任何副作用）；shell 元字符只被当作文本输入、从未执行；9 项资源上限全部拒绝；审计条目 238 字节、不含像素；窗口中途消失时三个入口都干净报错。 |

---

## 8. 文件清单

```
probe/
├─ x11.py               X11/EWMH/XTest ctypes 层（插件 lib/linux/ 的原型）
├─ atspi.py             AT-SPI 元素层（遍历/稳定采样/指纹重查找/动作）
├─ probe_app.py         GTK3 测试目标：把每次真实交互写进 JSONL（地面真相）
├─ probe_safe.py        无害组：冷启动 / 截图延迟 / a11y 覆盖率
├─ probe_inject.py      注入组：聚焦 / 键盘 / 中文 / 鼠标 / 元素动作（带安全闸门）
├─ probe_unicode.py     Unicode 输入对照矩阵（7 个 case）
├─ probe_coords.py      坐标对齐 + 遮挡实验
└─ out/                 safe.json / inject.json / unicode.json / coords.json + 截图
```

安全设计（注入组）：每一步注入前重新校验焦点属于自己启动的窗口；
点击在**移动之后、按下之前**再校验落点；只操作自己开的窗口；
退出时恢复原指针位置与焦点。

复现：
```bash
cd probe
python3 probe_safe.py        # 完全无害
python3 probe_inject.py      # 会短暂接管鼠标键盘（仅限自开窗口内）
python3 probe_unicode.py     # 中文输入对照
python3 probe_coords.py      # 坐标对齐 + 遮挡
```

---

## 9. 下一步

数据齐了，可以直接进入实现：

1. **P1** — host 半 `lib/index.js`（照蓝图 §1 的手法重写）+ 只读三件套
   `screen_look` / `screen_zoom` / `screen_windows`
2. **P2** — 输入层 `screen_move` / `screen_click` / `screen_key` / `screen_type` / `screen_wait`
   （`screen_type` 走 AT-SPI 优先 + XTest ASCII 兜底）
3. **P3** — 元素层 `screen_elements` / `screen_act` / `screen_window`
4. **剪贴板缺口** — 与 P3 并行解决，或先如实降级并在工具描述里写明

参考实现是 BSD-3-Clause；本次是**按手法重写**，不是代码移植，出处会在源文件里注明。
