#!/usr/bin/env python3
"""SQLite lead store — the pipeline backbone.

Every lead carries a state. Each stage picks up whatever is ready for it, so a
failure at stage 5 re-runs stage 5 only; earlier work is never re-paid for.

    discovered -> imaged -> qualified -> rendered -> composed -> approved -> mailed
                              \\-> rejected(reason)
                               \\-> failed(stage, error)   [retryable]

Idempotency: normalized address is UNIQUE, so re-running never duplicates a
lead and never double-mails an address.
"""
import json, pathlib, re, sqlite3, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY,
    address         TEXT NOT NULL,
    address_key     TEXT NOT NULL UNIQUE,
    state           TEXT NOT NULL DEFAULT 'discovered',
    lat             REAL,
    lon             REAL,
    precision       TEXT,
    before_path     TEXT,
    after_path      TEXT,
    mask_path       TEXT,
    postcard_path   TEXT,
    qualification   TEXT,
    qc              TEXT,
    fail_stage      TEXT,
    fail_error      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);

CREATE TABLE IF NOT EXISTS costs (
    id          INTEGER PRIMARY KEY,
    lead_id     INTEGER,
    stage       TEXT NOT NULL,
    model       TEXT,
    usd         REAL NOT NULL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);
CREATE INDEX IF NOT EXISTS idx_costs_stage ON costs(stage);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    lead_id     INTEGER,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    note        TEXT,
    created_at  REAL NOT NULL
);
"""

TERMINAL = {"rejected", "mailed", "suppressed"}


def address_key(address):
    """Normalize for dedupe: lowercase, collapse whitespace, strip punctuation."""
    a = address.lower().strip()
    a = re.sub(r"[.,#]", " ", a)
    a = re.sub(r"\s+", " ", a)
    return a


class Store:
    def __init__(self, path="pipeline.db"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- leads ----------

    def add_lead(self, address):
        """Insert if new. Returns (lead_id, created)."""
        key, now = address_key(address), time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO leads (address, address_key, state, created_at, updated_at)"
            " VALUES (?,?,'discovered',?,?)", (address, key, now, now))
        self.db.commit()
        if cur.rowcount:
            return cur.lastrowid, True
        row = self.db.execute(
            "SELECT id FROM leads WHERE address_key=?", (key,)).fetchone()
        return row["id"], False

    def get(self, lead_id):
        return self.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def ready_for(self, state, limit=None):
        """Leads sitting in `state`, oldest first."""
        q = "SELECT * FROM leads WHERE state=? ORDER BY id"
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.db.execute(q, (state,)).fetchall()

    def advance(self, lead_id, to_state, note=None, **fields):
        row = self.get(lead_id)
        frm = row["state"] if row else None
        now = time.time()
        sets, vals = ["state=?", "updated_at=?"], [to_state, now]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
        vals.append(lead_id)
        self.db.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", vals)
        self.db.execute(
            "INSERT INTO events (lead_id, from_state, to_state, note, created_at)"
            " VALUES (?,?,?,?,?)", (lead_id, frm, to_state, note, now))
        self.db.commit()

    def fail(self, lead_id, stage, error):
        self.db.execute("UPDATE leads SET attempts = attempts + 1 WHERE id=?", (lead_id,))
        self.advance(lead_id, "failed", note=f"{stage}: {error}"[:400],
                     fail_stage=stage, fail_error=str(error)[:500])

    def retry_failed(self, max_attempts=3):
        """Reset retryable failures to the state before the failing stage."""
        back = {"imagery": "discovered", "qualify": "imaged",
                "render": "qualified", "compose": "rendered"}
        n = 0
        for row in self.db.execute(
                "SELECT * FROM leads WHERE state='failed' AND attempts < ?",
                (max_attempts,)).fetchall():
            prev = back.get(row["fail_stage"])
            if prev:
                self.advance(row["id"], prev, note=f"retry after {row['fail_stage']}")
                n += 1
        return n

    # ---------- costs ----------

    def add_cost(self, lead_id, stage, usd, model=None):
        self.db.execute(
            "INSERT INTO costs (lead_id, stage, model, usd, created_at) VALUES (?,?,?,?,?)",
            (lead_id, stage, model, float(usd), time.time()))
        self.db.commit()

    def total_spend(self):
        r = self.db.execute("SELECT COALESCE(SUM(usd),0) AS t FROM costs").fetchone()
        return r["t"]

    def spend_by_stage(self):
        return {r["stage"]: r["t"] for r in self.db.execute(
            "SELECT stage, SUM(usd) AS t FROM costs GROUP BY stage ORDER BY t DESC")}

    # ---------- reporting ----------

    def counts(self):
        return {r["state"]: r["n"] for r in self.db.execute(
            "SELECT state, COUNT(*) AS n FROM leads GROUP BY state")}

    def close(self):
        self.db.close()
