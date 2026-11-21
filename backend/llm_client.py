"""Thin client for the user's self-hosted "Claude Code API wrapper"
(POST {endpoint}/api/chat -> {"response": "..."}), used to draft SQL.

Never trust what comes back as safe to execute — the caller always runs it
through validator.validate_sql() first. This module only answers the
question "what did the model say", nothing more.
"""
import os
import requests

WRAPPER_ENDPOINT = os.environ.get("WRAPPER_ENDPOINT", "").rstrip("/")
WRAPPER_API_KEY = os.environ.get("WRAPPER_API_KEY")
WRAPPER_MODEL = os.environ.get("WRAPPER_MODEL", "claude-opus-5-5")

TIMEOUT_SECONDS = 60


def available() -> bool:
    return bool(WRAPPER_ENDPOINT and WRAPPER_API_KEY)


def chat(message: str) -> str | None:
    """Returns the model's text response, or None on any failure (network
    error, timeout, non-2xx, malformed body) so callers can fall back.
    """
    if not available():
        return None
    try:
        resp = requests.post(
            f"{WRAPPER_ENDPOINT}/api/chat",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {WRAPPER_API_KEY}",
            },
            json={"message": message, "model": WRAPPER_MODEL},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response")
        return text.strip() if isinstance(text, str) else None
    except Exception:
        return None
