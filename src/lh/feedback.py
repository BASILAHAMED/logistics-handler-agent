"""Feedback loop #2 (human-in-the-loop learning).

`record_feedback` stores a rating + comment after a recommendation. Low ratings become
"lessons" that are injected into the planner/reviewer instructions on the NEXT request,
so the system measurably changes behaviour from user feedback (persisted in SQLite,
survives restarts). Feedback loop #1 is the planner <-> reviewer LoopAgent in agent.py.
"""

from __future__ import annotations

from google.adk.tools import ToolContext

from lh import db


def record_feedback(rating: int, comment: str, topic: str = "general", shipment_id: str = "", tool_context: ToolContext = None) -> dict:
    """Save the user's feedback on a plan or answer. rating 1-5; topic e.g. 'carrier choice',
    'cost', 'speed', 'communication'. Call whenever the user praises, complains or corrects you."""
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return {"status": "error", "error": "rating must be an integer 1-5"}
    if not 1 <= rating <= 5:
        return {"status": "error", "error": "rating must be between 1 and 5", "hint": "map 'bad' to 2, 'great' to 5"}
    lesson = (f"AVOID: {comment}" if rating <= 2 else f"ADJUST: {comment}" if rating == 3 else f"KEEP: {comment}")
    with db.connect() as con:
        cur = con.execute("INSERT INTO feedback(at,rating,comment,topic,shipment_id,lesson) VALUES(?,?,?,?,?,?)",
                          (db.now_iso(), rating, comment, topic, shipment_id, lesson))
        avg = con.execute("SELECT ROUND(AVG(rating),2), COUNT(*) FROM feedback").fetchone()
    if tool_context is not None:
        tool_context.state["user:last_feedback"] = lesson
    return {"status": "success", "feedback_id": cur.lastrowid, "lesson_learned": lesson,
            "avg_rating": avg[0], "total_feedback": avg[1],
            "effect": "this lesson is now part of every future plan and review"}


def lessons(limit: int = 6) -> list[str]:
    with db.connect() as con:
        return [r[0] for r in con.execute("SELECT lesson FROM feedback ORDER BY id DESC LIMIT ?", (limit,))]


def lessons_block() -> str:
    ls = lessons()
    if not ls:
        return "No user feedback recorded yet."
    return "Lessons from past user feedback (newest first, MUST be respected):\n" + "\n".join(f"- {x}" for x in ls)


def with_lessons(base: str):
    """Instruction provider: base prompt + live lessons, re-read on every model call."""

    def _provider(ctx) -> str:  # ReadonlyContext
        return base.replace("{LESSONS}", lessons_block())

    return _provider
