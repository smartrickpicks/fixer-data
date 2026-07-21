#!/usr/bin/env bash
# news_cycle.sh — one Pi cron tick of the news loop.
#
#   RSS -> mo gate -> tags (news_pipeline.py)  ->  publish news_data.json to
#   fixer-data (git)  ->  ping hivesync /api/news/consume (routes to Discord).
#
# The mo gate is deterministic and free, so this makes NO LLM calls (no --extract).
# Run it from cron every ~15 min:
#   */15 * * * * /path/to/news_cycle.sh >> ~/news-cycle.log 2>&1
#
# ── set these three for your Pi, then chmod +x this file ─────────────────────
THOUGHTCELLS_DIR="/opt/otto-beam/otto-rooms/deploy/pi/thought-cells"   # where news_pipeline.py lives
FIXER_DATA_DIR="/opt/ottonomy/mlb/fixer-data"                          # the fixer-data git checkout the Pi pushes picks from
HIVESYNC_URL="https://hivesync-rw1o.onrender.com"
CRON_SECRET="hs_cron_4Fn8pQ2vX9rL6mK3wT7bY5cJ1dG0sZa"
# ─────────────────────────────────────────────────────────────────────────────

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "[$(ts)] $*"; }

# fail loud if a path is wrong — silent misconfig is worse than a red line
[ -f "$THOUGHTCELLS_DIR/news_pipeline.py" ] || { say "ERR: news_pipeline.py not in $THOUGHTCELLS_DIR"; exit 1; }
[ -d "$FIXER_DATA_DIR/.git" ]               || { say "ERR: $FIXER_DATA_DIR is not a git checkout"; exit 1; }

OUT="$FIXER_DATA_DIR/news_data.json"

# 1) collect -> gate -> tag -> write the feed
say "pipeline -> $OUT"
if ! python3 "$THOUGHTCELLS_DIR/news_pipeline.py" --out "$OUT"; then
  say "ERR: pipeline failed"; exit 1
fi

# 2) publish only if the feed actually changed (no empty commits)
cd "$FIXER_DATA_DIR" || { say "ERR: cd $FIXER_DATA_DIR"; exit 1; }
git add news_data.json
if git diff --cached --quiet; then
  say "feed unchanged — nothing to push"
else
  git commit -m "news feed $(date -u +%Y-%m-%dT%H:%MZ)" >/dev/null
  # rebase onto any picks-pipeline commits first so the two jobs never race
  git pull --rebase -q 2>/dev/null || true
  git push -q && say "pushed news_data.json" || say "ERR: git push failed"
fi

# 3) ping hivesync to route HOT/WARM to Discord (dedup makes this safe to repeat)
say "consume ->"
curl -s -X POST -H "Authorization: Bearer $CRON_SECRET" "$HIVESYNC_URL/api/news/consume"
echo   # newline after the JSON
