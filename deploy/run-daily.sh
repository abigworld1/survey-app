#!/usr/bin/env bash
# GitHub Actions entry point. Persistence and Pages deployment are workflow jobs.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m pipeline.run "$@"
