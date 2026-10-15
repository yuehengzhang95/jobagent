"""SQLite store for jobs and their pipeline state."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from . import paths
from .sources import Posting

DB_PATH = paths.DATA / "jobs.db"
_lock = threading.Lock()

# discovered -> (triage) filtered | discovered -> scored -> tailoring -> ready | needs_review | error -> applied
# any state -> skipped
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT, company TEXT, tier TEXT, ext_id TEXT,
  title TEXT, location TEXT, url TEXT, posted_at TEXT,
  description TEXT, detail_ref TEXT,
  first_seen TEXT, last_seen TEXT,
  status TEXT DEFAULT 'discovered',
  triage INTEGER, triage_reason TEXT,
  fit_score INTEGER, archetype TEXT, analysis TEXT,
  run_dir TEXT, error TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    paths.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_posting(p: Posting, tier: str) -> bool:
    """Insert a new posting or refresh last_seen. Returns True if it was new."""
    with _lock, connect() as conn:
        row = conn.execute("SELECT id, description FROM jobs WHERE id = ?", (p.id,)).fetchone()
        if row:
            conn.execute("UPDATE jobs SET last_seen = ?, title = ?, location = ?, url = ?, "
                         "description = CASE WHEN ? != '' THEN ? ELSE description END WHERE id = ?",
                         (now(), p.title, p.location, p.url, p.description, p.description, p.id))
            return False
        conn.execute("INSERT INTO jobs (id, source, company, tier, ext_id, title, location, url, posted_at, "
                     "description, detail_ref, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (p.id, p.source, p.company, tier, p.ext_id, p.title, p.location, p.url, p.posted_at,
                      p.description, p.detail_ref, now(), now()))
        return True


def update(job_id: str, **fields) -> None:
    if not fields:
        return
    if "analysis" in fields and not isinstance(fields["analysis"], str):
        fields["analysis"] = json.dumps(fields["analysis"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock, connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def get(job_id: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def query(where: str = "1=1", params: tuple = (), order: str = "fit_score DESC NULLS LAST, posted_at DESC",
          limit: int = 1000) -> List[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY {order} LIMIT ?",
                            (*params, limit)).fetchall()


def to_posting(row: sqlite3.Row) -> Posting:
    return Posting(source=row["source"], company=row["company"], ext_id=row["ext_id"], title=row["title"],
                   location=row["location"], url=row["url"], posted_at=row["posted_at"] or "",
                   description=row["description"] or "", detail_ref=row["detail_ref"] or "")
