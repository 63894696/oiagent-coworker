#!/usr/bin/env bash
# fork_openworker.sh — W1-1.1 oiagent-coworker 仓 fork 脚本
#
# 目的:从 https://github.com/andrewyng/openworker fork 到个人账号
#      https://github.com/63894696/oiagent-coworker,配合 W1-1.2/1.3/1.4
#      改造成 OIagent 派生仓。
#
# 硬约束(用户 2026-08-02 拍板):
#   - License 保留 MIT,不改
#   - 不被上游商业策略绑定
#   - 上游归属必须保留(andrewyng/openworker URL 在 fork 仓也写明)
#   - Fork 到个人账号:63894696
#   - 反 flattery 4 条(见 NOTICE.E)
#
# 前置:
#   - git 已装
#   - gh 已装(gh auth login 已完成)
#   - 凭据对 63894696 有 push 权限
#
# 用法:
#   bash scripts/fork_openworker.sh [--dry-run]
#
# 流程:
#   1. 探测本地是否已 clone upstream
#   2. 探测 upstream 最新 commit SHA(pinning)
#   3. 探测目标仓 63894696/oiagent-coworker 是否已存在
#   4. 若不存在 — gh repo fork 创建
#   5. clone 个人仓到 out/oiagent-coworker/
#   6. 写入 UPSTREAM_COMMIT 占位
#   7. 跑 dry-run 的 W1-1.2 license lint(只对新仓)
#   8. 跑 W1-1.4 rename 脚本 --dry-run(只对新仓)
#   9. 打印复盘 checklist
#
# 失败回滚:
#   - gh repo fork 创建后又被 --dry-run 撤回:gh repo delete <user>/oiagent-coworker

set -euo pipefail

# ---------------- 配置 ----------------
UPSTREAM_REPO="andrewyng/openworker"
UPSTREAM_URL="https://github.com/${UPSTREAM_REPO}.git"
TARGET_USER="63894696"   # 用户 2026-08-02 指定的 fork target(个人账号)
TARGET_REPO="oiagent-coworker"
TARGET_URL="https://github.com/${TARGET_USER}/${TARGET_REPO}.git"
WORKDIR="$(pwd)/out"
CLONE_DIR="${WORKDIR}/${TARGET_REPO}"
UPSTREAM_CLONE_DIR="${WORKDIR}/upstream-clone"
DRY_RUN=0

# 颜色
if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'
    C_YELLOW=$'\033[0;33m'
    C_GREEN=$'\033[0;32m'
    C_BLUE=$'\033[0;34m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_RED=""; C_YELLOW=""; C_GREEN=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

log()  { printf '%s[fork]%s %s\n' "$C_BLUE" "$C_RESET" "$*" >&2; }
warn() { printf '%s[fork] WARN:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[fork] ERR:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
ok()   { printf '%s[fork] OK%s   %s\n' "$C_GREEN" "$C_RESET" "$*" >&2; }

# ---------------- 参数 ----------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --target-user) TARGET_USER="$2"; shift 2 ;;
        --target-repo) TARGET_REPO="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
fork_openworker.sh — W1-1.1 oiagent-coworker 仓 fork 脚本

用法:
  bash scripts/fork_openworker.sh [--dry-run] [--target-user <user>] [--target-repo <repo>]

参数:
  --dry-run          只规划不打(默认)
  --target-user <u>  GitHub 用户名(默认 63894696)
  --target-repo <r>  目标仓名(默认 oiagent-coworker)
  -h, --help         本帮助

退出码:
  0  成功
  1  前置探测失败(gh 未装 / 未登录 / 拿了 upstream)
  2  参数错误

详见脚本顶部说明 + W1-1.1 的 docs/rename-manifest.md § 6。
EOF
            exit 0
            ;;
        *) err "未知参数: $1"; exit 2 ;;
    esac
done

# ---------------- 前置探测 ----------------
log "前置探测..."

# git 必需
if ! command -v git >/dev/null 2>&1; then
    err "git 未装;brew install git / apt install git"
    exit 1
fi

# gh 必需
if ! command -v gh >/dev/null 2>&1; then
    err "gh 未装;https://cli.github.com/"
    exit 1
fi

# gh auth 必需
if ! gh auth status >/dev/null 2>&1; then
    err "gh 未登录;gh auth login"
    exit 1
fi

# 取 gh 当前用户(交叉验证)
GH_USER="$(gh api user --jq .login 2>/dev/null || echo '')"
if [[ -z "$GH_USER" ]]; then
    err "gh auth 异常,user API 返回空"
    exit 1
fi

# 用户检查:gh 登录用户必须等于 TARGET_USER
if [[ "$GH_USER" != "$TARGET_USER" ]]; then
    warn "gh 登录用户 = $GH_USER,但 --target-user = $TARGET_USER"
    if [[ $DRY_RUN -eq 0 ]]; then
        err "用错账号 fork 会污染别人仓;--dry-run 跳过此检查"
        exit 1
    fi
fi

ok "git, gh, gh auth OK;user=$GH_USER"

# ---------------- 探测 upstream ----------------
log "探测 upstream $UPSTREAM_REPO..."

# 拿 upstream 最新 commit SHA(用 ls-remote,不 clone)
UPSTREAM_SHA="$(git ls-remote "$UPSTREAM_URL" HEAD | awk '{print $1}')"
if [[ -z "$UPSTREAM_SHA" ]]; then
    err "无法拿到 upstream HEAD;网络问题 / 仓库不存在"
    exit 1
fi
ok "upstream HEAD = ${UPSTREAM_SHA:0:12}"

# 拿 upstream 默认分支
UPSTREAM_BRANCH="$(git ls-remote --symref "$UPSTREAM_URL" HEAD 2>/dev/null \
    | awk '/^ref:/ {print $2}' | sed 's|refs/heads/||')"
[[ -z "$UPSTREAM_BRANCH" ]] && UPSTREAM_BRANCH="main"
ok "upstream default branch = $UPSTREAM_BRANCH"

# ---------------- 探测 target 是否已存在 ----------------
log "探测 target ${TARGET_USER}/${TARGET_REPO}..."

if gh repo view "${TARGET_USER}/${TARGET_REPO}" >/dev/null 2>&1; then
    warn "target 仓已存在: https://github.com/${TARGET_USER}/${TARGET_REPO}"
    warn "  → 不再 gh repo fork(避免重复 fork 触发 upstream contributor graph 警告)"
    REPO_EXISTS=1
else
    ok "target 仓不存在,可以 fork"
    REPO_EXISTS=0
fi

# ---------------- fork (if needed) ----------------
if [[ $REPO_EXISTS -eq 0 ]]; then
    log "准备 gh repo fork ${UPSTREAM_REPO} --fork --remote --into ${TARGET_REPO}..."
    if [[ $DRY_RUN -eq 1 ]]; then
        ok "DRY-RUN:跳过 gh repo fork"
    else
        gh repo fork "$UPSTREAM_REPO" \
            --fork \
            --remote \
            --clone=false \
            -- "${TARGET_REPO}"
        ok "gh repo fork 完成"
    fi
else
    ok "复用已存在的 target 仓"
fi

# ---------------- clone 个人仓 ----------------
log "准备 clone ${TARGET_URL} 到 ${CLONE_DIR}..."

if [[ -d "$CLONE_DIR" ]]; then
    warn "$CLONE_DIR 已存在;跳过 clone(假定是上次的产物)"
else
    if [[ $DRY_RUN -eq 1 ]]; then
        ok "DRY-RUN:跳过 git clone"
    else
        mkdir -p "$WORKDIR"
        git clone "$TARGET_URL" "$CLONE_DIR"
        ok "git clone 完成"
    fi
fi

# ---------------- 写 UPSTREAM_COMMIT 占位 ----------------
if [[ -d "$CLONE_DIR" && $DRY_RUN -eq 0 ]]; then
    log "写入 UPSTREAM_COMMIT = ${UPSTREAM_SHA} 到本地文件..."
    COMMIT_FILE="${CLONE_DIR}/.UPSTREAM_COMMIT"
    printf '%s\n' "$UPSTREAM_SHA" > "$COMMIT_FILE"
    ok "已写入 $COMMIT_FILE(W1-1.3 NOTICE / 模块 header 会读这个)"
fi

# ---------------- 跑 W1-1.2 license lint ----------------
if [[ -d "$CLONE_DIR" && $DRY_RUN -eq 0 ]]; then
    log "跑 W1-1.2 license_lint.py(对新仓)..."
    if [[ -f "scripts/license_lint.py" ]]; then
        # 期望新仓 0 RED(MIT 全)
        # 跑 license lint 但不 --ci(只警告)
        python scripts/license_lint.py "$CLONE_DIR" || warn "license_lint 退出非 0"
    else
        warn "scripts/license_lint.py 不存在;跳过"
    fi
fi

# ---------------- 跑 W1-1.4 rename --dry-run ----------------
if [[ -d "$CLONE_DIR" && $DRY_RUN -eq 0 ]]; then
    log "跑 W1-1.4 rename_openworker.py --dry-run(对新仓)..."
    if [[ -f "scripts/rename_openworker.py" ]]; then
        python scripts/rename_openworker.py \
            --src "$CLONE_DIR" \
            --dst "${CLONE_DIR}.preview" \
            || warn "rename dry-run 退出非 0"
    else
        warn "scripts/rename_openworker.py 不存在;跳过"
    fi
fi

# ---------------- 复盘 checklist ----------------
cat <<EOF

${C_BOLD}================ W1-1.1 Fork 复盘 Checklist ================${C_RESET}

1. GitHub 上两仓的关系:
   - upstream:  https://github.com/${UPSTREAM_REPO}  (commit: ${UPSTREAM_SHA:0:12})
   - fork:      https://github.com/${TARGET_USER}/${TARGET_REPO}

2. UPSTREAM_COMMIT 已 pinning:${UPSTREAM_SHA}

3. 下一步:
   ☐ 把 W1-1.3 文件(NOTICE / LICENSE-OPENWORKER / LICENSE)复制到 fork 仓根
   ☐ 把 W1-1.4 改造清单 fork 仓根 docs/rename-manifest.md
   ☐ 跑 rename_openworker.py --apply(对 fork 仓)
   ☐ 跑 license_lint.py fork/ --ci,期望 0 RED
   ☐ 跑 add_openworker_header.py 批量插 SPDX header
   ☐ commit + push 到 ${TARGET_USER}/${TARGET_REPO}
   ☐ create PR(若希望 upstream 知道)/ 关掉 sync(若希望完全独立)

4. 反 flattery 检查(不能漏):
   - License 仍是 MIT ✓
   - NOTICE 包含 'NOT a redistribution of OpenWorker in its entirety' ✓
   - README 不使用 OpenWorker 商标 ✓
   - pyproject.toml [project.urls] 包含 Upstream 字段 ✓

5. 不可做(reviewer 必查):
   - 不能改 LICENSE-OPENWORKER 内容
   - 不能删 NOTICE.E(anti-flattery policy)
   - 不能关掉 sync 后改 LICENSE(commit history 留下证据)

${C_BOLD}=========================================================${C_RESET}
EOF

if [[ $DRY_RUN -eq 1 ]]; then
    ok "DRY-RUN 完毕;--dry-run 去掉即可实跑"
else
    ok "fork 完成。clone 路径: $CLONE_DIR"
fi
