# dsh-plugins-linux

[`Arthur303Author/dsh-plugins`](https://github.com/Arthur303Author/dsh-plugins)（Windows 版）的 **Linux 对应仓库**：
存放只在 Linux 上跑的 DSH 插件，以及本仓库自己的推送前隐私筛选机制。

> 这里是**副本**：插件的实际生效位置在本机（经 profile 的 `link:` 依赖就地引用，例如
> `~/dsh-ubuntu-kit/plugins/<名字>`），本仓库保留一份受 git 管理、可对外可见的历史。
> 源目录与副本之间的搬运由 [`scripts/export-plugin.sh`](scripts/export-plugin.sh) 负责。

## 插件

| 目录 | 名称 | 用途 | 许可 |
|---|---|---|---|
| [`plugins/dsh-screen-agent-linux`](plugins/dsh-screen-agent-linux) | `@dsh-external/dsh-screen-agent-linux` | **Linux 屏幕视觉 + UI 元素操作**：按 Windows 版 `dsh-screen-agent` 的**手法**干净重写（Win32 → X11 / XTest / AT-SPI），不是逐行移植。截屏回图、按原生分辨率裁剪读小字、EWMH 窗口列表、无障碍树读元素并按元素操作、鼠标与键盘注入、窗口级截图。零第三方 npm 依赖；Python 侧只用系统自带的 Pillow / libX11 / libXtst / python3-gi | BSD-3-Clause |

## 环境要求

- Linux + **X11** 会话（输入注入走 XTest；Wayland 未验证）
- Node 20+
- Python 3 + `Pillow`（截图）、`python3-gi` + `at-spi2-core`（无障碍树）
- **不需要** `xdotool` / `ydotool` / `scrot`

## 推送前自检

仓库一旦对外可见，推送前跑一次隐私自检——它扫四个面（**已跟踪内容 + 未跟踪文件 + 全部历史提交 + 文件路径**）：

```sh
sh scripts/privacy-check.sh
sh scripts/privacy-check.sh --extra 'internal-host' --extra 'corp\.example'   # 追加关键词（可重复）
sh scripts/privacy-check.sh --strict   # 超长行里的裸用户名也算命中
```

发现命中时，**把值改写成等价的可移植写法，而不是删掉**——删掉等于把功能改坏：

| 命中类型 | 正确改法 |
|---|---|
| 硬编码家目录 | Node：`process.env.DSH_HOME ?? join(homedir(), '.dsh')`；shell：`$HOME` |
| 凭据 / token | 移到环境变量或本地配置文件，并把该文件写进 `.gitignore` |
| 本机运行产物（探测截图、`*.tgz`） | 不提交 + `.gitignore` |

判据是**改完之后功能仍然可用**（跑一次测试或在真机验证）。

> `git grep` 只看得见已跟踪文件，所以脚本额外用 `git ls-files --others` 扫了一遍未跟踪文件：
> 否则「刚导出、还没 `git add`」的副本会被假报成干净。

### 自动运行（pre-push hook）

`.git/hooks/` 不被 git 跟踪，所以 hook 模板放在仓库里、每个克隆安装一次：

```sh
sh scripts/install-hooks.sh              # 安装
sh scripts/install-hooks.sh --uninstall  # 卸载（移入回收站）
```

装好后**每次 `git push` 前自动运行**，命中即中止推送；确实需要绕过某一次用 `git push --no-verify`。

退出码语义：`0` 放行、`1` 有命中、`2` 不在仓库里、`3` **扫描本身失败**（绝不能当成干净）。

## 把本机插件筛进仓库

```sh
sh scripts/export-plugin.sh dsh-screen-agent-linux            # 演练（默认只演练）
sh scripts/export-plugin.sh dsh-screen-agent-linux --apply    # 落盘
```

- **文件级筛选**：`node_modules/`、`__pycache__/`、`*.py[cod]`、日志、`logs/`、`state/`、
  `dist/`、编辑器垃圾、探测产物 `probe/out/`、打包产物 `*.tgz` 一律不复制。
- **内容级把关**：搬完自动跑一次 `privacy-check.sh`，命中就报出来并让脚本非零退出。
- **不改写源码**：它不替你改敏感值——改写必须保证功能仍然可用，交给人或 agent 逐个判断。
- 名字带 `/` 就按仓库内同名路径镜像（例如 `presets/xxx`），否则落在 `plugins/` 下。

`rsync --delete` 是镜像语义，演练输出里的 `deleting` 是真会删——**先看清演练结果再 `--apply`**。
