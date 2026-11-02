"""Tool plumbing. Any function decorated with @tool is handed to Gemini as a callable tool.

Rules for writing a tool: type-hint every argument, write a clear docstring (Gemini reads it
to decide when to call the tool), return a short string. Exceptions become "Error: ..." results.
"""
import functools
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("jarvis.tools")
REGISTRY: list = []


@dataclass
class Context:
    cfg: Any = None
    client: Any = None
    speaker: Any = None
    memory: Any = None
    confirm: Callable[[str], bool] = lambda prompt: False


ctx = Context()


def tool(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        log.info("tool call: %s %s", fn.__name__, kwargs or args)
        try:
            return str(fn(*args, **kwargs))
        except Exception as exc:
            log.exception("tool %s failed", fn.__name__)
            return f"Error: {exc}"

    REGISTRY.append(wrapper)
    return wrapper
