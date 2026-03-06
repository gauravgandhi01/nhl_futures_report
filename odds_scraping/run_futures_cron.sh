#!/bin/zsh

set -euo pipefail

ROOT_DIR="/Users/ggandhi001/nhl_tools/nhl_futures_report"
SCRAPE_DIR="$ROOT_DIR/odds_scraping"
LOG_FILE="$SCRAPE_DIR/cron_log.txt"

PYTHON_BIN="/usr/local/bin/python3"
GIT_BIN="/usr/bin/git"
DATE_BIN="/bin/date"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "${PYTHON_BIN:-}" ]]; then
  echo "python3 not found" >> "$LOG_FILE"
  exit 1
fi

{
  echo "=== $($DATE_BIN -Iseconds) START futures ==="

  "$PYTHON_BIN" "$SCRAPE_DIR/scrape_odds.py"
  "$PYTHON_BIN" "$SCRAPE_DIR/generate_report.py" --no-browser

  cd "$ROOT_DIR"
  "$GIT_BIN" add -A

  if "$GIT_BIN" diff --cached --quiet; then
    echo "No changes to commit"
  else
    "$GIT_BIN" commit -m "Auto-update: futures report $($DATE_BIN -Iminutes)"
    "$GIT_BIN" push
  fi

  echo "=== $($DATE_BIN -Iseconds) END futures rc=0 ==="
} >> "$LOG_FILE" 2>&1
