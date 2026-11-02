"""Jarvis's management tools for the agent team."""
from .base import ctx, current_task, tool


@tool
def delegate_task(agent: str, brief: str, engine: str = "") -> str:
    """Hand a job to a background agent and return immediately; the agent works in parallel and Jarvis announces when it finishes. Write a complete, self-contained brief: goal, file names or paths, formats, constraints, what 'done' looks like. agent is one of the available agent names. Leave engine empty normally; use 'gemini' to force the stronger cloud model for a hard job, or 'ollama' for the on-device model."""
    parent = current_task()
    t = ctx.manager.spawn(agent, brief, parent=parent.id if parent else 0, depth=parent.depth + 1 if parent else 0, engine=engine)
    return f"Task {t.id} started with the {t.agent} agent."


@tool
def list_tasks() -> str:
    """Show running and recent background tasks with their status and latest step."""
    return ctx.manager.summary()


@tool
def task_result(task_id: int) -> str:
    """Get the result (or the latest log lines if still running) of a background task."""
    t = ctx.manager.tasks.get(int(task_id))
    if t is None:
        return f"No task {task_id} in this session."
    if t.status in ("queued", "running"):
        return f"Task {t.id} is {t.status}. Recent steps: " + "; ".join(t.log[-4:])
    return f"Task {t.id} {t.status}: {t.result}"


@tool(scope="jarvis")
def cancel_task(task_id: int) -> str:
    """Stop a background task."""
    return ctx.manager.cancel(task_id)


@tool(scope="jarvis")
def list_agents() -> str:
    """List the available agents and what each does."""
    return ctx.manager.describe_agents()


@tool(scope="jarvis")
def create_agent(name: str, description: str, instructions: str, tool_groups: str = "all") -> str:
    """Create a new permanent specialist agent. tool_groups is a comma-separated list from: files, shell, excel, web, gui, system, memory, delegate, or 'all'. instructions is its standing expertise and style."""
    from . import GROUPS as G
    groups = [g.strip() for g in tool_groups.split(",") if g.strip()] or ["all"]
    bad = [g for g in groups if g != "all" and g not in G]
    if bad:
        return f"Error: unknown tool groups {bad}. Valid: {', '.join(G)}, all"
    d = ctx.manager.create_agent(name, description, instructions, groups)
    return f"Agent '{d.name}' created and ready to receive tasks."


@tool(scope="jarvis")
def delete_agent(name: str) -> str:
    """Delete a custom agent (built-in agents cannot be deleted)."""
    return "Deleted." if ctx.manager.delete_agent(name) else "No such custom agent."


@tool(scope="jarvis")
def list_approvals() -> str:
    """Show actions that agents are waiting for the user to approve."""
    return ctx.broker.describe()


@tool(scope="jarvis")
def resolve_approval(approve: bool, approval_id: int = 0) -> str:
    """Approve or deny an action an agent is waiting on. approval_id 0 means the most recent pending request. Only call this after the user clearly said yes or no."""
    return ctx.broker.resolve(approval_id, approve)
