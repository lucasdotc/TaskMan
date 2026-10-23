import psycopg
from psycopg.rows import dict_row
import os
from dotenv import load_dotenv
load_dotenv()

def get_connection():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def save_task(telegram_user_id: int, description: str, due_datetime, recurrence_rule: str = None, category: str = "personal", priority: int = None) -> str:
    """Inserts a new task and returns its id."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (telegram_user_id, description, due_datetime, recurrence_rule, category, priority)
                VALUES(%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (telegram_user_id, description, due_datetime, recurrence_rule, category, priority)
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

def delete_unsent_reminders(task_id: str) -> None:
    """Drops any not-yet-fired reminders for a task -- used when a task's
    due date is edited, so stale reminders from the old due date don't
    still fire alongside the newly-scheduled ones."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reminders WHERE task_id = %s AND sent = FALSE",
                (task_id,)
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

def get_pending_tasks(telegram_user_id: int, category: str = None, sort: str = "due") -> list[dict]:
    """
    Returns pending tasks for a user, optionally scoped to one category.

    sort="due" (the default) orders by category then due date, so callers
    with no explicit category can group the results for display. sort=
    "priority" orders by priority instead (1 = most urgent first, unset
    tasks last), then due date, ignoring category grouping -- used by
    "/list priority" and whenever the AI agent is asked to sort by urgency.

    There's no stored "task number" column: a task's number is just its
    1-based position in this list, resolved fresh every time from whatever
    is currently pending (see bot.py's _task_at_position and agent.py) --
    so it naturally shifts as tasks are completed, added, or removed, the
    same way a normal numbered to-do list would.
    """
    order_clause = (
        "priority ASC NULLS LAST, due_datetime ASC NULLS LAST" if sort == "priority"
        else "category ASC, due_datetime ASC NULLS LAST"
    )

    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            query = f"""
                SELECT id, description, due_datetime, recurrence_rule,
                       category, assigned_by, priority, notes
                FROM tasks
                WHERE telegram_user_id = %s
                    AND is_complete = FALSE
                    {{category_clause}}
                ORDER BY {order_clause}
            """
            if category:
                cur.execute(query.format(category_clause="AND category = %s"), (telegram_user_id, category))
            else:
                cur.execute(query.format(category_clause=""), (telegram_user_id,))
            return cur.fetchall()
    finally:
        conn.close()

def get_task_by_id(task_id: str, telegram_user_id: int = None) -> dict | None:
    """Looks up one task by its internal UUID, optionally restricted to a
    given owner (pass telegram_user_id whenever the caller shouldn't be
    able to act on someone else's task just by guessing/leaking a UUID)."""
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            if telegram_user_id is not None:
                cur.execute(
                    """SELECT id, description, due_datetime, recurrence_rule,
                              category, assigned_by, priority, notes, telegram_user_id, is_complete
                       FROM tasks WHERE id = %s AND telegram_user_id = %s""",
                    (task_id, telegram_user_id)
                )
            else:
                cur.execute(
                    """SELECT id, description, due_datetime, recurrence_rule,
                              category, assigned_by, priority, notes, telegram_user_id, is_complete
                       FROM tasks WHERE id = %s""",
                    (task_id,)
                )
            return cur.fetchone()
    finally:
        conn.close()

_UPDATABLE_TASK_FIELDS = {"description", "due_datetime", "recurrence_rule", "category", "priority", "notes"}

def update_task(task_id: str, **fields) -> dict | None:
    """
    Updates only the columns passed in `fields`. A column left out of
    `fields` entirely is untouched; a column passed with value None clears
    it (e.g. update_task(id, due_datetime=None) removes a task's due date).
    Only names in _UPDATABLE_TASK_FIELDS are ever accepted, so this can't be
    used to overwrite telegram_user_id, is_complete, etc. Returns the
    updated row, or the current row unchanged if `fields` had nothing
    applicable.
    """
    columns = [k for k in fields if k in _UPDATABLE_TASK_FIELDS]
    if not columns:
        return get_task_by_id(task_id)

    # set_clause is built only from the fixed whitelist above, never from
    # caller-supplied strings, so interpolating the column names here is
    # safe -- the actual values are still passed as %s parameters.
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    values = [fields[col] for col in columns]

    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""UPDATE tasks SET {set_clause} WHERE id = %s
                    RETURNING id, description, due_datetime, recurrence_rule,
                              category, priority, notes""",
                values + [task_id]
            )
            updated = cur.fetchone()
        conn.commit()
        return updated
    finally:
        conn.close()

def delete_task(task_id: str) -> dict | None:
    """
    Permanently removes a task, and -- via the reminders.task_id foreign
    key's ON DELETE CASCADE -- any of its reminders along with it. Unlike
    mark_task_complete/mark_all_tasks_complete, which only ever soft-
    complete a task, this is the one place a task row is actually erased.
    Returns the deleted row's id/description, or None if task_id didn't
    match anything.
    """
    conn = get_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "DELETE FROM tasks WHERE id = %s RETURNING id, description",
                (task_id,)
            )
            deleted = cur.fetchone()
        conn.commit()
        return deleted
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
