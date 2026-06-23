import psycopg2
import psycopg2.extras
import os
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def save_task(telegram_user_id: int, description: str, due_datetime)-> str:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (telegram_user_id, description, due_datetime)
                VALUES(%s, %s, %s)
                RETURNING id
                """,
                (telegram_user_id, description, due_datetime)
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT r.id as reminder_id, r.task_id, r.scheduled_for, t.description, t.telegram_user_id
                FROM reminders r
                JOIN tasks t ON t.id = r.task_id
                WHERE r.sent = FALSE
                    AND r.scheduled_for <= NOW() + INTERVAL '30 seconds'
                    and t.is_complete = FALSE
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

def get_pending_tasks(telegram_user_id: int):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id , description, due_datetime
                FROM tasks
                WHERE telegram_user_id = %s
                    AND is_complete = FALSE
                ORDER BY due_datetime ASC NULLS LAST
                """,
                (telegram_user_id)
            )
            return cur.fetchall()
    finally:
        conn.close()

def mark_task_complete(task_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET sent = TRUE WHERE task_id = %s",
                (task_id)
            )
        conn.commit()
    finally:
        conn.close()