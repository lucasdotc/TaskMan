"""
Turns text into a semantic embedding vector, via HuggingFace's hosted
Inference API, for pgvector-based task matching.

Why this exists: matching "the dentist one" to an actual task used to mean
sending your whole task list to Claude and asking it to pick one -- an LLM
call on every single completion/deletion/transfer. An embedding lets
Postgres do that matching itself with a similarity search (see
find_similar_task() in db.py), which is faster and cheaper for the common
case. See resolve_task_by_description() in bot.py for how the two are
combined: try the embedding-based match first, and only fall back to
asking Claude (fuzzy_match) if that match isn't confident enough.

Model: sentence-transformers/all-MiniLM-L6-v2 -- a small, well-established
sentence-embedding model (384 dimensions, hence `vector(384)` in
schema.sql). Chosen over an OpenAI embeddings model specifically because it
runs on HuggingFace's free hosted Inference API rather than adding a third
paid API vendor alongside Anthropic and OpenAI.
"""
import os
import logging
from huggingface_hub import InferenceClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_client = InferenceClient(token=os.getenv("HUGGINGFACE_API_TOKEN"))


def embed_text(text: str) -> list[float] | None:
    """
    Returns a 384-dimension embedding for `text`, or None if the
    HuggingFace API is unavailable, unconfigured, or errors for any reason.

    Deliberately returns None instead of raising: callers (see
    resolve_task_by_description() in bot.py) treat a missing embedding the
    same as "no confident vector match" and fall back to fuzzy_match, so a
    HuggingFace outage degrades this feature rather than breaking task
    matching entirely.
    """
    try:
        result = _client.feature_extraction(text, model=EMBEDDING_MODEL)
    except Exception:
        logger.exception(f"HuggingFace embedding request failed for: {text!r}")
        return None

    # feature_extraction() returns a numpy array. For a single string input
    # it's normally already the pooled sentence vector (shape (384,)), but
    # depending on the model/endpoint it can come back as one row per token
    # instead (shape (num_tokens, 384)) -- mean-pooling across the token
    # axis in that case still gives a single usable sentence vector, rather
    # than erroring or silently using only the first token's embedding.
    if result.ndim == 2:
        result = result.mean(axis=0)

    return result.tolist()
