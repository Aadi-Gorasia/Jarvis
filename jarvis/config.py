import os
from pathlib import Path
from types import SimpleNamespace

import yaml
from dotenv import load_dotenv

from .util import PROJECT_ROOT


def _ns(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in value.items()})
    return value


def load_config(path=None):
    load_dotenv(PROJECT_ROOT / ".env")
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    cfg = _ns(yaml.safe_load(cfg_path.read_text()) or {})
    cfg.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return cfg
