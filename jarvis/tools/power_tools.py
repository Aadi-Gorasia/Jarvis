"""The agents' hands: shell, Python, and unrestricted-by-folder file operations (guarded by policy.py)."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import policy
from .base import tool

SHELL = "/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/bash"


@tool(scope="agent")
def shell(command: str, timeout: int = 120, cwd: str = "") -> str:
    """Run a shell (zsh) command on the Mac and return its exit code and output. Working directory defaults to the Jarvis workspace. Use for git, brew, pip, npm, curl, open, unzip, etc. Not interactive."""
    policy.guard_shell(command)
    wd = policy.resolve(cwd) if cwd else policy.workspace()
    r = subprocess.run(command, shell=True, executable=SHELL, cwd=wd, capture_output=True,
                       text=True, timeout=min(int(timeout), 900))
    return f"exit code {r.returncode}\n{(r.stdout + r.stderr).strip()[-6000:]}"


@tool(scope="agent")
def run_python(code: str, timeout: int = 180) -> str:
    """Run a Python 3 script and return what it prints. pandas, openpyxl, python-docx, python-pptx and requests are installed. Relative paths resolve inside the Jarvis workspace. Print anything you need to see."""
    policy.guard_python(code)
    ws = policy.workspace()
    script = ws / "scratch" / f"run_{int(time.time() * 1000)}.py"
    script.write_text(code, encoding="utf-8")
    r = subprocess.run([sys.executable, str(script)], cwd=ws, capture_output=True, text=True,
                       timeout=min(int(timeout), 900))
    return f"exit code {r.returncode}\n{(r.stdout + r.stderr).strip()[-6000:]}"


@tool(scope="agent")
def read_file(path: str, start: int = 0, max_chars: int = 8000) -> str:
    """Read a text file (code, markdown, csv, json...). Use start to page through large files. Relative paths are inside the workspace."""
    p = policy.resolve(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    chunk = text[int(start): int(start) + int(max_chars)]
    return f"[{p} | chars {start}-{start + len(chunk)} of {len(text)}]\n{chunk}"


@tool(scope="agent")
def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file (parent folders are created). An existing file is backed up first."""
    p = policy.resolve(path)
    bak = policy.guard_write(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {p}" + (f" (previous version backed up to {bak})" if bak else "")


@tool(scope="agent")
def append_file(path: str, content: str) -> str:
    """Append text to the end of a file, creating it if needed."""
    p = policy.resolve(path)
    policy.guard_write(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(content)
    return f"Appended to {p}"


@tool(scope="agent")
def edit_file(path: str, old: str, new: str) -> str:
    """Replace one exact piece of text in a file. 'old' must appear exactly once."""
    p = policy.resolve(path)
    text = p.read_text(encoding="utf-8")
    if text.count(old) != 1:
        return f"Error: 'old' text found {text.count(old)} times; it must appear exactly once."
    bak = policy.guard_write(p)
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"Edited {p}" + (f" (backup: {bak})" if bak else "")


@tool(scope="agent")
def list_dir(path: str = ".") -> str:
    """List a folder's contents with sizes. Relative paths are inside the workspace."""
    p = policy.resolve(path)
    rows = []
    for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]:
        rows.append(f"{c.name}/" if c.is_dir() else f"{c.name}  ({c.stat().st_size} bytes)")
    return f"{p}\n" + ("\n".join(rows) or "(empty)")


@tool(scope="agent")
def make_dir(path: str) -> str:
    """Create a folder (and parents)."""
    p = policy.resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Created {p}"


@tool(scope="agent")
def copy_path(src: str, dst: str) -> str:
    """Copy a file or folder."""
    s, d = policy.resolve(src), policy.resolve(dst)
    policy.guard_write(d)
    if s.is_dir():
        shutil.copytree(s, d, dirs_exist_ok=True)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    return f"Copied {s} to {d}"


@tool(scope="agent")
def move_path(src: str, dst: str) -> str:
    """Move or rename a file or folder."""
    s, d = policy.resolve(src), policy.resolve(dst)
    policy.guard_write(d)
    policy.guard_delete(s)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return f"Moved {s} to {d}"


@tool(scope="agent")
def trash_path(path: str) -> str:
    """Move a file or folder to the Trash (recoverable). Never permanently deletes."""
    p = policy.resolve(path)
    policy.guard_delete(p)
    trash = Path.home() / ".Trash"
    dest = trash / p.name
    if dest.exists():
        dest = trash / f"{p.stem}-{int(time.time())}{p.suffix}"
    shutil.move(str(p), str(dest))
    return f"Moved {p} to the Trash"
