import os
import anthropic
import requests
from celery_app import app
from db import get_due_reminders, mark_reminder_sent
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """
You are a bot that reminds a user about his tasks. You will receive a task description of the task that the user wants to complete, followed by the due date of said task.
Your job is to make a message that reminds the user of the task- what they have to do along with the due date or how many days/hours are left.
Be as creative as you want in structuring the reminder message, so long as the essence of the task description and due date are not lost.
"""

@app.task(name="tasks.check_and_send_reminders")
def check_and_send_reminders():
    due_reminders = get_due_reminders()

    for reminder in due_reminders:
        mark_reminder_sent(str(reminder["reminder_id"]))

        send_telegram_message.delay(
            chat_id=reminder["telegram_user_id"],
            task_description=reminder["description"],
            scheduled_for=str(reminder["scheduled_for"])
        )

@app.task(name="tasks.send_telegram_message", bind=True, max_retries=3)
def send_telegram_message(self, chat_id: int, task_description: str, scheduled_for: str):
    message_text = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": task_description + scheduled_for}
        ]
    )
    try:
        response = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message_text,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
        response.raise_for_status()
    
    except requests.RequestException as exc:
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
    