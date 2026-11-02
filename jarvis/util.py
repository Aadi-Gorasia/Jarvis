import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path.home() / ".jarvis"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = Path.home() / "Library" / "Logs" / "jarvis.log"


def setup_logging(verbose: bool = False) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers = [file_handler, stream]
    for noisy in ("httpx", "httpcore", "urllib3", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def clean_for_speech(text: str) -> str:
    """Strip markdown/URLs so the TTS engine doesn't read symbols aloud."""
    text = re.sub(r"```.*?```", " code omitted. ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*#`>~]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def notify(title: str, message: str) -> None:
    """macOS notification banner (silently ignored if unavailable)."""
    import subprocess
    esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')[:200]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


_lock_handle = None


def acquire_single_instance() -> bool:
    """Only one Jarvis may own the microphone. Returns False if another copy is already running."""
    import fcntl
    global _lock_handle
    _lock_handle = open(DATA_DIR / "jarvis.lock", "w")
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle.write(str(__import__("os").getpid()))
        _lock_handle.flush()
        return True
    except OSError:
        return False
