"""
HiveClient — minimal OpenAI-compat client for the GEIANT Hive proxy.

This is the LAB's gateway to real Hive inference. It calls the same
endpoint that gns-backend's ai_bot.ts and compute_executor.ts use:

    POST {HIVE_URL}/v1/chat/completions
    X-GNS-PublicKey: <64 hex chars — the caller's Ed25519 public key>
    Content-Type: application/json

    body: { model, messages, max_tokens, stream: false }

The proxy at gns-browser-production.up.railway.app/hive routes the job
to the GEIANT Hive swarm. A real worker (e.g. your laptop running
hive-worker join) executes inference using llama.cpp and returns:

    {
      "id":     "hive-...",
      "model":  "...",
      "choices":[{"message":{"role":"assistant","content":"..."}}],
      "usage":  {"prompt_tokens":N, "completion_tokens":N, "total_tokens":N},
      "hive":   {"job_id":"...", "tokens_per_second":N, "h3_cell":"..."}
    }

The 'hive.h3_cell' is the H3 cell of the worker that physically served
the request — the LAB records it in the propagation breadcrumb so the
audit trail captures where inference happened, not just that it happened.

Configuration:
    HIVE_URL    Default: https://gns-browser-production.up.railway.app/hive
    HIVE_MODEL  Default: tinyllama
    HIVE_TIMEOUT_S  Default: 60.0  (Hive can be slow when swarm is cold)
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional


log = logging.getLogger(__name__)


@dataclass
class ChatMessage:
    role: str  # 'system' | 'user' | 'assistant'
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class HiveInferenceResult:
    """Structured result of a successful Hive inference call.

    Mirrors the shape of `callHive`'s return in ai_bot.ts plus the
    additional Hive-specific fields the proxy returns (h3_cell of the
    worker, tokens_per_second).
    """
    content: str
    model: str                    # actual model that served (may differ from request)
    tokens: int                   # total_tokens
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    job_id: Optional[str] = None
    worker_h3_cell: Optional[str] = None  # the worker's H3 cell — for the chain
    tokens_per_second: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "content":            self.content,
            "model":              self.model,
            "tokens":             self.tokens,
            "prompt_tokens":      self.prompt_tokens,
            "completion_tokens":  self.completion_tokens,
            "latency_ms":         self.latency_ms,
            "job_id":             self.job_id,
            "worker_h3_cell":     self.worker_h3_cell,
            "tokens_per_second":  self.tokens_per_second,
        }


class HiveError(RuntimeError):
    """Raised when Hive cannot serve the request.

    The LAB does NOT silently fall back to another provider — that would
    defeat the purpose of demoing Hive integration. If Hive is unhealthy,
    the caller decides what to do (retry, abort the run, switch demo
    mode, etc.).
    """
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class HiveClient:
    """Minimal OpenAI-compat client for the GEIANT Hive proxy."""

    DEFAULT_URL    = "https://gns-browser-production.up.railway.app/hive"
    DEFAULT_MODEL  = "tinyllama"
    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        public_key_hex: str,
        base_url: str = None,
        model: str = None,
        timeout_s: float = None,
        max_tokens: int = 256,
    ):
        """Construct a client bound to a single GNS public key.

        Args:
            public_key_hex: 64-character hex string of the caller's Ed25519
                public key. The Hive proxy validates the format and ties
                each inference to this identity for accounting.
            base_url:  Hive endpoint. Defaults to the gns-browser proxy.
            model:     Model name to request. Defaults to 'tinyllama'.
            timeout_s: Per-request timeout. Defaults to 60s.
            max_tokens: Default max_tokens per request. Per-call override
                available via chat(max_tokens=...).
        """
        if not (isinstance(public_key_hex, str) and len(public_key_hex) == 64):
            raise ValueError(
                f"public_key_hex must be exactly 64 hex characters; "
                f"got {len(public_key_hex) if isinstance(public_key_hex, str) else 'non-string'}"
            )
        try:
            int(public_key_hex, 16)
        except ValueError:
            raise ValueError("public_key_hex must be hexadecimal")

        self.public_key_hex = public_key_hex
        self.base_url = (base_url or os.environ.get("HIVE_URL") or self.DEFAULT_URL).rstrip("/")
        self.model = model or os.environ.get("HIVE_MODEL") or self.DEFAULT_MODEL
        self.timeout_s = timeout_s if timeout_s is not None else float(
            os.environ.get("HIVE_TIMEOUT_S", self.DEFAULT_TIMEOUT)
        )
        self.max_tokens = max_tokens

    def chat(
        self,
        messages: List[ChatMessage],
        model: str = None,
        max_tokens: int = None,
    ) -> HiveInferenceResult:
        """Send a chat-completions request and return the result.

        Raises HiveError on any failure (timeout, 4xx, 5xx, parse error).
        Does not fall back to alternate providers — failures propagate.
        """
        body = {
            "model":      model or self.model,
            "messages":   [m.to_dict() for m in messages],
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream":     False,
        }
        url = self.base_url + "/v1/chat/completions"
        req = urllib.request.Request(
            url=url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-GNS-PublicKey": self.public_key_hex,
            },
        )

        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                body_bytes = resp.read()
                try:
                    parsed = json.loads(body_bytes.decode("utf-8"))
                except json.JSONDecodeError as e:
                    raise HiveError(
                        f"Hive returned non-JSON body (HTTP {resp.getcode()}): {e}",
                        status_code=resp.getcode(),
                    )

                # Validate the OpenAI-compat shape
                choices = parsed.get("choices") or []
                if not choices:
                    raise HiveError(f"Hive response had no choices: {parsed!r}")
                content = (choices[0].get("message") or {}).get("content")
                if content is None:
                    raise HiveError(f"Hive choice had no message content: {parsed!r}")

                usage = parsed.get("usage") or {}
                hive_meta = parsed.get("hive") or {}

                return HiveInferenceResult(
                    content=content,
                    model=parsed.get("model") or self.model,
                    tokens=int(usage.get("total_tokens") or 0),
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    latency_ms=latency_ms,
                    job_id=hive_meta.get("job_id"),
                    worker_h3_cell=hive_meta.get("h3_cell"),
                    tokens_per_second=hive_meta.get("tokens_per_second"),
                )

        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise HiveError(
                f"Hive HTTP {e.code}: {err_body or str(e)}",
                status_code=e.code,
            )
        except urllib.error.URLError as e:
            raise HiveError(f"Hive network error: {e.reason}")
        except TimeoutError:
            raise HiveError(f"Hive timed out after {self.timeout_s}s — swarm may be cold or no workers serving model {body['model']!r}")

    def health(self) -> dict:
        """Send a tiny chat request as a liveness probe.

        Returns {"status": "ok", ...} on success or
                {"status": "unreachable", "error": ...} on failure.
        """
        try:
            result = self.chat(
                messages=[ChatMessage(role="user", content="ping")],
                max_tokens=4,
            )
            return {
                "status":           "ok",
                "model":            result.model,
                "worker_h3_cell":   result.worker_h3_cell,
                "tokens_per_second": result.tokens_per_second,
                "latency_ms":       result.latency_ms,
            }
        except HiveError as e:
            return {"status": "unreachable", "error": str(e)}
