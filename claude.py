import anthropic
import json
import os
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are a task parser. The user sends you a natural language reminder request.
You extract the task information and return ONLY valid JSON — no explanation, no markdown, no backticks.

Return this exact schema:
{{
  "description": "short description of the task",
  "due_datetime": "ISO 8601 datetime string in UTC, or null if no time mentioned",
  "reminder_offsets_minutes": [0]
}}

reminder_offsets_minutes controls when to send reminders relative to due_datetime:
- 0 means at the exact due time
- 60 means 1 hour before
- 1440 means 1 day before
- Always include 0 (the actual due time reminder)
- If the task has a due date, also include 60 (1 hour before) and 1440 (1 day before)
- If no due date, just use [0] and set due_datetime to null

Examples:
User: "remind me to call the dentist tomorrow at 2pm"
Response: {{"description": "call the dentist", "due_datetime": "2025-01-16T14:00:00Z", "reminder_offsets_minutes": [0, 60, 1440]}}

User: "don't forget to buy groceries"
Response: {{"description": "buy groceries", "due_datetime": null, "reminder_offsets_minutes": [0]}}

Current UTC time: {current_utc}
User's local timezone: {timezone}
"""

def parse_task(user_message: str, timezone: str = "America/Edmonton")->dict:
    current_utc = datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    prompt = SYSTEM_PROMPT.format(
        current_utc=current_utc,
        timezone=timezone
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=prompt,
        messages=[
            {"role": "user", "content": user_message}
        ]
    )

    raw = message.content[0].text.strip()

    parsed = json.loads(raw)

    if parsed.get("due_datetime"):
        parsed["due_datetime"] = datetime.fromisoformat(
            parsed["due_datetime"].replace("Z", "+00:00")
        )
    return parsed