import psycopg
from psycopg.rows import dict_row
import os
from dotenv import load_dotenv
load_dotenv()

def get_connection():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def save_task(telegram_user_id: int, description: str, due_datetime, recurrence_rule: str = None, category: str = "personal") -> str:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (telegram_user_id, description, due_datetime, recurrence_rule, category)
                VALUES(%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (telegram_user_id, description, due_datetime, recurrence_rule, category)
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
                    """SELECT id, description, due_datetime, category, assigned_by
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
                    """SELECT id, description, due_datetime, category, assigned_by
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
