"""Tool plumbing. Any function decorated with @tool is exposed to Gemini as a callable tool.

Rules for writing a tool: type-hint every argument, write a clear docstring (Gemini reads it to
decide when to call the tool), return a short string. Exceptions become "Error: ..." results.

scope="jarvis"  only the voice assistant sees it
scope="agent"   only background agents can use it (via tool groups)
scope="both"    default
"""
import functools
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("jarvis.tools")
REGISTRY: list = []
current = threading.local()  # .task is set inside a background agent's worker thread


def current_task():
    return getattr(current, "task", None)


@dataclass
class Context:
    cfg: Any = None
    client: Any = None
    speaker: Any = None
    memory: Any = None
    manager: Any = None   # AgentManager
    broker: Any = None    # ApprovalBroker
    announce: Callable[[str], None] = print
    confirm: Callable[[str], bool] = lambda prompt: False


ctx = Context()


def tool(fn=None, *, scope: str = "both"):
    def decorate(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            task = current_task()
            summary = str(kwargs or args)[:160]
            log.info("tool call%s: %s %s", f" [task {task.id}]" if task else "", f.__name__, summary)
            if task is not None:
                if task.cancelled:
                    return "Error: this task was cancelled. Stop working and reply with a one-line summary."
                task.note(f"{f.__name__} {summary}")
            try:
                return str(f(*args, **kwargs))
            except Exception as exc:
                log.exception("tool %s failed", f.__name__)
                return f"Error: {exc}"

        wrapper.scope = scope
        REGISTRY.append(wrapper)
        return wrapper

    return decorate(fn) if fn is not None else decorate
