#!/usr/bin/env bash
# CD-076 部署持久化演练 —— 「重建容器后数据仍在」
#
# 背景（docs/carried_debts.md CD-076）：
#   修复前 compose 把卷挂在 /app/data，而 DB/chroma/wiki/audit/ystore/config 实际落 /app/*，
#   既不在卷里、又多被 .dockerignore 排除在镜像外 → `docker compose down` / 换镜像 / 重建容器
#   = Agent 名单 + 记忆 + 知识 + 审计一次全没。
#
# 本脚本是该项的**可重复执行验收**，判据不是看配置，而是真起容器写数据 → 销毁容器 → 再起 → 读回。
#
# 用法：
#   bash scripts/deploy_persistence_drill.sh                    # 本机：自己 build 镜像
#   DRILL_IMAGE=xingshu-hub:ci bash scripts/deploy_persistence_drill.sh   # 复用已构建镜像（CI 用）
#
# 环境变量：
#   DRILL_IMAGE  已存在的镜像 ref（给了就复用，不重复 build）
#   DRILL_PORT   宿主端口（默认 3077，避免占用生产 3060）
#   KEEP_UP      1 = 演练结束后留着容器（排障用，默认销毁）
#
# 退出码：0 = PASS；非 0 = FAIL（任一步骤失败即退出，并尽量打印现场）
#
# ⚠️ 全程严禁 `docker compose down -v`：卷就是被测对象，删卷等于把测试对象删了。
# ⚠️ 不碰生产：独立 project 名（xingshu-drill）+ 独立卷 + 独立端口 + 卷内自造测试 config.yaml。

set -uo pipefail

PROJECT="${DRILL_PROJECT:-xingshu-drill}"
PORT="${DRILL_PORT:-3077}"
IMAGE="${DRILL_IMAGE:-}"
KEEP_UP="${KEEP_UP:-0}"
BASE_URL="http://127.0.0.1:${PORT}"
TOKEN="drill-hub-token-$(date +%s)-not-a-secret"
AGENT_ID="drill-agent-$(date +%s)"
MEM_KEY="drill-memory-key"
KB_TITLE="CD-076 持久化演练知识条目 $(date +%Y-%m-%dT%H:%M:%S)"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

OVERRIDE=""
COMPOSE=(docker compose -p "$PROJECT")
NO_BUILD=()
if [ -n "$IMAGE" ]; then
  # ⚠️ 不用 `mktemp -t`：MSYS 返回 /tmp/... 路径，当参数交给原生 docker.exe 会被
  # 路径转换成 E:\tmp\...（文件其实不在那儿）→ 2026-09-22 实测 `open E:\tmp\
  # drill-override-*.yml: The system cannot find the file specified`。
  # 脚本已 cd 到仓库根，改用相对文件名：bash 与 docker.exe 双方都成立。
  OVERRIDE=".drill-compose-override.yml"
  printf 'services:\n  sync-hub:\n    image: %s\n' "$IMAGE" > "$OVERRIDE"
  COMPOSE+=( -f docker-compose.yml -f "$OVERRIDE" )
  NO_BUILD=( --no-build )
  echo "[drill] 复用镜像: $IMAGE"
fi

FAILED=0
step() { echo; echo "──── $* ────"; }
ok()   { echo "  [OK]   $*"; }
bad()  { echo "  [FAIL] $*"; FAILED=1; }

cleanup() {
  if [ "$KEEP_UP" != "1" ]; then
    echo
    echo "[drill] 收尾：docker compose -p $PROJECT down（**不带 -v**，卷保留）"
    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  else
    echo "[drill] KEEP_UP=1，容器与卷保留：${PROJECT}_hub-data"
  fi
  [ -n "$OVERRIDE" ] && rm -f "$OVERRIDE"
  rm -f .drill-body-*.json
}
trap cleanup EXIT

# ---------- 0. 前置：docker 可用 ----------
step "0. 前置检查"
if ! docker info >/dev/null 2>&1; then
  echo "[drill] 本机 Docker daemon 不可用 → 无法演练（这不是 PASS，也不是被测代码失败）"
  echo "[drill] 判据应落在有可用 daemon 的环境（CI 的 docker job）"
  exit 2
fi
ok "docker daemon 可用"

# ---------- 1. 卷内自造测试 config.yaml（等价于文档里的首次部署步骤）----------
step "1. 卷内放置测试 config.yaml（含测试 hub_token）"
# 注：`docker compose run` 无 --no-build（Compose v5.1.3 实测），只有 up 有；
# 镜像已构建（DRILL_IMAGE）或本次就是要 build，run 无需该标志。
"${COMPOSE[@]}" run --rm --no-deps --entrypoint sh sync-hub -c "
  set -e
  mkdir -p /app/data/config
  cat > /app/data/config/config.yaml <<'YAML'
auth:
  enabled: true
  hub_token: \"${TOKEN}\"
  registration: open
server:
  host: 0.0.0.0
  port: 3060
YAML
  echo '  config.yaml 写入卷内: /app/data/config/config.yaml'
" || { bad "写 config.yaml 失败"; exit 1; }
ok "config.yaml 在卷内（不是镜像内、不是宿主 bind）"

# ---------- 2. 起容器 ----------
step "2. 起容器（project=$PROJECT, 宿主端口=$PORT）"
HUB_PORT="$PORT" HUB_CONTAINER_NAME="${PROJECT}-hub" "${COMPOSE[@]}" up -d "${NO_BUILD[@]}" || { bad "compose up 失败"; exit 1; }
CID="$("${COMPOSE[@]}" ps -q sync-hub)"
[ -n "$CID" ] || { bad "拿不到容器 id"; exit 1; }
ok "容器已起: ${CID:0:12}"

step "3. 等待 /health 就绪（最多 300s；镜像首次启动要建库/加载向量栈）"
READY=0
for i in $(seq 1 100); do
  if curl -fsS --max-time 5 "${BASE_URL}/health" >/dev/null 2>&1; then READY=1; break; fi
  sleep 3
done
[ "$READY" = "1" ] || { bad "300s 内 /health 未就绪"; docker logs --tail 40 "$CID" 2>&1 | sed 's/^/    | /'; exit 1; }
ok "/health 就绪（第 ${i} 次轮询）"

# ---------- 4. 写数据 ----------
step "4. 写入真数据（agent / memory / knowledge / wiki）"
# ⚠️ 中文 JSON **不能内联 `-d`**（2026-09-22 本机探针实测）：MSYS 的 curl 会把 argv 里的
#    中文转成 GBK 字节发出，FastAPI 收到非法 UTF-8 → 400 "There was an error parsing
#    the body"（同内容内联=400 / 走 UTF-8 文件=200，只差交付方式）。故请求体一律经
#    `body()` 以 UTF-8 字节落盘 + `--data-binary @file`；CI（Linux）下同样成立。
jget() { # jget <json> <key>  —— 尽量不依赖 jq
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$1" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$2','') if isinstance(d,dict) else '')" 2>/dev/null
  else
    printf '%s' "$1" | tr ',' '\n' | grep -m1 "\"$2\"" | sed 's/.*: *"//; s/"$//'
  fi
}
body() { # body <名> <内容> —— UTF-8 字节落盘，回显文件路径
  printf '%s' "$2" > ".drill-body-$1.json"
  echo ".drill-body-$1.json"
}

REG_BODY="$(body register "{\"agent_id\":\"${AGENT_ID}\",\"agent_name\":\"演练 Agent\",\"role\":\"worker\"}")"
REG="$(curl -fsS --max-time 15 -X POST "${BASE_URL}/api/v1/agents/register" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer ${TOKEN}" \
        --data-binary @"$REG_BODY")" \
  || { bad "注册 agent 失败"; exit 1; }
AGENT_KEY="$(jget "$REG" api_key)"
[ -n "$AGENT_KEY" ] || { bad "注册未返回 api_key（原文：$REG）"; exit 1; }
ok "agent 已建: $AGENT_ID（拿到 api_key）"

MEM_BODY="$(body memory "{\"memory_key\":\"${MEM_KEY}\",\"content\":\"CD-076 持久化演练内容 $(date -u +%FT%TZ)\",\"kind\":\"fact\",\"tags\":[\"cd076\"]}")"
curl -fsS --max-time 20 -X POST "${BASE_URL}/api/v1/memory/store?agent_id=${AGENT_ID}" \
  -H "Authorization: Bearer ${AGENT_KEY}" -H 'Content-Type: application/json' \
  --data-binary @"$MEM_BODY" \
  >/dev/null || { bad "写 memory 失败（注意：这一步同时验证 agent 凭据已落库）"; exit 1; }
ok "memory 已写: $MEM_KEY"

KB_BODY="$(body knowledge "{\"title\":\"${KB_TITLE}\",\"content\":\"CD-076 持久化演练知识正文\",\"tags\":[\"cd076\"]}")"
curl -fsS --max-time 20 -X POST "${BASE_URL}/api/v1/knowledge" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  --data-binary @"$KB_BODY" \
  >/dev/null || { bad "写 knowledge 失败（这一步用卷内 config 的 hub_token，同时验证 config 落卷）"; exit 1; }
ok "knowledge 已写: $KB_TITLE"

curl -fsS --max-time 60 "${BASE_URL}/api/v1/wiki/sync?background=1" -H "Authorization: Bearer ${TOKEN}" >/dev/null \
  && ok "wiki sync 已触发" || echo "  [WARN] wiki sync 返回非 0（不阻断本演练主判据）"
STATS_BEFORE="$(curl -fsS --max-time 15 "${BASE_URL}/api/v1/stats" -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || echo '{}')"
echo "  写后 /api/v1/stats: $(printf '%s' "$STATS_BEFORE" | head -c 400)"

# ---------- 5. 销毁容器（保留卷）----------
step "5. docker compose down（**不带 -v**：卷必须活下来）"
"${COMPOSE[@]}" down --remove-orphans || { bad "compose down 失败"; exit 1; }
if docker ps -a --format '{{.Names}}' | grep -q "^${PROJECT}-hub$"; then
  bad "容器仍存在（down 未生效）"
else
  ok "容器已销毁"
fi
if docker volume ls --format '{{.Name}}' | grep -q "^${PROJECT}_hub-data$"; then
  ok "卷仍在: ${PROJECT}_hub-data"
else
  bad "卷不见了 —— down 把卷删了（绝不该发生）"
fi

# ---------- 6. 再起（模拟换镜像/重建容器）----------
step "6. 重新起容器（读回上面写的数据）"
HUB_PORT="$PORT" HUB_CONTAINER_NAME="${PROJECT}-hub" "${COMPOSE[@]}" up -d "${NO_BUILD[@]}" || { bad "第二次 compose up 失败"; exit 1; }
CID2="$("${COMPOSE[@]}" ps -q sync-hub)"
READY=0
for i in $(seq 1 100); do
  if curl -fsS --max-time 5 "${BASE_URL}/health" >/dev/null 2>&1; then READY=1; break; fi
  sleep 3
done
[ "$READY" = "1" ] || { bad "重建后 /health 未就绪"; docker logs --tail 40 "$CID2" 2>&1 | sed 's/^/    | /'; exit 1; }
ok "重建后 /health 就绪"

# ---------- 7. 读回断言 ----------
step "7. 读回断言（数据必须仍在）"

MEM="$(curl -fsS --max-time 20 "${BASE_URL}/api/v1/memory?agent_id=${AGENT_ID}" -H "Authorization: Bearer ${AGENT_KEY}" 2>/dev/null)"
if printf '%s' "$MEM" | grep -q "$MEM_KEY"; then
  ok "memory 仍在（且 agent 凭据仍可认证 → agents 行也仍在）"
else
  bad "memory 丢失：$MEM"
fi

KB="$(curl -fsS --max-time 20 "${BASE_URL}/api/v1/knowledge" -H "Authorization: Bearer ${TOKEN}" 2>/dev/null)"
if printf '%s' "$KB" | grep -q "CD-076 持久化演练知识条目"; then
  ok "knowledge 仍在（且卷内 config 的 hub_token 仍生效 → config 也在卷里）"
else
  bad "knowledge 丢失：$KB"
fi

PAGES="$(curl -fsS --max-time 20 "${BASE_URL}/api/v1/wiki/pages" -H "Authorization: Bearer ${TOKEN}" 2>/dev/null)"
echo "  wiki/pages: $(printf '%s' "$PAGES" | head -c 300)"

# ---------- 8. 反向断言：数据不许落在容器可写层 ----------
step "8. 反向断言：/app 根下不得有数据（否则重建即丢，本修复失败）"
LEAK="$(docker exec "$CID2" sh -c '
  for f in sync_hub.db chroma_db wiki audit ystore.db config; do
    if [ -e "/app/$f" ]; then echo "LEAK:/app/$f"; fi
  done
  for f in /app/data/sync_hub.db /app/data/chroma_db /app/data/ystore.db; do
    if [ -e "$f" ]; then echo "INVOLUME:$f"; fi
  done' 2>/dev/null)"
if printf '%s' "$LEAK" | grep -q "^LEAK:"; then
  bad "有数据落在容器可写层：$(printf '%s' "$LEAK" | grep '^LEAK:')"
else
  ok "/app 根下无数据落点"
fi
printf '%s' "$LEAK" | grep "^INVOLUME:" | sed 's/^/  [OK]   /' || bad "卷内未找到预期数据文件"

# ---------- 结论 ----------
step "结论"
if [ "$FAILED" = "0" ]; then
  echo "  PASS：容器销毁重建后数据仍在（CD-076 核心判据成立）"
  exit 0
else
  echo "  FAIL：见上面 [FAIL] 行。现场：docker logs ${CID2:0:12}"
  echo "  卷留存（需人工删）：docker volume rm ${PROJECT}_hub-data"
  exit 1
fi
