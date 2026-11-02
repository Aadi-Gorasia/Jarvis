import os
import re
import subprocess
import tempfile
import threading
import webbrowser
from datetime import datetime

from google.genai import types

from .base import ctx, tool


def _osascript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@tool
def get_current_time() -> str:
    """Get the current local date, time and weekday."""
    return datetime.now().strftime("%A, %d %B %Y, %I:%M %p")


@tool
def open_app(name: str) -> str:
    """Launch a macOS application by name, for example 'Safari', 'Spotify', 'Notes' or 'Visual Studio Code'."""
    r = subprocess.run(["open", "-a", name], capture_output=True, text=True)
    return f"Opened {name}." if r.returncode == 0 else f"Error: could not open {name}: {r.stderr.strip()}"


@tool
def open_url(url: str) -> str:
    """Open a web address (http or https) in the default browser."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}."


@tool
def set_volume(percent: int) -> str:
    """Set the Mac's output volume to a value between 0 and 100."""
    percent = max(0, min(100, int(percent)))
    _osascript(f"set volume output volume {percent}")
    return f"Volume set to {percent} percent."


@tool
def media_control(action: str, app: str = "Music") -> str:
    """Control music playback. action is one of: play, pause, playpause, next, previous. app is 'Music' or 'Spotify'."""
    commands = {"play": "play", "pause": "pause", "playpause": "playpause",
                "next": "next track", "previous": "previous track"}
    if action not in commands or app not in ("Music", "Spotify"):
        return "Error: unsupported action or app."
    _osascript(f'tell application "{app}" to {commands[action]}')
    return f"{app}: {action} done."


@tool
def system_status() -> str:
    """Report CPU load, memory use, disk use and battery level of this Mac."""
    import psutil

    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    out = f"CPU {cpu:.0f}%, memory {mem:.0f}%, disk {disk:.0f}% full."
    batt = psutil.sensors_battery()
    if batt:
        out += f" Battery {batt.percent:.0f}%{', charging' if batt.power_plugged else ''}."
    return out


@tool
def set_timer(seconds: int, label: str = "timer") -> str:
    """Start a countdown timer. Jarvis announces it out loud when it finishes. seconds must be between 1 and 86400."""
    seconds = int(seconds)
    if not 1 <= seconds <= 86400:
        return "Error: seconds must be between 1 and 86400."

    def fire():
        ctx.speaker.say(f"Your {label} is done.")
        try:
            _osascript(f'display notification "{_q(label)} finished" with title "Jarvis"')
        except Exception:
            pass

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return f"Timer '{label}' set for {seconds} seconds."


@tool
def notify(title: str, message: str) -> str:
    """Show a macOS notification banner."""
    _osascript(f'display notification "{_q(message)}" with title "{_q(title)}"')
    return "Notification shown."


@tool
def read_clipboard() -> str:
    """Read the current text on the clipboard."""
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout[:4000] or "(clipboard is empty)"


@tool
def copy_to_clipboard(text: str) -> str:
    """Copy text to the clipboard."""
    subprocess.run(["pbcopy"], input=text, text=True)
    return "Copied to clipboard."


@tool
def describe_screen(question: str = "Describe what is on the screen.") -> str:
    """Take a screenshot of the main display and answer a question about it (read errors, summarise a page, find a button)."""
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(["screencapture", "-x", "-t", "jpg", path], check=True)
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        os.remove(path)
    resp = ctx.client.models.generate_content(
        model=ctx.cfg.gemini.model,
        contents=[types.Part.from_bytes(data=data, mime_type="image/jpeg"), question],
    )
    return resp.text or "I couldn't make anything out."


BLOCKED = ("sudo", "rm -rf /", "rm -rf ~", "mkfs", ":(){", "dd if=", "> /dev/", "shutdown", "reboot", "diskutil erase")


@tool
def run_shell_command(command: str) -> str:
    """Run a terminal command on the Mac and return its output. The user must confirm out loud before it runs."""
    if not ctx.cfg.tools.allow_shell:
        return "Error: shell access is switched off in config.yaml (tools.allow_shell)."
    if any(b in command for b in BLOCKED):
        return "Error: that command is on the blocked list."
    if not ctx.confirm(f"Shall I run this command: {command}?"):
        return "The user declined, so nothing was run."
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return ((r.stdout + r.stderr).strip() or "(no output)")[:2000]
