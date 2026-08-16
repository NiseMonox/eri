#!/usr/bin/env bash
# 端到端冒烟:压缩时间参数,3 分钟内验证服药闭环(推送→重发→漏服)。
# 用法: API_TOKEN=xxx bash scripts/e2e_smoke.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8300}"
T="${API_TOKEN:?请设置 API_TOKEN 环境变量}"

j() { curl -sf -H "X-Token: $T" -H 'Content-Type: application/json' "$@"; }

echo "== 1. health =="
curl -sf "$BASE/api/health"; echo

echo "== 2. 建测试药 =="
MED_ID=$(j -X POST "$BASE/api/meds" -d '{"name":"冒烟测试药"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "med_id=$MED_ID"

echo "== 3. 60 秒后触发一次服药提醒(重发 1 分钟 / 漏服 3 分钟)=="
j -X POST "$BASE/api/dev/oneshot" -d "{\"type\":\"med\",\"in_sec\":60,\"payload\":{\"med_id\":$MED_ID,\"resend_every_min\":1,\"max_resends\":1,\"grace_min\":3}}"; echo
echo "→ 60 秒后手机应收到 Bark;不点确认,约 1 分钟后重发,3 分钟后标漏服"
echo "→ 全程可观察: watch -n5 'curl -s -H \"X-Token: $T\" $BASE/api/meds/logs?limit=3'"

echo "== 4. 手动记体重 + 图表 =="
j -X POST "$BASE/api/weights" -d '{"weight_kg":62.5}'; echo
curl -sf -H "X-Token: $T" "$BASE/api/weights/chart.png?days=30" -o /tmp/chart.png && echo "chart.png OK ($(stat -c%s /tmp/chart.png) bytes)"

echo "== 5. 通知测试 =="
j -X POST "$BASE/api/notify/test" -d '{"profile":"info"}'; echo

echo "完成。服药闭环结果 3 分钟后查:curl -s -H 'X-Token: $T' '$BASE/api/meds/logs?status=missed'"
