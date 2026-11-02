from .base import REGISTRY, ctx  # noqa: F401
from . import system_tools, file_tools, web_tools, memory_tools  # noqa: F401  (importing registers the tools)


def get_tools(cfg):
    disabled = set(cfg.tools.disabled or [])
    return [t for t in REGISTRY if t.__name__ not in disabled]
