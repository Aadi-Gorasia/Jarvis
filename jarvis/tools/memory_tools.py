from .base import ctx, tool


@tool
def remember(topic: str, detail: str) -> str:
    """Save a lasting fact about the user or their world, e.g. topic 'sister' detail 'Meera, lives in Mumbai'. Overwrites the same topic."""
    ctx.memory.remember(topic, detail)
    return f"Remembered {topic}."


@tool
def recall(query: str) -> str:
    """Search saved facts for a keyword."""
    rows = ctx.memory.search(query)
    return "\n".join(f"{t}: {d}" for t, d in rows) or "Nothing saved about that."


@tool
def forget(topic: str) -> str:
    """Delete saved facts whose topic matches the given text."""
    return "Forgotten." if ctx.memory.forget(topic) else "I had nothing saved under that topic."
