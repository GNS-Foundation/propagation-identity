"""
MobyDB-style local storage adapter.

This adapter implements the schema specified in §13 of the whitepaper
and Patch P5 of Addendum A, against a local SQLite database. The
composite primary key matches MobyDB's native 48-byte (h3_cell,
epoch_id, actor_pk, timestamp) layout. The schema is identical in
shape to what would be deployed on the real MobyDB at
mobydb-production.up.railway.app — the adapter is a drop-in.

Design intent: the adapter exposes a small, stable interface
(`write_breadcrumb`, `write_epoch`, `query_by_cell_range`,
`query_by_epoch`) so a `RemoteMobyDBAdapter` can be substituted later
without changing the demo, the verifier, or the dashboard.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Iterable, List, Optional

from ..chain.breadcrumb import SignedBreadcrumb
from ..chain.spatial import SealedEpoch, cell_contains


SCHEMA = """
CREATE TABLE IF NOT EXISTS propagation_breadcrumbs (
    h3_cell        TEXT NOT NULL,
    epoch_id       TEXT NOT NULL,
    actor_pk       TEXT NOT NULL,
    timestamp      REAL NOT NULL,
    breadcrumb_id  TEXT NOT NULL,
    record_json    TEXT NOT NULL,
    world_model_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (h3_cell, epoch_id, actor_pk, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_breadcrumbs_epoch
    ON propagation_breadcrumbs(epoch_id);
CREATE INDEX IF NOT EXISTS idx_breadcrumbs_cell
    ON propagation_breadcrumbs(h3_cell);

CREATE TABLE IF NOT EXISTS sealed_epochs (
    epoch_id        TEXT PRIMARY KEY,
    t_open          REAL NOT NULL,
    t_close         REAL NOT NULL,
    merkle_root     TEXT NOT NULL,
    sealer_pk       TEXT NOT NULL,
    sealer_sig      TEXT NOT NULL,
    breadcrumb_ids  TEXT NOT NULL  -- JSON array
);
"""


class LocalMobyDB:
    """Local SQLite-backed MobyDB-style adapter.

    Thread-safe for the single-threaded LAB; uses one connection with
    `check_same_thread=False` for simple multithreaded read access from
    the dashboard.
    """

    def __init__(self, path: str = "lab.mobydb.sqlite"):
        self.path = path
        # Allow other threads to read while the demo writes
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- write side ----

    def write_breadcrumb(self, signed: SignedBreadcrumb) -> None:
        record = signed.to_record()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO propagation_breadcrumbs
            (h3_cell, epoch_id, actor_pk, timestamp,
             breadcrumb_id, record_json, world_model_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signed.h3_cell,
                signed.epoch_id,
                signed.actuator_pk,
                signed.timestamp,
                signed.breadcrumb_id(),
                json.dumps(record, sort_keys=True),
                len(signed.world_model_infer),
            ),
        )
        self._conn.commit()

    def write_epoch(self, epoch: SealedEpoch) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sealed_epochs
            (epoch_id, t_open, t_close, merkle_root,
             sealer_pk, sealer_sig, breadcrumb_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                epoch.epoch_id,
                epoch.t_open,
                epoch.t_close,
                epoch.merkle_root,
                epoch.sealer_pk,
                epoch.sealer_signature,
                json.dumps(epoch.breadcrumb_ids),
            ),
        )
        self._conn.commit()

    # ---- read side ----

    def list_breadcrumbs(self, limit: Optional[int] = None) -> List[dict]:
        cur = self._conn.execute(
            "SELECT record_json FROM propagation_breadcrumbs ORDER BY timestamp ASC"
            + (f" LIMIT {int(limit)}" if limit else "")
        )
        return [json.loads(row[0]) for row in cur.fetchall()]

    def query_by_epoch(self, epoch_id: str) -> List[dict]:
        cur = self._conn.execute(
            "SELECT record_json FROM propagation_breadcrumbs "
            "WHERE epoch_id = ? ORDER BY timestamp ASC",
            (epoch_id,),
        )
        return [json.loads(row[0]) for row in cur.fetchall()]

    def query_by_cell_range(self, parent_cell: str) -> List[dict]:
        """Return all breadcrumbs whose H3 cell is contained in parent_cell.

        For demo purposes this enumerates and filters in Python; in a
        real MobyDB the H3 hierarchical index supports this natively as
        a constant-time range query.
        """
        out: List[dict] = []
        cur = self._conn.execute(
            "SELECT h3_cell, record_json FROM propagation_breadcrumbs"
        )
        for cell, rec_json in cur.fetchall():
            if cell_contains(parent_cell, cell):
                out.append(json.loads(rec_json))
        return out

    def list_epochs(self) -> List[SealedEpoch]:
        cur = self._conn.execute(
            "SELECT epoch_id, t_open, t_close, merkle_root, sealer_pk, "
            "sealer_sig, breadcrumb_ids FROM sealed_epochs "
            "ORDER BY t_open ASC"
        )
        out: List[SealedEpoch] = []
        for row in cur.fetchall():
            out.append(SealedEpoch(
                epoch_id=row[0],
                t_open=row[1],
                t_close=row[2],
                merkle_root=row[3],
                sealer_pk=row[4],
                sealer_signature=row[5],
                breadcrumb_ids=json.loads(row[6]),
            ))
        return out

    def get_epoch(self, epoch_id: str) -> Optional[SealedEpoch]:
        cur = self._conn.execute(
            "SELECT epoch_id, t_open, t_close, merkle_root, sealer_pk, "
            "sealer_sig, breadcrumb_ids FROM sealed_epochs WHERE epoch_id = ?",
            (epoch_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return SealedEpoch(
            epoch_id=row[0],
            t_open=row[1],
            t_close=row[2],
            merkle_root=row[3],
            sealer_pk=row[4],
            sealer_signature=row[5],
            breadcrumb_ids=json.loads(row[6]),
        )

    def reset(self) -> None:
        """Drop and recreate the schema. Used by the demo at startup."""
        self._conn.executescript(
            "DROP TABLE IF EXISTS propagation_breadcrumbs;"
            "DROP TABLE IF EXISTS sealed_epochs;"
        )
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
