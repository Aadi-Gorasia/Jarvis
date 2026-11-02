"""Agent roles. Jarvis delegates to these; you can add more by voice (create_agent) or edit here."""
import json
import re
from dataclasses import dataclass, field

from ..util import DATA_DIR

CUSTOM_FILE = DATA_DIR / "agents.json"


@dataclass
class AgentDef:
    name: str
    description: str          # one line: what Jarvis reads to decide who gets a job
    prompt: str               # the agent's specialist instructions
    groups: list = field(default_factory=lambda: ["all"])
    builtin: bool = True


BUILTIN = {
    "coder": AgentDef(
        "coder", "Writes, runs, tests and debugs code; builds scripts, tools, websites and automations.",
        "You are a senior software engineer. Create projects in their own folder inside the workspace unless the user "
        "named another location. Write clean, working code, run it, read the errors, fix them, and test before you finish. "
        "Install packages with pip or npm when needed. Add a short README for anything the user will reuse.",
        ["files", "shell", "web", "memory"]),
    "spreadsheet": AgentDef(
        "spreadsheet", "Fills in, cleans, analyses and builds Excel/CSV spreadsheets (including repetitive vocabulary, data-entry and report sheets).",
        "You are a spreadsheet specialist. If no path is given, find the file with search_files (try the name, then likely "
        "folders like Documents, Desktop, Downloads). Always run excel_inspect first to understand headers, languages, "
        "formulas and formatting, and follow the sheet's existing conventions. Fill or fix every row that is in scope, not just "
        "a sample. For language sheets (for example German), use correct spelling, umlauts, genders/articles and plurals. "
        "For large edits use run_python with openpyxl or pandas. Re-read the result to verify, and report how many rows you completed.",
        ["files", "excel", "shell", "web", "memory"]),
    "researcher": AgentDef(
        "researcher", "Researches topics on the web, compares sources and produces sourced written reports.",
        "You are a careful researcher. Search several angles, open the best sources with fetch_webpage, cross-check facts, "
        "and note disagreements. Save the finished report as a markdown or Word file in the workspace with sources listed.",
        ["web", "files", "shell", "memory"]),
    "writer": AgentDef(
        "writer", "Creates content: documents, emails, scripts, posts, presentations, essays, study material.",
        "You are a versatile writer and content producer. Match the requested tone, length and format. Produce real files "
        "when appropriate (python-docx for Word, python-pptx for slides, markdown otherwise) in the workspace and check "
        "that they open and look right.",
        ["files", "web", "shell", "memory"]),
    "operator": AgentDef(
        "operator", "Does hands-on computer work: organises files, installs software, drives apps with the mouse, keyboard and AppleScript.",
        "You are a computer operator who can do whatever a person can do at this Mac. Prefer scripts, shell and AppleScript "
        "over clicking; use the screen tools (describe_screen, locate_on_screen, gui_click, gui_type) when an app has no "
        "scripting. After GUI actions, look at the screen again to confirm they worked.",
        ["all"]),
}


def load_custom() -> dict:
    if not CUSTOM_FILE.exists():
        return {}
    try:
        return {d["name"]: AgentDef(builtin=False, **d) for d in json.loads(CUSTOM_FILE.read_text())}
    except Exception:
        return {}


def save_custom(defs: dict) -> None:
    rows = [{"name": d.name, "description": d.description, "prompt": d.prompt, "groups": d.groups}
            for d in defs.values() if not d.builtin]
    CUSTOM_FILE.write_text(json.dumps(rows, indent=2))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:30] or "agent"
