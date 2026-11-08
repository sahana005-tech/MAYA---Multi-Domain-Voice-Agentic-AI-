"""
db.py — SQLite state store for the voice agent.

This is the single source of truth for the whole system. The dashboard reads
from here; the call handler writes to here. Every metric on the dashboard is a
derived query (see the query helpers at the bottom), NOT a hand-maintained
counter — that is what keeps the numbers from ever disagreeing or freezing
mid-demo.

Two writes make up a call's whole lifecycle:
  start_call()  -> one INSERT  (Active Call becomes 1 automatically)
  end_call()    -> one UPDATE  (Total/Tasks tick up, row appears in Recent)

Using the Twilio CallSid (or any unique call id) as the PRIMARY KEY gives us
idempotency for free: INSERT OR IGNORE means a retried webhook can't double
count a call.
"""

import sqlite3
import json
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("VOICE_AGENT_DB", os.path.join(os.path.dirname(__file__), "data.db"))


@contextmanager
def get_conn():
    # check_same_thread=False so FastAPI's async workers and the Streamlit
    # process can both open connections. WAL mode allows concurrent readers
    # while a writer is active — important so the dashboard never blocks a call.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id             TEXT PRIMARY KEY,
                domain_id      TEXT,
                status         TEXT DEFAULT 'active',   -- 'active' | 'ended'
                task_completed INTEGER DEFAULT 0,
                tools_called   TEXT DEFAULT '[]',       -- JSON list of tool names
                rating         INTEGER,
                transcript     TEXT DEFAULT '',
                stt_ms         INTEGER,
                llm_ms         INTEGER,
                tts_ms         INTEGER,
                started_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at       TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bookings (
                ref_id     TEXT PRIMARY KEY,
                call_id    TEXT,
                domain_id  TEXT,
                service_id TEXT,
                datetime   TEXT,
                quantity   INTEGER,
                customer   TEXT,
                total      REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


# ---------- call lifecycle (the two writes) ----------

def start_call(call_id: str, domain_id: str):
    """Call connected. Active Call -> 1. Idempotent on retries."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO calls (id, domain_id, status) VALUES (?, ?, 'active')",
            (call_id, domain_id),
        )


def end_call(call_id: str, task_completed: bool, tools_called: list,
             rating=None, transcript: str = "",
             stt_ms=None, llm_ms=None, tts_ms=None):
    """Call ended. Total + Tasks tick up; row appears at top of Recent."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE calls
               SET status='ended', task_completed=?, tools_called=?, rating=?,
                   transcript=?, stt_ms=?, llm_ms=?, tts_ms=?,
                   ended_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (int(task_completed), json.dumps(tools_called), rating,
             transcript, stt_ms, llm_ms, tts_ms, call_id),
        )


def record_booking(ref_id, call_id, domain_id, service_id, datetime_str,
                   quantity, customer, total):
    """Persist a booking row (the actual write-action artifact)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO bookings
               (ref_id, call_id, domain_id, service_id, datetime, quantity, customer, total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref_id, call_id, domain_id, service_id, datetime_str, quantity, customer, total),
        )


# ---------- dashboard queries (every metric is derived, never counted) ----------

def metric_active_calls():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM calls WHERE status='active'").fetchone()["c"]


def metric_total_conversations():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM calls WHERE status='ended'").fetchone()["c"]


def metric_tasks_completed():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM calls WHERE task_completed=1").fetchone()["c"]


def metric_avg_rating():
    with get_conn() as conn:
        r = conn.execute("SELECT AVG(rating) a FROM calls WHERE rating IS NOT NULL").fetchone()["a"]
        return round(r, 2) if r is not None else None


def calls_per_domain():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT domain_id, COUNT(*) c FROM calls GROUP BY domain_id ORDER BY c DESC"
        ).fetchall()
        return {row["domain_id"]: row["c"] for row in rows}


def recent_conversations(limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def telemetry_snapshot():
    """The JSON view — if the guide specifically wants a telemetry.json shape,
    expose this via GET /telemetry. Same numbers, still derived from SQLite."""
    return {
        "active_agents": metric_active_calls(),
        "total_conversations": metric_total_conversations(),
        "tasks_completed": metric_tasks_completed(),
        "avg_rating": metric_avg_rating(),
        "calls_per_domain": calls_per_domain(),
        "recent_conversations": recent_conversations(10),
    }


if __name__ == "__main__":
    init_db()
    print("DB initialised at", DB_PATH)
