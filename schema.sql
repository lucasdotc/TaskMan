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
