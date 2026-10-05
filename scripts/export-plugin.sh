#!/bin/sh
# ============================================================================
# export-plugin.sh — 把本机插件目录“筛选后”同步进本仓库（源码镜像）
#
# 本仓库是公开的源码镜像：插件真正的生效位置在本机（例如
# ~/dsh-ubuntu-kit/plugins/<name>，经 profile 的 link: 依赖就地引用），
# 仓库里保留一份受 git 管理、可公开的历史。这个脚本负责两者之间的搬运，
# 并顺手把“不该进公开仓库的东西”挡在外面。
#
# 两层筛选：
#   1. 文件级：node_modules / __pycache__ / 日志 / state / dist / 探测产物
#      等本机运行数据一律不复制（与仓库 .gitignore 的意图一致）。
#   2. 内容级：搬完自动跑 scripts/privacy-check.sh，命中就报出来——
#      本脚本不替你改源码，因为“把 /home/x 改成环境变量”这种改写必须保证
#      功能仍然可用，交由人或 agent 逐个判断。
#
# 安全默认：**默认只演练（dry-run）**，看清将增删什么之后再 --apply。
# rsync 用 --delete 保持镜像语义，所以演练输出里的 "deleting" 是真的会删。
#
# 用法:
#   sh scripts/export-plugin.sh <插件名>              # 演练（默认落 plugins/<名字>）
#   sh scripts/export-plugin.sh presets/<名字>        # 名字带 / 就按仓库内同名路径
#   sh scripts/export-plugin.sh <插件名> --apply      # 落盘
#   sh scripts/export-plugin.sh <插件名> --from <源目录> --apply
#   sh scripts/export-plugin.sh <插件名> --no-check --apply   # 跳过隐私自检
#
# 源目录默认取 $DSH_PLUGIN_SRC/<仓库内相对路径>，$DSH_PLUGIN_SRC 默认为
# ~/dsh-ubuntu-kit。
# ============================================================================
set -u

NAME=''
SRC_ROOT="${DSH_PLUGIN_SRC:-$HOME/dsh-ubuntu-kit}"
FROM=''
APPLY=0
CHECKS=1

while [ $# -gt 0 ]; do
	case "$1" in
		--apply) APPLY=1 ;;
		--from)
			shift
			[ $# -gt 0 ] || { echo "export-plugin: --from 需要一个目录" >&2; exit 2; }
			FROM=$1
			;;
		--no-check) CHECKS=0 ;;
		-h|--help)
			sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
			exit 0
			;;
		-*) echo "export-plugin: 未知参数: $1" >&2; exit 2 ;;
		*)
			[ -z "$NAME" ] || { echo "export-plugin: 只能指定一个插件名" >&2; exit 2; }
			NAME=$1
			;;
	esac
	shift
done

[ -n "$NAME" ] || { echo "export-plugin: 缺少插件名（用法见 --help）" >&2; exit 2; }

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || ROOT=''
if [ -z "$ROOT" ]; then
	echo "export-plugin: 不在 git 仓库里" >&2
	exit 2
fi
cd "$ROOT" || exit 2

# 名字带 / 就按仓库内同名路径镜像（例如 presets/dsh-router-standard），
# 不带 / 则默认落在 plugins/ 下。这样 presets、tools 之类不需要特例。
case "$NAME" in
	*/*) REL=$NAME ;;
	*)   REL="plugins/$NAME" ;;
esac

# 目标必须落在本仓库内：挡住绝对路径和 .. 之类把别处镜像掉。
case "$REL" in
	/*|.|..|../*|*/../*|*/..)
		echo "export-plugin: 非法目标路径: $REL" >&2
		exit 2
		;;
esac
DST="$ROOT/$REL"
case "$DST" in
	"$ROOT"/*) : ;;
	*) echo "export-plugin: 拒绝写入仓库之外的路径: $DST" >&2; exit 2 ;;
esac

if [ -n "$FROM" ]; then
	SRC=${FROM%/}
else
	SRC="${SRC_ROOT%/}/$REL"
fi

if [ ! -d "$SRC" ]; then
	echo "export-plugin: 源目录不存在: $SRC" >&2
	exit 1
fi

# 演练阶段不落任何东西——连目标父目录也不建，否则 dry-run 会留下空目录。
if [ "$APPLY" = "1" ]; then
	mkdir -p "$(dirname "$DST")" || exit 1
fi

# 与 .gitignore 的意图对齐，外加本机运行数据与打包产物。
EXCLUDES='--exclude=node_modules/ --exclude=__pycache__/ --exclude=*.py[cod] --exclude=*.log --exclude=logs/ --exclude=state/ --exclude=dist/ --exclude=.DS_Store --exclude=Thumbs.db --exclude=desktop.ini --exclude=*.tmp --exclude=*.swp --exclude=*~ --exclude=probe/out/ --exclude=*.tgz'

echo "源   : $SRC"
echo "目标 : $DST"
echo "排除 : node_modules/ __pycache__/ *.py[cod] *.log logs/ state/ dist/ 编辑器垃圾 probe/out/ *.tgz"
if [ "$APPLY" = "0" ]; then
	echo "模式 : 演练（加 --apply 才真正写入）"
else
	echo "模式 : 落盘"
fi
echo ""

# shellcheck disable=SC2086
if [ "$APPLY" = "0" ]; then
	# shellcheck disable=SC2086
	rsync -a --delete --itemize-changes --dry-run $EXCLUDES "$SRC/" "$DST/"
	RC=$?
else
	# shellcheck disable=SC2086
	rsync -a --delete --itemize-changes $EXCLUDES "$SRC/" "$DST/"
	RC=$?
fi
[ "$RC" -eq 0 ] || { echo "export-plugin: rsync 失败（退出 $RC）" >&2; exit 3; }

if [ "$APPLY" = "1" ]; then
	echo ""
	echo "已同步。下一步：git status 看差异，再决定提交。"
	if [ "$CHECKS" = "1" ] && [ -f scripts/privacy-check.sh ]; then
		echo ""
		echo "--- 隐私自检 ---"
		sh scripts/privacy-check.sh
		RC=$?
		if [ "$RC" -ne 0 ]; then
			echo ""
			echo "export-plugin: 隐私自检未通过（退出 $RC）——修完再提交，不要直接推。" >&2
			exit "$RC"
		fi
	fi
fi

exit 0
