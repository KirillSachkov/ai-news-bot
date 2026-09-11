#!/usr/bin/env bash
# One collect+deliver cycle, then exit. For cron; no buttons (they need a live process).
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec python3 bot.py once "$@"
