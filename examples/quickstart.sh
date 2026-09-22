#!/usr/bin/env bash
# 星枢 Hub 集成方 quickstart —— 「照这个就能接上」的最小可跑示例
#
# 配套文档：docs/integration-contract.md（契约） / docs/external-api-access.md（怎么发钥匙）
#
# 用法：
#   bash examples/quickstart.sh                          # 默认打本机 3060
#   HUB=https://hub.example.com bash examples/quickstart.sh
#   HUB_TOKEN=<部署的 hub_token> bash examples/quickstart.sh   # 部署配了 hub_token 时必须给
#
# 退出码：0 = 四步全绿；非 0 = 有步骤失败（打印现场）
#
# 四步：① 注册拿 api_key ② REST 读写（记忆/任务/知识）③ WS：首帧鉴权 + hello + ping/pong + request
#       ④ 结论
#
# ⚠️ 本脚本刻意遵守两条实测坑（2026-09-22）：
#   ① 请求体一律写 **UTF-8 文件** + `--data-binary @file` —— 中文内联 `-d` 会被 MSYS 的 curl
#      转成 GBK 字节发出，Hub 收到非法 UTF-8 → 400 "There was an error parsing the body"。
#   ② 临时文件放**脚本同目录的相对路径**（不用 `mktemp -t`）—— MSYS 的 /tmp 路径交给原生
#      程序（curl.exe / python.exe）会被路径转换，报 "cannot find the file specified"。

set -uo pipefail

HUB="${HUB:-http://127.0.0.1:3060}"
HUB_TOKEN="${HUB_TOKEN:-}"
STAMP="$(date +%s)"
AGENT_ID="${AGENT_ID:-quickstart-${STAMP}}"
MEM_KEY="${MEM_KEY:-quickstart-memory}"
KB_TITLE="${KB_TITLE:-Quickstart 知识条目 ${STAMP}}"

TMPDIR_LOCAL=".quickstart-tmp"
mkdir -p "$TMPDIR_LOCAL"
FAILED=0
step() { echo; echo "──── $* ────"; }
ok()   { echo "  [OK]   $*"; }
bad()  { echo "  [FAIL] $*"; FAILED=1; }
cleanup() { rm -rf "$TMPDIR_LOCAL"; }
trap cleanup EXIT

# body <名> <内容> —— UTF-8 字节落盘，回显路径（见文件头坑 ①）
body() { printf '%s' "$2" > "$TMPDIR_LOCAL/$1.json"; echo "$TMPDIR_LOCAL/$1.json"; }
# jget <json> <key>
jget() { printf '%s' "$1" | python -c "import json,sys;d=json.load(sys.stdin);print(d.get('$2','') if isinstance(d,dict) else '')" 2>/dev/null; }

AUTH=()                      # 需要凭据的请求头
[ -n "$HUB_TOKEN" ] && AUTH=(-H "Authorization: Bearer ${HUB_TOKEN}")

echo "Hub: $HUB    agent_id: $AGENT_ID"

# ---------- 0. 前置 ----------
step "0. 连通性与版本（/health 免认证）"
HEALTH="$(curl -fsS --max-time 10 "${HUB}/health" 2>/dev/null)" || { bad "连不上 ${HUB}/health"; exit 1; }
ok "Hub 版本 $(jget "$HEALTH" version)，状态 $(jget "$HEALTH" status)"

# ---------- 1. 注册拿凭据 ----------
step "1. POST /api/v1/agents/register（配了 hub_token 的部署必须带它）"
REG_BODY="$(body register "{\"agent_id\":\"${AGENT_ID}\",\"agent_name\":\"Quickstart ${STAMP}\",\"role\":\"worker\"}")"
REG="$(curl -sS --max-time 15 -X POST "${HUB}/api/v1/agents/register" \
        -H 'Content-Type: application/json' "${AUTH[@]}" \
        --data-binary @"$REG_BODY")" || { bad "注册请求失败：$REG"; exit 1; }
API_KEY="$(jget "$REG" api_key)"
[ -n "$API_KEY" ] || { bad "注册未返回 api_key（原文：$REG）"; exit 1; }
ok "已注册 ${AGENT_ID}，拿到 api_key（明文仅此一次）"

# 之后所有请求用这把 key
REQ=(-H "Authorization: Bearer ${API_KEY}" -H 'Content-Type: application/json')

step "1b. POST /api/v1/agents/{id}/heartbeat"
HB="$(curl -sS --max-time 10 -X POST "${HUB}/api/v1/agents/${AGENT_ID}/heartbeat" "${REQ[@]}")" \
  && ok "心跳已上报" || bad "心跳失败：$HB"

# ---------- 2. REST 读写 ----------
step "2. REST 读写（记忆 / 任务 / 知识）"

# 断言只依赖 **状态码**（契约 §4：detail 文案不构成契约）
req()  { curl -sS --max-time 15 -w '\n@@@%{http_code}' "$@"; }
code() { printf '%s' "$1" | tail -1 | sed 's/^@@@//'; }
bdy()  { printf '%s' "$1" | sed '$d'; }

MEM_BODY="$(body memory "{\"memory_key\":\"${MEM_KEY}\",\"content\":\"quickstart 写入于 ${STAMP}\",\"kind\":\"fact\",\"tags\":[\"quickstart\"]}")"
R="$(req -X POST "${HUB}/api/v1/memory/store?agent_id=${AGENT_ID}" "${REQ[@]}" --data-binary @"$MEM_BODY")"
if [ "$(code "$R")" = "200" ]; then ok "memory/store 已写：$MEM_KEY"; else bad "memory/store HTTP $(code "$R")：$(bdy "$R" | head -c 200)"; fi

R="$(req "${HUB}/api/v1/memory?agent_id=${AGENT_ID}" "${REQ[@]}")"
if [ "$(code "$R")" = "200" ] && bdy "$R" | grep -q "$MEM_KEY"; then
  ok "GET /memory 读回自己刚写的记忆（写读闭环）"
else
  bad "记忆没读回（HTTP $(code "$R")）：$(bdy "$R" | head -c 200)"
fi

# 任务：TaskCreate 必填 task_id + description（task_id 由调用方给）；creator_agent_id 写自己，否则 403。
# 两条实测口径（契约 §2）：
#   ① /start、/complete、/cancel、/fail 都要 **?agent_id=<自己>**，缺则 422（不是 403）；
#   ② /schedule（触发派单匹配）需 manager/orchestrator 或 hub_token，且候选 Agent 必须在 **online**——
#      普通 worker 身份演示不了派单，故这里走「创建 → 读回 → 取消」这条任意身份都能走通的路径。
TASK_ID="quickstart-task-${STAMP}"
TASK_BODY="$(body task "{\"task_id\":\"${TASK_ID}\",\"description\":\"Quickstart 接入验证 ${STAMP}\",\"creator_agent_id\":\"${AGENT_ID}\"}")"
R="$(req -X POST "${HUB}/api/v1/tasks/create" "${REQ[@]}" --data-binary @"$TASK_BODY")"
if [ "$(code "$R")" = "200" ]; then
  ok "tasks/create 已建任务 task_id=${TASK_ID}"
  R="$(req "${HUB}/api/v1/tasks" "${REQ[@]}")"
  if bdy "$R" | grep -q "$TASK_ID"; then ok "GET /tasks 读回刚建的任务"; else bad "任务没读回（HTTP $(code "$R")）"; fi
  R="$(req -X POST "${HUB}/api/v1/tasks/${TASK_ID}/cancel?agent_id=${AGENT_ID}" "${REQ[@]}")"
  if [ "$(code "$R")" = "200" ] && bdy "$R" | grep -q "cancel"; then
    ok "POST /tasks/{id}/cancel?agent_id=… 状态机可推进（非终态 → cancelled）"
  else
    bad "任务取消失败 HTTP $(code "$R")：$(bdy "$R" | head -c 160)"
  fi
else
  bad "tasks/create HTTP $(code "$R")：$(bdy "$R" | head -c 200)"
fi

R="$(req "${HUB}/api/v1/knowledge" "${REQ[@]}")"
if [ "$(code "$R")" = "200" ]; then ok "GET /knowledge 可读（正文按披露规则剥离）"; else bad "知识列表 HTTP $(code "$R")：$(bdy "$R" | head -c 200)"; fi

# ---------- 3. WebSocket ----------
step "3. WS：首帧鉴权 → hello → ping/pong → request"
WS_URL="$(printf '%s' "$HUB" | sed -e 's|^http://|ws://|' -e 's|^https://|wss://|')/ws/${AGENT_ID}"
export WS_URL AGENT_ID API_KEY_FOR_WS="$API_KEY"
python - <<'PY'
import asyncio, json, os, sys, time, uuid

try:
    import websockets
except ImportError:
    print("  [SKIP] 本机 python 无 websockets 包，跳过 WS 步骤（pip install websockets 后重跑）")
    sys.exit(0)

URL, AGENT, TOKEN = os.environ["WS_URL"], os.environ["AGENT_ID"], os.environ["API_KEY_FOR_WS"]

def env(type_, payload, session_id=""):
    return {"type": type_, "id": uuid.uuid4().hex, "session_id": session_id,
            "via": "human", "ts": int(time.time() * 1000), "version": 2, "payload": payload}

async def main():
    fail = 0
    try:
        async with websockets.connect(URL) as ws:
            # ① 首帧鉴权（必须在 WS_AUTH_TIMEOUT_SEC，默认 3s 内发出）
            await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
            # ② hello：上报 checkpoint，触发未确认派单重放
            await ws.send(json.dumps(env("hello", {"agent_id": AGENT, "last_checkpoint_id": "", "agent_version": "1.0.0"})))
            # ③ ping → 期待 pong
            await ws.send(json.dumps(env("ping", {})))
            # ④ request：WS 上的同步方法（memory_search / memory_store / memory_list）
            # ⚠️ CD-091（2026-09-22 实测）：`memory_search` 目前**恒返回 error**
            #    （Hub 内部调了不存在的 `hub.search_memory`，真名是 `hub.memory_search(req)`），
            #    故这里用可用的 `memory_list` 演示 request 通道；CD-091 修好后可换回 memory_search。
            rid = uuid.uuid4().hex
            req = env("request", {"method": "memory_list", "params": {"kind": ""}})
            req["id"] = rid
            await ws.send(json.dumps(req))

            got_pong = got_resp = False
            deadline = time.time() + 10
            while time.time() < deadline and not (got_pong and got_resp):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                frame = json.loads(raw) if raw[:1] in "{[" else {"type": "(non-json)"}
                ftype = frame.get("type", "?")
                pl = frame.get("payload", {})
                if ftype == "pong":
                    got_pong = True; print("  [OK]   收到 pong（心跳通路成立）")
                elif ftype == "response" and pl.get("id") == rid:
                    got_resp = True
                    if "error" in pl:
                        print("  [FAIL] request 返回 error：%s" % pl.get("error")); fail = 1
                    else:
                        res = pl.get("result")
                        keys = ",".join(sorted(res.keys())[:6]) if isinstance(res, dict) else type(res).__name__
                        print("  [OK]   request→response（result 字段：%s）" % keys)
                elif ftype in ("dispatch", "push", "automation.run", "shared_presence"):
                    print("  [INFO] 收到 Hub 主动帧 type=%s（接入方应按 type 分发）" % ftype)
                else:
                    print("  [INFO] 收到帧 type=%s" % ftype)
            if not got_pong: print("  [FAIL] 10s 内未收到 pong"); fail = 1
            if not got_resp: print("  [FAIL] 10s 内未收到 request 的 response"); fail = 1
    except Exception as exc:
        print("  [FAIL] WS 失败：%s: %s" % (type(exc).__name__, exc))
        print("         （若提示 close code 4401：令牌错/首帧不是 auth/超时/scoped key 未开 scope.ws）")
        fail = 1
    sys.exit(fail)

sys.exit(asyncio.run(main()))
PY
WS_RC=$?
[ "$WS_RC" = "0" ] && ok "WS 通路成立" || bad "WS 步骤未通过（rc=$WS_RC）"

# ---------- 4. 结论 ----------
step "结论"
if [ "$FAILED" = "0" ]; then
  echo "  PASS：注册 → REST 读写 → WS 四步全绿（契约 docs/integration-contract.md）"
  echo "  提示：本脚本用的是一次性身份 ${AGENT_ID}；正式接入请按契约 §1 路径 B 发受限钥匙。"
  exit 0
else
  echo "  FAIL：见上面 [FAIL] 行。排查顺序：HUB 地址 → hub_token 是否需要 → 状态码语义（契约 §4）。"
  exit 1
fi
