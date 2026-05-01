"""
Wire-format converter between LAB internal types and MobyDB production format.

The LAB uses string H3 cells ("8c1f8d44c44a5ff") and human-readable epoch
identifiers ("bmw-cell-7-shift-1-0001") because they're easier to read in
the dashboard and in tests. The production MobyDB service expects:

    h3_cell  : uint64 integer (the H3 v4 native representation)
    epoch    : signed bigint, derived as floor((now_ms - GEP_GENESIS_MS) / 3_600_000)

This module provides the conversions in both directions, kept in one place
so the rest of the codebase doesn't have to think about wire format.

GEP_GENESIS_MS matches the constant in gns-backend/src/services/ai_bot.ts:
    const GEP_GENESIS_MS = 1743552000000;  // 2025-04-01 00:00:00 UTC
"""
from __future__ import annotations

import time
from typing import Any, Dict

from .breadcrumb import SignedBreadcrumb


GEP_GENESIS_MS = 1743552000000  # 2025-04-01 00:00:00 UTC


# -----------------------------------------------------------------------------
# H3 cell conversions
# -----------------------------------------------------------------------------

def h3_cell_to_uint64(cell_str: str) -> int:
    """Convert an H3 v4 hex string ('8c1f8d44c44a5ff') to its uint64 integer.

    H3 v4 cells are 64-bit integers; the string form is the same integer in
    hexadecimal. Conversion is exact and reversible.
    """
    return int(cell_str, 16)


def uint64_to_h3_cell(cell_int: int) -> str:
    """Convert a uint64 H3 cell back to the canonical hex string form.

    The hex string is lowercase, no '0x' prefix, no leading zeros — matching
    the form h3-py produces.
    """
    return format(cell_int, "x")


# -----------------------------------------------------------------------------
# Epoch conversions
# -----------------------------------------------------------------------------

def current_epoch(now_ms: int = None) -> int:
    """Hour-counter since GEP genesis. Matches ai_bot.ts currentEpoch().

    Args:
        now_ms: optional override for testing. Defaults to current wall time.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return (now_ms - GEP_GENESIS_MS) // 3_600_000


def epoch_to_unix_ms(epoch: int) -> int:
    """The earliest wall-clock millisecond inside the given epoch."""
    return GEP_GENESIS_MS + (epoch * 3_600_000)


# -----------------------------------------------------------------------------
# Breadcrumb to MobyDB record
# -----------------------------------------------------------------------------

def signed_breadcrumb_to_mobydb_record(
    signed: SignedBreadcrumb,
    *,
    schema_version: str = "0.1.1",
    trust_tier: str = "Seedling",
) -> Dict[str, Any]:
    """Convert a fully-signed LAB breadcrumb to a MobyDB /write request body.

    The mapping follows the schema decision documented in the LAB README:

        outer envelope:
            address.h3_cell    = uint64 of the action's H3 cell
            address.epoch      = production-style integer epoch (hour counter)
            address.public_key = actuator's hex public key
            payload.collection_type = 'inference'  (reuse existing collection)
            payload.payload_type    = 'propagation/breadcrumb'  (new subtype)
            payload.data            = the entire propagation chain envelope
            signature               = actuator signature over canonical(data)
            trust_tier              = 'Seedling' for LAB demo data
            written_at_ms           = wall-clock at write time

    The chain itself (operator delegation + signature, model inference +
    signature, runtime invocation + signature, sensor data, WFM hooks)
    lives entirely inside payload.data, which is jsonb on the production
    side.

    Args:
        signed: a fully-signed SignedBreadcrumb produced by emit_breadcrumb().
        schema_version: bumped when the chain envelope schema evolves.
        trust_tier: one of MobyDB's accepted tiers — 'Seedling' for LAB.

    Returns:
        dict ready for json.dumps and POST to /write.
    """
    # Map LAB string epoch ('bmw-cell-7-shift-1-0001') to a real integer epoch.
    # The LAB's epoch_id is a human label; the production address.epoch is the
    # hour counter at the time of the action. We derive it from the actuator's
    # signed timestamp so it's consistent with the action time.
    breadcrumb_ts_ms = int(signed.timestamp * 1000)
    epoch_int = current_epoch(now_ms=breadcrumb_ts_ms)

    # Carry the LAB's human-readable epoch id inside payload.data so the
    # round-trip retains the LAB's grouping semantics even though the
    # production address.epoch is the integer form.
    data_block = {
        "schema_version":         schema_version,
        "lab_epoch_id":           signed.epoch_id,

        # Layer 1 — operator delegation
        "operator_pk":            signed.delegation["operator_pk"],
        "delegation":             signed.delegation,
        "delegation_signature":   signed.delegation_signature,

        # Layer 2 — model inference
        "model_pk":               signed.inference["model_pk"],
        "inference":              signed.inference,
        "inference_signature":    signed.inference_signature,

        # Layer 3 — runtime invocation
        "runtime_pk":             signed.invocation["runtime_pk"],
        "invocation":             signed.invocation,
        "invocation_signature":   signed.invocation_signature,

        # Layer 4 — actuator action
        "actuator_pk":            signed.actuator_pk,
        "motion_primitive":       signed.motion_primitive,
        "sensor_readings":        signed.sensor_readings,
        "sensor_digest":          signed.sensor_digest,

        # Addendum A — WFM hooks
        "world_model_infer":      signed.world_model_infer,

        # Composability hooks
        "tpm_quote":              signed.tpm_quote,
        "spiffe_id":              signed.spiffe_id,
        "aarm_ref":               signed.aarm_ref,
    }

    return {
        "address": {
            "h3_cell":    h3_cell_to_uint64(signed.h3_cell),
            "epoch":      epoch_int,
            "public_key": signed.actuator_pk,
        },
        "payload": {
            "collection_type": "inference",
            "payload_type":    "propagation/breadcrumb",
            "data":            data_block,
        },
        "signature":     signed.actuator_signature,
        "trust_tier":    trust_tier,
        "written_at_ms": breadcrumb_ts_ms,
    }
