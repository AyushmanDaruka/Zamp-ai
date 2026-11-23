"""Run store. SQLite locally; Postgres (Neon, via Vercel) when DATABASE_URL / POSTGRES_URL is set."""
import json
import os
import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path

PG_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
IS_PG = bool(PG_URL and PG_URL.startswith("postgres"))
_sqlite_lock = threading.Lock()


def _locked():
    """SQLite needs writes serialised; Postgres handles concurrency itself (and a lock held across a slow
    network call would stall every request on the instance)."""
    return nullcontext() if IS_PG else _sqlite_lock

COLS = ["id", "created_at", "source", "filename", "file_hash", "status", "decision", "vendor_name", "invoice_number",
        "total", "approved_amount", "risk_caught", "duration_ms", "doc_kind", "summary", "result", "events"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, created_at TEXT, source TEXT, filename TEXT, file_hash TEXT, status TEXT, decision TEXT,
  vendor_name TEXT, invoice_number TEXT, total DOUBLE PRECISION, approved_amount DOUBLE PRECISION,
  risk_caught DOUBLE PRECISION, duration_ms INTEGER, doc_kind TEXT, summary TEXT, result TEXT, events TEXT, pdf {blob}
)"""


def _conn():
    if IS_PG:
        import psycopg
        return psycopg.connect(PG_URL, autocommit=True, connect_timeout=10)
    path = os.environ.get("SQLITE_PATH") or ("/tmp/runs.db" if os.environ.get("VERCEL") else str(Path(__file__).resolve().parent.parent / "data" / "runs.db"))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    return c


def _q(sql):
    return sql.replace("?", "%s") if IS_PG else sql


_ready = False


def init():
    global _ready
    if _ready:
        return
    with _locked(), _conn() as c:
        c.execute(SCHEMA.format(blob="BYTEA" if IS_PG else "BLOB"))
    _ready = True
    if count() == 0:
        load_seed()


def count():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def save(run, pdf=None):
    init_noseed()
    vals = [run.get(k) for k in COLS]
    for i, k in enumerate(COLS):
        if k in ("summary", "result", "events") and vals[i] is not None and not isinstance(vals[i], str):
            vals[i] = json.dumps(vals[i], default=str)
    cols = COLS + ["pdf"]
    vals.append(pdf)
    ph = ",".join(["?"] * len(cols))
    upd = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    with _locked(), _conn() as c:
        c.execute(_q(f"INSERT INTO runs ({','.join(cols)}) VALUES ({ph}) ON CONFLICT (id) DO UPDATE SET {upd}"), vals)


def init_noseed():
    global _ready
    if not _ready:
        with _locked(), _conn() as c:
            c.execute(SCHEMA.format(blob="BYTEA" if IS_PG else "BLOB"))


def _row(r, full=False):
    d = dict(zip(COLS, r))
    for k in ("summary", "result", "events"):
        if d.get(k) and isinstance(d[k], str):
            d[k] = json.loads(d[k])
    if not full:
        d.pop("result", None)
        d.pop("events", None)
    return d


def list_runs(limit=500):
    init()
    with _conn() as c:
        rows = c.execute(_q(f"SELECT {','.join(COLS)} FROM runs ORDER BY created_at DESC LIMIT ?"), (limit,)).fetchall()
    return [_row(r) for r in rows]


def get_run(rid):
    init()
    with _conn() as c:
        r = c.execute(_q(f"SELECT {','.join(COLS)} FROM runs WHERE id = ?"), (rid,)).fetchone()
    return _row(r, full=True) if r else None


def exceptions_full():
    """Every run that wasn't auto-approved, with its result (for the exceptions inbox)."""
    init()
    cols = [c for c in COLS if c != "events"]
    with _conn() as c:
        rows = c.execute(f"SELECT {','.join(cols)} FROM runs WHERE decision != 'APPROVED' ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        for k in ("summary", "result"):
            if d.get(k) and isinstance(d[k], str):
                d[k] = json.loads(d[k])
        out.append(d)
    return out


def update_resolution(rid, summary, approved_amount):
    with _locked(), _conn() as c:
        c.execute(_q("UPDATE runs SET summary = ?, approved_amount = ? WHERE id = ?"), (json.dumps(summary, default=str), approved_amount, rid))


def get_pdf(rid):
    with _conn() as c:
        r = c.execute(_q("SELECT pdf FROM runs WHERE id = ?"), (rid,)).fetchone()
    return bytes(r[0]) if r and r[0] is not None else None


def prior_runs():
    """Compact view used by duplicate + PO-consumption checks."""
    init()
    with _conn() as c:
        rows = c.execute("SELECT id, file_hash, decision, summary FROM runs WHERE status = 'done'").fetchall()
    out = []
    for rid, fh, dec, summ in rows:
        summ = json.loads(summ) if isinstance(summ, str) and summ else (summ or {})
        # a human resolution (e.g. a released hold) changes what the run means for PO consumption
        dec = (summ.get("resolution") or {}).get("final_decision") or dec
        out.append({"id": rid, "file_hash": fh, "decision": dec, "summary": summ})
    return out


def reset():
    """Remove everything except the seeded history."""
    init()
    with _locked(), _conn() as c:
        c.execute("DELETE FROM runs WHERE source != 'seed'")


def load_seed():
    p = Path(__file__).resolve().parent / "data" / "seed_runs.json"
    if not p.exists():
        return
    for run in json.loads(p.read_text()):
        save(run)
