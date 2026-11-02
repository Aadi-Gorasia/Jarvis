"""Shared helpers for building Gemini chat sessions with tools and retrying transient errors."""
import logging
import time

from google.genai import types

log = logging.getLogger("jarvis.llm")
TRANSIENT = ("429", "RESOURCE_EXHAUSTED", "500", "503", "UNAVAILABLE", "INTERNAL", "DEADLINE")


def make_chat(client, model, system, tools, max_calls, thinking_level=None):
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


def with_retry(fn, attempts: int = 4):
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i < attempts - 1 and any(k in str(exc) for k in TRANSIENT):
                log.warning("transient Gemini error (%s); retrying", str(exc)[:120])
                time.sleep(2 ** (i + 1))
                continue
            raise
