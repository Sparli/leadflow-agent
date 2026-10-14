"""Audit ledger: one row for every write the agent makes to the CRM.

Three questions this answers that HubSpot alone cannot:
- What did run X do? (debugging, and the honest answer to a customer)
- Which records did the agent create? (one-command cleanup, never touching
  records a human made)
- How much did each run cost? (cost per lead is a first-class number)
- What is the agent waiting for a human to approve? (proposals)

SQLite because it is a single file, needs no server, and is ACID. At this
volume a database server would add operations work and no benefit.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    lead_text   TEXT NOT NULL,
    model       TEXT NOT NULL,
    outcome     TEXT,
    steps       INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd    REAL
);
CREATE TABLE IF NOT EXISTS writes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    created_at  TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('create', 'associate')),
    payload     TEXT NOT NULL,
    archived_at TEXT
);
-- A write the agent wants to make. Nothing reaches HubSpot until a human approves it.
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    created_at  TEXT NOT NULL,
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'executed', 'reused', 'rejected', 'failed')),
    object_id   TEXT,
    decided_at  TEXT,
    error       TEXT
);
"""

# The id the model sees for a record that does not exist yet. It uses it in later
# calls (a contact's company_id), and approval swaps it for the real HubSpot id.
PLACEHOLDER_PREFIX = "pending-"


@dataclass(frozen=True)
class WriteRecord:
    run_id: str
    object_type: str
    object_id: str
    action: str


@dataclass(frozen=True)
class Proposal:
    id: int
    run_id: str
    tool: str
    args: dict[str, Any]
    status: str
    object_id: str | None
    error: str | None

    @property
    def placeholder(self) -> str:
        return f"{PLACEHOLDER_PREFIX}{self.id}"


class Ledger:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)

    def start_run(self, run_id: str, lead_text: str, model: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO runs (run_id, started_at, lead_text, model) VALUES (?, ?, ?, ?)",
                (run_id, _now(), lead_text, model),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        steps: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        with self._db:
            self._db.execute(
                "UPDATE runs SET outcome=?, steps=?, input_tokens=?, output_tokens=?, cost_usd=? "
                "WHERE run_id=?",
                (outcome, steps, input_tokens, output_tokens, cost_usd, run_id),
            )

    def record_write(
        self, run_id: str, object_type: str, object_id: str, action: str, payload: dict[str, Any]
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO writes (run_id, created_at, object_type, object_id, action, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    _now(),
                    object_type,
                    object_id,
                    action,
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def created_records(self, run_id: str | None = None) -> list[WriteRecord]:
        """Records the agent created and that are not yet archived, newest first.

        Newest first matters for cleanup: deals are created after the contacts
        and companies they point at, so deleting in reverse order never leaves
        a deal referencing a record that is already gone.
        """
        query = "SELECT run_id, object_type, object_id, action FROM writes WHERE action='create' AND archived_at IS NULL"
        params: tuple[str, ...] = ()
        if run_id is not None:
            query += " AND run_id=?"
            params = (run_id,)
        rows = self._db.execute(query + " ORDER BY id DESC", params).fetchall()
        return [WriteRecord(*row) for row in rows]

    def mark_archived(self, object_type: str, object_id: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE writes SET archived_at=? WHERE object_type=? AND object_id=? AND action='create'",
                (_now(), object_type, object_id),
            )

    def recent_runs(
        self, limit: int
    ) -> list[tuple[str, str, str | None, int | None, float | None]]:
        rows = self._db.execute(
            "SELECT run_id, started_at, outcome, steps, cost_usd FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [tuple(r) for r in rows]

    def add_proposal(self, run_id: str, tool: str, args: dict[str, Any]) -> Proposal:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO proposals (run_id, created_at, tool, args) VALUES (?, ?, ?, ?)",
                (run_id, _now(), tool, json.dumps(args, sort_keys=True)),
            )
        return Proposal(cur.lastrowid or 0, run_id, tool, args, "pending", None, None)

    def proposals(self, run_id: str) -> list[Proposal]:
        """Every proposal for a run, in the order the agent made them.

        Order matters: a contact proposal can point at a company proposal, so
        approval must execute them in the same order.
        """
        rows = self._db.execute(
            "SELECT id, run_id, tool, args, status, object_id, error FROM proposals "
            "WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [Proposal(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5], r[6]) for r in rows]

    def decide(
        self,
        proposal_id: int,
        status: str,
        *,
        object_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._db:
            self._db.execute(
                # COALESCE: once a record exists in HubSpot its id is never forgotten,
                # or a retry would create the record a second time.
                "UPDATE proposals SET status=?, object_id=COALESCE(?, object_id), error=?, "
                "decided_at=? WHERE id=?",
                (status, object_id, error, _now(), proposal_id),
            )

    def pending_counts(self) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT run_id, COUNT(*) FROM proposals WHERE status IN ('pending', 'failed') "
            "GROUP BY run_id"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def close(self) -> None:
        self._db.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()
