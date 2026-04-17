import os
import secrets
import hashlib
import base64
import time
from contextlib import asynccontextmanager
from typing import Optional
from collections import defaultdict

import psycopg2
import psycopg2.extras
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import FastMCP

# ── Config ───────────────────────────────────────────────────────────────────

DATABASE_URL        = os.environ["DATABASE_URL"]
PHILOSOPHY_PATH     = os.environ.get("PHILOSOPHY_PATH", "/data/philosophy.md")
ADMIN_PASSWORD      = os.environ["ADMIN_PASSWORD"]
OAUTH_CLIENT_ID     = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
SERVER_URL          = os.environ.get("SERVER_URL", "http://localhost:8000").rstrip("/")

# ── In-memory OAuth state (resets on restart — Claude re-auths automatically) ─

_auth_codes: dict = {}   # code -> {client_id, redirect_uri, code_challenge, exp}
_tokens: set = set()     # valid bearer tokens

# ── Database helper ───────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def resolve_exercise(cur, name: str) -> tuple[int, str]:
    """Return (exercise_id, canonical_name), creating the exercise if needed."""
    cur.execute("SELECT id, name FROM exercises WHERE LOWER(name) = LOWER(%s)", (name,))
    row = cur.fetchone()
    if row:
        return row[0], row[1]

    cur.execute(
        "SELECT id, name FROM exercises WHERE LOWER(name) LIKE %s",
        (f"%{name.lower()}%",),
    )
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0][0], rows[0][1]

    cur.execute("INSERT INTO exercises (name) VALUES (%s) RETURNING id", (name,))
    new_id = cur.fetchone()[0]
    return new_id, name


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("GymBuddy")


# Session tools

@mcp.tool()
def start_session(
    notes: Optional[str] = None,
    session_date: Optional[str] = None,
) -> dict:
    """
    Start a new workout session. Call this before logging any sets.
    Returns the session_id needed for log_set calls.
    session_date is ISO 8601 "YYYY-MM-DD"; defaults to today if omitted.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        if session_date:
            cur.execute(
                "INSERT INTO sessions (session_date, notes) VALUES (%s, %s) RETURNING id",
                (session_date, notes),
            )
        else:
            cur.execute(
                "INSERT INTO sessions (notes) VALUES (%s) RETURNING id",
                (notes,),
            )
        session_id = cur.fetchone()[0]
        conn.commit()
        return {"session_id": session_id, "session_date": session_date or "today"}
    finally:
        conn.close()


@mcp.tool()
def end_session(session_id: int, notes: Optional[str] = None) -> dict:
    """
    Finalise a workout session. Optionally append closing notes.
    Returns a summary: exercises hit, total sets, and total volume (weight * reps).
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        if notes:
            cur.execute(
                "UPDATE sessions SET notes = COALESCE(notes || ' | ' || %s, %s) WHERE id = %s",
                (notes, notes, session_id),
            )
        cur.execute(
            """
            SELECT
                COUNT(DISTINCT s.exercise_id) AS exercises,
                COUNT(*) AS total_sets,
                COALESCE(SUM(s.weight * s.reps), 0) AS total_volume
            FROM sets s
            WHERE s.session_id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "session_id": session_id,
            "exercises": row[0],
            "total_sets": row[1],
            "total_volume": float(row[2]),
        }
    finally:
        conn.close()


# Logging tools

@mcp.tool()
def log_set(
    session_id: int,
    exercise: str,
    weight: float,
    reps: int,
    notes: Optional[str] = None,
) -> dict:
    """
    Log a single set within an active session.
    exercise is matched case-insensitively; a new exercise is created if no match exists.
    Returns the set_id, resolved exercise name, and estimated e1RM.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        exercise_id, canonical_name = resolve_exercise(cur, exercise)

        cur.execute(
            "SELECT COALESCE(MAX(set_number), 0) FROM sets WHERE session_id = %s AND exercise_id = %s",
            (session_id, exercise_id),
        )
        set_number = cur.fetchone()[0] + 1

        cur.execute(
            "INSERT INTO sets (session_id, exercise_id, set_number, weight, reps, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (session_id, exercise_id, set_number, weight, reps, notes),
        )
        set_id = cur.fetchone()[0]
        conn.commit()

        e1rm = weight if reps == 1 else round(weight * (1 + reps / 30.0), 1)
        return {
            "set_id": set_id,
            "exercise": canonical_name,
            "set_number": set_number,
            "weight": weight,
            "reps": reps,
            "e1rm": e1rm,
        }
    finally:
        conn.close()


@mcp.tool()
def log_bodyweight(
    weight: float,
    measured_at: Optional[str] = None,
) -> dict:
    """
    Record a bodyweight measurement.
    measured_at is an ISO 8601 datetime string; defaults to now if omitted.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        if measured_at:
            cur.execute(
                "INSERT INTO bodyweight (weight, measured_at) VALUES (%s, %s) RETURNING id",
                (weight, measured_at),
            )
        else:
            cur.execute(
                "INSERT INTO bodyweight (weight) VALUES (%s) RETURNING id",
                (weight,),
            )
        entry_id = cur.fetchone()[0]
        conn.commit()
        return {"id": entry_id, "weight": weight}
    finally:
        conn.close()


@mcp.tool()
def log_workout(
    exercises: list,
    session_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Log a complete workout in one call — creates the session, logs all exercises and sets,
    and returns a summary.
    exercises must be a list of objects:
      [{"name": "Bench Press", "sets": [{"weight": 100, "reps": 8}, ...]}, ...]
    Each set may also include an optional "notes" field.
    session_date is ISO 8601 "YYYY-MM-DD"; defaults to today if omitted.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        if session_date:
            cur.execute(
                "INSERT INTO sessions (session_date, notes) VALUES (%s, %s) RETURNING id",
                (session_date, notes),
            )
        else:
            cur.execute(
                "INSERT INTO sessions (notes) VALUES (%s) RETURNING id",
                (notes,),
            )
        session_id = cur.fetchone()[0]

        total_sets = 0
        total_volume = 0.0
        logged_exercises = []

        for ex_data in exercises:
            exercise_id, canonical_name = resolve_exercise(cur, ex_data["name"])
            for i, s in enumerate(ex_data["sets"], start=1):
                w = float(s["weight"])
                reps = int(s["reps"])
                cur.execute(
                    "INSERT INTO sets (session_id, exercise_id, set_number, weight, reps, notes) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (session_id, exercise_id, i, w, reps, s.get("notes")),
                )
                total_volume += w * reps
                total_sets += 1
            logged_exercises.append({"exercise": canonical_name, "sets": len(ex_data["sets"])})

        conn.commit()
        return {
            "session_id": session_id,
            "session_date": session_date or "today",
            "exercises": logged_exercises,
            "total_sets": total_sets,
            "total_volume": round(total_volume, 1),
        }
    finally:
        conn.close()


# Query tools

@mcp.tool()
def get_recent_sessions(
    limit: int = 10,
    days: Optional[int] = None,
) -> list:
    """
    Return recent workout sessions with full set detail, ordered newest-first.
    Use this before planning a workout to understand recent training.
    """
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if days is not None:
            cur.execute(
                """
                SELECT id, session_date, notes
                FROM sessions
                WHERE session_date >= CURRENT_DATE - %s
                ORDER BY session_date DESC
                LIMIT %s
                """,
                (days, limit),
            )
        else:
            cur.execute(
                "SELECT id, session_date, notes FROM sessions ORDER BY session_date DESC LIMIT %s",
                (limit,),
            )
        sessions = cur.fetchall()
        result = []
        for sess in sessions:
            cur.execute(
                """
                SELECT exercise, set_number, weight, reps, e1rm, set_notes
                FROM set_history
                WHERE session_id = %s
                ORDER BY exercise, set_number
                """,
                (sess["id"],),
            )
            sets = cur.fetchall()
            result.append({
                "session_id": sess["id"],
                "session_date": str(sess["session_date"]),
                "notes": sess["notes"],
                "sets": [
                    {
                        "exercise": s["exercise"],
                        "set_number": s["set_number"],
                        "weight": float(s["weight"]),
                        "reps": s["reps"],
                        "e1rm": float(s["e1rm"]),
                        "notes": s["set_notes"],
                    }
                    for s in sets
                ],
            })
        return result
    finally:
        conn.close()


@mcp.tool()
def get_exercise_history(
    exercise: str,
    days: int = 90,
    limit: Optional[int] = None,
) -> list:
    """
    Return all logged sets for a specific exercise over the past N days, grouped by session date.
    """
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur2 = conn.cursor()
        cur2.execute(
            "SELECT id, name FROM exercises WHERE LOWER(name) = LOWER(%s)", (exercise,)
        )
        row = cur2.fetchone()
        if not row:
            cur2.execute(
                "SELECT id, name FROM exercises WHERE LOWER(name) LIKE %s",
                (f"%{exercise.lower()}%",),
            )
            rows = cur2.fetchall()
            if not rows:
                return []
            row = rows[0]
        canonical_name = row[1]

        query = """
            SELECT session_date, set_number, weight, reps, e1rm, set_notes
            FROM set_history
            WHERE exercise = %s AND session_date >= CURRENT_DATE - %s
            ORDER BY session_date, set_number
        """
        params = [canonical_name, days]
        if limit:
            query += " LIMIT %s"
            params.append(limit)

        cur.execute(query, params)
        rows = cur.fetchall()

        by_date = defaultdict(list)
        for r in rows:
            by_date[str(r["session_date"])].append({
                "set_number": r["set_number"],
                "weight": float(r["weight"]),
                "reps": r["reps"],
                "e1rm": float(r["e1rm"]),
                "notes": r["set_notes"],
            })

        return [
            {"date": date, "exercise": canonical_name, "sets": sets}
            for date, sets in sorted(by_date.items())
        ]
    finally:
        conn.close()


@mcp.tool()
def get_prs(
    exercise: Optional[str] = None,
    pr_type: str = "e1rm",
) -> list:
    """
    Return personal records.
    pr_type: 'e1rm' (estimated 1RM), 'weight' (heaviest lift), 'volume_session' (most weight*reps in one session).
    Omit exercise to get the top PR for every logged exercise.
    """
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if pr_type == "e1rm":
            if exercise:
                cur.execute(
                    "SELECT exercise, session_date, weight, reps, e1rm FROM set_history WHERE LOWER(exercise) = LOWER(%s) ORDER BY e1rm DESC LIMIT 1",
                    (exercise,),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT ON (exercise) exercise, session_date, weight, reps, e1rm FROM set_history ORDER BY exercise, e1rm DESC"
                )
        elif pr_type == "weight":
            if exercise:
                cur.execute(
                    "SELECT exercise, session_date, weight, reps, e1rm FROM set_history WHERE LOWER(exercise) = LOWER(%s) ORDER BY weight DESC LIMIT 1",
                    (exercise,),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT ON (exercise) exercise, session_date, weight, reps, e1rm FROM set_history ORDER BY exercise, weight DESC"
                )
        elif pr_type == "volume_session":
            if exercise:
                cur.execute(
                    "SELECT exercise, session_date, SUM(weight * reps) AS total_volume FROM set_history WHERE LOWER(exercise) = LOWER(%s) GROUP BY exercise, session_date ORDER BY total_volume DESC LIMIT 1",
                    (exercise,),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT ON (exercise) exercise, session_date, SUM(weight * reps) OVER (PARTITION BY exercise, session_date) AS total_volume FROM set_history ORDER BY exercise, total_volume DESC"
                )
        else:
            return [{"error": f"Unknown pr_type '{pr_type}'. Use 'e1rm', 'weight', or 'volume_session'."}]

        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@mcp.tool()
def get_volume_over_time(
    exercise: Optional[str] = None,
    days: int = 90,
    group_by: str = "week",
) -> list:
    """
    Return training volume (weight * reps) grouped by time period ('day', 'week', 'month').
    Omit exercise to aggregate across all exercises.
    """
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        trunc = {"day": "day", "week": "week", "month": "month"}.get(group_by, "week")

        if exercise:
            cur.execute(
                f"""
                SELECT DATE_TRUNC('{trunc}', session_date::timestamp)::date AS period,
                       SUM(weight * reps) AS volume
                FROM set_history
                WHERE LOWER(exercise) = LOWER(%s) AND session_date >= CURRENT_DATE - %s
                GROUP BY period ORDER BY period
                """,
                (exercise, days),
            )
        else:
            cur.execute(
                f"""
                SELECT DATE_TRUNC('{trunc}', session_date::timestamp)::date AS period,
                       SUM(weight * reps) AS volume
                FROM set_history
                WHERE session_date >= CURRENT_DATE - %s
                GROUP BY period ORDER BY period
                """,
                (days,),
            )
        return [{"period": str(r["period"]), "volume": float(r["volume"])} for r in cur.fetchall()]
    finally:
        conn.close()


@mcp.tool()
def get_bodyweight_history(days: int = 90) -> list:
    """
    Return bodyweight measurements over the last N days, ordered oldest-first.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT measured_at::date AS date, weight FROM bodyweight WHERE measured_at >= NOW() - INTERVAL '%s days' ORDER BY measured_at",
            (days,),
        )
        return [{"date": str(r[0]), "weight": float(r[1])} for r in cur.fetchall()]
    finally:
        conn.close()


@mcp.tool()
def search_exercises(query: str) -> list:
    """
    Search for exercises by name (case-insensitive substring). Returns name, total sets, last trained date.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.name, COUNT(s.id) AS total_sets, MAX(sess.session_date) AS last_trained
            FROM exercises e
            LEFT JOIN sets s ON s.exercise_id = e.id
            LEFT JOIN sessions sess ON sess.id = s.session_id
            WHERE LOWER(e.name) LIKE %s
            GROUP BY e.name ORDER BY total_sets DESC
            """,
            (f"%{query.lower()}%",),
        )
        return [
            {"exercise": r[0], "total_sets": r[1], "last_trained": str(r[2]) if r[2] else None}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


@mcp.tool()
def list_exercises(orderby: str = "frequency") -> list:
    """
    List all exercises ever logged with stats.
    orderby: 'frequency' (most sets), 'name' (alphabetical), 'last_trained' (most recent).
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        order_clause = {
            "frequency": "total_sets DESC",
            "name": "name ASC",
            "last_trained": "last_trained DESC NULLS LAST",
        }.get(orderby, "total_sets DESC")

        cur.execute(
            f"""
            SELECT e.name, COUNT(s.id) AS total_sets, MAX(sess.session_date) AS last_trained,
                   MAX(CASE WHEN s.reps = 1 THEN s.weight ELSE s.weight * (1 + s.reps / 30.0) END) AS best_e1rm
            FROM exercises e
            LEFT JOIN sets s ON s.exercise_id = e.id
            LEFT JOIN sessions sess ON sess.id = s.session_id
            GROUP BY e.name ORDER BY {order_clause}
            """
        )
        return [
            {
                "exercise": r[0],
                "total_sets": r[1],
                "last_trained": str(r[2]) if r[2] else None,
                "best_e1rm": round(float(r[3]), 1) if r[3] else None,
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


@mcp.tool()
def get_session_detail(session_id: int) -> dict:
    """
    Return full detail for a single session: date, notes, all sets, total volume.
    """
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, session_date, notes FROM sessions WHERE id = %s", (session_id,))
        sess = cur.fetchone()
        if not sess:
            return {"error": f"Session {session_id} not found."}

        cur.execute(
            "SELECT exercise, set_number, weight, reps, e1rm, set_notes FROM set_history WHERE session_id = %s ORDER BY exercise, set_number",
            (session_id,),
        )
        sets = cur.fetchall()
        total_volume = sum(float(s["weight"]) * s["reps"] for s in sets)

        return {
            "session_id": sess["id"],
            "session_date": str(sess["session_date"]),
            "notes": sess["notes"],
            "total_sets": len(sets),
            "total_volume": round(total_volume, 1),
            "sets": [
                {
                    "exercise": s["exercise"],
                    "set_number": s["set_number"],
                    "weight": float(s["weight"]),
                    "reps": s["reps"],
                    "e1rm": float(s["e1rm"]),
                    "notes": s["set_notes"],
                }
                for s in sets
            ],
        }
    finally:
        conn.close()


# Delete tools

@mcp.tool()
def delete_set(set_id: int) -> dict:
    """
    Delete a single set by set_id. Renumbers remaining sets for that exercise within the session.
    Use get_session_detail or get_recent_sessions to find set_ids.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT session_id, exercise_id FROM sets WHERE id = %s", (set_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Set {set_id} not found."}
        session_id, exercise_id = row
        cur.execute("DELETE FROM sets WHERE id = %s", (set_id,))
        cur.execute(
            "SELECT id FROM sets WHERE session_id = %s AND exercise_id = %s ORDER BY set_number",
            (session_id, exercise_id),
        )
        for i, (sid,) in enumerate(cur.fetchall(), start=1):
            cur.execute("UPDATE sets SET set_number = %s WHERE id = %s", (i, sid))
        conn.commit()
        return {"deleted_set_id": set_id}
    finally:
        conn.close()


@mcp.tool()
def delete_session(session_id: int) -> dict:
    """
    Delete a session and all its sets. This is irreversible.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT session_date FROM sessions WHERE id = %s", (session_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Session {session_id} not found."}
        cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        conn.commit()
        return {"deleted_session_id": session_id, "session_date": str(row[0])}
    finally:
        conn.close()


@mcp.tool()
def delete_exercise(exercise: str) -> dict:
    """
    Delete an exercise and all sets ever logged for it across all sessions.
    Use search_exercises first to confirm the exact name. This is irreversible.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM exercises WHERE LOWER(name) = LOWER(%s)", (exercise,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Exercise '{exercise}' not found."}
        exercise_id, canonical_name = row
        cur.execute("SELECT COUNT(*) FROM sets WHERE exercise_id = %s", (exercise_id,))
        set_count = cur.fetchone()[0]
        cur.execute("DELETE FROM sets WHERE exercise_id = %s", (exercise_id,))
        cur.execute("DELETE FROM exercises WHERE id = %s", (exercise_id,))
        conn.commit()
        return {"deleted_exercise": canonical_name, "sets_removed": set_count}
    finally:
        conn.close()


# Edit tools

@mcp.tool()
def update_set(
    set_id: int,
    weight: Optional[float] = None,
    reps: Optional[int] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Update weight, reps, or notes on an existing set. Only provided fields are changed.
    Returns the updated set.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT weight, reps, notes FROM sets WHERE id = %s", (set_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Set {set_id} not found."}
        new_weight = weight if weight is not None else float(row[0])
        new_reps   = reps   if reps   is not None else row[1]
        new_notes  = notes  if notes  is not None else row[2]
        cur.execute(
            "UPDATE sets SET weight = %s, reps = %s, notes = %s WHERE id = %s",
            (new_weight, new_reps, new_notes, set_id),
        )
        conn.commit()
        e1rm = new_weight if new_reps == 1 else round(new_weight * (1 + new_reps / 30.0), 1)
        return {"set_id": set_id, "weight": new_weight, "reps": new_reps, "notes": new_notes, "e1rm": e1rm}
    finally:
        conn.close()


@mcp.tool()
def update_session(
    session_id: int,
    notes: Optional[str] = None,
    session_date: Optional[str] = None,
) -> dict:
    """
    Update the notes or date of an existing session. Only provided fields are changed.
    session_date is ISO 8601 "YYYY-MM-DD".
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT session_date, notes FROM sessions WHERE id = %s", (session_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Session {session_id} not found."}
        new_date  = session_date if session_date is not None else str(row[0])
        new_notes = notes        if notes        is not None else row[1]
        cur.execute(
            "UPDATE sessions SET session_date = %s, notes = %s WHERE id = %s",
            (new_date, new_notes, session_id),
        )
        conn.commit()
        return {"session_id": session_id, "session_date": new_date, "notes": new_notes}
    finally:
        conn.close()


@mcp.tool()
def rename_exercise(exercise: str, new_name: str) -> dict:
    """
    Rename an exercise. All historical sets remain associated under the new name.
    exercise is matched case-insensitively.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM exercises WHERE LOWER(name) = LOWER(%s)", (exercise,))
        row = cur.fetchone()
        if not row:
            return {"error": f"Exercise '{exercise}' not found."}
        exercise_id, old_name = row
        cur.execute("UPDATE exercises SET name = %s WHERE id = %s", (new_name, exercise_id))
        conn.commit()
        return {"renamed": old_name, "to": new_name}
    finally:
        conn.close()


# Philosophy tools

@mcp.tool()
def get_training_philosophy() -> str:
    """
    Return the user's training philosophy document verbatim.
    """
    try:
        with open(PHILOSOPHY_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return "(No training philosophy file found.)"


@mcp.tool()
def update_training_philosophy(content: str) -> dict:
    """
    Overwrite the training philosophy document with new content.
    Always show the user what you're writing before calling this tool.
    """
    with open(PHILOSOPHY_PATH, "w") as f:
        f.write(content)
    return {"status": "ok", "bytes_written": len(content)}


# ── REST API endpoints (no auth required — access controlled by Tailscale) ────

async def api_get_templates(request: Request) -> JSONResponse:
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name FROM workout_templates ORDER BY sort_order, id")
        return JSONResponse({"templates": [dict(r) for r in cur.fetchall()]})
    finally:
        conn.close()


async def api_get_workout(request: Request) -> JSONResponse:
    try:
        template_id = int(request.path_params["template_id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "invalid template_id"}, status_code=400)

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT name FROM workout_templates WHERE id = %s", (template_id,))
        tmpl = cur.fetchone()
        if not tmpl:
            return JSONResponse({"error": "template not found"}, status_code=404)

        cur.execute(
            """
            SELECT te.position, te.default_sets, te.target_reps_min, te.target_reps_max,
                   e.id AS exercise_id, e.name AS exercise_name
            FROM template_exercises te
            JOIN exercises e ON e.id = te.exercise_id
            WHERE te.template_id = %s
            ORDER BY te.position
            """,
            (template_id,),
        )
        template_rows = cur.fetchall()

        exercises_out = []
        for row in template_rows:
            ex_id = row["exercise_id"]
            cur.execute(
                """
                SELECT s.set_number, s.weight, s.reps
                FROM sets s
                JOIN sessions sess ON sess.id = s.session_id
                WHERE s.exercise_id = %s
                  AND sess.session_date = (
                      SELECT MAX(sess2.session_date)
                      FROM sets s2
                      JOIN sessions sess2 ON sess2.id = s2.session_id
                      WHERE s2.exercise_id = %s
                  )
                ORDER BY s.set_number
                """,
                (ex_id, ex_id),
            )
            last_sets = cur.fetchall()
            exercises_out.append({
                "exercise_id": ex_id,
                "exercise_name": row["exercise_name"],
                "default_sets": row["default_sets"],
                "target_reps_min": row["target_reps_min"],
                "target_reps_max": row["target_reps_max"],
                "last_sets": [
                    {"set_number": s["set_number"], "weight": float(s["weight"]), "reps": s["reps"]}
                    for s in last_sets
                ],
            })

        return JSONResponse({
            "template_id": template_id,
            "template_name": tmpl["name"],
            "exercises": exercises_out,
        })
    finally:
        conn.close()


async def api_save_session(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    exercises_payload = body.get("exercises", [])
    if not exercises_payload:
        return JSONResponse({"error": "no exercises provided"}, status_code=400)

    notes = body.get("notes")
    template_id = body.get("template_id")
    if template_id:
        notes = f"[template:{template_id}]{(' ' + notes) if notes else ''}"

    conn = get_db()
    try:
        cur = conn.cursor()
        session_date = body.get("session_date")
        if session_date:
            cur.execute(
                "INSERT INTO sessions (session_date, notes) VALUES (%s, %s) RETURNING id",
                (session_date, notes),
            )
        else:
            cur.execute(
                "INSERT INTO sessions (notes) VALUES (%s) RETURNING id",
                (notes,),
            )
        session_id = cur.fetchone()[0]

        total_sets = 0
        total_volume = 0.0
        logged = []

        for ex_data in exercises_payload:
            ex_id = ex_data.get("exercise_id")
            ex_name = ex_data.get("name", "")
            if not ex_id:
                ex_id, ex_name = resolve_exercise(cur, ex_name)

            for i, s in enumerate(ex_data.get("sets", []), start=1):
                w = float(s.get("weight") or 0)
                reps = int(s.get("reps") or 0)
                cur.execute(
                    "INSERT INTO sets (session_id, exercise_id, set_number, weight, reps) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (session_id, ex_id, i, w, reps),
                )
                total_volume += w * reps
                total_sets += 1
            logged.append({"exercise": ex_name, "sets": len(ex_data.get("sets", []))})

        conn.commit()
        return JSONResponse({
            "session_id": session_id,
            "total_sets": total_sets,
            "total_volume": round(total_volume, 1),
            "exercises": logged,
        }, status_code=201)
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


# ── OAuth endpoints ───────────────────────────────────────────────────────────

OPEN_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/oauth/authorize",
    "/oauth/token",
    "/authorize",
    "/token",
    "/app",
    "/manifest.json",
}

_AUTHORIZE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GymBuddy — Authorise</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 380px; margin: 80px auto; padding: 0 20px; color: #111; }}
    h2   {{ margin-bottom: 4px; }}
    p    {{ color: #555; margin-top: 4px; }}
    input[type=password] {{ width: 100%; padding: 10px; margin: 12px 0 8px; box-sizing: border-box;
                            border: 1px solid #ccc; border-radius: 6px; font-size: 1rem; }}
    button {{ width: 100%; padding: 11px; background: #0066ff; color: #fff; border: none;
              border-radius: 6px; font-size: 1rem; cursor: pointer; }}
    button:hover {{ background: #0052cc; }}
    .err {{ color: #cc0000; margin-top: 8px; font-size: .9rem; }}
  </style>
</head>
<body>
  <h2>GymBuddy</h2>
  <p>Claude is requesting access to your gym data.</p>
  <form method="post">
    <input type="hidden" name="client_id"      value="{client_id}">
    <input type="hidden" name="redirect_uri"   value="{redirect_uri}">
    <input type="hidden" name="state"          value="{state}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Authorise</button>
    {error}
  </form>
</body>
</html>"""


async def serve_app(request: Request) -> FileResponse:
    return FileResponse("/data/index.html", media_type="text/html")


async def serve_manifest(request: Request) -> FileResponse:
    return FileResponse("/data/manifest.json", media_type="application/manifest+json")


async def oauth_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": SERVER_URL,
        "authorization_endpoint": f"{SERVER_URL}/oauth/authorize",
        "token_endpoint": f"{SERVER_URL}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    })


async def oauth_authorize(request: Request):
    if request.method == "GET":
        p = request.query_params
        if p.get("client_id") != OAUTH_CLIENT_ID:
            return HTMLResponse("Unknown client.", status_code=400)
        return HTMLResponse(_AUTHORIZE_HTML.format(
            client_id=p.get("client_id", ""),
            redirect_uri=p.get("redirect_uri", ""),
            state=p.get("state", ""),
            code_challenge=p.get("code_challenge", ""),
            error="",
        ))

    form = await request.form()
    client_id      = form.get("client_id", "")
    redirect_uri   = form.get("redirect_uri", "")
    state          = form.get("state", "")
    code_challenge = form.get("code_challenge", "")
    password       = form.get("password", "")

    if client_id != OAUTH_CLIENT_ID or password != ADMIN_PASSWORD:
        return HTMLResponse(_AUTHORIZE_HTML.format(
            client_id=client_id, redirect_uri=redirect_uri,
            state=state, code_challenge=code_challenge,
            error='<p class="err">Incorrect password.</p>',
        ), status_code=401)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "exp": time.time() + 300,
    }
    dest = f"{redirect_uri}?code={code}"
    if state:
        dest += f"&state={state}"
    return RedirectResponse(dest, status_code=302)


async def oauth_token(request: Request) -> JSONResponse:
    form = await request.form()

    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    client_id     = form.get("client_id", "")
    client_secret = form.get("client_secret", "")
    code          = form.get("code", "")
    code_verifier = form.get("code_verifier", "")

    if client_id != OAUTH_CLIENT_ID or client_secret != OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    entry = _auth_codes.pop(code, None)
    if not entry or time.time() > entry["exp"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if entry["code_challenge"]:
        digest    = hashlib.sha256(code_verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != entry["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    token = secrets.token_urlsafe(32)
    _tokens.add(token)
    return JSONResponse({
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 31536000,
    })


# ── Auth middleware ───────────────────────────────────────────────────────────

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith("/api/"):
            return await call_next(request)
        auth  = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token not in _tokens:
            if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
                return RedirectResponse("/app")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


# ── Wire up the full app ──────────────────────────────────────────────────────

mcp.settings.streamable_http_path = "/"
mcp.settings.stateless_http = True

_funnel_host = SERVER_URL.removeprefix("https://").removeprefix("http://").split("/")[0]
from mcp.server.streamable_http import TransportSecuritySettings
mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=[_funnel_host, "localhost", "localhost:8000"],
    allowed_origins=["https://claude.ai"],
)

_mcp_asgi_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(outer_app):
    async with mcp._session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/.well-known/oauth-authorization-server", oauth_metadata),
        Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
        Route("/authorize",       oauth_authorize, methods=["GET", "POST"]),
        Route("/oauth/token",     oauth_token,     methods=["POST"]),
        Route("/token",           oauth_token,     methods=["POST"]),
        Route("/app",             serve_app),
        Route("/manifest.json",   serve_manifest),
        Route("/api/templates",              api_get_templates,  methods=["GET"]),
        Route("/api/workout/{template_id}",  api_get_workout,    methods=["GET"]),
        Route("/api/sessions",               api_save_session,   methods=["POST"]),
        Mount("/",                app=_mcp_asgi_app),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
