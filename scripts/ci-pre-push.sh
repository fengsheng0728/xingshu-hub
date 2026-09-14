#!/usr/bin/env bash
# 本地 CI 门禁（pre-push hook 本体，入库可复用）
# 用法：安装到 .git/hooks/pre-push（Windows git-bash 环境）
#   或手动执行: bash scripts/ci-pre-push.sh
# 行为：push 前跑全量离线集回归，非 0 退出即拦截 push
# 基线（2026-09-02 实测）：468 passed + 60 skipped + 5 deselected + 0 failed/errors
set -u

echo "▶ 本地 CI 门禁：全量离线集回归（~3min，基线 468 passed）..."

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -n "$REPO_ROOT" ]; then cd "$REPO_ROOT"; fi

# 前置：清理残留测试 Hub（3062 测试专用端口——绝不碰 3060 dev Hub）
# 坑：test_semantic_degrade 等自起测试 Hub，上次运行残留会致 register 401 flaky（2026-09-02 实测 468→466）
TEST_HUB_PIDS=$(netstat -ano 2>/dev/null | grep ":3062" | grep -i "LISTENING" | awk '{print $NF}' | sort -u)
if [ -n "$TEST_HUB_PIDS" ]; then
  echo "▶ 清理残留测试 Hub (3062): $TEST_HUB_PIDS"
  for p in $TEST_HUB_PIDS; do
    taskkill //F //PID $p >/dev/null 2>&1 || true
  done
  sleep 2
fi

python -m pytest tests/ -q \
  -k "not test_cross_agent_403 and not test_memory_search_self_access and not test_memory_list_self_access" \
  --ignore=tests/test_team_integration.py

rc=$?
if [ $rc -ne 0 ]; then
  echo ""
  echo "✗ 门禁拦截：回归失败（exit=$rc），push 已阻止。先修复再 push。"
  echo "  临时绕过（不推荐）：git push --no-verify"
  exit 1
fi
echo "✓ 门禁通过（离线集全绿）"
exit 0
