"""
Canonical JSON for cross-language signing compatibility.

Ed25519 signs bytes. To make a signature portable across Flutter, TypeScript,
Python and Rust signers/verifiers, every party must serialize the payload
to *exactly* the same byte sequence before signing. Standard json.dumps()
output is not stable across languages or library versions; the canonical
encoding below is.

Rules:
    1. Object keys are sorted lexicographically at every level.
    2. Null/None values are excluded from the output.
    3. Separators are tight (",",":") with no whitespace.
    4. UTF-8 encoded bytes are returned, not str.

This matches the encoding used elsewhere in the GNS Foundation codebase,
notably comm_crypto_service.dart and crypto.ts.
"""
from __future__ import annotations

import json
from typing import Any


def _strip_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(item) for item in value]
    return value


def canonicalize(obj: Any) -> bytes:
    """Encode obj to canonical JSON bytes for signing/hashing.

    The returned bytes are byte-for-byte identical to the output produced
    by canonicalizing the same logical object in Dart, TypeScript or Rust.
    """
    cleaned = _strip_nulls(obj)
    text = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")
