#!/usr/bin/env bash
# 部署 air 到本機。冪等,可重複執行。
#
# 驗證不是「compose 有沒有噴錯」而是「新的容器有沒有真的在服務」:
# 容器 id 必須換新,而且 /api/health 必須回報參考資料是有列數的 —— 一個
# 200 但 row_count 為 0 的服務,看起來跟健康的一模一樣。
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-/usr/local/bin/docker-compose}
URL=${URL:-http://127.0.0.1:3007}

if [[ ! -f .env ]]; then
  echo "缺少 .env。先執行:cp .env.example .env,再填入 token(可留空)。" >&2
  exit 1
fi

before=$($COMPOSE ps -q air 2>/dev/null || true)

echo "==> build"
$COMPOSE build

echo "==> up"
$COMPOSE up -d

after=$($COMPOSE ps -q air)
if [[ -n "$before" && "$before" == "$after" ]]; then
  echo "容器 id 沒有換新($after) —— 這次部署沒有生效。" >&2
  exit 1
fi

echo "==> 等待服務就緒"
for _ in $(seq 1 45); do
  if curl -fsS "$URL/api/health" >/dev/null 2>&1; then break; fi
  sleep 2
done

health=$(curl -fsS "$URL/api/health")
airports=$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["row_counts"]["airports"])')

if [[ "$airports" -lt 1000 ]]; then
  echo "健康檢查回 200,但機場資料只有 $airports 筆 —— 參考資料沒有載進來。" >&2
  printf '%s\n' "$health" >&2
  exit 1
fi

echo "==> 部署完成:容器 ${after:0:12},機場資料 $airports 筆"
printf '%s\n' "$health" | python3 -m json.tool
