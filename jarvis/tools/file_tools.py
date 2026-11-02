import subprocess
from datetime import datetime
from pathlib import Path

from ..util import DATA_DIR
from .base import ctx, tool

PROTECTED = {".ssh", ".gnupg", ".aws", "Keychains", ".kube"}
NOTES = DATA_DIR / "notes.md"


def _safe(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    roots = [Path(r).expanduser().resolve() for r in ctx.cfg.tools.allowed_roots]
    if not any(p == r or r in p.parents for r in roots):
        raise PermissionError("that location is outside the folders Jarvis may access")
    if PROTECTED & set(p.parts):
        raise PermissionError("that location is protected")
    return p


@tool
def search_files(query: str, folder: str = "~") -> str:
    """Find files by name using Spotlight. folder defaults to the home folder. Returns up to 10 paths."""
    root = _safe(folder)
    r = subprocess.run(["mdfind", "-onlyin", str(root), "-name", query],
                       capture_output=True, text=True, timeout=20)
    hits = [p for p in r.stdout.splitlines() if not (PROTECTED & set(Path(p).parts))][:10]
    return "\n".join(hits) or "No matching files."


@tool
def list_folder(folder: str = "~") -> str:
    """List the files and folders inside a directory."""
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in _safe(folder).iterdir() if not p.name.startswith("."))
    return ", ".join(entries[:60]) or "(empty)"


@tool
def read_text_file(path: str) -> str:
    """Read the first part of a plain-text file (notes, code, markdown, csv)."""
    p = _safe(path)
    if p.stat().st_size > 5_000_000:
        return "Error: file is too large."
    return p.read_text(errors="replace")[:4000]


@tool
def add_note(text: str) -> str:
    """Append a timestamped note to the user's Jarvis notes file."""
    with open(NOTES, "a", encoding="utf-8") as fh:
        fh.write(f"- {datetime.now():%Y-%m-%d %H:%M} {text}\n")
    return "Note saved."


@tool
def read_notes() -> str:
    """Read back the most recent notes the user asked Jarvis to save."""
    if not NOTES.exists():
        return "No notes yet."
    return "".join(NOTES.read_text(encoding="utf-8").splitlines(True)[-20:])
