#!/usr/bin/env bash
# 工作目录：${CONTRACT_E2E_WORK:-.contract-e2e}/（仓库内相对路径，已 gitignore；证据落 _sync 的是副本）
# CD-079 前半验收：起一个**隔离**的 Hub 实例（独立配置目录 / 独立库 / 独立端口），
# 对着它真跑 examples/quickstart.sh，最后收尾杀进程。
#
# 隔离口径（沿用本仓 E2E 惯例，绝不碰生产 ./sync_hub.db）：
#   全部数据路径走 SYNC_HUB_* env 覆盖到临时目录；config.yaml 里只留 auth 与 server。
set -uo pipefail
cd /e/sync-hub-case || exit 1

BASE="${CONTRACT_E2E_WORK:-.contract-e2e}/hub-e2e"
rm -rf "$BASE"; mkdir -p "$BASE/config"
TOKEN="qs-hub-token-$(date +%s)"
PORT=3075

cat > "$BASE/config/config.yaml" <<YAML
auth:
  enabled: true
  hub_token: "$TOKEN"
  registration: open
server:
  host: 127.0.0.1
  port: $PORT
database:
  path: "$BASE/qs.db"
  backup_enabled: false
rate_limit:
  per_ip: 1000
YAML

export SYNC_HUB_CONFIG_DIR="$BASE/config"
export SYNC_HUB_DB="$BASE/qs.db"
export SYNC_HUB_DB_GUARD=1              # 硬门：解析到仓库根生产库即 RuntimeError
export SYNC_HUB_CHROMA_PATH="$BASE/chroma"
export SYNC_HUB_AUDIT_DIR="$BASE/audit"
export SYNC_HUB_WIKI_ROOT="$BASE/wiki"
export SYNC_HUB_YSTORE_PATH="$BASE/ystore.db"
export SYNC_HUB_DATA_TRUNK=0
export SYNC_HUB_DISABLE_WIKI_SYNC=1

echo "隔离实例：config=$SYNC_HUB_CONFIG_DIR  db=$SYNC_HUB_DB  port=$PORT"

python main.py > "$BASE/hub.log" 2>&1 &
HUB_PID=$!
cleanup() {
  echo "[e2e] 收尾：kill Hub pid=$HUB_PID"
  kill "$HUB_PID" 2>/dev/null
  sleep 2
  kill -9 "$HUB_PID" 2>/dev/null
  true
}
trap cleanup EXIT

READY=0
for i in $(seq 1 40); do
  if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
if [ "$READY" != "1" ]; then
  echo "[e2e] Hub 未就绪，日志尾部："
  tail -25 "$BASE/hub.log"
  exit 1
fi
echo "[e2e] Hub 就绪（第 ${i} 次轮询）"

echo
echo "===================== quickstart 真跑 ====================="
HUB="http://127.0.0.1:$PORT" HUB_TOKEN="$TOKEN" bash examples/quickstart.sh
QS_RC=$?
echo "===================== QUICKSTART_EXIT=$QS_RC ====================="

echo
echo "[e2e] Hub 侧留痕（证明请求真的到达）："
grep -c "POST /api/v1" "$BASE/hub.log" 2>/dev/null | sed 's/^/  写类请求数: /'
grep -E "register|memory/store|tasks/create|/ws/" "$BASE/hub.log" 2>/dev/null | tail -8 | sed 's/^/  | /'

exit "$QS_RC"
