"""Shared helpers for building Gemini chat sessions with tools and retrying transient errors."""
import logging
import re
import time

from google.genai import types

log = logging.getLogger("jarvis.llm")
TRANSIENT = ("429", "RESOURCE_EXHAUSTED", "500", "503", "UNAVAILABLE", "INTERNAL", "DEADLINE")


def make_chat(client, model, system, tools, max_calls, thinking_level=None, provider="gemini", cfg=None):
    if provider == "ollama":
        from .local_llm import LocalChat
        return LocalChat(cfg, system, tools, max_calls)
    def build(think):
        kw = dict(
            system_instruction=system,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=max_calls),
        )
        if think and thinking_level:
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
        return client.chats.create(model=model, config=types.GenerateContentConfig(**kw))

    try:
        return build(True)
    except Exception as exc:
        log.warning("thinking config rejected (%s); continuing without it", exc)
        return build(False)


class QuotaExhausted(RuntimeError):
    """The daily Gemini quota for this model is used up (retrying will not help)."""


def _wait_seconds(msg: str, attempt: int) -> float:
    """Honour the retry delay Google puts in 429 errors; otherwise back off progressively."""
    m = re.search(r"retry in ([\d.]+)s", msg) or re.search(r"retryDelay'?\"?: ?'?\"?(\d+)", msg)
    if m:
        return min(float(m.group(1)) + 1, 90)
    return min(15 * (attempt + 1), 90) if "429" in msg or "RESOURCE_EXHAUSTED" in msg else 2 ** (attempt + 1)


def is_transient(exc) -> bool:
    return any(k in str(exc) for k in TRANSIENT)


def with_retry(fn, attempts: int = 6, max_wait: float = 90):
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            msg = str(exc)
            if "PerDay" in msg or "per day" in msg.lower():
                raise QuotaExhausted("Gemini daily quota is used up. Wait for it to reset, enable billing in "
                                     "Google AI Studio, or switch to a different model.") from exc
            if i < attempts - 1 and any(k in msg for k in TRANSIENT):
                wait = min(_wait_seconds(msg, i), max_wait)
                log.warning("Gemini busy or rate-limited; retrying in %.0fs (%s)", wait, msg[:90].replace("\n", " "))
                time.sleep(wait)
                continue
            raise
