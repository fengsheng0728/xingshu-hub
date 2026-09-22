#!/usr/bin/env bash
# 工作目录：${CONTRACT_E2E_WORK:-.contract-e2e}/（仓库内相对路径，已 gitignore；证据落 _sync 的是副本）
# CD-079 反向验证：契约文档里写的「负向语义」是否真的成立（全绿不等于真绿，要有红的对照）
#   N1  register 不带 hub_token            → 期望 401（契约 §1）
#   N1b register 带 hub_token              → 期望 200（对照，证明 N1 不是路由不存在导致的）
#   N2  GET /api/v1/memory 不带凭据         → 期望 401
#   N3  带错误凭据                          → 期望 401
#   N3b 带正确 api_key                      → 期望 200（对照）
#   N4  /tasks/{id}/cancel 缺 ?agent_id     → 期望 422（契约 §2 ①）
#   N5  WS 首帧令牌错                       → 期望 close 4401（契约 §1 WS 段）
#
# ⚠️ 上一版把 URL 漏了 /api/v1 前缀：N1/N2/N3 拿到的 401 其实是「未知路径 → 统一中间件要凭据」，
#    与「该端点的鉴权语义」无关——所以必须配 1b/3b 两条**正向对照**才能证明负向结论是真的。
set -uo pipefail
cd /e/sync-hub-case || exit 1

BASE="${CONTRACT_E2E_WORK:-.contract-e2e}/hub-neg"
rm -rf "$BASE"; mkdir -p "$BASE/config"
TOKEN="neg-hub-token-$(date +%s)"
PORT=3076

cat > "$BASE/config/config.yaml" <<YAML
auth:
  enabled: true
  hub_token: "$TOKEN"
  registration: open
server:
  host: 127.0.0.1
  port: $PORT
database:
  path: "$BASE/neg.db"
  backup_enabled: false
YAML

export SYNC_HUB_CONFIG_DIR="$BASE/config"
export SYNC_HUB_DB="$BASE/neg.db"
export SYNC_HUB_DB_GUARD=1
export SYNC_HUB_CHROMA_PATH="$BASE/chroma"
export SYNC_HUB_AUDIT_DIR="$BASE/audit"
export SYNC_HUB_WIKI_ROOT="$BASE/wiki"
export SYNC_HUB_YSTORE_PATH="$BASE/ystore.db"
export SYNC_HUB_DATA_TRUNK=0

python main.py > "$BASE/hub.log" 2>&1 &
HUB_PID=$!
trap 'kill $HUB_PID 2>/dev/null; sleep 1; kill -9 $HUB_PID 2>/dev/null; true' EXIT

for i in $(seq 1 40); do
  curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 2
done
H="http://127.0.0.1:$PORT"
API="$H/api/v1"
T=".neg-tmp"; mkdir -p "$T"
echo "隔离实例就绪：$H（port=$PORT）"

FAIL=0
chk() { # chk <名> <期望> <实际> [现场文件]
  if [ "$2" = "$3" ]; then
    echo "  [OK]   $1 → $3（期望 $2）"
  else
    echo "  [FAIL] $1 → $3（期望 $2）  现场：$(head -c 160 "${4:-/dev/null}" 2>/dev/null)"
    FAIL=1
  fi
}
# st <名> <期望> -- <curl 参数...>：把状态码与响应体分开取，避免 -o /dev/null 在 MSYS 上的写错误
st() {
  local name="$1" want="$2"; shift 2; [ "$1" = "--" ] && shift
  local body="$T/out.json"
  local code; code="$(curl -sS -o "$body" -w '%{http_code}' "$@" 2>/dev/null)"
  chk "$name" "$want" "$code" "$body"
}

echo; echo "──── N1 register 不带 hub_token（期望 401）────"
printf '%s' '{"agent_id":"neg-a1","agent_name":"neg","role":"worker"}' > "$T/r.json"
st "register 无凭据" "401" -- -X POST "$API/agents/register" -H 'Content-Type: application/json' --data-binary @"$T/r.json"

echo "──── N1b register 带 hub_token（期望 200，对照）────"
st "register 带 hub_token" "200" -- -X POST "$API/agents/register" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" --data-binary @"$T/r.json"
KEY="$(python -c "import json;print(json.load(open('$T/out.json')).get('api_key',''))" 2>/dev/null)"
echo "        （拿到 api_key：${KEY:0:8}…${KEY: -4}，长度 ${#KEY}）"

echo "──── N2 业务端点不带凭据（期望 401）────"
st "GET /api/v1/memory 无凭据" "401" -- "$API/memory?agent_id=neg-a1"

echo "──── N3 业务端点带错误凭据（期望 401）────"
st "GET /api/v1/memory 错凭据" "401" -- -H 'Authorization: Bearer wrong-key-xxxx' "$API/memory?agent_id=neg-a1"

echo "──── N3b 业务端点带正确 api_key（期望 200，对照）────"
st "GET /api/v1/memory 正确 key" "200" -- -H "Authorization: Bearer $KEY" "$API/memory?agent_id=neg-a1"

echo "──── N4 状态推进缺 ?agent_id（期望 422）────"
printf '%s' '{"task_id":"neg-task-1","description":"neg","creator_agent_id":"neg-a1"}' > "$T/t.json"
curl -sS -o "$T/tc.json" -X POST "$API/tasks/create" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEY" --data-binary @"$T/t.json"
st "POST /tasks/neg-task-1/cancel 缺 agent_id" "422" -- -X POST "$API/tasks/neg-task-1/cancel" -H "Authorization: Bearer $KEY"

echo "──── N5 WS 首帧令牌错（期望 close 4401）────"
export NEG_WS_URL="ws://127.0.0.1:$PORT/ws/neg-a1"
python - <<'PY'
import asyncio, json, os, sys
import websockets
async def main():
    try:
        async with websockets.connect(os.environ["NEG_WS_URL"]) as ws:
            await ws.send(json.dumps({"type": "auth", "token": "wrong-token-xxxx"}))
            await asyncio.wait_for(ws.recv(), timeout=5)
            print("  [FAIL] WS 错令牌竟然拿到了数据帧（期望被拒）"); return 1
    except websockets.exceptions.ConnectionClosed as e:
        code = e.code
        if code == 4401:
            print("  [OK]   WS 错令牌 → close 4401（期望 4401）"); return 0
        print(f"  [FAIL] WS 关闭码 {code}（期望 4401）"); return 1
    except Exception as e:
        print(f"  [FAIL] WS 异常 {type(e).__name__}: {e}"); return 1
sys.exit(asyncio.run(main()))
PY
[ "$?" = "0" ] || FAIL=1

rm -rf "$T"
echo
if [ "$FAIL" = "0" ]; then
  echo "  反向验证 PASS：契约 §1/§2/§4 的负向语义与实测一致（每条形都有正向对照）"
  exit 0
else
  echo "  反向验证 FAIL：契约与实测不符 —— 改文档或改代码，不许改期望值凑绿"
  exit 1
fi
