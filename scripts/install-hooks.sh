#!/bin/sh
# ============================================================================
# install-hooks.sh — 安装本仓库的 git hooks（Linux / POSIX 版）
#
# 对应 scripts/install-hooks.ps1。为什么用安装器而不是直接提交 hook：
# git 从不跟踪 .git/hooks/，hook 只能靠“被复制进去”才能到达某台机器，
# 所以模板放在仓库里，每个克隆安装一次。
#
# 用法:
#   sh scripts/install-hooks.sh              # 安装 pre-push
#   sh scripts/install-hooks.sh --uninstall  # 卸载（移入回收站，不物理删除）
#
# 装好后每次 git push 前自动跑隐私自检，命中即中止推送。
# 确实需要绕过某一次：git push --no-verify
# ============================================================================
set -u

UNINSTALL=0
for arg in "$@"; do
	case "$arg" in
		--uninstall|-u) UNINSTALL=1 ;;
		-h|--help)
			echo "用法: sh scripts/install-hooks.sh [--uninstall]"
			exit 0
			;;
		*) echo "install-hooks: 未知参数: $arg" >&2; exit 2 ;;
	esac
done

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || ROOT=''
if [ -z "$ROOT" ]; then
	echo "install-hooks: 不在 git 仓库里" >&2
	exit 2
fi
cd "$ROOT" || exit 2

HOOK_REL='.git/hooks/pre-push'
HOOK="$ROOT/$HOOK_REL"

if [ "$UNINSTALL" = "1" ]; then
	if [ ! -e "$HOOK" ]; then
		echo "install-hooks: 没有可卸载的 pre-push"
		exit 0
	fi
	# 约定：删除走回收站，不做物理删除。
	if command -v gio >/dev/null 2>&1; then
		gio trash "$HOOK" && echo "install-hooks: 已卸载 pre-push（移入回收站）"
	else
		TRASH_FILES="$HOME/.local/share/Trash/files"
		TRASH_INFO="$HOME/.local/share/Trash/info"
		mkdir -p "$TRASH_FILES" "$TRASH_INFO" || exit 1
		STAMP=$(date +%Y%m%d-%H%M%S)
		mv "$HOOK" "$TRASH_FILES/pre-push.$STAMP" || exit 1
		{
			printf '[Trash Info]\n'
			printf 'Path=%s\n' "$HOOK"
			printf 'DeletionDate=%s\n' "$(date +%Y-%m-%dT%H:%M:%S)"
		} > "$TRASH_INFO/pre-push.$STAMP.trashinfo"
		echo "install-hooks: 已卸载 pre-push（移入回收站）"
	fi
	exit 0
fi

if [ ! -f scripts/pre-push ]; then
	echo "install-hooks: 模板缺失 scripts/pre-push" >&2
	exit 1
fi

mkdir -p "$ROOT/.git/hooks" || exit 1
cp -f scripts/pre-push "$HOOK" || exit 1
chmod +x "$HOOK" || exit 1

echo "install-hooks: 已安装 .git/hooks/pre-push"
echo "  每次 push 前跑 scripts/privacy-check.（ps1|sh），命中即中止推送。"
echo "  绕过单次: git push --no-verify"
