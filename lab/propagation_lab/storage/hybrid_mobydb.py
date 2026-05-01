"""
HybridMobyDB — composes local SQLite + production HTTP.

Writes go to BOTH:
    1. Local SQLite (synchronous, authoritative) — always succeeds barring disk error
    2. Production /write (asynchronous in spirit, fire-and-forget) — best-effort

Reads go ONLY to local SQLite. Production has no public read endpoint as
of April 2026; once /query or /read are added, the read methods here can
delegate to RemoteMobyDB without changing callers.

This is the adapter the demo and the dashboard use when MOBYDB_MODE=hybrid.
For pure local development, use LocalMobyDB directly.

Per-breadcrumb remote write outcomes are stored in a small in-memory map
keyed by breadcrumb_id, so the dashboard can render the production-write
status next to each row.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from ..chain.breadcrumb import SignedBreadcrumb
from ..chain.spatial import SealedEpoch
from .local_mobydb import LocalMobyDB
from .remote_mobydb import RemoteMobyDB, RemoteWriteResult


log = logging.getLogger(__name__)


class HybridMobyDB:
    """Local-authoritative + production-mirroring MobyDB adapter."""

    def __init__(
        self,
        local_path: str = "lab.mobydb.sqlite",
        remote: RemoteMobyDB = None,
        async_remote: bool = True,
    ):
        self.local = LocalMobyDB(local_path)
        self.remote = remote or RemoteMobyDB()
        self.async_remote = async_remote
        # In-memory store of remote write outcomes, keyed by breadcrumb_id.
        # Bounded by the number of writes per process; the LAB demo writes
        # tens, the long-term dashboard would garbage-collect this.
        self._remote_results: Dict[str, RemoteWriteResult] = {}
        self._results_lock = threading.Lock()

    # ---- write side ----

    def write_breadcrumb(self, signed: SignedBreadcrumb) -> RemoteWriteResult:
        """Write to local first (authoritative), then to production (mirror).

        The local write is synchronous and authoritative; if it fails, an
        exception propagates. The remote write is best-effort; its result
        is stored under the breadcrumb_id and returned for callers that
        want to surface the integration status immediately.
        """
        # 1. Authoritative local write
        self.local.write_breadcrumb(signed)

        # 2. Mirror to production
        breadcrumb_id = signed.breadcrumb_id()

        if self.async_remote:
            # Fire-and-forget on a worker thread. Demo runs short enough
            # that we don't bother with a thread pool.
            def _do_remote():
                result = self.remote.write_breadcrumb(signed)
                with self._results_lock:
                    self._remote_results[breadcrumb_id] = result
                if result.success:
                    log.info(
                        "MobyDB remote: written %s (%dms, status=%d)",
                        result.key_hex[:24] if result.key_hex else "?",
                        result.latency_ms,
                        result.status_code,
                    )
                else:
                    log.warning(
                        "MobyDB remote: failed status=%d error=%s",
                        result.status_code,
                        (result.error or "")[:120],
                    )

            t = threading.Thread(target=_do_remote, daemon=True)
            t.start()
            # Return a placeholder — caller can poll via remote_result_for()
            placeholder = RemoteWriteResult(
                success=False, status_code=-1, error="pending",
            )
            return placeholder

        # Synchronous mode (used by tests and by single-shot demos)
        result = self.remote.write_breadcrumb(signed)
        with self._results_lock:
            self._remote_results[breadcrumb_id] = result
        return result

    def write_epoch(self, epoch: SealedEpoch) -> None:
        """Sealed epochs are local-only. Production has no /epoch_seal endpoint yet."""
        self.local.write_epoch(epoch)

    # ---- read side: delegated to local ----

    def list_breadcrumbs(self, limit: Optional[int] = None) -> List[dict]:
        return self.local.list_breadcrumbs(limit=limit)

    def query_by_epoch(self, epoch_id: str) -> List[dict]:
        return self.local.query_by_epoch(epoch_id)

    def query_by_cell_range(self, parent_cell: str) -> List[dict]:
        return self.local.query_by_cell_range(parent_cell)

    def list_epochs(self) -> List[SealedEpoch]:
        return self.local.list_epochs()

    def get_epoch(self, epoch_id: str) -> Optional[SealedEpoch]:
        return self.local.get_epoch(epoch_id)

    def reset(self) -> None:
        """Clear local state. Does not touch production."""
        self.local.reset()
        with self._results_lock:
            self._remote_results.clear()

    def close(self) -> None:
        self.local.close()

    # ---- integration status ----

    def remote_result_for(self, breadcrumb_id: str) -> Optional[RemoteWriteResult]:
        """Return the production write outcome for a given breadcrumb, or None.

        Returns None if the breadcrumb hasn't been written remotely yet
        (still pending in async mode) or if the breadcrumb_id is unknown.
        """
        with self._results_lock:
            return self._remote_results.get(breadcrumb_id)

    def remote_health(self) -> dict:
        """Pass-through to RemoteMobyDB.health() for the dashboard banner."""
        return self.remote.health()

    def wait_for_remote_writes(self, timeout_s: float = 10.0) -> None:
        """Block until all in-flight remote writes have completed.

        The LAB's demo runner calls this at the end of a run so the
        dashboard immediately reflects every write's final outcome.
        Implementation: spin-wait until the result map size matches the
        local breadcrumb count, with a timeout.
        """
        import time as _time
        target = len(self.local.list_breadcrumbs())
        deadline = _time.time() + timeout_s
        while _time.time() < deadline:
            with self._results_lock:
                if len(self._remote_results) >= target:
                    return
            _time.sleep(0.1)
        # Timed out — caller will see a partial set; that's fine.
