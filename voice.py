"""
Speech-to-text for Telegram voice messages.

Telegram voice notes arrive as OGG/Opus audio, and Claude's Messages API
doesn't accept audio input -- it's text (and image) only. So this module
sends the raw audio bytes to OpenAI's Whisper transcription endpoint and
hands back plain text. That text is then fed into the exact same
parse_intent()/parse_task() pipeline in claude.py that a typed message goes
through, so voice messages get identical handling to text ones once
transcribed.

Requires OPENAI_API_KEY in .env (separate from ANTHROPIC_API_KEY -- this is
the one thing in the bot that isn't Claude, because Claude doesn't do audio).
"""
import io
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """
    Transcribes raw audio bytes to text using OpenAI's whisper-1 model.
    `filename` just needs the right extension -- the SDK uses it to tell
    Whisper what container format the bytes are in.
    """
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename

    transcript = _client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
    )
    return transcript.text.strip()
