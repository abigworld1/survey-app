#!/usr/bin/env bash
# 日次: パイプライン実行 → 変更があれば commit & push。user cron から呼ぶ。
set -euo pipefail
cd "$(dirname "$0")/.."
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}"
export LLM_API_KEY="${LLM_API_KEY:-dummy}"
.venv/bin/python -m pipeline.run
if [ -n "$(git status --porcelain)" ]; then
git add -A
git commit -q -m "daily: $(date +%F)"
git push -q origin main
echo "$(date +%FT%T) pushed"
else
echo "$(date +%FT%T) no changes"
fi
