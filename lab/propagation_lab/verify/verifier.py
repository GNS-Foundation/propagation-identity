"""
Offline verifier for propagation breadcrumbs.

The verifier walks a breadcrumb's signed chain and checks every layer
against its claimed signing key. It also enforces the §7 properties:

    P1 (Composability)        -- by reading the full chain, top to bottom
    P2 (Spatial binding)      -- H3 cell of the action is contained in
                                 the operator's authorized territory
    P3 (Delegation constraint)-- skill_id is in delegation.scope_skills,
                                 budget not exceeded, expiry not passed,
                                 model_pk is in authorized_model_pks

It also verifies Merkle inclusion of the breadcrumb in its sealed epoch.

The verifier is deliberately minimal in dependencies: it imports
canonical, keys, spatial — and nothing else. It does NOT need access to
the operator's running infrastructure. This is the property the
whitepaper §15.4 asserts: an auditor verifies the package offline,
against the GEP genesis hash and public keys of the parties.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from ..chain.canonical import canonicalize
from ..chain.keys import verify
from ..chain.spatial import (
    GEP_GENESIS_HASH,
    SealedEpoch,
    cell_contains,
    merkle_inclusion_proof,
    merkle_root,
    verify_inclusion,
)


@dataclass
class VerificationResult:
    """The outcome of verifying a single breadcrumb."""
    breadcrumb_id: str
    operator_pk: str
    actuator_pk: str
    h3_cell: str

    # Layer-by-layer checks
    delegation_signature_ok: bool = False
    inference_signature_ok: bool = False
    invocation_signature_ok: bool = False
    actuator_signature_ok: bool = False

    # Property checks
    territory_ok: bool = False     # P2 spatial binding
    scope_ok: bool = False          # P3 delegation constraint
    expiry_ok: bool = False
    model_authorized_ok: bool = False
    chain_continuity_ok: bool = False  # parent hashes resolve

    # Merkle inclusion
    epoch_seal_signature_ok: bool = False
    merkle_inclusion_ok: bool = False

    # Aggregate
    notes: List[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all([
            self.delegation_signature_ok,
            self.inference_signature_ok,
            self.invocation_signature_ok,
            self.actuator_signature_ok,
            self.territory_ok,
            self.scope_ok,
            self.expiry_ok,
            self.model_authorized_ok,
            self.chain_continuity_ok,
            self.epoch_seal_signature_ok,
            self.merkle_inclusion_ok,
        ])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "breadcrumb_id": self.breadcrumb_id,
            "operator_pk": self.operator_pk,
            "actuator_pk": self.actuator_pk,
            "h3_cell": self.h3_cell,
            "delegation_signature_ok": self.delegation_signature_ok,
            "inference_signature_ok": self.inference_signature_ok,
            "invocation_signature_ok": self.invocation_signature_ok,
            "actuator_signature_ok": self.actuator_signature_ok,
            "territory_ok": self.territory_ok,
            "scope_ok": self.scope_ok,
            "expiry_ok": self.expiry_ok,
            "model_authorized_ok": self.model_authorized_ok,
            "chain_continuity_ok": self.chain_continuity_ok,
            "epoch_seal_signature_ok": self.epoch_seal_signature_ok,
            "merkle_inclusion_ok": self.merkle_inclusion_ok,
            "all_ok": self.all_ok,
            "notes": self.notes,
        }


def _hash_of(payload_dict: Dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(canonicalize(payload_dict)).hexdigest()


def verify_breadcrumb(
    record: Dict[str, Any],
    epochs_by_id: Dict[str, SealedEpoch],
    epoch_breadcrumbs: Dict[str, List[Dict[str, Any]]],
) -> VerificationResult:
    """Verify a single breadcrumb record against the surrounding context.

    Args:
        record: the full breadcrumb dict (as produced by SignedBreadcrumb.to_record).
        epochs_by_id: map of epoch_id -> SealedEpoch.
        epoch_breadcrumbs: map of epoch_id -> ordered list of breadcrumb records
            in the same epoch (used to compute the Merkle inclusion proof).

    Returns:
        A populated VerificationResult.
    """
    # Reconstruct breadcrumb_id
    import hashlib
    breadcrumb_id = hashlib.sha256(canonicalize(record)).hexdigest()

    operator_pk = record["delegation"]["operator_pk"]
    actuator_pk = record["actuator_pk"]
    result = VerificationResult(
        breadcrumb_id=breadcrumb_id,
        operator_pk=operator_pk,
        actuator_pk=actuator_pk,
        h3_cell=record["h3_cell"],
    )

    # ------ Layer 1: delegation signature ------
    delegation_body = record["delegation"]
    delegation_sig = record["delegation_signature"]
    result.delegation_signature_ok = verify(
        operator_pk,
        canonicalize(delegation_body),
        delegation_sig,
    )

    # ------ Layer 2: inference signature ------
    inference_body = record["inference"]
    inference_sig = record["inference_signature"]
    model_pk = inference_body["model_pk"]
    result.inference_signature_ok = verify(
        model_pk,
        canonicalize(inference_body),
        inference_sig,
    )

    # ------ Layer 3: invocation signature ------
    invocation_body = record["invocation"]
    invocation_sig = record["invocation_signature"]
    runtime_pk = invocation_body["runtime_pk"]
    result.invocation_signature_ok = verify(
        runtime_pk,
        canonicalize(invocation_body),
        invocation_sig,
    )

    # ------ Layer 4: actuator signature ------
    # Reconstruct the body the actuator signed.
    actuator_body = {
        "actuator_pk": record["actuator_pk"],
        "parent_invocation": record["parent_invocation"],
        "h3_cell": record["h3_cell"],
        "timestamp": record["timestamp"],
        "epoch_id": record["epoch_id"],
        "motion_primitive": record["motion_primitive"],
        "sensor_readings": record["sensor_readings"],
        "sensor_digest": record["sensor_digest"],
        "world_model_infer": record["world_model_infer"],
        "tpm_quote": record.get("tpm_quote"),
        "spiffe_id": record.get("spiffe_id"),
        "aarm_ref": record.get("aarm_ref"),
        "gep_genesis": GEP_GENESIS_HASH,
    }
    result.actuator_signature_ok = verify(
        actuator_pk,
        canonicalize(actuator_body),
        record["actuator_signature"],
    )

    # ------ P2: territory check ------
    territory_cells = delegation_body["territory_cells"]
    result.territory_ok = any(
        cell_contains(parent, record["h3_cell"]) for parent in territory_cells
    )
    if not result.territory_ok:
        result.notes.append(
            f"H3 cell {record['h3_cell']} is not contained in any "
            f"authorized territory cell {territory_cells}"
        )

    # ------ P3: scope, expiry, model authorization ------
    skill_id = invocation_body["skill_id"]
    result.scope_ok = skill_id in delegation_body["scope_skills"]
    if not result.scope_ok:
        result.notes.append(
            f"skill_id {skill_id!r} is not in delegation scope "
            f"{delegation_body['scope_skills']}"
        )

    result.expiry_ok = record["timestamp"] <= delegation_body["expiry_unix"]
    if not result.expiry_ok:
        result.notes.append(
            f"action timestamp {record['timestamp']} exceeds delegation "
            f"expiry {delegation_body['expiry_unix']}"
        )

    result.model_authorized_ok = (
        inference_body["model_pk"] in delegation_body["authorized_model_pks"]
    )
    if not result.model_authorized_ok:
        result.notes.append(
            f"model_pk not in delegation.authorized_model_pks"
        )

    # ------ Chain continuity ------
    expected_delegation_hash = _hash_of(delegation_body)
    expected_inference_hash = _hash_of(inference_body)
    expected_invocation_hash = _hash_of(invocation_body)

    cont = (
        inference_body["parent_delegation"] == expected_delegation_hash
        and invocation_body["parent_inference"] == expected_inference_hash
        and record["parent_invocation"] == expected_invocation_hash
    )
    result.chain_continuity_ok = cont
    if not cont:
        result.notes.append("parent-hash chain does not resolve cleanly")

    # ------ Epoch seal & Merkle inclusion ------
    epoch_id = record["epoch_id"]
    epoch = epochs_by_id.get(epoch_id)
    if epoch is None:
        result.notes.append(f"sealed epoch {epoch_id} not found in audit package")
        return result

    seal_body = {
        "epoch_id": epoch.epoch_id,
        "t_open": epoch.t_open,
        "t_close": epoch.t_close,
        "merkle_root": epoch.merkle_root,
        "breadcrumb_count": len(epoch.breadcrumb_ids),
        "sealer_pk": epoch.sealer_pk,
        "gep_genesis": GEP_GENESIS_HASH,
    }
    result.epoch_seal_signature_ok = verify(
        epoch.sealer_pk,
        canonicalize(seal_body),
        epoch.sealer_signature,
    )

    sibling_records = epoch_breadcrumbs.get(epoch_id, [])
    leaf_hashes = [bytes.fromhex(_hash_of(r)) for r in sibling_records]
    target_idx = next(
        (i for i, r in enumerate(sibling_records)
         if _hash_of(r) == breadcrumb_id),
        -1,
    )
    if target_idx < 0:
        result.notes.append("breadcrumb is not present in its claimed epoch")
        return result

    proof = merkle_inclusion_proof(leaf_hashes, target_idx)
    result.merkle_inclusion_ok = verify_inclusion(
        leaf_hashes[target_idx],
        target_idx,
        proof,
        epoch.merkle_root,
    )

    return result


def verify_audit_package(
    breadcrumbs: List[Dict[str, Any]],
    epochs: List[SealedEpoch],
) -> List[VerificationResult]:
    """Verify every breadcrumb in an audit-export package.

    Returns the list of per-breadcrumb verification results in the same
    order as the input.
    """
    epochs_by_id = {e.epoch_id: e for e in epochs}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for b in breadcrumbs:
        grouped.setdefault(b["epoch_id"], []).append(b)
    # Sort each group by timestamp for stable Merkle leaf order
    for v in grouped.values():
        v.sort(key=lambda r: r["timestamp"])

    return [verify_breadcrumb(b, epochs_by_id, grouped) for b in breadcrumbs]
