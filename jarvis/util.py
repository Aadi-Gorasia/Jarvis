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


# ── staying lightweight ──────────────────────────────────────────
def lower_priority(nice: int = 10) -> None:
    """Run at a lower priority so your own apps always win when the Mac is busy."""
    try:
        import os
        os.nice(int(nice))
    except Exception:
        pass


def system_busy(threshold: float = 0.75) -> bool:
    """True when the Mac is already working hard (1-minute load per core above threshold)."""
    try:
        import os
        return os.getloadavg()[0] / (os.cpu_count() or 4) > threshold
    except Exception:
        return False


def on_battery() -> bool:
    try:
        import psutil
        b = psutil.sensors_battery()
        return bool(b and not b.power_plugged)
    except Exception:
        return False


_front = (0.0, "")


def frontmost_app() -> str:
    """Name of the frontmost app (cached for 3 s)."""
    global _front
    import subprocess
    import time as _t
    if _t.time() - _front[0] < 3:
        return _front[1]
    name = ""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "name", asn], capture_output=True, text=True, timeout=3).stdout
        name = info.split("=")[-1].strip().strip('"')
    except Exception:
        pass
    _front = (_t.time(), name)
    return name


def perf_text() -> str:
    try:
        import psutil
        proc = psutil.Process()
        return f"{proc.cpu_percent(interval=None) / (psutil.cpu_count() or 1):.0f}% CPU, {proc.memory_info().rss / 1e6:.0f} MB"
    except Exception:
        return "n/a"


_audio = (0.0, False)


def audio_playing() -> bool:
    """True while something is playing sound on the Mac (music, video). Cached for 6 s."""
    global _audio
    import subprocess
    import time as _t
    if _t.time() - _audio[0] < 6:
        return _audio[1]
    playing = False
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=4).stdout
        playing = any("com.apple.audio" in line and "PreventUserIdle" in line for line in out.splitlines())
    except Exception:
        pass
    _audio = (_t.time(), playing)
    return playing
