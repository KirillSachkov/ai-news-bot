#!/usr/bin/env bash
# Start the bot in the foreground (dev mode).
# For permanent operation use launchd (macOS) or systemd/cron — see README.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

exec python3 bot.py run "$@"
