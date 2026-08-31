#!/usr/bin/env python3
"""MCP local health and habit tracker server.

Tools:
  - create_habit: define a habit to track
  - list_habits: list habits
  - log_habit: record habit completion
  - log_workout: record exercise
  - log_meal: record meals/nutrition
  - log_measurement: record weight/body measurements
  - list_logs: query health logs by date/type
  - get_daily_summary: summarize one day
  - get_weekly_report: summarize the last 7 days and habit streaks
  - save_health_note: append/save Markdown health notes
  - delete_log: delete a log entry by ID

Data is stored under MCP_HEALTH_DIR (default: ~/MCPHealth).
This server does not give medical advice.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover - compatibility with older SDK releases
    from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(os.environ.get("MCP_HEALTH_DIR", str(Path.home() / "MCPHealth"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

HABITS_PATH = DATA_DIR / "habits.json"
LOGS_PATH = DATA_DIR / "logs.json"
NOTES_PATH = DATA_DIR / "notes.md"

VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack", "other"}
VALID_INTENSITIES = {"low", "moderate", "high", "very_high", "rest"}

mcp = FastMCP("health-tracker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._ -]+", "-", value.strip()).strip(".-")
    slug = re.sub(r"[-_ ]+", "-", slug)
    return slug[:80] or "habit"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + f".corrupted-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        path.rename(backup)
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _load_habits() -> list[dict[str, Any]]:
    return _load_json(HABITS_PATH, [])


def _write_habits(habits: list[dict[str, Any]]) -> None:
    _write_json(HABITS_PATH, habits)


def _load_logs() -> list[dict[str, Any]]:
    logs = _load_json(LOGS_PATH, [])
    return sorted(logs, key=lambda item: (item.get("log_date", ""), item.get("created_at", "")), reverse=True)


def _write_logs(logs: list[dict[str, Any]]) -> None:
    _write_json(LOGS_PATH, logs)


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _require_date(value: str) -> str:
    value = value or _today()
    if not _valid_date(value):
        raise ValueError("Date must use YYYY-MM-DD format")
    return value


def _find_habits(habits: list[dict[str, Any]], habit_id: str = "", habit_name: str = "") -> list[dict[str, Any]]:
    if habit_id:
        return [h for h in habits if h["id"] == habit_id]
    if habit_name:
        wanted = habit_name.casefold().strip()
        return [h for h in habits if h["name"].casefold() == wanted]
    return []


def _habit_or_error(habit_id: str = "", habit_name: str = "") -> dict[str, Any]:
    habits = _load_habits()
    matches = _find_habits(habits, habit_id, habit_name)
    if not matches:
        return {"error": f"Habit not found: {habit_id or habit_name}"}
    if len(matches) > 1:
        return {"error": "Multiple habits matched. Use habit_id instead of habit_name."}
    return {"habit": matches[0]}


def _numeric(
    value: Any,
    field: str,
    required: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be {minimum} or greater")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be {maximum} or less")
    return number


def _add_log(log_type: str, log_date: str, fields: dict[str, Any]) -> dict[str, Any]:
    logs = _load_logs()
    entry = {
        "id": f"{log_type}-{uuid.uuid4().hex[:8]}",
        "type": log_type,
        "log_date": log_date,
        "created_at": _now(),
        **fields,
    }
    logs.append(entry)
    _write_logs(logs)
    return entry


@mcp.tool()
def create_habit(name: str, description: str = "", target_per_week: int = 7, unit: str = "completion") -> dict[str, Any]:
    """Create a habit to track.

    Args:
        name: Habit name, such as 'Walk' or 'Drink water'.
        description: Optional reminder/description.
        target_per_week: Target completions per week (1-21).
        unit: Unit such as 'minutes', 'steps', 'ounces', or 'completion'.
    """
    name = name.strip()
    if not name:
        return {"error": "Habit name is required"}
    try:
        target = int(target_per_week)
    except (TypeError, ValueError):
        return {"error": "target_per_week must be an integer"}
    if not 1 <= target <= 21:
        return {"error": "target_per_week must be between 1 and 21"}

    habits = _load_habits()
    if any(h["name"].casefold() == name.casefold() for h in habits):
        return {"error": f"A habit named '{name}' already exists"}

    habit = {
        "id": f"habit-{_slugify(name)}-{uuid.uuid4().hex[:4]}",
        "name": name,
        "description": description.strip(),
        "target_per_week": target,
        "unit": unit.strip() or "completion",
        "active": True,
        "created_at": _now(),
    }
    habits.append(habit)
    _write_habits(habits)
    return {"created": True, "habit": habit}


@mcp.tool()
def list_habits(active_only: bool = True) -> dict[str, Any]:
    """List habits.

    Args:
        active_only: If true, return only active habits.
    """
    habits = _load_habits()
    if active_only:
        habits = [h for h in habits if h.get("active", True)]
    return {"data_dir": str(DATA_DIR), "habits": habits}


@mcp.tool()
def log_habit(
    log_date: str = "",
    value: float = 1,
    notes: str = "",
    habit_id: str = "",
    habit_name: str = "",
) -> dict[str, Any]:
    """Log a habit completion. Use either habit_name or habit_id.

    Args:
        log_date: YYYY-MM-DD date. Defaults to today.
        value: Amount completed. Use 1 for simple check-off habits.
        notes: Optional context.
        habit_id: Habit ID from create_habit/list_habits.
        habit_name: Exact habit name.
    """
    try:
        log_date = _require_date(log_date)
        amount = _numeric(value, "value", required=True, minimum=0)
    except ValueError as exc:
        return {"error": str(exc)}

    found = _habit_or_error(habit_id, habit_name)
    if "error" in found:
        return found
    habit = found["habit"]

    entry = _add_log(
        "habit",
        log_date,
        {
            "habit_id": habit["id"],
            "habit_name": habit["name"],
            "value": amount,
            "unit": habit["unit"],
            "notes": notes.strip(),
        },
    )
    return {"logged": True, "entry": entry}


@mcp.tool()
def log_workout(
    activity: str,
    duration_minutes: float,
    log_date: str = "",
    intensity: str = "moderate",
    calories: float | None = None,
    distance_km: float | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Log a workout.

    Args:
        activity: Exercise name/type.
        duration_minutes: Duration in minutes.
        log_date: YYYY-MM-DD date. Defaults to today.
        intensity: One of low, moderate, high, very_high, rest.
        calories: Optional estimated calories burned.
        distance_km: Optional distance in kilometers.
        notes: Optional details.
    """
    activity = activity.strip()
    if not activity:
        return {"error": "activity is required"}
    intensity = (intensity or "moderate").strip().lower()
    if intensity not in VALID_INTENSITIES:
        return {"error": f"intensity must be one of: {', '.join(sorted(VALID_INTENSITIES))}"}
    try:
        log_date = _require_date(log_date)
        minutes = _numeric(duration_minutes, "duration_minutes", required=True, minimum=0)
        cals = _numeric(calories, "calories", minimum=0)
        dist = _numeric(distance_km, "distance_km", minimum=0)
    except ValueError as exc:
        return {"error": str(exc)}

    entry = _add_log(
        "workout",
        log_date,
        {
            "activity": activity,
            "duration_minutes": minutes,
            "intensity": intensity,
            "calories": cals,
            "distance_km": dist,
            "notes": notes.strip(),
        },
    )
    return {"logged": True, "entry": entry}


@mcp.tool()
def log_meal(
    description: str,
    meal_type: str = "other",
    log_date: str = "",
    calories: float | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Log a meal or food entry.

    Args:
        description: What was eaten.
        meal_type: One of breakfast, lunch, dinner, snack, other.
        log_date: YYYY-MM-DD date. Defaults to today.
        calories: Optional calories.
        protein_g: Optional protein grams.
        carbs_g: Optional carbohydrate grams.
        fat_g: Optional fat grams.
        notes: Optional context.
    """
    description = description.strip()
    if not description:
        return {"error": "description is required"}
    meal_type = (meal_type or "other").strip().lower()
    if meal_type not in VALID_MEAL_TYPES:
        return {"error": f"meal_type must be one of: {', '.join(sorted(VALID_MEAL_TYPES))}"}
    try:
        log_date = _require_date(log_date)
        cals = _numeric(calories, "calories", minimum=0)
        protein = _numeric(protein_g, "protein_g", minimum=0)
        carbs = _numeric(carbs_g, "carbs_g", minimum=0)
        fat = _numeric(fat_g, "fat_g", minimum=0)
    except ValueError as exc:
        return {"error": str(exc)}

    entry = _add_log(
        "meal",
        log_date,
        {
            "meal_type": meal_type,
            "description": description,
            "calories": cals,
            "protein_g": protein,
            "carbs_g": carbs,
            "fat_g": fat,
            "notes": notes.strip(),
        },
    )
    return {"logged": True, "entry": entry}


@mcp.tool()
def log_measurement(
    weight_kg: float | None = None,
    log_date: str = "",
    body_fat_pct: float | None = None,
    waist_cm: float | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Log a body measurement or weigh-in.

    Args:
        weight_kg: Weight in kilograms.
        log_date: YYYY-MM-DD date. Defaults to today.
        body_fat_pct: Optional body-fat percentage.
        waist_cm: Optional waist measurement in centimeters.
        notes: Optional context.
    """
    try:
        log_date = _require_date(log_date)
        weight = _numeric(weight_kg, "weight_kg", minimum=0)
        body_fat = _numeric(body_fat_pct, "body_fat_pct", minimum=0, maximum=100) if body_fat_pct is not None else None
        waist = _numeric(waist_cm, "waist_cm", minimum=0)
    except ValueError as exc:
        return {"error": str(exc)}
    if weight is None and body_fat is None and waist is None:
        return {"error": "Provide at least one measurement: weight_kg, body_fat_pct, or waist_cm"}

    entry = _add_log(
        "measurement",
        log_date,
        {
            "weight_kg": weight,
            "body_fat_pct": body_fat,
            "waist_cm": waist,
            "notes": notes.strip(),
        },
    )
    return {"logged": True, "entry": entry}


@mcp.tool()
def list_logs(log_type: str = "", from_date: str = "", to_date: str = "", limit: int = 50) -> dict[str, Any]:
    """List health logs, newest first.

    Args:
        log_type: Optional type: habit, workout, meal, measurement.
        from_date: Optional inclusive YYYY-MM-DD start date.
        to_date: Optional inclusive YYYY-MM-DD end date.
        limit: Max entries to return (1-500).
    """
    valid_types = {"habit", "workout", "meal", "measurement"}
    log_type = (log_type or "").strip().lower()
    if log_type and log_type not in valid_types:
        return {"error": f"log_type must be one of: {', '.join(sorted(valid_types))}"}
    try:
        limit = max(1, min(int(limit), 500))
        if from_date:
            from_date = _require_date(from_date)
        if to_date:
            to_date = _require_date(to_date)
        if from_date and to_date and from_date > to_date:
            return {"error": "from_date cannot be after to_date"}
    except ValueError as exc:
        return {"error": str(exc)}

    logs = _load_logs()
    if log_type:
        logs = [item for item in logs if item["type"] == log_type]
    if from_date:
        logs = [item for item in logs if item.get("log_date", "") >= from_date]
    if to_date:
        logs = [item for item in logs if item.get("log_date", "") <= to_date]
    return {"data_dir": str(DATA_DIR), "count_returned": len(logs[:limit]), "logs": logs[:limit]}


@mcp.tool()
def delete_log(log_id: str) -> dict[str, Any]:
    """Delete a log entry by ID.

    Args:
        log_id: Log ID from list_logs or a log response.
    """
    logs = _load_logs()
    remaining = [item for item in logs if item.get("id") != log_id]
    if len(remaining) == len(logs):
        return {"deleted": False, "error": f"Log not found: {log_id}"}
    _write_logs(remaining)
    return {"deleted": True, "log_id": log_id}


@mcp.tool()
def save_health_note(content: str, append: bool = True) -> dict[str, Any]:
    """Save a Markdown health/fitness note.

    Args:
        content: Markdown/text content.
        append: true to append; false to overwrite.
    """
    if not content.strip():
        return {"error": "content is required"}
    if append and NOTES_PATH.exists():
        existing = NOTES_PATH.read_text(encoding="utf-8").rstrip()
        content = f"{existing}\n\n---\n\n{content.lstrip()}"
    NOTES_PATH.write_text(content, encoding="utf-8")
    return {"saved": True, "path": str(NOTES_PATH), "bytes": NOTES_PATH.stat().st_size}


def _logs_for_date(target: date) -> list[dict[str, Any]]:
    key = target.isoformat()
    return [item for item in _load_logs() if item.get("log_date") == key]


def _sum_metric(logs: list[dict[str, Any]], log_type: str, field: str) -> float:
    total = 0.0
    for item in logs:
        if item["type"] == log_type and item.get(field) is not None:
            total += float(item[field])
    return total


def _habit_day_completions(logs: list[dict[str, Any]]) -> set[str]:
    return {item["habit_id"] for item in logs if item["type"] == "habit" and float(item.get("value", 0)) > 0}


def _calculate_streaks(through: date) -> list[dict[str, Any]]:
    all_logs = _load_logs()
    habits = _load_habits()
    streaks = []
    for habit in habits:
        if not habit.get("active", True):
            continue
        current = 0
        check = through
        while True:
            day_logs = [item for item in all_logs if item.get("log_date") == check.isoformat()]
            completed = any(
                item["type"] == "habit"
                and item.get("habit_id") == habit["id"]
                and float(item.get("value", 0)) > 0
                for item in day_logs
            )
            if completed:
                current += 1
                check -= timedelta(days=1)
            else:
                break
        streaks.append(
            {
                "habit_id": habit["id"],
                "habit_name": habit["name"],
                "current_daily_streak": current,
                "target_per_week": habit["target_per_week"],
            }
        )
    return sorted(streaks, key=lambda item: item["current_daily_streak"], reverse=True)


@mcp.tool()
def get_daily_summary(for_date: str = "") -> dict[str, Any]:
    """Get a one-day health summary.

    Args:
        for_date: YYYY-MM-DD date. Defaults to today.
    """
    try:
        target = date.fromisoformat(for_date or _today())
    except ValueError:
        return {"error": "for_date must use YYYY-MM-DD format"}

    logs = _logs_for_date(target)
    workouts = [item for item in logs if item["type"] == "workout"]
    meals = [item for item in logs if item["type"] == "meal"]
    measurements = [item for item in logs if item["type"] == "measurement"]
    completed_habit_ids = _habit_day_completions(logs)
    habits = [h for h in _load_habits() if h.get("active", True)]

    return {
        "date": target.isoformat(),
        "data_dir": str(DATA_DIR),
        "totals": {
            "workouts": len(workouts),
            "workout_minutes": _sum_metric(logs, "workout", "duration_minutes"),
            "calories_burned": _sum_metric(logs, "workout", "calories"),
            "meals": len(meals),
            "calories_eaten": _sum_metric(logs, "meal", "calories"),
            "protein_g": _sum_metric(logs, "meal", "protein_g"),
            "carbs_g": _sum_metric(logs, "meal", "carbs_g"),
            "fat_g": _sum_metric(logs, "meal", "fat_g"),
        },
        "habits_completed": len(completed_habit_ids),
        "habits_total": len(habits),
        "incomplete_habits": [h["name"] for h in habits if h["id"] not in completed_habit_ids],
        "measurements": measurements,
        "logs": logs,
    }


@mcp.tool()
def get_weekly_report(for_date: str = "") -> dict[str, Any]:
    """Get a 7-day report ending on for_date, including streaks.

    Args:
        for_date: YYYY-MM-DD end date. Defaults to today.
    """
    try:
        end = date.fromisoformat(for_date or _today())
    except ValueError:
        return {"error": "for_date must use YYYY-MM-DD format"}
    start = end - timedelta(days=6)

    week_logs = [
        item
        for item in _load_logs()
        if start.isoformat() <= item.get("log_date", "") <= end.isoformat()
    ]
    workouts = [item for item in week_logs if item["type"] == "workout"]
    meals = [item for item in week_logs if item["type"] == "meal"]

    habits = [h for h in _load_habits() if h.get("active", True)]
    habit_counts = []
    for habit in habits:
        count = sum(
            1
            for item in week_logs
            if item["type"] == "habit" and item.get("habit_id") == habit["id"] and float(item.get("value", 0)) > 0
        )
        weekly_target = int(habit.get("target_per_week", 7))
        habit_counts.append(
            {
                "habit_id": habit["id"],
                "habit_name": habit["name"],
                "completed_days": count,
                "target_per_week": weekly_target,
                "on_track": count >= weekly_target,
            }
        )

    latest_measurement = None
    for item in _load_logs():
        if item["type"] == "measurement" and item.get("log_date", "") <= end.isoformat():
            latest_measurement = item
            break

    calories_eaten = _sum_metric(week_logs, "meal", "calories")
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "data_dir": str(DATA_DIR),
        "totals": {
            "workouts": len(workouts),
            "workout_minutes": _sum_metric(week_logs, "workout", "duration_minutes"),
            "calories_burned": _sum_metric(week_logs, "workout", "calories"),
            "distance_km": _sum_metric(week_logs, "workout", "distance_km"),
            "meals_logged": len(meals),
            "calories_eaten": calories_eaten,
            "avg_daily_calories_eaten": round(calories_eaten / 7, 1) if calories_eaten else 0,
            "protein_g": _sum_metric(week_logs, "meal", "protein_g"),
        },
        "habits": sorted(habit_counts, key=lambda item: (item["on_track"], item["completed_days"]), reverse=True),
        "streaks": _calculate_streaks(end),
        "latest_measurement": latest_measurement,
    }


@mcp.resource("health://data-directory")
def data_directory() -> str:
    """Return the health tracker data directory."""
    return str(DATA_DIR)


if __name__ == "__main__":
    mcp.run(transport="stdio")
