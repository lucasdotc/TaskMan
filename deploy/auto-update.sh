#!/usr/bin/env bash
#
# Pulls the latest code, redeploys via Docker Compose, and re-applies
# schema.sql. Meant to run on a schedule (see the cron line in README.md's
# Deployment section) on whatever host is actually running the bot -- not
# on a dev machine.
#
# Safe to run unconditionally even when nothing changed:
#   - `docker compose up -d --build` is a no-op when nothing changed --
#     Docker's layer cache makes an unchanged rebuild fast, and Compose
#     only recreates a container when its image or config actually
#     differs.
#   - schema.sql is written entirely with IF NOT EXISTS / ADD COLUMN IF
#     NOT EXISTS, so re-running it against an already-up-to-date database
#     does nothing either.
# That's what makes "always pull, always redeploy, always re-apply schema"
# safe to put on a timer instead of needing to detect what changed.
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --quiet
docker compose up -d --build

# schema.sql only auto-applies via docker-entrypoint-initdb.d the first
# time Postgres's data volume is created (see docker-compose.yml) --
# re-running it here on every deploy is what makes future schema changes
# (new columns, new tables) roll out automatically too, not just code
# changes.
#
# Values are pulled straight out of .env with grep/cut rather than
# `source .env` -- DATABASE_URL in that file contains a literal "$@",
# which bash would try to expand as the positional-parameters variable if
# the file were sourced directly. Extracting just the two lines we need as
# plain text sidesteps that entirely.
POSTGRES_USER=$(grep -E '^POSTGRES_USER=' .env | cut -d '=' -f2-)
POSTGRES_DB=$(grep -E '^POSTGRES_DB=' .env | cut -d '=' -f2-)

# A few retries in case this runs right after docker-compose.yml itself
# changed and Postgres is still coming up.
for attempt in 1 2 3 4 5; do
    if docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < schema.sql; then
        break
    fi
    sleep 3
done
