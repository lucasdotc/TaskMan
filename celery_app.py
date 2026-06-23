import os
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv()

app = Celery(
    "reminder_bot",
    broker=os.getenv("REDIS_URL"),
    backend=os.getenv("REDIS_URL"),
    include=["tasks"]
)

app.config_from_object({
    "redbeat_redis_url": os.getenv("REDIS_URL")
})

app.conf.beat_scheduler = "redbeat.RedBeatScheduler"
app.conf.beat_schedule = {
    "check-reminders-every-minute": {
        "task": "tasks.check_and_send_reminders",
        "schedule": crontab()
    }
}

app.conf.timezone = "UTC"
app.conf.enable_utc = True