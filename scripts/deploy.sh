#!/usr/bin/env bash
# 部署 air 到本機。冪等,可重複執行。
#
# 驗證不是「compose 有沒有噴錯」而是「服務跑的是不是這份程式碼」:
#   * 映像變了 → 容器必須換新,沒換就是這次部署沒生效
#   * 映像沒變 → 本來就沒東西要部署,那是成功不是失敗
# 這兩件事分開報,否則「已經是最新」會長得跟「部署失敗」一模一樣,
# 而一個常常誤報紅燈的檢查,很快就會變成沒有人看的檢查。
#
# 最後一關是 /api/health 的列數:一個 200 但參考資料為 0 的服務,
# 看起來跟健康的完全一樣。
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-/usr/local/bin/docker-compose}
URL=${URL:-http://127.0.0.1:3007}
SERVICE=air

image_id() { docker images "$(basename "$PWD")-${SERVICE}" --format '{{.ID}}' | head -1; }

if [[ ! -f .env ]]; then
  echo "缺少 .env。先執行:cp .env.example .env,再填入 token(可留空)。" >&2
  exit 1
fi

container_before=$($COMPOSE ps -q $SERVICE 2>/dev/null || true)
image_before=$(image_id)

echo "==> build"
$COMPOSE build

image_after=$(image_id)

echo "==> up"
$COMPOSE up -d

container_after=$($COMPOSE ps -q $SERVICE)

if [[ "$image_before" == "$image_after" && "$container_before" == "$container_after" ]]; then
  echo "==> 映像與容器都沒變:已經是最新,不需要重新部署"
else
  if [[ -n "$container_before" && "$container_before" == "$container_after" ]]; then
    echo "映像換了($image_before → $image_after)但容器 id 沒換 —— 這次部署沒有生效。" >&2
    exit 1
  fi
  echo "==> 容器已更新:${container_after:0:12}"
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

# 服務跑的是不是這份原始碼,用檔案指紋比,不是用「build 有沒有成功」推論。
mismatched=0
for f in backend/app/main.py backend/app/reverse.py frontend/app.js frontend/index.html; do
  host=$(sha256sum "$f" | cut -d' ' -f1)
  # Dockerfile 把整個 backend/ 與 frontend/ 原樣複製到 /app/ 底下,
  # 所以容器內的路徑就是 repo 的相對路徑加上 /app 前綴。
  inside=$(docker exec "$container_after" sha256sum "/app/$f" 2>/dev/null | cut -d' ' -f1 || true)
  if [[ "$host" != "$inside" ]]; then
    echo "容器裡的 $f 跟工作區不一致。" >&2
    mismatched=1
  fi
done
[[ "$mismatched" -eq 0 ]] || exit 1

echo "==> 部署完成:容器 ${container_after:0:12},機場資料 $airports 筆,程式碼與工作區一致"
