"""Drive the screen like a person: click, type, shortcuts, AppleScript. Needs Accessibility permission.
Emergency stop: slam the mouse into the top-left corner of the screen (pyautogui failsafe)."""
import os
import re
import subprocess
import tempfile
import time

from google.genai import types

from . import policy
from .base import ctx, tool


def _pg():
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.15
    return pyautogui


@tool(scope="agent")
def locate_on_screen(description: str) -> str:
    """Find a visible element (button, text field, icon, menu item) on screen from a description and return the x,y point to click, e.g. 'the blue Send button'."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(["screencapture", "-x", path], check=True)
        data = open(path, "rb").read()
    finally:
        os.remove(path)
    prompt = (f"Find this on the screen: {description}. Reply with ONLY JSON like {{\"point\": [y, x]}} where y and x "
              "are integers from 0 to 1000 relative to the image height and width, at the centre of the element. "
              "If it is not visible reply {\"point\": null}.")
    resp = ctx.client.models.generate_content(
        model=ctx.cfg.gemini.model,
        contents=[types.Part.from_bytes(data=data, mime_type="image/png"), prompt])
    m = re.search(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]", resp.text or "")
    if not m:
        return "Not visible on screen."
    w, h = _pg().size()
    return f"x={int(int(m.group(2)) * w / 1000)}, y={int(int(m.group(1)) * h / 1000)} (screen is {w}x{h})"


@tool(scope="agent")
def gui_click(x: int, y: int, clicks: int = 1, button: str = "left") -> str:
    """Click at screen coordinates. clicks=2 double-clicks; button is left or right."""
    policy.guard_gui(f"click at {x},{y}")
    _pg().click(int(x), int(y), clicks=int(clicks), button=button)
    return f"Clicked {x},{y}"


@tool(scope="agent")
def gui_type(text: str) -> str:
    """Type text into whatever has keyboard focus (pastes via the clipboard, so any language works, including German umlauts)."""
    policy.guard_gui("type text on screen")
    subprocess.run(["pbcopy"], input=text, text=True)
    pg = _pg()
    pg.hotkey("command", "v")
    time.sleep(0.2)
    return f"Typed {len(text)} characters"


@tool(scope="agent")
def gui_hotkey(keys: str) -> str:
    """Press a keyboard shortcut written like 'command+s', 'command+shift+4', 'return', 'tab' or 'escape'."""
    policy.guard_gui(f"press {keys}")
    _pg().hotkey(*[k.strip() for k in keys.split("+")])
    return f"Pressed {keys}"


@tool(scope="agent")
def gui_scroll(amount: int) -> str:
    """Scroll the mouse wheel. Positive scrolls up, negative scrolls down."""
    policy.guard_gui("scroll")
    _pg().scroll(int(amount))
    return "Scrolled"


@tool(scope="agent")
def run_applescript(script: str) -> str:
    """Run AppleScript to control Mac apps (Finder, Mail, Calendar, Notes, Safari, Numbers, Excel...) and return its result."""
    policy.guard_applescript(script)
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"Error: {r.stderr.strip()}"
    return r.stdout.strip() or "(done)"
