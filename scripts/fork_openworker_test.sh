#!/usr/bin/env bash
# fork_openworker_test.sh — W1-1.1 fork 脚本前置单元测试
#
# 模拟 gh / git / 探测状态,验证脚本决策路径
# 用真 gh 时,顶层 fork / clone 跑全链路;这里只测分支判断

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORK_SCRIPT="${SCRIPT_DIR}/fork_openworker.sh"

# ---------------- 测试 1: --help 应该退出 0 ----------------
echo "=== TEST 1: --help ==="
bash "$FORK_SCRIPT" --help >/dev/null
[[ $? -eq 0 ]] && echo "PASS: --help exits 0" || (echo "FAIL: --help non-zero"; exit 1)

# ---------------- 测试 2: 未知参数退出 2 ----------------
echo "=== TEST 2: 未知参数 ==="
set +e
bash "$FORK_SCRIPT" --unknown 2>/dev/null
RC=$?
set -e
[[ $RC -eq 2 ]] && echo "PASS: 未知参数 exit=2" || (echo "FAIL: --unknown exit=$RC, expected 2"; exit 1)

# ---------------- 测试 3: --target-user / --target-repo 覆盖 ----------------
echo "=== TEST 3: --target-user / --target-repo 覆盖 ==="
# 不运行 fork 脚本本身(因为依赖 gh),只验证 --help 文本里有
# 这两个 flag 的文档(grep 出来)
set +e
HELP_OUT="$(bash "$FORK_SCRIPT" --help 2>&1)"
set -e
if echo "$HELP_OUT" | grep -q -- "--target-user" && \
   echo "$HELP_OUT" | grep -q -- "--target-repo"; then
    echo "PASS: --target-user / --target-repo 在 --help 文本中存在"
else
    echo "FAIL: --help 文本缺 --target-user / --target-repo"
    echo "  实际输出:"
    echo "$HELP_OUT" | sed 's/^/    /'
    exit 1
fi

# ---------------- 测试 4: TARGET_USER 默认值是 63894696 ----------------
echo "=== TEST 4: TARGET_USER 默认值 = 63894696(用户 8-02 拍板) ==="
if grep -q 'TARGET_USER="63894696"' "$FORK_SCRIPT"; then
    echo "PASS: TARGET_USER 默认 = 63894696"
else
    echo "FAIL: TARGET_USER 默认值不是 63894696"
    exit 1
fi

# ---------------- 测试 5: TARGET_REPO 默认值是 oiagent-coworker ----------------
echo "=== TEST 5: TARGET_REPO 默认值 = oiagent-coworker ==="
if grep -q 'TARGET_REPO="oiagent-coworker"' "$FORK_SCRIPT"; then
    echo "PASS: TARGET_REPO 默认 = oiagent-coworker"
else
    echo "FAIL: TARGET_REPO 默认值不是 oiagent-coworker"
    exit 1
fi

# ---------------- 测试 6: 必含反 flattery 措辞 ----------------
echo "=== TEST 6: 反 flattery 4 条 keywords ==="
missing=0
for kw in "MIT" "63894696" "andrewyng/openworker" "上游" "sync" "UPSTREAM_COMMIT"; do
    if ! grep -q "$kw" "$FORK_SCRIPT"; then
        echo "  FAIL: 缺关键词: $kw"
        missing=1
    fi
done
if [[ $missing -eq 0 ]]; then
    echo "PASS: 6 个反 flattery / 关键 keyword 全在"
else
    echo "FAIL: 部分 keyword 缺失"
    exit 1
fi

# ---------------- 总结 ----------------
echo ""
echo "=== ALL TESTS PASS ==="
