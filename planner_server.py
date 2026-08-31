#!/usr/bin/env python3
"""MCP local planning/task server.

Tools:
  - create_project: create a local project folder
  - list_projects: list project names
  - create_task: add a task to a project
  - list_tasks: view tasks, optionally filtered by status
  - update_task: edit title, notes, priority, due date, or status
  - complete_task: mark a task complete
  - delete_task: remove a task from a project
  - save_project_note: append/save a Markdown project note
  - get_daily_focus: list overdue, due, and in-progress tasks

Data is stored under MCP_PLANNER_DIR (default: ~/MCPPlanner).
No shell commands or arbitrary filesystem writes outside the planner directory.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover - compatibility with older SDK releases
    from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(os.environ.get("MCP_PLANNER_DIR", str(Path.home() / "MCPPlanner"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

VALID_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}

mcp = FastMCP("local-planner")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._ -]+", "-", value.strip()).strip(".-")
    slug = re.sub(r"[-_ ]+", "-", slug)
    return slug[:80] or "project"


def _safe_project_dir(project: str) -> Path:
    slug = _slugify(project)
    target = (DATA_DIR / slug).resolve()
    if DATA_DIR not in target.parents and target != DATA_DIR:
        raise ValueError("Invalid project name")
    return target


def _tasks_path(project_dir: Path) -> Path:
    return project_dir / "tasks.json"


def _read_tasks(project_dir: Path) -> list[dict[str, Any]]:
    path = _tasks_path(project_dir)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_tasks(project_dir: Path, tasks: list[dict[str, Any]]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    path = _tasks_path(project_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _find_task(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    for task in tasks:
        if task["id"] == task_id:
            return task
    return None


def _project_summary(project_dir: Path) -> dict[str, Any]:
    tasks = _read_tasks(project_dir)
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return {
        "project": project_dir.name,
        "path": str(project_dir),
        "task_count": len(tasks),
        "counts_by_status": counts,
    }


@mcp.tool()
def create_project(name: str, description: str = "") -> dict[str, Any]:
    """Create a local project.

    Args:
        name: Project name, such as 'Home remodel' or 'Q4 launch'.
        description: Optional project description saved in the project note.
    """
    if not name or not name.strip():
        return {"error": "Project name is required"}

    project_dir = _safe_project_dir(name)
    if project_dir.exists() and (project_dir / "tasks.json").exists():
        return {"created": False, "project": project_dir.name, "path": str(project_dir), "message": "Project already exists"}

    project_dir.mkdir(parents=True, exist_ok=True)
    _write_tasks(project_dir, [])

    note_path = project_dir / "notes.md"
    if description.strip():
        note_path.write_text(
            f"# {name.strip()}\n\nCreated: {_now()}\n\n{description.strip()}\n",
            encoding="utf-8",
        )
    elif not note_path.exists():
        note_path.write_text(f"# {name.strip()}\n\nCreated: {_now()}\n", encoding="utf-8")

    return {"created": True, "project": project_dir.name, "path": str(project_dir)}


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List all local planner projects with task counts."""
    projects = []
    for child in sorted(DATA_DIR.iterdir()):
        if child.is_dir() and (child / "tasks.json").exists():
            projects.append(_project_summary(child))
    return {"data_dir": str(DATA_DIR), "projects": projects}


@mcp.tool()
def create_task(
    project: str,
    title: str,
    notes: str = "",
    priority: str = "medium",
    due_date: str = "",
    status: str = "todo",
) -> dict[str, Any]:
    """Create a task in a project.

    Args:
        project: Project name. The project is created if it does not exist.
        title: Short task title.
        notes: Optional details/checklist in Markdown.
        priority: One of low, medium, high, urgent.
        due_date: Optional YYYY-MM-DD due date.
        status: One of todo, in_progress, blocked, done, cancelled.
    """
    title = title.strip()
    if not title:
        return {"error": "Task title is required"}
    priority = (priority or "medium").strip().lower()
    status = (status or "todo").strip().lower()
    if priority not in VALID_PRIORITIES:
        return {"error": f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}"}
    if status not in VALID_STATUSES:
        return {"error": f"status must be one of: {', '.join(sorted(VALID_STATUSES))}"}
    if due_date:
        try:
            date.fromisoformat(due_date)
        except ValueError:
            return {"error": "due_date must use YYYY-MM-DD format"}

    project_dir = _safe_project_dir(project)
    create_project(project, description="")
    tasks = _read_tasks(project_dir)

    task = {
        "id": f"task-{uuid.uuid4().hex[:8]}",
        "title": title,
        "notes": notes.strip(),
        "priority": priority,
        "due_date": due_date,
        "status": status,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": _now() if status == "done" else None,
    }
    tasks.append(task)
    _write_tasks(project_dir, tasks)
    return {"created": True, "project": project_dir.name, "task": task}


@mcp.tool()
def list_tasks(project: str = "", status: str = "") -> dict[str, Any]:
    """List tasks for one project or all projects.

    Args:
        project: Optional project name. Omit to list all tasks.
        status: Optional status filter: todo, in_progress, blocked, done, cancelled.
    """
    status = (status or "").strip().lower()
    if status and status not in VALID_STATUSES:
        return {"error": f"status must be one of: {', '.join(sorted(VALID_STATUSES))}"}

    if project:
        project_dir = _safe_project_dir(project)
        if not project_dir.exists():
            return {"error": f"Project not found: {project_dir.name}"}
        projects = [project_dir]
    else:
        projects = [p for p in sorted(DATA_DIR.iterdir()) if p.is_dir() and (p / "tasks.json").exists()]

    result = []
    for project_dir in projects:
        tasks = _read_tasks(project_dir)
        if status:
            tasks = [task for task in tasks if task["status"] == status]
        result.append({"project": project_dir.name, "tasks": tasks})

    return {"data_dir": str(DATA_DIR), "projects": result}


@mcp.tool()
def update_task(
    project: str,
    task_id: str,
    title: str = "",
    notes: str = "",
    priority: str = "",
    due_date: str = "",
    status: str = "",
) -> dict[str, Any]:
    """Update an existing task. Only provided fields are changed.

    Args:
        project: Project name.
        task_id: Task ID from create_task/list_tasks.
        title: New task title.
        notes: Replace existing notes.
        priority: New priority: low, medium, high, urgent.
        due_date: New YYYY-MM-DD due date, or 'none' to clear it.
        status: New status: todo, in_progress, blocked, done, cancelled.
    """
    project_dir = _safe_project_dir(project)
    if not project_dir.exists():
        return {"error": f"Project not found: {project_dir.name}"}

    tasks = _read_tasks(project_dir)
    task = _find_task(tasks, task_id)
    if not task:
        return {"error": f"Task not found: {task_id}"}

    if title.strip():
        task["title"] = title.strip()
    if notes:
        task["notes"] = notes.strip()
    if priority:
        priority = priority.strip().lower()
        if priority not in VALID_PRIORITIES:
            return {"error": f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}"}
        task["priority"] = priority
    if due_date:
        if due_date.strip().lower() == "none":
            task["due_date"] = ""
        else:
            try:
                date.fromisoformat(due_date)
            except ValueError:
                return {"error": "due_date must use YYYY-MM-DD format or 'none'"}
            task["due_date"] = due_date
    if status:
        status = status.strip().lower()
        if status not in VALID_STATUSES:
            return {"error": f"status must be one of: {', '.join(sorted(VALID_STATUSES))}"}
        task["status"] = status
        task["completed_at"] = _now() if status == "done" else None

    task["updated_at"] = _now()
    _write_tasks(project_dir, tasks)
    return {"updated": True, "project": project_dir.name, "task": task}


@mcp.tool()
def complete_task(project: str, task_id: str) -> dict[str, Any]:
    """Mark a task complete.

    Args:
        project: Project name.
        task_id: Task ID to complete.
    """
    return update_task(project=project, task_id=task_id, status="done")


@mcp.tool()
def delete_task(project: str, task_id: str) -> dict[str, Any]:
    """Delete a task permanently from a project.

    Args:
        project: Project name.
        task_id: Task ID to delete.
    """
    project_dir = _safe_project_dir(project)
    if not project_dir.exists():
        return {"error": f"Project not found: {project_dir.name}"}
    tasks = _read_tasks(project_dir)
    remaining = [task for task in tasks if task["id"] != task_id]
    if len(remaining) == len(tasks):
        return {"deleted": False, "error": f"Task not found: {task_id}"}
    _write_tasks(project_dir, remaining)
    return {"deleted": True, "project": project_dir.name, "task_id": task_id}


@mcp.tool()
def save_project_note(project: str, content: str, append: bool = True) -> dict[str, Any]:
    """Save Markdown notes for a project.

    Args:
        project: Project name. Created if it does not exist.
        content: Markdown content to save.
        append: true to append to notes.md; false to overwrite notes.md.
    """
    if not content.strip():
        return {"error": "Content is required"}
    project_dir = _safe_project_dir(project)
    create_project(project, description="")
    note_path = project_dir / "notes.md"

    if append and note_path.exists():
        existing = note_path.read_text(encoding="utf-8").rstrip()
        content = f"{existing}\n\n---\n\n{content.lstrip()}"

    note_path.write_text(content, encoding="utf-8")
    return {"saved": True, "project": project_dir.name, "path": str(note_path), "bytes": note_path.stat().st_size}


@mcp.tool()
def get_daily_focus(for_date: str = "") -> dict[str, Any]:
    """Get a daily focus list: overdue tasks, due-today tasks, and in-progress work.

    Args:
        for_date: Optional YYYY-MM-DD date. Defaults to today in local time.
    """
    if for_date:
        try:
            today = date.fromisoformat(for_date)
        except ValueError:
            return {"error": "for_date must use YYYY-MM-DD format"}
    else:
        today = date.today()

    overdue = []
    due_today = []
    in_progress = []
    blocked = []

    for project_dir in sorted(DATA_DIR.iterdir()):
        if not project_dir.is_dir() or not (project_dir / "tasks.json").exists():
            continue
        for task in _read_tasks(project_dir):
            item = {"project": project_dir.name, **task}
            due = task.get("due_date") or ""
            if task["status"] in {"todo", "in_progress", "blocked"} and due:
                try:
                    due_d = date.fromisoformat(due)
                except ValueError:
                    continue
                if due_d < today:
                    overdue.append(item)
                elif due_d == today:
                    due_today.append(item)
            if task["status"] == "in_progress":
                in_progress.append(item)
            elif task["status"] == "blocked":
                blocked.append(item)

    priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    for items in (overdue, due_today, in_progress, blocked):
        items.sort(key=lambda t: (priority_order.get(t["priority"], 9), t.get("due_date") or "9999-99-99", t["title"]))

    return {
        "date": today.isoformat(),
        "data_dir": str(DATA_DIR),
        "overdue": overdue,
        "due_today": due_today,
        "in_progress": in_progress,
        "blocked": blocked,
    }


@mcp.resource("planner://data-directory")
def data_directory() -> str:
    """Return the planner data directory."""
    return str(DATA_DIR)


if __name__ == "__main__":
    mcp.run(transport="stdio")
