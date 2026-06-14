#!/usr/bin/env bash
# 日次: パイプライン実行 → 変更があれば commit & push。user cron から呼ぶ。
set -euo pipefail
cd "$(dirname "$0")/.."
# 秘密情報・環境変数は .env（gitignore 済み）から読む（例: S2_API_KEY=xxxx）
[ -f .env ] && { set -a; . ./.env; set +a; }
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
