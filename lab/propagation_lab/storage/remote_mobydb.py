"""
RemoteMobyDB — production write adapter.

This adapter implements the write-side of the LocalMobyDB interface against
the production MobyDB service at https://mobydb-production.up.railway.app.
It is deliberately incomplete on the read side because the production
service does not yet expose a public read API; reads in the LAB are served
by LocalMobyDB.

Backing store: production MobyDB persists records in a RocksDB instance on
its own Railway volume, separate from the postgis service in the same
Railway project (which is used by mobydb-benchmark for comparison runs).
The 48-byte composite key (h3_cell, epoch, public_key) returned by /write
is a real RocksDB key.

Pattern: fire-and-forget with timeout, matching the convention already in
gns-backend/src/services/ai_bot.ts. A failed remote write does not abort
the local write; it is logged and recorded as a status flag on the
breadcrumb so the dashboard can surface it.

Configuration (environment variables):
    MOBYDB_URL          Default: https://mobydb-production.up.railway.app
    MOBYDB_TIMEOUT_S    Default: 3.0
    MOBYDB_TRUST_TIER   Default: Seedling  (LAB demo tier)
    MOBYDB_DRY_RUN      Default: 0  (set to 1 to skip actual HTTP calls)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import urllib.request
import urllib.error

from ..chain.breadcrumb import SignedBreadcrumb
from ..chain.mobydb_format import signed_breadcrumb_to_mobydb_record


log = logging.getLogger(__name__)


@dataclass
class RemoteWriteResult:
    """Outcome of a single remote write attempt.

    Returned by RemoteMobyDB.write_breadcrumb so the caller can record
    per-breadcrumb integration status without raising on failure.
    """
    success: bool
    status_code: int                # HTTP status; 0 if never reached server
    key_hex: Optional[str] = None   # Composite key returned by MobyDB
    epoch: Optional[int] = None     # Echoed back by MobyDB
    error: Optional[str] = None     # Human-readable error if not success
    latency_ms: int = 0             # Wall-clock latency of the call

    def to_dict(self) -> dict:
        return {
            "success":     self.success,
            "status_code": self.status_code,
            "key_hex":     self.key_hex,
            "epoch":       self.epoch,
            "error":       self.error,
            "latency_ms":  self.latency_ms,
        }


class RemoteMobyDB:
    """Production write adapter for the propagation LAB.

    Methods are intentionally a subset of LocalMobyDB. write_breadcrumb()
    works; reads raise ReadNotAvailable to be loud about the limitation.
    """

    DEFAULT_URL     = "https://mobydb-production.up.railway.app"
    DEFAULT_TIMEOUT = 3.0
    DEFAULT_TIER    = "Seedling"

    def __init__(
        self,
        base_url: str = None,
        timeout_s: float = None,
        trust_tier: str = None,
        dry_run: bool = None,
    ):
        self.base_url = (base_url or os.environ.get("MOBYDB_URL") or self.DEFAULT_URL).rstrip("/")
        self.timeout_s = timeout_s if timeout_s is not None else float(
            os.environ.get("MOBYDB_TIMEOUT_S", self.DEFAULT_TIMEOUT)
        )
        self.trust_tier = trust_tier or os.environ.get("MOBYDB_TRUST_TIER") or self.DEFAULT_TIER
        if dry_run is None:
            dry_run = os.environ.get("MOBYDB_DRY_RUN", "0").lower() in ("1", "true", "yes")
        self.dry_run = dry_run

    # ---- write side ----

    def write_breadcrumb(self, signed: SignedBreadcrumb) -> RemoteWriteResult:
        """POST a breadcrumb to the production MobyDB service.

        Returns a RemoteWriteResult; never raises on HTTP/network failure.
        Caller should log or surface the result; the local write should
        still proceed regardless.
        """
        record = signed_breadcrumb_to_mobydb_record(
            signed,
            trust_tier=self.trust_tier,
        )

        if self.dry_run:
            return RemoteWriteResult(
                success=True,
                status_code=200,
                key_hex="dryrun:" + signed.actuator_pk[:32],
                epoch=record["address"]["epoch"],
                error=None,
                latency_ms=0,
            )

        body = json.dumps(record).encode("utf-8")
        url = self.base_url + "/write"
        req = urllib.request.Request(
            url=url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                status = resp.getcode()
                resp_body = resp.read().decode("utf-8")
                try:
                    parsed = json.loads(resp_body)
                except json.JSONDecodeError:
                    parsed = {}
                # Production response shape, observed during reconnaissance:
                #   { "success": true, "data": { "key_hex": ..., "h3_cell": ..., "epoch": ... }, "error": null }
                ok = bool(parsed.get("success", False)) or 200 <= status < 300
                data = parsed.get("data") or {}
                return RemoteWriteResult(
                    success=ok,
                    status_code=status,
                    key_hex=data.get("key_hex"),
                    epoch=data.get("epoch"),
                    error=parsed.get("error"),
                    latency_ms=latency_ms,
                )
        except urllib.error.HTTPError as e:
            # 4xx / 5xx — server reached, request rejected
            latency_ms = int((time.perf_counter() - t0) * 1000)
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                err_body = str(e)
            return RemoteWriteResult(
                success=False,
                status_code=e.code,
                error=err_body,
                latency_ms=latency_ms,
            )
        except urllib.error.URLError as e:
            # Network / DNS / timeout
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return RemoteWriteResult(
                success=False,
                status_code=0,
                error=f"network: {e.reason}",
                latency_ms=latency_ms,
            )
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return RemoteWriteResult(
                success=False,
                status_code=0,
                error=f"unexpected: {e}",
                latency_ms=latency_ms,
            )

    # ---- liveness ----

    def health(self) -> dict:
        """Hit GET /health and return the parsed JSON.

        Production responds with:
            {"engine":"MobyDB","protocol":"GEP","status":"ok","version":"0.1.0"}

        Returns the dict on success or {"status": "unreachable", ...} on failure.
        """
        url = self.base_url + "/health"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    # ---- read side: NOT YET AVAILABLE ----

    def list_breadcrumbs(self, *args, **kwargs):
        raise NotImplementedError(
            "RemoteMobyDB does not support reads yet. "
            "Production MobyDB has no public /query or /read endpoint as of April 2026. "
            "Use HybridMobyDB to read from local SQLite while still writing to production."
        )

    def query_by_epoch(self, *args, **kwargs):
        raise NotImplementedError(self._read_not_available())

    def query_by_cell_range(self, *args, **kwargs):
        raise NotImplementedError(self._read_not_available())

    def list_epochs(self, *args, **kwargs):
        raise NotImplementedError(self._read_not_available())

    def get_epoch(self, *args, **kwargs):
        raise NotImplementedError(self._read_not_available())

    def write_epoch(self, *args, **kwargs):
        # Production MobyDB does not expose an /epoch_seal endpoint.
        # Epoch sealing remains a local concern; the LAB writes individual
        # breadcrumbs to production but seals epochs only locally for now.
        raise NotImplementedError(
            "RemoteMobyDB does not write sealed epochs. "
            "Epoch sealing is local-only until production exposes /epoch_seal."
        )

    def reset(self):
        raise NotImplementedError(
            "RemoteMobyDB cannot reset production data. Use LocalMobyDB.reset() for the local store."
        )

    @staticmethod
    def _read_not_available() -> str:
        return (
            "RemoteMobyDB read methods are not implemented. "
            "Production MobyDB has no public read API as of April 2026. "
            "HybridMobyDB serves reads from local SQLite."
        )
