-- TaskMan database schema.
--
-- This file didn't exist before -- the original tables were created by hand
-- on the old EC2 box's Postgres instance. It's written to be safe to run
-- against BOTH a brand new database and your existing one: every statement
-- uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so re-running it never
-- errors out or destroys data.
--
-- Usage:
--   psql "$DATABASE_URL" -f schema.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- provides gen_random_uuid()

-- Needed for the `vector` column type and the <=> similarity operator used
-- by find_similar_task() in db.py. The stock postgres:16 image doesn't ship
-- this extension -- docker-compose.yml uses pgvector/pgvector:pg16 instead,
-- which is the same Postgres with this extension precompiled in. Running a
-- host Postgres instead of Docker? Install the `postgresql-16-pgvector`
-- package (or equivalent for your version/distro) first.
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- tasks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id BIGINT NOT NULL,
    description      TEXT NOT NULL,
    due_datetime     TIMESTAMPTZ,
    recurrence_rule  TEXT,
    is_complete      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Category: personal / work / school / or anything else the user types.
-- Defaults to 'personal' so existing rows (and any row that doesn't mention
-- a category) get a sensible bucket instead of NULL.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'personal';

-- Who handed this task off, if anyone. NULL means the owner created it
-- themselves. Set when an admin transfers a task to another user -- see
-- transfer_task() in db.py.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assigned_by BIGINT;

-- A free-text note beyond the short `description`, set/viewed via /note.
-- NULL until someone sets one.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notes TEXT;

-- 1 = most urgent (hourly overdue nudges), 3 = least (12h), NULL = no
-- priority set (24h, the default cadence) -- see get_overdue_tasks_needing_
-- reminder() in db.py for where these numbers turn into actual intervals.
-- Only ever set from an explicit statement ("priority 1", "urgent"), never
-- guessed from vague wording -- see the parse_task prompt in claude.py.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority SMALLINT
    CHECK (priority IS NULL OR priority IN (1, 2, 3));

-- When this task last got an "still overdue" nudge (see reminders.py:
-- check_overdue_tasks). NULL means it hasn't been nudged yet -- which is
-- also what triggers the very first nudge as soon as a task is noticed
-- overdue, before any interval has had a chance to elapse.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_overdue_reminder_at TIMESTAMPTZ;

-- Sentence embedding of `description`, used by find_similar_task() to
-- match "the dentist one" to a task without an LLM call. 384 dimensions
-- because that's what sentence-transformers/all-MiniLM-L6-v2 (see
-- embeddings.py) outputs. NULL for tasks created before this feature
-- existed, or if the embedding call failed at creation time -- either way,
-- find_similar_task() simply skips rows with no embedding rather than
-- erroring, and the bot falls back to asking Claude instead (fuzzy_match).
--
-- No ANN index (ivfflat/hnsw) on this column on purpose: those trade exact
-- results for speed on large tables, and a personal bot's pending-task
-- count is nowhere near where a sequential scan would be slow enough to
-- matter. Worth adding only if this table ever grows to many thousands of
-- rows.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS embedding vector(384);

-- A permanent task_number column briefly lived here. Reverted: task
-- numbers are shown and addressed as a task's current position in the
-- pending list instead (see db.py/bot.py/agent.py) -- they shift as tasks
-- are completed/added/removed, same as a normal numbered to-do list. This
-- drop is a no-op on any database that never had the column (DROP COLUMN
-- IF EXISTS also takes its owned sequence and index along with it).
ALTER TABLE tasks DROP COLUMN IF EXISTS task_number;
DROP SEQUENCE IF EXISTS tasks_task_number_seq;

CREATE INDEX IF NOT EXISTS idx_tasks_user_pending
    ON tasks (telegram_user_id, is_complete);

-- ---------------------------------------------------------------------------
-- reminders
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id        UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    scheduled_for  TIMESTAMPTZ NOT NULL,
    sent           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders (scheduled_for) WHERE sent = FALSE;

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
-- New table backing task transfers. A row is created the first time someone
-- talks to the bot (see db.py: ensure_user) or via /register. is_admin
-- controls who is allowed to run /transfer -- it's seeded from
-- ADMIN_USER_IDS in .env on bot startup (see bot.py: sync_admins_from_env),
-- not editable through the bot itself, so a non-admin can't grant themselves
-- admin rights via a message.
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id BIGINT PRIMARY KEY,
    display_name     TEXT,
    is_admin         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
