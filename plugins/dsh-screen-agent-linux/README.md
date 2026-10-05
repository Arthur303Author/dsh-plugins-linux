# @dsh-external/dsh-screen-agent-linux

给 DSH 装一双看得见的眼睛和一只点得动的手 —— **Linux/X11 版**（对标 codex computer use）。

按 `Arthur303Author/dsh-plugins` 的 Windows 版 `dsh-screen-agent`（BSD-3-Clause）**手法重写**，
不是代码移植：Win32 层（`SendInput` / `GDI` / UIA / PowerShell）整体换成 X11 + XTest + AT-SPI，
保留的是那些用实测换来的设计决策。

## 十一个工具

| 阶段 | 工具 | 作用 |
|---|---|---|
| 看 | `screen_look` | 全屏截图（所有显示器）作为图片返回 |
| 看 | `screen_zoom` | 按**原生分辨率**裁剪一块区域——读小字、找小控件 |
| 读 | `screen_windows` | 列出受管顶层窗口（z 序、标题、尺寸、位置、WM class、pid） |
| 读 | `screen_wait` | 等屏幕**变化**或**稳定**，替代固定 sleep |
| 读 | `screen_elements` | **无障碍树读元素**：角色、名字、可执行动作、当前状态、屏幕坐标 |
| 操作 | `screen_move` | **只移动光标**，不按任何键 |
| 操作 | `screen_click` | 按鼠标键；不给坐标就是**原地按下** |
| 操作 | `screen_key` | 发送按键组合（Esc / Tab / 方向键 / F1-F24 / 修饰键组合） |
| 操作 | `screen_type` | 发送按键组合和/或文本（ASCII 走键盘，非 ASCII 走无障碍文本接口） |
| 操作 | `screen_act` | **按元素操作**（invoke / set_value / insert_text / toggle / expand / collapse / focus / describe），不量坐标、不动光标 |
| 操作 | `screen_window` | 聚焦某个窗口，可选在其中点击/输入，然后只截这个窗口 |

**选路顺序：`screen_key` > `screen_act` > `screen_elements` + 坐标 > 截图。**
键盘不依赖位置；元素动作不依赖像素；无障碍树给结构；截图那套是最后的兜底。

**为什么需要 `screen_zoom`**：provider 把每张图归一到约 800×800 等效（640,000 px），
所以 2560×1600 的全屏图到达模型时只有约 1011×632，细节在进入上下文之前就丢了；
而 ≤640k px 的裁剪是**无损**的。坐标一律**归一化**（0..1），因为比例能穿过任何缩放，像素不能。

## 依赖

零第三方 npm 依赖（host 半只用 node 内置模块，**运行时不 import 任何 dsh 包**）。
Python 侧只用系统自带的：`python3` + `Pillow`（截图）、`libX11` / `libXtst`（ctypes 直连）、
`python3-gi` + `at-spi2-core`（无障碍树）。**不需要** `xdotool` / `ydotool` / `scrot`。

解释器可用 `DSH_SCREEN_AGENT_PYTHON` 覆盖。

## 装配

```sh
dsh plugin --profile web add /home/sir/dsh-ubuntu-kit/plugins/dsh-screen-agent-linux
dsh --profile web --dump-config | grep -A2 screen-agent   # 不启动服务就验证这一层
```

或运行时注入（免重启）：`dev_inject_plugin {"dir": "<此目录>"}`。

## 状态

**P1 / P2 / P3 全部完成**，验收 + 稳定性 + 安全性共 **70/70**：

| 套件 | 覆盖 | 结果 |
|---|---|---|
| `tools/probe/sidecar_p2_test.py` | 指针、键盘、ASCII/非 ASCII 文本、保护窗口、变化检测、settle | **16/16** |
| `tools/probe/sidecar_p3_test.py` | 元素树、元素动作、窗口截图、保护窗口三层一致 | **14/14** |
| `tools/probe/stability_test.py` | 持续负载无漂移、16 路并发、孤儿进程、X 连接释放、payload 预算 | **13/13** |
| `tools/probe/security_test.py` | 六入口保护覆盖、拒绝零副作用、注入面、资源上限、审计、窗口消失容错 | **27/27** |
| `tools/probe/probe_*.py` | P0 可行性探针（见 `tools/probe/REPORT.md`） | 全部有结论 |

## 保护与审计

- **受保护窗口**：默认拦标题含 `DeepSeek Harness` 的窗口（agent 由本机浏览器里的对话驱动，
  点进自己的窗口会掐断会话）。`screen_click` 在**移动之后、按下之前**用 `XQueryPointer` 校验落点；
  `screen_key` / `screen_type` 校验焦点窗口；`screen_elements` / `screen_act` / `screen_window`
  校验目标窗口——**四个入口一致**。`DSH_SCREEN_AGENT_PROTECT="a,b"` 可覆盖（留空即关闭）。
- **审计日志**：每个动作落一行 JSONL 到 `$DSH_HOME/screen-agent/audit.jsonl`，
  记录意图（动作、坐标、目标窗口）而**不**记录像素，避免日志变成屏幕内容的第二份拷贝。

## 设计依据（全部来自实测，详见 `tools/probe/REPORT.md`）

| 决策 | 依据 |
|---|---|
| 每次 spawn sidecar，不做常驻进程 | 冷启动固定开销仅 ~30ms |
| 图像走 inline base64，不落临时文件 | 并发调用不会在文件名上竞态 |
| 双重降级：无附件服务 / 模型不收图 → 落盘 + 文本路径 | 截图永远不因探测失败而丢失 |
| 像素预算收敛**截断不四舍五入** | 四舍五入会把 640,385 px 顶过 640,000 的预算 |
| `XSetErrorHandler` 必须在任何窗口调用之前装 | Xlib 默认处理器会因一个窗口消失而**杀掉进程** |
| 落点判定用逐层下降 + `_NET_CLIENT_LIST` 匹配 | Mutter 的装饰框架不在 client list 里，直接比永远不相等 |
| 差异检测用 RGB，等稳定用灰度 | 灰度噪声阈值把"深灰 vs 棕色"的 76% 差异读成 0.02% |
| 非 ASCII 走 AT-SPI `insert_text`，且 `length` 按**字节**算 | XTest 合成 Unicode keysym 在 GTK 上完全不产生文本（**与输入法无关**）；传字符数会把中文拦腰截断 |
| 可打印 ASCII 的 keysym **等于其码点** | `XStringToKeysym` 只认名字（`"space"`），对字面量 `" "` 返回 0——空格和所有符号都会打不出来 |
| 输入动作支持 `settle`（基线在**同一进程内**取） | 独立调 `screen_wait` 时，快速重绘已变成它的基线，变化检测不到 |
| 元素缺失是**领域结果**（`actionOk:false`），不是信封失败 | 规范：基础设施故障才抛异常，可预期的状态应写进规范值 |
| `screen_act` 不实现"归还焦点"补偿 | Linux 上 AT-SPI 动作**不需要**焦点：实测指针与焦点都不变（Windows 版相反，所有 pattern 都会抬窗口） |
| `screen_window` 必须"抬窗 → 截屏 → 裁剪 → 归还焦点" | X11 没有 `PrintWindow` 等价物，被遮挡的窗口在截图里看不到 |

## 目录

```
dsh-screen-agent-linux/
├─ package.json          dsh.bundle.patch + files（含 patch 文件本身）
├─ cordis.patch.yml      bundle 层：一行 insert
├─ PLAN.md               重写蓝图与参考实现手法提炼
├─ lib/
│  ├─ index.js           host 半：11 个工具 + sidecar RPC + 回图链路
│  ├─ screen_tools.py    sidecar：11 个 action + 保护 + 审计
│  └─ linux/
│     ├─ x11.py          X11/EWMH/XTest ctypes 层
│     ├─ atspi.py        AT-SPI 元素层（稳定采样、指纹重查找、动作、文本）
│     └─ keymap.py       键名与修饰键组合解析
└─ tools/probe/          P0 探针 + 三份实测报告与原始数据
```
