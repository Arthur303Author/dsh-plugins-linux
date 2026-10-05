#!/bin/sh
# ============================================================================
# privacy-check.sh — 推送前隐私自检（Linux / POSIX shell 版）
#
# 与 scripts/privacy-check.ps1 行为对齐。仓库是公开的，推送前扫四个面：
#   1. 工作树（已跟踪文件）
#   2. 全部历史提交（一次 git grep 覆盖所有 rev）
#   3. 已跟踪文件的“路径”本身
#   4. 未跟踪文件——刚导出、还没 git add 的副本正是这种状态
#
# 命中即非零退出，pre-push hook 据此中止推送。
#
# 从 ps1 版继承的两条硬教训：
#   * 每个针脚都以“固定字符串”匹配（`git grep -F -f <针脚文件>`），不用正则——
#     `git grep -E` 用的是 POSIX ERE，没有 \s 之类简写，一条坏 pattern 会让整个
#     alternation 编译失败；Windows 上含双引号的正则还会被命令行引号吃掉，静默
#     匹配不到。从文件读针脚也免掉了 shell 拼参数的转义风险。
#   * 必须检查退出码：git grep 0=有命中、1=无命中、>1=出错。不检查就会把
#     “扫描本身坏了”当成“干净”，这是这类工具最糟的失败模式。
#
# 低置信度分级：裸用户名（如 asir）是短子串，容易撞上 base64 内联资源。所以裸
# 用户名单独扫，且只在该行长度 <= 200 时才算命中；超长行降级为提示，--strict
# 可把它恢复成阻断。带路径的针脚（/home/<user>、C:\Users\<user>）不受此豁免。
#
# 用法：
#   sh scripts/privacy-check.sh
#   sh scripts/privacy-check.sh --extra 'internal-host' --extra 'corp\.example'
#   sh scripts/privacy-check.sh --strict --quiet
#
# 退出码：0 干净；1 有命中；2 不在 git 仓库里；3 扫描本身失败（绝不能当成干净）。
# ============================================================================
set -u

QUIET=0
STRICT=0
EXTRA=''

usage() {
	cat <<'EOF'
用法: sh scripts/privacy-check.sh [选项]

  -e, --extra <字符串>   追加一个针脚（可重复）
  -s, --strict           低置信度命中（超长行里的裸用户名）也视为命中
  -q, --quiet            只在有命中时输出
  -h, --help             显示本帮助

退出码: 0 干净 | 1 有命中 | 2 不在 git 仓库 | 3 扫描失败
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
		-q|--quiet) QUIET=1 ;;
		-s|--strict) STRICT=1 ;;
		-e|--extra)
			shift
			if [ $# -eq 0 ]; then
				echo "privacy-check: --extra 需要一个值" >&2
				exit 2
			fi
			if [ -n "$EXTRA" ]; then
				EXTRA="$EXTRA
$1"
			else
				EXTRA="$1"
			fi
			;;
		-h|--help) usage; exit 0 ;;
		*) echo "privacy-check: 未知参数: $1" >&2; exit 2 ;;
	esac
	shift
done

# --- 定位仓库根 -------------------------------------------------------------
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || ROOT=''
if [ -z "$ROOT" ]; then
	echo "privacy-check: 不在 git 仓库里" >&2
	exit 2
fi
cd "$ROOT" || exit 2

# --- 账户名 -----------------------------------------------------------------
USER_NAME=${USER:-}
if [ -z "$USER_NAME" ]; then
	USER_NAME=$(id -un 2>/dev/null || echo '')
fi
if [ -z "$USER_NAME" ]; then
	echo "privacy-check: 无法确定当前账户名，拒绝在不确定的情况下扫描" >&2
	exit 3
fi

# --- 临时工作区 -------------------------------------------------------------
TMP=$(mktemp -d 2>/dev/null) || {
	echo "privacy-check: mktemp 失败" >&2
	exit 3
}
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

HIGH="$TMP/high"
LOW="$TMP/low"
FIND="$TMP/findings"
NOTE="$TMP/notes"
: > "$FIND"
: > "$NOTE"

# --- 针脚清单 ---------------------------------------------------------------
# 高置信度：特异性足够，命中即阻断。
{
	printf '%s\n' \
		"/home/$USER_NAME" \
		"/Users/$USER_NAME" \
		"C:\\Users\\$USER_NAME" \
		'api_key=' 'apikey=' 'api-key=' \
		'api_key:' 'apikey:' 'api-key:' \
		'password=' 'password:' \
		'secret=' 'secret:' \
		'BEGIN RSA PRIVATE KEY' \
		'BEGIN OPENSSH PRIVATE KEY' \
		'BEGIN PRIVATE KEY' \
		'ghp_' 'sk-ant-' 'sk-proj-' 'sk-live-'
	# 刻意不用裸 'sk-'：它会命中 disk-mode 这类普通词（ps1 版踩过）。
	if [ -n "$EXTRA" ]; then
		printf '%s\n' "$EXTRA"
	fi
} > "$HIGH"
printf '%s\n' "$USER_NAME" > "$LOW"

# 本脚本与 ps1 版都列出了全部针脚，所以**两张表都会自匹配**——必须用 git 的
# pathspec 把两者一起排除，而不是事后过滤输出。（ps1 版原先只排除它自己，
# 于是会命中本文件；两边现在都排除了对方。）

SCAN_FAILED=0

# 把命中行分类归档：低置信度的超长行（多为 base64 内联资源）进 NOTE，其余进
# FIND；低置信度只补高置信度没覆盖到的行，免得同一行报两次。
record_stream() {
	_prefix=$1
	_low=$2
	while IFS= read -r _line; do
		[ -n "$_line" ] || continue
		if [ "$_low" = "1" ]; then
			if [ "${#_line}" -gt 200 ]; then
				printf '%s  %s\n' "$_prefix" "$_line" >> "$NOTE"
				continue
			fi
			if grep -qF -- "$_prefix  $_line" "$FIND" 2>/dev/null; then
				continue
			fi
		fi
		printf '%s  %s\n' "$_prefix" "$_line" >> "$FIND"
	done
}

# gather <worktree|history> <针脚文件> <是否低置信度> [rev...]
gather() {
	_prefix=$1
	_needles=$2
	_low=$3
	shift 3

	# -f 让 git grep 自己从文件读针脚，省掉把每个针脚拼成 -e 参数的麻烦。
	_out=$(git grep -I -i -n -F -f "$_needles" "$@" -- . \
		':(exclude)scripts/privacy-check.sh' \
		':(exclude)scripts/privacy-check.ps1' 2>&1)
	_rc=$?

	if [ "$_rc" -eq 0 ]; then
		printf '%s\n' "$_out" | record_stream "$_prefix" "$_low"
	elif [ "$_rc" -gt 1 ]; then
		printf '%s\n' "$_out" | head -3 >&2
		return 3
	fi
	return 0
}

# 未跟踪文件不在 `git grep` 的视野里（它只搜已跟踪内容）——可导出完还没 git add
# 时，它们已经躺在仓库里了。不单独扫一遍，自检就会假报“干净”，而假干净正是这
# 类工具最危险的失败模式。
gather_untracked() {
	_needles=$1
	_low=$2
	git ls-files --others --exclude-standard | while IFS= read -r _f; do
		[ -n "$_f" ] || continue
		case "$_f" in
			scripts/privacy-check.sh|scripts/privacy-check.ps1) continue ;;
		esac
		[ -f "$_f" ] || continue
		# -H：只搜单个文件时 grep 默认不打印文件名，那报告就没法定位了。
		_out=$(grep -I -i -H -n -F -f "$_needles" -- "$_f" 2>/dev/null)
		[ -n "$_out" ] || continue
		printf '%s\n' "$_out" | record_stream untracked "$_low"
	done
}

if [ "$QUIET" = "0" ]; then
	echo "privacy-check: 扫描已跟踪内容、未跟踪文件、全部历史与路径"
	echo "  账户名 : $USER_NAME"
	echo "  高置信 : $(wc -l < "$HIGH" | tr -d ' ') 个针脚"
	[ -n "$EXTRA" ] && echo "  额外   : $(printf '%s' "$EXTRA" | tr '\n' ' ')"
fi

# --- 1/2. 工作树 + 全部历史 --------------------------------------------------
gather worktree "$HIGH" 0 || SCAN_FAILED=1
gather worktree "$LOW" 1 || SCAN_FAILED=1
gather_untracked "$HIGH" 0
gather_untracked "$LOW" 1

REVS=$(git rev-list --all 2>/dev/null || echo '')
if [ -n "$REVS" ]; then
	gather history "$HIGH" 0 $REVS || SCAN_FAILED=1
	gather history "$LOW" 1 $REVS || SCAN_FAILED=1
fi

# --- 3. 已跟踪路径 -----------------------------------------------------------
git ls-files 2>/dev/null | while IFS= read -r _p; do
	[ -n "$_p" ] || continue
	_lp=$(printf '%s' "$_p" | tr 'A-Z' 'a-z')
	{ cat "$HIGH"; cat "$LOW"; } | while IFS= read -r _n; do
		[ -n "$_n" ] || continue
		_ln=$(printf '%s' "$_n" | tr 'A-Z' 'a-z')
		case "$_lp" in
			*"$_ln"*)
				printf 'path      %s  (matches %s)\n' "$_p" "$_n" >> "$FIND"
				break
				;;
		esac
	done
done

if [ "$SCAN_FAILED" = "1" ]; then
	echo "privacy-check: 中止——扫描本身失败了，必须当成“不干净”处理。" >&2
	exit 3
fi

FIND_COUNT=$(wc -l < "$FIND" | tr -d ' ')
NOTE_COUNT=$(wc -l < "$NOTE" | tr -d ' ')

BLOCKED=0
[ "$FIND_COUNT" -gt 0 ] && BLOCKED=1
if [ "$STRICT" = "1" ] && [ "$NOTE_COUNT" -gt 0 ]; then
	BLOCKED=1
fi

if [ "$BLOCKED" = "1" ]; then
	echo ""
	echo "发现 $FIND_COUNT 处疑似隐私泄漏（另有 $NOTE_COUNT 处低置信度）:" >&2
	head -40 "$FIND" >&2
	[ "$FIND_COUNT" -le 40 ] || echo "  ... 其余 $((FIND_COUNT - 40)) 处省略" >&2
	if [ "$STRICT" = "1" ] && [ "$NOTE_COUNT" -gt 0 ]; then
		echo "  --strict 下的低置信度命中:" >&2
		head -10 "$NOTE" >&2
	fi
	echo "" >&2
	echo "把每个值改写成等价的可移植写法（环境变量 / homedir() / .gitignore），" >&2
	echo "不要直接删——删了等于把功能改坏，判据是改完之后仍然可用。" >&2
	exit 1
fi

if [ "$NOTE_COUNT" -gt 0 ] && [ "$QUIET" = "0" ]; then
	echo ""
	echo "privacy-check: 忽略 $NOTE_COUNT 处低置信度命中（超长行，多为内联 base64）:"
	head -3 "$NOTE"
	echo "  用 --strict 可让它们也阻断。"
fi

[ "$QUIET" = "1" ] || echo "privacy-check: 干净"
exit 0
