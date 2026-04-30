"""
The four-layer signing chain — the operational core of the LAB.

This module implements §8 of the Propagation Identity whitepaper and
the additive WFM hooks from Addendum A (§4.4 and Patch P4). The chain
is parametric in the number of layers; the four required layers are:

    operator    -- signs the delegation certificate
    model       -- signs the inference (planner output)
    runtime     -- signs the skill invocation
    actuator    -- signs the physical action

Optional WFM provenance is recorded in two positions:

    world_model_train  -- training-time provenance, in the operator cert
    world_model_infer  -- inference-time invocations, per breadcrumb

Each layer signs a payload that includes a hash of the upstream layer's
payload. The actuator's signature is therefore an end-to-end commitment
to the entire chain.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from .canonical import canonicalize
from .keys import Keypair, verify
from .spatial import GEP_GENESIS_HASH, Breadcrumb


# ---------------------------------------------------------------------------
# Layer 1: Operator delegation certificate
# ---------------------------------------------------------------------------

@dataclass
class WorldModelTrainProvenance:
    """Training-time WFM provenance, per Addendum A §4.4.3.

    Recorded once in the operator's delegation certificate, not per-
    breadcrumb. Re-distillation from a new WFM version is a delegation
    rotation.
    """
    provider: str
    model: str
    version: str
    weights_hash: str  # blake3 or sha256 hex
    license: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "weights_hash": self.weights_hash,
            "license": self.license,
        }


@dataclass
class DelegationCertificate:
    """An operator's signed authorization for a deployment.

    Constrains scope (which skills), territory (which H3 cells), budget
    (max actions), expiry (until when), and the set of authorized model
    public keys. Optionally carries training-time WFM provenance.
    """
    operator_pk: str
    scope_skills: List[str]
    territory_cells: List[str]  # parent cells; child cells are allowed via H3 containment
    budget_actions: int
    expiry_unix: float
    authorized_model_pks: List[str]
    world_model_train: Optional[WorldModelTrainProvenance] = None
    issued_at: float = field(default_factory=time.time)
    signature: str = ""  # operator's signature over the canonical body

    def body(self) -> Dict[str, Any]:
        return {
            "operator_pk": self.operator_pk,
            "scope_skills": list(self.scope_skills),
            "territory_cells": list(self.territory_cells),
            "budget_actions": self.budget_actions,
            "expiry_unix": self.expiry_unix,
            "authorized_model_pks": list(self.authorized_model_pks),
            "world_model_train": self.world_model_train.to_dict() if self.world_model_train else None,
            "issued_at": self.issued_at,
            "gep_genesis": GEP_GENESIS_HASH,
        }

    def hash(self) -> str:
        return hashlib.sha256(canonicalize(self.body())).hexdigest()


def issue_delegation(
    operator: Keypair,
    scope_skills: List[str],
    territory_cells: List[str],
    budget_actions: int,
    expiry_seconds: float,
    authorized_model_pks: List[str],
    world_model_train: Optional[WorldModelTrainProvenance] = None,
) -> DelegationCertificate:
    """Issue a signed delegation certificate."""
    cert = DelegationCertificate(
        operator_pk=operator.public_hex,
        scope_skills=scope_skills,
        territory_cells=territory_cells,
        budget_actions=budget_actions,
        expiry_unix=time.time() + expiry_seconds,
        authorized_model_pks=authorized_model_pks,
        world_model_train=world_model_train,
    )
    cert.signature = operator.sign(canonicalize(cert.body()))
    return cert


# ---------------------------------------------------------------------------
# Layer 2: Model inference
# ---------------------------------------------------------------------------

@dataclass
class WorldModelInferenceCall:
    """A single WFM invocation during action execution, per Addendum A §4.4.3.

    Multiple invocations may occur per breadcrumb. The output itself is
    stored externally (object storage); the breadcrumb carries the hash.
    """
    provider: str
    model: str
    version: str
    prompt_hash: str  # sha256 of the prompt sent to the WFM
    output_hash: str  # blake3 of the WFM output (video/scene)
    output_uri: str   # external store URI
    attestation: str  # "signed-by-runtime" or "signed-by-provider"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "prompt_hash": self.prompt_hash,
            "output_hash": self.output_hash,
            "output_uri": self.output_uri,
            "attestation": self.attestation,
        }


@dataclass
class ModelInference:
    """The model layer's signed contribution to the chain."""
    model_pk: str
    delegation_hash: str  # parent: hash of the delegation certificate
    prompt_hash: str
    input_scene_hash: str
    plan_output: Dict[str, Any]
    reasoning_trace: str
    inference_at: float = field(default_factory=time.time)
    signature: str = ""

    def body(self) -> Dict[str, Any]:
        return {
            "model_pk": self.model_pk,
            "parent_delegation": self.delegation_hash,
            "prompt_hash": self.prompt_hash,
            "input_scene_hash": self.input_scene_hash,
            "plan_output": self.plan_output,
            "reasoning_trace": self.reasoning_trace,
            "inference_at": self.inference_at,
        }

    def hash(self) -> str:
        return hashlib.sha256(canonicalize(self.body())).hexdigest()


def sign_inference(
    model: Keypair,
    delegation: DelegationCertificate,
    prompt: str,
    input_scene_hash: str,
    plan_output: Dict[str, Any],
    reasoning_trace: str,
) -> ModelInference:
    inf = ModelInference(
        model_pk=model.public_hex,
        delegation_hash=delegation.hash(),
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        input_scene_hash=input_scene_hash,
        plan_output=plan_output,
        reasoning_trace=reasoning_trace,
    )
    inf.signature = model.sign(canonicalize(inf.body()))
    return inf


# ---------------------------------------------------------------------------
# Layer 3: Runtime skill invocation
# ---------------------------------------------------------------------------

@dataclass
class SkillInvocation:
    runtime_pk: str
    inference_hash: str  # parent
    skill_id: str
    parameters: Dict[str, Any]
    preconditions_ok: bool
    scope_check_ok: bool
    invocation_at: float = field(default_factory=time.time)
    signature: str = ""

    def body(self) -> Dict[str, Any]:
        return {
            "runtime_pk": self.runtime_pk,
            "parent_inference": self.inference_hash,
            "skill_id": self.skill_id,
            "parameters": self.parameters,
            "preconditions_ok": self.preconditions_ok,
            "scope_check_ok": self.scope_check_ok,
            "invocation_at": self.invocation_at,
        }

    def hash(self) -> str:
        return hashlib.sha256(canonicalize(self.body())).hexdigest()


def sign_invocation(
    runtime: Keypair,
    inference: ModelInference,
    skill_id: str,
    parameters: Dict[str, Any],
    preconditions_ok: bool,
    scope_check_ok: bool,
) -> SkillInvocation:
    inv = SkillInvocation(
        runtime_pk=runtime.public_hex,
        inference_hash=inference.hash(),
        skill_id=skill_id,
        parameters=parameters,
        preconditions_ok=preconditions_ok,
        scope_check_ok=scope_check_ok,
    )
    inv.signature = runtime.sign(canonicalize(inv.body()))
    return inv


# ---------------------------------------------------------------------------
# Layer 4: Actuator physical action — the breadcrumb
# ---------------------------------------------------------------------------

@dataclass
class SignedBreadcrumb:
    """The fully-signed propagation breadcrumb.

    This is the structure that is written to MobyDB, that the verifier
    walks, and that is included in the audit-export package.
    """
    actuator_pk: str
    invocation_hash: str  # parent

    h3_cell: str
    timestamp: float
    epoch_id: str

    motion_primitive: Dict[str, Any]
    sensor_readings: Dict[str, Any]
    sensor_digest: str

    # Signatures from each layer
    delegation: Dict[str, Any]   # full delegation certificate body
    delegation_signature: str
    inference: Dict[str, Any]
    inference_signature: str
    invocation: Dict[str, Any]
    invocation_signature: str
    actuator_signature: str = ""

    # WFM hooks — Addendum A
    world_model_infer: List[Dict[str, Any]] = field(default_factory=list)

    # Composability hooks — §13 of the whitepaper
    tpm_quote: Optional[str] = None
    spiffe_id: Optional[str] = None
    aarm_ref: Optional[str] = None

    def actuator_body(self) -> Dict[str, Any]:
        """The body the actuator signs — the full envelope minus actuator_signature."""
        return {
            "actuator_pk": self.actuator_pk,
            "parent_invocation": self.invocation_hash,
            "h3_cell": self.h3_cell,
            "timestamp": self.timestamp,
            "epoch_id": self.epoch_id,
            "motion_primitive": self.motion_primitive,
            "sensor_readings": self.sensor_readings,
            "sensor_digest": self.sensor_digest,
            "world_model_infer": self.world_model_infer,
            "tpm_quote": self.tpm_quote,
            "spiffe_id": self.spiffe_id,
            "aarm_ref": self.aarm_ref,
            "gep_genesis": GEP_GENESIS_HASH,
        }

    def to_record(self) -> Dict[str, Any]:
        """Full breadcrumb dict for storage / serialization."""
        return {
            "actuator_pk": self.actuator_pk,
            "parent_invocation": self.invocation_hash,
            "h3_cell": self.h3_cell,
            "timestamp": self.timestamp,
            "epoch_id": self.epoch_id,
            "motion_primitive": self.motion_primitive,
            "sensor_readings": self.sensor_readings,
            "sensor_digest": self.sensor_digest,
            "world_model_infer": self.world_model_infer,
            "tpm_quote": self.tpm_quote,
            "spiffe_id": self.spiffe_id,
            "aarm_ref": self.aarm_ref,
            "gep_genesis": GEP_GENESIS_HASH,

            "delegation": self.delegation,
            "delegation_signature": self.delegation_signature,
            "inference": self.inference,
            "inference_signature": self.inference_signature,
            "invocation": self.invocation,
            "invocation_signature": self.invocation_signature,

            "actuator_signature": self.actuator_signature,
        }

    def breadcrumb_id(self) -> str:
        # SHA-256 of the canonical fully-signed record
        return hashlib.sha256(canonicalize(self.to_record())).hexdigest()

    def payload_hash(self) -> str:
        # The Merkle leaf hash — SHA-256 over the canonical record
        return self.breadcrumb_id()


def emit_breadcrumb(
    actuator: Keypair,
    invocation: SkillInvocation,
    inference: ModelInference,
    delegation: DelegationCertificate,
    h3_cell: str,
    motion_primitive: Dict[str, Any],
    sensor_readings: Dict[str, Any],
    epoch_id: str,
    world_model_infer: Optional[List[WorldModelInferenceCall]] = None,
    tpm_quote: Optional[str] = None,
    spiffe_id: Optional[str] = None,
    aarm_ref: Optional[str] = None,
) -> SignedBreadcrumb:
    """Build, sign, and return a fully-signed breadcrumb.

    The actuator signs an envelope that includes the H3 cell, timestamp,
    motion primitive and sensor readings, plus the parent hash chaining
    back through invocation -> inference -> delegation.
    """
    sensor_digest = hashlib.blake2b(
        canonicalize(sensor_readings),
        digest_size=32,
    ).hexdigest()

    bc = SignedBreadcrumb(
        actuator_pk=actuator.public_hex,
        invocation_hash=invocation.hash(),
        h3_cell=h3_cell,
        timestamp=time.time(),
        epoch_id=epoch_id,
        motion_primitive=motion_primitive,
        sensor_readings=sensor_readings,
        sensor_digest=sensor_digest,
        delegation=delegation.body(),
        delegation_signature=delegation.signature,
        inference=inference.body(),
        inference_signature=inference.signature,
        invocation=invocation.body(),
        invocation_signature=invocation.signature,
        world_model_infer=[c.to_dict() for c in (world_model_infer or [])],
        tpm_quote=tpm_quote,
        spiffe_id=spiffe_id,
        aarm_ref=aarm_ref,
    )
    bc.actuator_signature = actuator.sign(canonicalize(bc.actuator_body()))
    return bc


def to_storage_breadcrumb(signed: SignedBreadcrumb) -> Breadcrumb:
    """Convert a fully-signed breadcrumb to the lightweight storage view."""
    return Breadcrumb(
        breadcrumb_id=signed.breadcrumb_id(),
        h3_cell=signed.h3_cell,
        timestamp=signed.timestamp,
        actor_pk=signed.actuator_pk,
        payload_hash=signed.payload_hash(),
    )
