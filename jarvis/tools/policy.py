"""What agents may do. Three modes (config agents.permission_mode):

ask      every write, delete, shell command and GUI action needs your approval
trusted  (default) agents work freely; existing files are backed up before any change; risky
         actions (deleting outside the workspace, killing processes, network uploads, sending
         mail/messages, system settings) pause and ask you
full     no approval prompts at all, only the hard blocks below still apply

Always blocked in every mode: sudo, disk erasing, shutdown, pipe-to-shell installs, and anything
touching SSH keys, cloud credentials, keychains, browser profiles or Jarvis's own .env.
That is a deliberate guard against prompt injection from web pages or files.
"""
import re
import shlex
import shutil
import time
from pathlib import Path

from ..util import PROJECT_ROOT
from .base import ctx, current_task

HARD_BLOCK = [
    r"\bsudo\b", r"\bmkfs", r":\(\)\s*\{", r"\bdd\s+if=", r">\s*/dev/(sd|disk|rdisk)",
    r"\bdiskutil\s+(erase|partition|apfs\s+delete)", r"\b(shutdown|reboot|halt)\b",
    r"(curl|wget)[^|;]*\|\s*(ba|z)?sh", r"\bcsrutil\b", r"\bnvram\b",
    r"rm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\$HOME)(\s|$|\*)",
    r"find-generic-password|find-internet-password|dump-keychain",
    r"\.ssh\b|\.gnupg\b|\.aws\b|\.kube\b|Keychains|Chrome/Default|Chrome/Profile|Login Data|Firefox/Profiles|\.jarvis/\.env",
]
RISKY_SHELL = [
    r"\brm\b", r"\brmdir\b", r"\bkill(all)?\b", r"\bpkill\b", r"\bchmod\b", r"\bchown\b",
    r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard", r"\bgit\s+clean\b",
    r"\bcurl\b.*(-X\s*(POST|PUT|DELETE|PATCH)|\s-d\s|--data|\s-F\s|\s-T\s|--upload)",
    r"\bscp\b", r"\brsync\b.*:", r"\bssh\b", r"\bnc\b", r"\blaunchctl\b", r"\bdefaults\s+write",
    r"\bnetworksetup\b", r"\btccutil\b", r"\bosascript\b", r"\bmail\b", r"\bsendmail\b",
    r"\bbrew\s+(uninstall|remove)", r"\bpip3?\s+uninstall", r"\bmv\b.*\s/(etc|usr|System|Library|Applications)",
    r">\s*/(etc|usr|System|Library|Applications)",
]
RISKY_PYTHON = [
    r"shutil\.rmtree", r"os\.(remove|unlink|rmdir|removedirs|rename|system|popen)", r"\.unlink\(", r"shutil\.move",
    r"smtplib|ftplib|paramiko|socket\.", r"requests\.(post|put|delete|patch)", r"urllib.*data\s*=",
]
RISKY_APPLESCRIPT = [r"do shell script", r"\bdelete\b", r"\bsend\b", r"empty (the )?trash", r"\bquit\b",
                     r"shut down|restart|log out", r"\bpassword\b"]

SECRET_FILE = (PROJECT_ROOT / ".env").resolve()
PROTECTED_PARTS = {".ssh", ".gnupg", ".aws", ".kube", "Keychains"}
PROTECTED_FRAGMENTS = ("Application Support/Google/Chrome", "Application Support/Firefox",
                       "Library/Cookies", "Library/Safari", "Application Support/Jarvis")


def mode() -> str:
    return str(ctx.cfg.agents.permission_mode).lower()


def workspace() -> Path:
    ws = Path(ctx.cfg.agents.workspace).expanduser()
    for sub in ("", "tasks", "backups", "scratch"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def inside_workspace(p: Path) -> bool:
    ws = workspace().resolve()
    return p == ws or ws in p.parents


def resolve(path: str) -> Path:
    """Expand ~ and relative paths (relative = inside the workspace) and refuse protected places."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = workspace() / p
    p = p.resolve()
    if p == SECRET_FILE or (PROTECTED_PARTS & set(p.parts)) or any(f in str(p) for f in PROTECTED_FRAGMENTS):
        raise PermissionError(f"{p} is a protected location (credentials, keys or browser data)")
    return p


def approve(action: str, reason: str) -> bool:
    action = action if len(action) <= 160 else action[:157] + "..."
    task = current_task()
    if task is not None and ctx.broker is not None:
        return ctx.broker.request(f"the {task.agent} agent, task {task.id}", action, reason)
    return ctx.confirm(f"{reason}: {action}. Approve?")


def _need(action: str, reason: str) -> None:
    if not approve(action, reason):
        raise PermissionError("the user did not approve this action; choose a different approach or report back")


def _hard_block(text: str) -> None:
    for pat in HARD_BLOCK:
        if re.search(pat, text):
            raise PermissionError("blocked by the safety policy (privileged, destructive or credential access)")


def _rm_inside_workspace(cmd: str) -> bool:
    if re.search(r"[;&|`$()<>]", cmd):
        return False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts or parts[0] != "rm":
        return False
    targets = [a for a in parts[1:] if not a.startswith("-")]
    try:
        return bool(targets) and all(inside_workspace(resolve(t)) for t in targets)
    except PermissionError:
        return False


def guard_shell(cmd: str) -> None:
    _hard_block(cmd)
    m = mode()
    if m == "full":
        return
    if m == "ask":
        _need(cmd, "Shell command")
        return
    if any(re.search(p, cmd) for p in RISKY_SHELL) and not _rm_inside_workspace(cmd):
        _need(cmd, "Risky shell command")


def guard_python(code: str) -> None:
    _hard_block(code)
    m = mode()
    if m == "full":
        return
    if m == "ask":
        _need("run a Python script: " + code.strip().splitlines()[0][:80], "Python code")
        return
    hit = next((p for p in RISKY_PYTHON if re.search(p, code)), None)
    if hit:
        _need("run Python that deletes, moves or uploads something", "Risky Python")


def guard_applescript(script: str) -> None:
    _hard_block(script)
    m = mode()
    if m == "full":
        return
    if m == "ask" or any(re.search(p, script, re.I) for p in RISKY_APPLESCRIPT):
        _need("AppleScript: " + script.strip().replace("\n", "; ")[:120], "Automation script")


def guard_gui(what: str) -> None:
    if mode() == "ask":
        _need(what, "Screen control")


def backup(p: Path):
    """Copy an existing file into <workspace>/backups before it is changed."""
    if p.is_file():
        dest = workspace() / "backups" / f"{time.strftime('%Y%m%d-%H%M%S')}_{p.name}"
        shutil.copy2(p, dest)
        return dest
    return None


def guard_write(p: Path):
    """Call before creating/overwriting a file. Returns the backup path if one was made."""
    if mode() == "ask" and not inside_workspace(p):
        _need(f"write {p}", "File change")
    return backup(p)


def guard_delete(p: Path) -> None:
    if mode() == "full" or (mode() == "trusted" and inside_workspace(p)):
        return
    _need(f"move {p} to the Trash", "Deleting a file")
