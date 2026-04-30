"""
Worked-example simulator: a Comau-style welding cell.

This module brings the §15 worked example to life. It models a small
welding workcell at a German automotive plant in 2027, with:

    - A factory operator who issues a delegation certificate
    - A planner model (Gemini Robotics ER 1.6, simulated) that signs
      inferences for each unit
    - A runtime (Flowstate-style) that signs each skill invocation
    - An actuator (arm controller) that signs each weld seam breadcrumb
    - A WFM at inference time (Cosmos Reason 2, simulated) consulted
      for spatial reasoning during ambiguous welds
    - A WFM at training time (Cosmos Predict 2.5) recorded once in the
      delegation certificate

Run:
    python -m propagation_lab.demo.run_demo

Output:
    A populated lab.mobydb.sqlite, ready for the verifier and dashboard.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List

from ..chain.breadcrumb import (
    SignedBreadcrumb,
    WorldModelInferenceCall,
    emit_breadcrumb,
    issue_delegation,
    sign_inference,
    sign_invocation,
    to_storage_breadcrumb,
)
from ..chain.breadcrumb import WorldModelTrainProvenance
from ..chain.keys import Keypair
from ..chain.spatial import EpochSealer, cell_at
from ..storage.local_mobydb import LocalMobyDB


# Site: Munich BMW area; the cell array is four resolution-9 H3 cells.
# Coordinates are illustrative.
SITE_LATITUDE = 48.1771
SITE_LONGITUDE = 11.5573

# Welding seams the planner schedules per unit
SEAMS = [
    "L-pillar-inner-left",
    "L-pillar-inner-right",
    "B-pillar-cross-front",
    "B-pillar-cross-rear",
    "rear-quarter-upper",
]


def _site_territory_cells() -> List[str]:
    """The factory operator authorizes operations in 4 res-9 cells around the site."""
    base = cell_at(SITE_LATITUDE, SITE_LONGITUDE, resolution=9)
    import h3
    # Take the base cell plus its three nearest neighbours
    neighbours = list(h3.grid_disk(base, 1))[:4]
    return neighbours


def _seam_position(seam_id: str, unit_index: int) -> tuple:
    """Each seam is at a slightly different position on the chassis."""
    # Tiny offsets so each seam ends up in a different res-12 cell
    offset = (
        SEAMS.index(seam_id) * 1e-5,
        unit_index * 1e-6,
    )
    return (
        SITE_LATITUDE + offset[0],
        SITE_LONGITUDE + offset[1],
    )


def run_demo(
    db_path: str = "lab.mobydb.sqlite",
    n_units: int = 3,
    seams_per_unit: int = 5,
    inference_wfm_probability: float = 0.30,
    rng_seed: int = 42,
) -> Dict[str, Any]:
    """Execute the worked-example simulation.

    Returns a summary dict with the operator's public key, the count of
    breadcrumbs written, the count of epochs sealed, and the total
    number of WFM inference-time invocations.
    """
    random.seed(rng_seed)

    # ------ Identity setup ------
    operator = Keypair.generate()
    model = Keypair.generate()
    runtime = Keypair.generate()
    actuator = Keypair.generate()
    sealer = Keypair.generate()  # often the operator, but kept distinct here

    # ------ Storage ------
    db = LocalMobyDB(db_path)
    db.reset()

    # ------ Training-time WFM provenance ------
    # The VLA was post-trained on Cosmos-Predict 2.5 synthetic data.
    train_wfm = WorldModelTrainProvenance(
        provider="nvidia",
        model="cosmos-predict-2.5-2b",
        version="2.5.0",
        weights_hash="blake3:" + hashlib.blake2b(b"cosmos-2.5-weights", digest_size=32).hexdigest(),
        license="NVIDIA Open Model License v1.0",
    )

    # ------ Issue the delegation certificate ------
    territory = _site_territory_cells()
    delegation = issue_delegation(
        operator=operator,
        scope_skills=["weld_seam", "place_panel"],
        territory_cells=territory,
        budget_actions=n_units * seams_per_unit + 10,
        expiry_seconds=3600.0,
        authorized_model_pks=[model.public_hex],
        world_model_train=train_wfm,
    )

    # ------ Epoch sealer ------
    sealer_eng = EpochSealer(sealer, epoch_label="bmw-cell-7-shift-1")

    breadcrumb_records: List[SignedBreadcrumb] = []
    inference_wfm_calls = 0

    for unit_index in range(n_units):
        # The planner produces one inference per unit
        plan_output = {
            "unit_sku": "1842",
            "unit_index": unit_index,
            "primitives": [
                {"skill_id": "weld_seam", "seam_id": s}
                for s in SEAMS[:seams_per_unit]
            ],
        }
        inference = sign_inference(
            model=model,
            delegation=delegation,
            prompt=f"assemble unit SKU 1842 #{unit_index}",
            input_scene_hash=hashlib.sha256(
                f"scene-{unit_index}".encode("utf-8")
            ).hexdigest(),
            plan_output=plan_output,
            reasoning_trace=(
                f"Identified unit SKU 1842 in fixture. Planning {seams_per_unit} seams. "
                f"Force-sensing engaged. No collisions in predicted trajectory."
            ),
        )

        for seam_id in SEAMS[:seams_per_unit]:
            # Runtime: one invocation per seam
            invocation = sign_invocation(
                runtime=runtime,
                inference=inference,
                skill_id="weld_seam",
                parameters={
                    "seam_id": seam_id,
                    "current_A": 220,
                    "voltage_V": 23,
                    "speed_mm_s": 8.5,
                },
                preconditions_ok=True,
                scope_check_ok=True,
            )

            # Sometimes the planner consults Cosmos Reason 2 for spatial
            # disambiguation mid-skill (e.g. when sensor readings are
            # noisy). When it does, the WFM call is recorded in the
            # breadcrumb's world_model_infer field.
            wfm_calls: List[WorldModelInferenceCall] = []
            if random.random() < inference_wfm_probability:
                inference_wfm_calls += 1
                wfm_call = WorldModelInferenceCall(
                    provider="nvidia",
                    model="cosmos-reason-2",
                    version="2.0.1",
                    prompt_hash=hashlib.sha256(
                        f"resolve seam {seam_id} ambiguity unit {unit_index}".encode()
                    ).hexdigest(),
                    output_hash="blake3:" + hashlib.blake2b(
                        f"out-{seam_id}-{unit_index}".encode(),
                        digest_size=32,
                    ).hexdigest(),
                    output_uri=f"r2://obj/cosmos-out/{unit_index}-{seam_id}.json",
                    attestation="signed-by-runtime",
                )
                wfm_calls.append(wfm_call)

            # Actuator: emit the breadcrumb at the seam's H3 cell
            lat, lon = _seam_position(seam_id, unit_index)
            h3_cell = cell_at(lat, lon, resolution=12)
            breadcrumb = emit_breadcrumb(
                actuator=actuator,
                invocation=invocation,
                inference=inference,
                delegation=delegation,
                h3_cell=h3_cell,
                motion_primitive={
                    "type": "weld_seam",
                    "seam_id": seam_id,
                    "duration_s": 2.4,
                },
                sensor_readings={
                    "current_A": 218 + random.random() * 4,
                    "voltage_V": 22.8 + random.random() * 0.4,
                    "ir_peak_C": 1480 + int(random.random() * 60),
                    "force_N": 12 + random.random() * 2,
                },
                epoch_id=sealer_eng.current_epoch_id,
                world_model_infer=wfm_calls,
                tpm_quote="bytes:" + hashlib.sha256(
                    f"tpm-{seam_id}".encode()
                ).hexdigest()[:32],
                spiffe_id="spiffe://comau-style-cell/cell-7/arm-1",
                aarm_ref=None,
            )

            breadcrumb_records.append(breadcrumb)
            db.write_breadcrumb(breadcrumb)
            sealer_eng.add(to_storage_breadcrumb(breadcrumb))

    # Seal the epoch and persist it. Because the sealer pre-allocated
    # `current_epoch_id` before any breadcrumb was signed, the Merkle
    # leaves and the actuator signatures are stable across the seal:
    # no post-hoc patching is required.
    sealed = sealer_eng.seal()
    db.write_epoch(sealed)
    db.close()

    return {
        "operator_pk": operator.public_hex,
        "operator_fingerprint": operator.fingerprint,
        "model_pk": model.public_hex,
        "runtime_pk": runtime.public_hex,
        "actuator_pk": actuator.public_hex,
        "sealer_pk": sealer.public_hex,
        "breadcrumbs_written": len(breadcrumb_records),
        "epochs_sealed": 1,
        "wfm_inference_calls": inference_wfm_calls,
        "epoch_id": sealed.epoch_id,
        "merkle_root": sealed.merkle_root,
        "territory_cells": territory,
    }


if __name__ == "__main__":
    summary = run_demo()
    print("LAB demo complete.")
    print(f"  breadcrumbs written:    {summary['breadcrumbs_written']}")
    print(f"  epochs sealed:          {summary['epochs_sealed']}")
    print(f"  WFM inference calls:    {summary['wfm_inference_calls']}")
    print(f"  epoch id:               {summary['epoch_id']}")
    print(f"  merkle root:            {summary['merkle_root']}")
    print(f"  operator fingerprint:   {summary['operator_fingerprint']}")
