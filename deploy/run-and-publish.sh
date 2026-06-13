#!/usr/bin/env bash
# 日次実行 → 生成物を GitHub Pages リポジトリへ push する。
# systemd timer（survey.timer）から呼ばれる想定。
#
# 必要な環境変数:
#   REPO_DIR    survey-app リポジトリの絶対パス（例: /home/hirayama/survey-app）
#   COMPOSE     既存の docker compose プロジェクトのディレクトリ（vLLM/Open WebUI がある場所）
#   GIT_REMOTE  push 先（例: https://<token>@github.com/abigworld1/survey-app.git）
#
# 認証: 最小スコープ(対象リポのみ・Contents:write)の fine-grained PAT を使うこと。
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/hirayama/survey-app}"
COMPOSE="${COMPOSE:-$REPO_DIR}"

cd "$COMPOSE"
# vLLM と同じネットワークでパイプラインを1回だけ実行（生成物は REPO_DIR に出る）
docker compose run --rm survey-app python -m pipeline.run

cd "$REPO_DIR"
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  git -c user.name="survey-app bot" -c user.email="hirayama.h77@gmail.com" \
      commit -m "daily: $(date +%F)"
  git push "${GIT_REMOTE:-origin}" HEAD:main
  echo "pushed."
else
  echo "no changes."
fi
