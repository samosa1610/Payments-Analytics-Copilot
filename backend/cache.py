"""Tiny in-process TTL cache for /api/ask responses, keyed by (question,
role). Identical questions asked again within the TTL skip SQL generation,
validation, execution and any LLM call entirely — a cheap but real cost
control on top of the row-limit / step-budget controls in validator.py and
analysis.py.
"""
import time
import threading

TTL_SECONDS = 120
_lock = threading.Lock()
_store: dict[tuple[str, str], tuple[float, dict]] = {}


def _key(question: str, role: str) -> tuple[str, str]:
    return question.strip().lower(), role


def get(question: str, role: str) -> tuple[dict, float] | None:
    with _lock:
        entry = _store.get(_key(question, role))
        if not entry:
            return None
        ts, payload = entry
        age = time.monotonic() - ts
        if age > TTL_SECONDS:
            del _store[_key(question, role)]
            return None
        return payload, age


def set(question: str, role: str, payload: dict) -> None:
    with _lock:
        _store[_key(question, role)] = (time.monotonic(), payload)
