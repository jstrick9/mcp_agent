#!/usr/bin/env python3
"""MCP flashcards and spaced-repetition server.

Tools:
  - create_deck: create a study deck
  - list_decks: list decks with card and due counts
  - delete_deck: delete a deck and its cards
  - add_card: add a card (front/back, optional tags)
  - edit_card: update a card
  - delete_card: delete a card
  - list_cards: list cards, optionally filtered by deck
  - get_due_cards: cards to study now (new cards plus cards that came due)
  - record_review: grade your recall with SM-2 and reschedule the card
  - get_review_session: stats for one study session
  - get_stats: overall progress, retention, streak, and per-deck breakdown

Scheduling uses the SM-2 algorithm (SuperMemo 2), the same family of
algorithms Anki is built on.

Data is stored under MCP_FLASHCARDS_DIR (default: ~/MCPFlashcards).
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

DATA_DIR = Path(os.environ.get("MCP_FLASHCARDS_DIR", str(Path.home() / "MCPFlashcards"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DECKS_PATH = DATA_DIR / "decks.json"
CARDS_PATH = DATA_DIR / "cards.json"
REVIEWS_PATH = DATA_DIR / "reviews.json"

# SM-2 constants.
DEFAULT_EASE = 2.5
MIN_EASE = 1.3
PASS_QUALITY = 3  # SM-2 treats quality < 3 as a lapse.

mcp = FastMCP("flashcards")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._ -]+", "-", value.strip()).strip(".-")
    slug = re.sub(r"[-_ ]+", "-", slug)
    return slug[:80] or "deck"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_decks() -> list[dict[str, Any]]:
    return _load_json(DECKS_PATH, [])


def _write_decks(decks: list[dict[str, Any]]) -> None:
    _write_json(DECKS_PATH, decks)


def _load_cards() -> list[dict[str, Any]]:
    return _load_json(CARDS_PATH, [])


def _write_cards(cards: list[dict[str, Any]]) -> None:
    _write_json(CARDS_PATH, cards)


def _load_reviews() -> list[dict[str, Any]]:
    return _load_json(REVIEWS_PATH, [])


def _write_reviews(reviews: list[dict[str, Any]]) -> None:
    _write_json(REVIEWS_PATH, reviews)


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _normalize_tags(tags: Any) -> list[str]:
    if not tags:
        return []
    raw = re.split(r"[,;]+", tags) if isinstance(tags, str) else [str(t) for t in tags]
    cleaned: list[str] = []
    for tag in raw:
        tag = tag.strip().lower().strip("#")
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return cleaned[:20]


def _split_tags(value: str) -> list[str]:
    return [t for t in (value or "").split(",") if t]


def _find_deck(decks: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    slug = _slugify(name)
    for deck in decks:
        if deck["id"] == name or deck["slug"] == slug or deck["name"].lower() == (name or "").strip().lower():
            return deck
    return None


def _find_card(cards: list[dict[str, Any]], card_id: str) -> dict[str, Any] | None:
    for card in cards:
        if card["id"] == card_id:
            return card
    return None


def _deck_counts(cards: list[dict[str, Any]], reviews: list[dict[str, Any]], deck_id: str) -> dict[str, int]:
    deck_cards = [c for c in cards if c["deck_id"] == deck_id]
    today = _today()
    due = sum(1 for c in deck_cards if c.get("due_date") and c["due_date"] <= today)
    new = sum(1 for c in deck_cards if not c.get("last_reviewed"))
    review_count = sum(1 for r in reviews if r.get("deck_id") == deck_id)
    return {"cards": len(deck_cards), "due": due, "new": new, "reviews": review_count}


def _sm2(quality: int, repetitions: int, ease_factor: float, interval: int) -> dict[str, Any]:
    """Apply one step of SM-2. ease_factor is the 2.5-style multiplier."""
    quality = max(0, min(5, int(quality)))

    if quality < PASS_QUALITY:
        repetitions = 0
        interval = 1
    else:
        repetitions += 1
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 6
        else:
            interval = max(1, round(interval * ease_factor))

    new_ease = ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ease = max(MIN_EASE, round(new_ease, 4))

    return {"repetitions": repetitions, "interval": interval, "ease_factor": new_ease, "quality": quality}


def _calculate_streak(reviews: list[dict[str, Any]], today: date) -> int:
    """Consecutive days with at least one review, ending today or yesterday."""
    days = {r["review_date"] for r in reviews if r.get("review_date")}
    if not days:
        return 0

    cursor = today
    # A streak is still alive if you studied yesterday but not yet today.
    if cursor.isoformat() not in days:
        cursor = today - timedelta(days=1)
        if cursor.isoformat() not in days:
            return 0

    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# --------------------------------------------------------------------------- #
# Deck tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def create_deck(name: str, description: str = "", tags: str = "") -> dict[str, Any]:
    """Create a study deck to hold flashcards.

    Args:
        name: Deck name, such as 'Spanish vocab' or 'MCP concepts'.
        description: Optional description of what this deck covers.
        tags: Optional comma-separated tags.
    """
    try:
        name = (name or "").strip()
        if not name:
            return {"created": False, "error": "name must not be empty."}

        decks = _load_decks()
        if _find_deck(decks, name):
            return {"created": False, "error": f"A deck named '{name}' already exists."}

        now = _now()
        deck = {
            "id": f"deck-{uuid.uuid4().hex[:8]}",
            "name": name,
            "slug": _slugify(name),
            "description": (description or "").strip(),
            "tags": ",".join(_normalize_tags(tags)),
            "created_at": now,
        }
        decks.append(deck)
        _write_decks(decks)
        return {"created": True, "deck": deck, "data_dir": str(DATA_DIR)}
    except OSError as exc:
        return {"created": False, "error": f"could not write data: {exc}"}


@mcp.tool()
def list_decks() -> dict[str, Any]:
    """List every deck with its card count, due count, and review total."""
    try:
        decks = _load_decks()
        cards = _load_cards()
        reviews = _load_reviews()
        return {
            "count": len(decks),
            "today": _today(),
            "data_dir": str(DATA_DIR),
            "decks": [
                {**deck, "tags": _split_tags(deck["tags"]), **_deck_counts(cards, reviews, deck["id"])}
                for deck in decks
            ],
        }
    except OSError as exc:
        return {"count": 0, "decks": [], "error": f"could not read data: {exc}"}


@mcp.tool()
def delete_deck(deck: str) -> dict[str, Any]:
    """Delete a deck together with all of its cards. Review history is kept.

    Args:
        deck: Deck name or ID.
    """
    try:
        decks = _load_decks()
        target = _find_deck(decks, deck or "")
        if not target:
            return {"deleted": False, "error": f"No deck matching '{deck}'."}

        cards = _load_cards()
        removed_cards = sum(1 for c in cards if c["deck_id"] == target["id"])
        _write_cards([c for c in cards if c["deck_id"] != target["id"]])
        _write_decks([d for d in decks if d["id"] != target["id"]])
        return {"deleted": True, "deck": target["name"], "cards_removed": removed_cards}
    except OSError as exc:
        return {"deleted": False, "error": f"could not write data: {exc}"}


# --------------------------------------------------------------------------- #
# Card tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def add_card(
    deck: str,
    front: str,
    back: str,
    tags: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add a flashcard to a deck. The deck is created if it does not exist.

    Args:
        deck: Deck name or ID. Created automatically if missing.
        front: The question or prompt shown first.
        back: The answer revealed after you attempt recall.
        tags: Optional comma-separated tags.
        notes: Optional extra context, mnemonics, or a source.
    """
    try:
        front = (front or "").strip()
        back = (back or "").strip()
        if not front or not back:
            return {"created": False, "error": "Both front and back are required."}

        decks = _load_decks()
        target = _find_deck(decks, deck or "")
        if not target:
            deck_name = (deck or "").strip()
            if not deck_name:
                return {"created": False, "error": "deck must not be empty."}
            target = {
                "id": f"deck-{uuid.uuid4().hex[:8]}",
                "name": deck_name,
                "slug": _slugify(deck_name),
                "description": "",
                "tags": "",
                "created_at": _now(),
            }
            decks.append(target)
            _write_decks(decks)

        now = _now()
        card = {
            "id": f"card-{uuid.uuid4().hex[:8]}",
            "deck_id": target["id"],
            "deck_name": target["name"],
            "front": front,
            "back": back,
            "tags": ",".join(_normalize_tags(tags)),
            "notes": (notes or "").strip(),
            "repetitions": 0,
            "interval": 0,
            "ease_factor": DEFAULT_EASE,
            "lapses": 0,
            "last_reviewed": "",
            "due_date": _today(),
            "created_at": now,
            "updated_at": now,
        }
        cards = _load_cards()
        cards.append(card)
        _write_cards(cards)
        return {"created": True, "card": {**card, "tags": _split_tags(card["tags"])}, "data_dir": str(DATA_DIR)}
    except OSError as exc:
        return {"created": False, "error": f"could not write data: {exc}"}


@mcp.tool()
def edit_card(
    card_id: str,
    front: str = "",
    back: str = "",
    tags: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Update a card's text, tags, or notes. Only supplied fields change.

    Args:
        card_id: Card ID such as 'card-1a2b3c4d'.
        front: New question text. Omit to keep the current one.
        back: New answer text. Omit to keep the current one.
        tags: New comma-separated tags. Omit to keep the current ones.
        notes: New notes. Omit to keep the current ones.
    """
    try:
        cards = _load_cards()
        card = _find_card(cards, (card_id or "").strip())
        if not card:
            return {"updated": False, "error": f"No card with id '{card_id}'."}

        if front.strip():
            card["front"] = front.strip()
        if back.strip():
            card["back"] = back.strip()
        if tags.strip():
            card["tags"] = ",".join(_normalize_tags(tags))
        if notes.strip():
            card["notes"] = notes.strip()
        card["updated_at"] = _now()

        _write_cards(cards)
        return {"updated": True, "card": {**card, "tags": _split_tags(card["tags"])}}
    except OSError as exc:
        return {"updated": False, "error": f"could not write data: {exc}"}


@mcp.tool()
def delete_card(card_id: str) -> dict[str, Any]:
    """Delete a card by ID.

    Args:
        card_id: Card ID such as 'card-1a2b3c4d'.
    """
    try:
        cards = _load_cards()
        card = _find_card(cards, (card_id or "").strip())
        if not card:
            return {"deleted": False, "error": f"No card with id '{card_id}'."}
        _write_cards([c for c in cards if c["id"] != card["id"]])
        return {"deleted": True, "id": card["id"], "front": card["front"][:80]}
    except OSError as exc:
        return {"deleted": False, "error": f"could not write data: {exc}"}


@mcp.tool()
def list_cards(deck: str = "", tag: str = "", limit: int = 50, due_only: bool = False) -> dict[str, Any]:
    """List cards, optionally filtered by deck, tag, or due status.

    Args:
        deck: Optional deck name or ID to filter by.
        tag: Optional tag to filter by.
        limit: Maximum cards to return, from 1 to 500.
        due_only: If true, return only cards that are due today or overdue.
    """
    try:
        limit = max(1, min(int(limit or 50), 500))
        tag = (tag or "").strip().lower().strip("#")
        today = _today()

        decks = _load_decks()
        cards = _load_cards()

        deck_id = ""
        if deck.strip():
            target = _find_deck(decks, deck)
            if not target:
                return {"count": 0, "cards": [], "error": f"No deck matching '{deck}'."}
            deck_id = target["id"]

        selected = []
        for card in cards:
            if deck_id and card["deck_id"] != deck_id:
                continue
            if tag and tag not in _split_tags(card["tags"]):
                continue
            if due_only and not (card.get("due_date") and card["due_date"] <= today):
                continue
            selected.append(card)

        selected.sort(key=lambda c: (c.get("due_date") or "9999-99-99", c["created_at"]))
        return {
            "count": len(selected[:limit]),
            "total_matching": len(selected),
            "deck": deck,
            "tag": tag,
            "due_only": due_only,
            "cards": [{**c, "tags": _split_tags(c["tags"])} for c in selected[:limit]],
        }
    except OSError as exc:
        return {"count": 0, "cards": [], "error": f"could not read data: {exc}"}


# --------------------------------------------------------------------------- #
# Review tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_due_cards(deck: str = "", limit: int = 10, include_back: bool = False) -> dict[str, Any]:
    """Get the cards to study right now: new cards plus cards that came due.

    Show the user each front, wait for their answer, then call record_review.

    Args:
        deck: Optional deck name or ID. Omit to study across all decks.
        limit: Maximum cards to return, from 1 to 100.
        include_back: If false, answers are omitted so you can test recall first.
    """
    try:
        limit = max(1, min(int(limit or 10), 100))
        today = _today()

        decks = _load_decks()
        cards = _load_cards()

        deck_id = ""
        if deck.strip():
            target = _find_deck(decks, deck)
            if not target:
                return {"count": 0, "cards": [], "error": f"No deck matching '{deck}'."}
            deck_id = target["id"]

        due = [c for c in cards if (not deck_id or c["deck_id"] == deck_id) and c.get("due_date") and c["due_date"] <= today]

        # New cards first, then overdue, then the rest by due date.
        due.sort(key=lambda c: (bool(c.get("last_reviewed")), c["due_date"], c["created_at"]))
        due = due[:limit]

        payload = []
        for card in due:
            entry = {
                "id": card["id"],
                "deck_name": card["deck_name"],
                "front": card["front"],
                "tags": _split_tags(card["tags"]),
                "repetitions": card["repetitions"],
                "is_new": not card.get("last_reviewed"),
                "due_date": card["due_date"],
            }
            if include_back:
                entry["back"] = card["back"]
                entry["notes"] = card["notes"]
            payload.append(entry)

        return {
            "count": len(payload),
            "today": today,
            "answers_hidden": not include_back,
            "data_dir": str(DATA_DIR),
            "cards": payload,
        }
    except OSError as exc:
        return {"count": 0, "cards": [], "error": f"could not read data: {exc}"}


@mcp.tool()
def record_review(
    card_id: str,
    quality: int,
    seconds_spent: int = 0,
    session_id: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Grade your recall of a card and reschedule it using SM-2.

    Args:
        card_id: Card ID such as 'card-1a2b3c4d'.
        quality: Recall quality from 0 to 5. 0 = complete blackout,
            3 = correct with serious difficulty, 5 = perfect recall.
            Below 3 counts as a lapse and restarts the card.
        seconds_spent: Optional time spent on this card.
        session_id: Optional session ID from get_due_cards grouping.
        notes: Optional note about this review.
    """
    try:
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            return {"reviewed": False, "error": "quality must be an integer from 0 to 5."}
        if not 0 <= quality <= 5:
            return {"reviewed": False, "error": "quality must be between 0 and 5."}

        cards = _load_cards()
        card = _find_card(cards, (card_id or "").strip())
        if not card:
            return {"reviewed": False, "error": f"No card with id '{card_id}'."}

        before = {
            "repetitions": card["repetitions"],
            "interval": card["interval"],
            "ease_factor": card["ease_factor"],
        }
        result = _sm2(quality, card["repetitions"], card["ease_factor"], card["interval"])

        today = date.today()
        card["repetitions"] = result["repetitions"]
        card["interval"] = result["interval"]
        card["ease_factor"] = result["ease_factor"]
        card["last_reviewed"] = today.isoformat()
        card["due_date"] = (today + timedelta(days=result["interval"])).isoformat()
        if quality < PASS_QUALITY:
            card["lapses"] = card.get("lapses", 0) + 1
        card["updated_at"] = _now()
        _write_cards(cards)

        reviews = _load_reviews()
        review = {
            "id": f"rev-{uuid.uuid4().hex[:8]}",
            "card_id": card["id"],
            "deck_id": card["deck_id"],
            "quality": quality,
            "passed": quality >= PASS_QUALITY,
            "seconds_spent": max(0, int(seconds_spent or 0)),
            "session_id": (session_id or "").strip(),
            "notes": (notes or "").strip(),
            "review_date": today.isoformat(),
            "created_at": _now(),
        }
        reviews.append(review)
        _write_reviews(reviews)

        return {
            "reviewed": True,
            "card_id": card["id"],
            "front": card["front"][:80],
            "quality": quality,
            "passed": quality >= PASS_QUALITY,
            "before": before,
            "after": {
                "repetitions": card["repetitions"],
                "interval": card["interval"],
                "ease_factor": card["ease_factor"],
            },
            "next_due": card["due_date"],
            "lapses": card.get("lapses", 0),
        }
    except (OSError, ValueError) as exc:
        return {"reviewed": False, "error": f"could not save review: {exc}"}


@mcp.tool()
def get_review_session(session_id: str = "", for_date: str = "") -> dict[str, Any]:
    """Summarize a study session, or everything reviewed on a given date.

    Args:
        session_id: Optional session ID to summarize.
        for_date: Optional YYYY-MM-DD date. Defaults to today when omitted.
    """
    try:
        for_date = (for_date or "").strip() or _today()
        if not _valid_date(for_date):
            return {"error": "for_date must use YYYY-MM-DD format."}

        reviews = [r for r in _load_reviews() if r["review_date"] == for_date]
        if session_id.strip():
            reviews = [r for r in reviews if r.get("session_id") == session_id.strip()]

        cards = {c["id"]: c for c in _load_cards()}
        if not reviews:
            return {"date": for_date, "session_id": session_id, "reviewed_cards": 0, "reviews": []}

        qualities = [r["quality"] for r in reviews]
        passed = sum(1 for r in reviews if r["passed"])
        unique_cards = len({r["card_id"] for r in reviews})

        return {
            "date": for_date,
            "session_id": session_id,
            "reviews": len(reviews),
            "reviewed_cards": unique_cards,
            "passed": passed,
            "lapsed": len(reviews) - passed,
            "accuracy_pct": round(100 * passed / len(reviews), 1),
            "avg_quality": round(sum(qualities) / len(qualities), 2),
            "total_seconds": sum(r["seconds_spent"] for r in reviews),
            "next_due": sorted(
                {cards[r["card_id"]]["due_date"] for r in reviews if r["card_id"] in cards}
            )[:5],
        }
    except OSError as exc:
        return {"error": f"could not read data: {exc}"}


@mcp.tool()
def get_stats(deck: str = "") -> dict[str, Any]:
    """Overall progress: totals, retention, streak, and a per-deck breakdown.

    Args:
        deck: Optional deck name or ID to scope the breakdown to.
    """
    try:
        decks = _load_decks()
        cards = _load_cards()
        reviews = _load_reviews()
        today = date.today()

        deck_id = ""
        if deck.strip():
            target = _find_deck(decks, deck)
            if not target:
                return {"error": f"No deck matching '{deck}'."}
            deck_id = target["id"]
            cards = [c for c in cards if c["deck_id"] == deck_id]
            reviews = [r for r in reviews if r["deck_id"] == deck_id]
            decks = [target]

        passed = sum(1 for r in reviews if r["passed"])
        due = sum(1 for c in cards if c.get("due_date") and c["due_date"] <= today.isoformat())
        new = sum(1 for c in cards if not c.get("last_reviewed"))
        mature = sum(1 for c in cards if c["interval"] >= 21)

        hardest = sorted(
            (c for c in cards if c.get("lapses", 0) > 0),
            key=lambda c: -c["lapses"],
        )[:5]

        return {
            "today": today.isoformat(),
            "scope": deck or "all decks",
            "data_dir": str(DATA_DIR),
            "decks": len(decks),
            "cards": len(cards),
            "due_today": due,
            "new_cards": new,
            "mature_cards": mature,
            "total_reviews": len(reviews),
            "retention_pct": round(100 * passed / len(reviews), 1) if reviews else None,
            "avg_ease_factor": round(sum(c["ease_factor"] for c in cards) / len(cards), 3) if cards else None,
            "current_streak_days": _calculate_streak(_load_reviews(), today),
            "hardest_cards": [
                {"id": c["id"], "front": c["front"][:60], "lapses": c["lapses"], "deck": c["deck_name"]}
                for c in hardest
            ],
            "by_deck": [
                {
                    "id": d["id"],
                    "name": d["name"],
                    **_deck_counts(_load_cards(), _load_reviews(), d["id"]),
                }
                for d in decks
            ],
        }
    except OSError as exc:
        return {"error": f"could not read data: {exc}"}


@mcp.resource("flashcards://data-directory")
def data_directory() -> str:
    """Return the flashcards data directory."""
    return str(DATA_DIR)


if __name__ == "__main__":
    mcp.run(transport="stdio")
