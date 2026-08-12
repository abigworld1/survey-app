#!/usr/bin/env bash
# 日次: 最新mainへ同期 → パイプライン実行 → commit & push。user cron から呼ぶ。
set -euo pipefail

script_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")/.."
# 秘密情報・環境変数は .env（gitignore 済み）から読む（例: S2_API_KEY=xxxx）
[ -f .env ] && { set -a; . ./.env; set +a; }
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}"
export LLM_API_KEY="${LLM_API_KEY:-dummy}"
export GIT_TERMINAL_PROMPT=0
attempts="${PIPELINE_ATTEMPTS:-3}"
retry_delay="${PIPELINE_RETRY_DELAY_SECONDS:-900}"
git_attempts="${GIT_ATTEMPTS:-3}"
git_retry_delay="${GIT_RETRY_DELAY_SECONDS:-60}"

# cronと手動実行が重なって同じseen/indexを同時更新しないようにする。
lock_file="${PIPELINE_LOCK_FILE:-/tmp/survey-app-daily.lock}"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "$(date +%FT%T) another daily run is already active"
  exit 0
fi

git_retry() {
  local label="$1"
  shift
  local attempt
  for ((attempt=1; attempt<=git_attempts; attempt++)); do
    echo "$(date +%FT%T) ${label} ${attempt}/${git_attempts}"
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -lt "$git_attempts" ]; then
      echo "$(date +%FT%T) ${label} failed; retrying in ${git_retry_delay}s"
      sleep "$git_retry_delay"
    fi
  done
  echo "$(date +%FT%T) ${label} failed after ${git_attempts} attempts"
  return 1
}

# 前日の手動pushや、前回pushに失敗して残ったローカルcommitを統合する。
before_sync="$(git rev-parse HEAD)"
git_retry "git sync before pipeline" git pull --rebase --autostash origin main
after_sync="$(git rev-parse HEAD)"
if [ "$before_sync" != "$after_sync" ] && [ "${SURVEY_DAILY_REEXEC:-0}" != "1" ]; then
  echo "$(date +%FT%T) repository updated; restarting the latest daily script"
  export SURVEY_DAILY_REEXEC=1
  exec /bin/bash "$script_path"
fi

status=1
for ((attempt=1; attempt<=attempts; attempt++)); do
  echo "$(date +%FT%T) pipeline attempt ${attempt}/${attempts}"
  set +e
  .venv/bin/python -m pipeline.run
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    break
  fi
  if [ "$attempt" -lt "$attempts" ]; then
    echo "$(date +%FT%T) daily quota shortfall; retrying in ${retry_delay}s"
    sleep "$retry_delay"
  fi
done
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -q -m "daily: $(date +%F)"
else
  echo "$(date +%FT%T) no changes"
fi

# 実行中にmainが更新された場合もrebaseしてからpushする。前回の未push
# commitだけが残っている場合も、変更ファイルの有無にかかわらず公開する。
published=0
for ((attempt=1; attempt<=git_attempts; attempt++)); do
  echo "$(date +%FT%T) git publish ${attempt}/${git_attempts}"
  if git pull --rebase --autostash origin main && git push -q origin main; then
    published=1
    echo "$(date +%FT%T) pushed"
    break
  fi
  if [ "$attempt" -lt "$git_attempts" ]; then
    echo "$(date +%FT%T) git publish failed; retrying in ${git_retry_delay}s"
    sleep "$git_retry_delay"
  fi
done
if [ "$published" -ne 1 ]; then
  echo "$(date +%FT%T) git publish failed after ${git_attempts} attempts"
  exit 1
fi
exit "$status"
