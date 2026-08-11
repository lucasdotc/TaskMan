import psycopg
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector
import os
from dotenv import load_dotenv
load_dotenv()

def get_connection():
    conn = psycopg.connect(os.getenv("DATABASE_URL"))
    # Teaches this connection how to convert a Python list <-> Postgres's
    # `vector` type automatically, so callers can just pass/receive plain
    # lists of floats (see save_task's embedding param and
    # find_similar_task below) without manually formatting vector literals.
    register_vector(conn)
    return conn

def save_task(telegram_user_id: int, description: str, due_datetime, recurrence_rule: str = None, category: str = "personal", priority: int = None, embedding: list = None) -> str:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (telegram_user_id, description, due_datetime, recurrence_rule, category, priority, embedding)
                VALUES(%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (telegram_user_id, description, due_datetime, recurrence_rule, category, priority, embedding)
            )
            task_id = cur.fetchone()[0]
            conn.commit()
            return str(task_id)
    finally:
        conn.close()

def save_reminder(task_id: str, scheduled_for) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reminders (task_id, scheduled_for)
                VALUES (%s, %s)
                """,
                (task_id, scheduled_for)
            )
        conn.commit()
    finally:
        conn.close()

def get_due_reminders():
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT r.id as reminder_id, r.task_id, r.scheduled_for, t.description, t.telegram_user_id, t.recurrence_rule
                FROM reminders r
                JOIN tasks t ON t.id = r.task_id
                WHERE r.sent = FALSE
                    AND r.scheduled_for <= NOW() + INTERVAL '30 seconds'
                    AND t.is_complete = FALSE
                """
            )
            return cur.fetchall()
    finally:
        conn.close()

def mark_reminder_sent(reminder_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reminders SET sent = TRUE WHERE id = %s",
                (reminder_id,)
            )
        conn.commit()
    finally:
        conn.close()

def get_overdue_tasks_needing_reminder() -> list[dict]:
    """
    Finds incomplete tasks whose due_datetime has already passed and that
    haven't been "still overdue" nudged recently enough for their priority
    -- see check_overdue_tasks() in reminders.py, which calls this every 60
    seconds. This is a completely separate mechanism from get_due_reminders
    above: that one delivers the reminders you scheduled in advance (each
    fires once), this one is an open-ended nag that keeps repeating for as
    long as a task stays overdue and incomplete.

    A task with last_overdue_reminder_at = NULL (never nudged) always
    qualifies -- that's what makes the very first nudge fire as soon as a
    task is noticed overdue, rather than waiting a full interval first.
    Priority 1 = nudge every hour, 2 = every 6 hours, 3 = every 12 hours,
    anything else (no priority set) = every 24 hours.
    """
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, telegram_user_id, description, due_datetime, priority
                FROM tasks
                WHERE is_complete = FALSE
                    AND due_datetime IS NOT NULL
                    AND due_datetime < NOW()
                    AND (
                        last_overdue_reminder_at IS NULL
                        OR last_overdue_reminder_at < NOW() - (
                            CASE priority
                                WHEN 1 THEN INTERVAL '1 hour'
                                WHEN 2 THEN INTERVAL '6 hours'
                                WHEN 3 THEN INTERVAL '12 hours'
                                ELSE INTERVAL '24 hours'
                            END
                        )
                    )
                """
            )
            return cur.fetchall()
    finally:
        conn.close()

def mark_overdue_reminder_sent(task_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET last_overdue_reminder_at = NOW() WHERE id = %s",
                (task_id,)
            )
        conn.commit()
    finally:
        conn.close()

def get_pending_tasks(telegram_user_id: int, category: str = None):
    """
    Returns pending tasks for a user. If `category` is given, only tasks in
    that category are returned. Otherwise all pending tasks come back
    ordered by category first (so callers can group them for display) then
    by due date.
    """
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            if category:
                cur.execute(
                    """SELECT id, description, due_datetime, category, assigned_by, priority, notes
                    FROM tasks
                    WHERE telegram_user_id = %s
                        AND is_complete = FALSE
                        AND category = %s
                    ORDER BY due_datetime ASC NULLS LAST
                    """,
                    (telegram_user_id, category)
                )
            else:
                cur.execute(
                    """SELECT id, description, due_datetime, category, assigned_by, priority, notes
                    FROM tasks
                    WHERE telegram_user_id = %s
                        AND is_complete = FALSE
                    ORDER BY category ASC, due_datetime ASC NULLS LAST
                    """,
                    (telegram_user_id,)
                )
            return cur.fetchall()
    finally:
        conn.close()

def find_similar_task(telegram_user_id: int, embedding: list, category: str = None) -> dict | None:
    """
    Uses pgvector's <=> operator (cosine distance) to find the single
    pending task whose stored embedding is closest to the given one --
    entirely inside Postgres, no Python-side comparison needed.

    Returns the task plus its `distance` (0.0 = identical meaning, ~1.0 =
    unrelated, up to 2.0 = opposite) so the caller can decide whether it's
    confident enough to act on, or should fall back to fuzzy_match instead
    -- see resolve_task_by_description() in bot.py. Returns None if the
    user has no pending tasks with an embedding at all (e.g. HuggingFace
    was down when they were all created).
    """
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # category_clause is one of two fixed strings, never user
            # input -- same safe-to-interpolate reasoning as elsewhere in
            # this file (e.g. mark_all_tasks_complete above).
            category_clause = "AND category = %s" if category else ""
            params = [embedding, telegram_user_id] + ([category] if category else [])

            # The explicit ::vector cast matters: unlike an INSERT (where
            # Postgres can infer the parameter's type from the target
            # column), a bare %s here would parse as a generic array type
            # and the <=> operator has no overload for vector <=> array --
            # confirmed by actually hitting that error while testing this.
            cur.execute(
                f"""
                SELECT id, description, due_datetime, category, assigned_by, priority, notes,
                       embedding <=> %s::vector AS distance
                FROM tasks
                WHERE telegram_user_id = %s
                    AND is_complete = FALSE
                    AND embedding IS NOT NULL
                    {category_clause}
                ORDER BY distance
                LIMIT 1
                """,
                params
            )
            return cur.fetchone()
    finally:
        conn.close()

def set_task_notes(task_id: str, notes: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET notes = %s WHERE id = %s",
                (notes, task_id)
            )
        conn.commit()
    finally:
        conn.close()

def set_task_priority(task_id: str, priority: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET priority = %s WHERE id = %s",
                (priority, task_id)
            )
        conn.commit()
    finally:
        conn.close()

def mark_task_complete(task_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET is_complete = TRUE WHERE id = %s",
                (task_id,)
            )
        conn.commit()
    finally:
        conn.close()

def mark_all_tasks_complete(telegram_user_id: int, category: str = None) -> int:
    """
    Marks every pending task for a user as complete -- optionally scoped to
    one category -- and silences their not-yet-sent reminders, so a task
    that was just bulk-completed doesn't still get reminded about later.

    This backs both /clear ("remove all") and "mark all as done": they're
    the same underlying operation (tasks are soft-completed via is_complete,
    never hard-deleted, same as single-task completion elsewhere in this
    file), just presented with different wording at the call site. Returns
    how many tasks were affected.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # category_clause is always one of these two fixed strings, never
            # user input, so building it into the query text is safe -- the
            # actual category value is still passed as a %s parameter.
            category_clause = "AND category = %s" if category else ""
            params = (telegram_user_id, category) if category else (telegram_user_id,)

            cur.execute(
                f"""UPDATE reminders SET sent = TRUE
                WHERE task_id IN (
                    SELECT id FROM tasks
                    WHERE telegram_user_id = %s AND is_complete = FALSE {category_clause}
                )""",
                params
            )
            cur.execute(
                f"UPDATE tasks SET is_complete = TRUE WHERE telegram_user_id = %s AND is_complete = FALSE {category_clause}",
                params
            )
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()

def transfer_task(task_id: str, new_owner_id: int, assigned_by: int) -> dict | None:
    """
    Reassigns a task to a different Telegram user. Reminders aren't touched --
    they're linked to the task by task_id and get_due_reminders() joins back
    to tasks.telegram_user_id, so an already-scheduled reminder will simply
    fire to the new owner once ownership changes.

    Returns the updated task row, or None if task_id didn't match anything.
    """
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE tasks
                SET telegram_user_id = %s, assigned_by = %s
                WHERE id = %s
                RETURNING id, description, due_datetime, category
                """,
                (new_owner_id, assigned_by, task_id)
            )
            updated = cur.fetchone()
        conn.commit()
        return updated
    finally:
        conn.close()

def ensure_user(telegram_user_id: int, display_name: str = None) -> None:
    """
    Makes sure a row exists in `users` for this Telegram user, so admins can
    look them up (and so /transfer has something to notify). Uses
    ON CONFLICT DO NOTHING so it never overwrites a display_name the user
    already set with /register -- this is meant to run on every incoming
    message, not just registration.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_user_id, display_name)
                VALUES (%s, %s)
                ON CONFLICT (telegram_user_id) DO NOTHING
                """,
                (telegram_user_id, display_name)
            )
        conn.commit()
    finally:
        conn.close()

def set_display_name(telegram_user_id: int, display_name: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_user_id, display_name)
                VALUES (%s, %s)
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET display_name = EXCLUDED.display_name
                """,
                (telegram_user_id, display_name)
            )
        conn.commit()
    finally:
        conn.close()

def get_user(telegram_user_id: int) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT telegram_user_id, display_name, is_admin FROM users WHERE telegram_user_id = %s",
                (telegram_user_id,)
            )
            return cur.fetchone()
    finally:
        conn.close()

def get_all_users() -> list[dict]:
    """Every registered user (anyone who's ever messaged the bot, plus every
    ADMIN_USER_ID -- see sync_admins_from_env). Used by /users so admins can
    see who's available to /transfer a task to."""
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT telegram_user_id, display_name, is_admin
                FROM users
                ORDER BY display_name ASC NULLS LAST, telegram_user_id ASC
                """
            )
            return cur.fetchall()
    finally:
        conn.close()

def find_user_by_name(display_name: str) -> dict | None:
    """Case-insensitive lookup so admins can type '/transfer <task> sarah'."""
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT telegram_user_id, display_name, is_admin FROM users WHERE display_name ILIKE %s LIMIT 1",
                (display_name,)
            )
            return cur.fetchone()
    finally:
        conn.close()

def sync_admins_from_env(admin_ids: list[int]) -> None:
    """
    Makes the `users.is_admin` column match ADMIN_USER_ID from .env. Run once
    at bot startup. This is one-directional (env -> DB): there's no bot
    command that grants admin, so a compromised chat session can't promote
    itself -- the only way to become an admin is to be listed in .env on the
    machine running the bot.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for uid in admin_ids:
                cur.execute(
                    """
                    INSERT INTO users (telegram_user_id, is_admin)
                    VALUES (%s, TRUE)
                    ON CONFLICT (telegram_user_id) DO UPDATE SET is_admin = TRUE
                    """,
                    (uid,)
                )
            # Demote anyone who used to be an admin but was removed from .env.
            if admin_ids:
                cur.execute(
                    "UPDATE users SET is_admin = FALSE WHERE is_admin = TRUE AND telegram_user_id != ALL(%s)",
                    (admin_ids,)
                )
            else:
                cur.execute("UPDATE users SET is_admin = FALSE WHERE is_admin = TRUE")
        conn.commit()
    finally:
        conn.close()
