from .base import REGISTRY, ctx  # noqa: F401

GROUPS = {
    "files": ["read_file", "write_file", "append_file", "edit_file", "list_dir", "make_dir", "copy_path",
              "move_path", "trash_path", "search_files"],
    "shell": ["shell", "run_python"],
    "excel": ["excel_inspect", "excel_read", "excel_write"],
    "web": ["web_search", "fetch_webpage", "download_file"],
    "gui": ["describe_screen", "locate_on_screen", "gui_click", "gui_type", "gui_hotkey", "gui_scroll",
            "run_applescript", "open_app", "open_url"],
    "system": ["system_status", "get_current_time", "set_volume", "media_control", "notify", "read_clipboard",
               "copy_to_clipboard", "get_weather", "check_messages", "read_inbox"],
    "memory": ["remember", "recall", "forget", "add_note", "read_notes", "search_history"],
    "github": ["find_git_repos", "repo_audit", "github_api"],
    "cleanup": ["mac_scan", "trash_many", "move_many", "undo_cleanup", "disk_usage"],
    "delegate": ["delegate_task", "list_tasks", "task_result"],
}

from . import system_tools, file_tools, web_tools, memory_tools  # noqa: E402,F401
from . import power_tools, excel_tools, gui_tools, agent_tools, history_tools, social_tools, github_tools, cleanup_tools  # noqa: E402,F401  (importing registers the tools)


def get_tools(cfg):
    """Tools Jarvis himself uses: quick actions plus the agent-management tools."""
    disabled = set(cfg.tools.disabled or [])
    return [t for t in REGISTRY if t.scope in ("jarvis", "both") and t.__name__ not in disabled]


def tools_for_groups(groups):
    """Tools a background agent gets. 'all' = every group except 'delegate'."""
    names = set()
    for g in groups:
        names |= {n for k, v in GROUPS.items() if k != "delegate" for n in v} if g == "all" else set(GROUPS.get(g, []))
    names.add("report_progress")                       # every agent can show live progress
    by_name = {t.__name__: t for t in REGISTRY}
    return [by_name[n] for n in sorted(names) if n in by_name]
