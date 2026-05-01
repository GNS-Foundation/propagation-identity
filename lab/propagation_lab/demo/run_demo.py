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

Run modes:
    python -m propagation_lab.demo.run_demo                  # local-only (default)
    python -m propagation_lab.demo.run_demo --mode hybrid    # local + production mirror
    python -m propagation_lab.demo.run_demo --mode dryrun    # local + offline mock remote

In hybrid mode, every breadcrumb signed by the LAB is also POSTed to
https://mobydb-production.up.railway.app/write — the same endpoint that
gns-backend's @hai bot writes to. Local SQLite is the authoritative
store; the production write is fire-and-forget integration proof.

Output:
    A populated lab.mobydb.sqlite, ready for the verifier and dashboard.
    In hybrid mode, additional rows in production MobyDB tagged
    payload_type='propagation/breadcrumb', trust_tier='Seedling'.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List, Optional

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
from ..storage.hybrid_mobydb import HybridMobyDB
from ..storage.remote_mobydb import RemoteMobyDB
from ..inference.lab_hai import LabHai
from ..inference.hive_client import HiveError


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
    mode: str = "local",
    inference: str = "synthetic",
) -> Dict[str, Any]:
    """Execute the worked-example simulation.

    Args:
        mode: 'local' for SQLite-only, 'hybrid' for SQLite + production
              mirror, 'dryrun' for SQLite + mocked remote.
        inference: 'synthetic' for the original synthetic model layer (no
              external calls), 'hive' for real Hive inference signed by
              @lab-hai. With 'hive', each unit drives a real chat call
              against the Hive proxy and the response is recorded as
              the model layer's payload.

    Returns a summary dict including (in hive mode) the @lab-hai
    fingerprint, the worker H3 cells, and aggregate inference latency.
    """
    random.seed(rng_seed)

    # ------ Identity setup ------
    operator = Keypair.generate()
    runtime = Keypair.generate()
    actuator = Keypair.generate()
    sealer = Keypair.generate()  # often the operator, but kept distinct here

    # The model identity depends on the inference backend.
    lab_hai: Optional[LabHai] = None
    if inference == "hive":
        # Persistent @lab-hai actor: stable key, real Hive calls.
        lab_hai = LabHai.load_or_create(site_h3_cell="871e9a0ecffffff")
        # Probe Hive once before starting the run so we fail fast if
        # the swarm is unhealthy. The probe doubles as a warm-up.
        probe = lab_hai.hive.health()
        if probe.get("status") != "ok":
            print(f"   Hive UNREACHABLE: {probe.get('error')}")
            print("   Aborting — start a worker (`hive-worker join`) and retry.")
            raise HiveError(f"Hive health check failed: {probe.get('error')}")
        print(f"   Hive: {probe['model']} ready, "
              f"worker={probe['worker_h3_cell']}, "
              f"throughput~{probe.get('tokens_per_second', '?')}tok/s")
        # The chain-layer sign_inference() needs a Keypair with .public_hex
        # and .sign(). @lab-hai's keypair is exactly that — using it
        # directly means the model layer's chain signature and the
        # @lab-hai inference envelope share one identity.
        model = lab_hai.keypair
    elif inference == "synthetic":
        model = Keypair.generate()
    else:
        raise ValueError(f"unknown inference: {inference!r}; expected 'synthetic'|'hive'")

    # ------ Storage selection ------
    if mode == "local":
        db = LocalMobyDB(db_path)
        db.reset()
    elif mode in ("hybrid", "dryrun"):
        remote = RemoteMobyDB(dry_run=(mode == "dryrun"))
        # Pre-flight: print a one-line status of production reachability.
        h = remote.health()
        if h.get("status") == "ok":
            print(f"   MobyDB remote: {h.get('engine')} v{h.get('version')} "
                  f"({h.get('protocol')}) — reachable")
        elif mode == "dryrun":
            print("   MobyDB remote: dry-run mode (no HTTP calls)")
        else:
            print(f"   MobyDB remote: UNREACHABLE — {h.get('error')}")
            print("   Continuing in local-only mode for this run.")
        db = HybridMobyDB(local_path=db_path, remote=remote, async_remote=True)
        db.reset()
    else:
        raise ValueError(f"unknown mode: {mode!r}; expected 'local'|'hybrid'|'dryrun'")

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
    hive_inference_calls: List[Dict[str, Any]] = []  # one entry per unit when inference='hive'

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

        # Reasoning trace: in synthetic mode, a hardcoded string. In hive
        # mode, a real Hive call drives it — the response becomes the
        # planner's "reasoning" for this unit, signed under @lab-hai.
        reasoning_trace = (
            f"Identified unit SKU 1842 in fixture. Planning {seams_per_unit} seams. "
            f"Force-sensing engaged. No collisions in predicted trajectory."
        )
        hive_envelope: Optional[Dict[str, Any]] = None

        if inference == "hive":
            assert lab_hai is not None
            prompt = (
                f"You are the planning model for an industrial welding cell. "
                f"Unit SKU 1842 (chassis sub-frame) is in the fixture. "
                f"Plan {seams_per_unit} seams in execution order. "
                f"Reply concisely with the seam sequence and one risk note."
            )
            try:
                signed_inf = lab_hai.sign_inference(
                    prompt=prompt,
                    requester_pk=operator.public_hex,
                    session_id=f"lab-unit-{unit_index}",
                    max_tokens=120,
                )
                hive_inference_calls.append({
                    "unit_index":       unit_index,
                    "model":            signed_inf.model,
                    "tokens":           signed_inf.tokens,
                    "latency_ms":       signed_inf.latency_ms,
                    "worker_h3_cell":   signed_inf.worker_h3_cell,
                    "job_id":           signed_inf.job_id,
                })
                # The Hive envelope (cd block + signature) is recorded inside
                # the chain-layer inference's plan_output so an auditor can
                # trace the model layer's signature back to @lab-hai's PK
                # and verify the response_hash against the actual response.
                hive_envelope = {
                    "lab_hai_pk":     signed_inf.lab_hai_pk,
                    "cd":             signed_inf.cd,
                    "cd_signature":   signed_inf.signature,
                    "worker_h3_cell": signed_inf.worker_h3_cell,
                    "job_id":         signed_inf.job_id,
                }
                # Use the real Hive response as the reasoning trace.
                reasoning_trace = signed_inf.response[:400]
                print(f"   Hive[{unit_index}]: {signed_inf.tokens}tok in "
                      f"{signed_inf.latency_ms}ms (worker {signed_inf.worker_h3_cell})")
            except HiveError as e:
                # Honest failure: skip this run with a clear error rather
                # than silently regressing to synthetic behaviour.
                raise RuntimeError(
                    f"Hive inference failed for unit {unit_index}: {e}"
                )

        # Embed hive_envelope (if any) into plan_output so the chain
        # signature also commits to it.
        if hive_envelope:
            plan_output["hive_envelope"] = hive_envelope

        inference_obj = sign_inference(
            model=model,
            delegation=delegation,
            prompt=f"assemble unit SKU 1842 #{unit_index}",
            input_scene_hash=hashlib.sha256(
                f"scene-{unit_index}".encode("utf-8")
            ).hexdigest(),
            plan_output=plan_output,
            reasoning_trace=reasoning_trace,
        )

        for seam_id in SEAMS[:seams_per_unit]:
            # Runtime: one invocation per seam
            invocation = sign_invocation(
                runtime=runtime,
                inference=inference_obj,
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
                inference=inference_obj,
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

    # ------ Remote write summary (hybrid/dryrun only) ------
    remote_summary: Dict[str, Any] = {}
    if mode in ("hybrid", "dryrun"):
        # Wait for fire-and-forget HTTP calls to settle so the summary is final.
        db.wait_for_remote_writes(timeout_s=15.0)
        n_pending = 0
        n_success = 0
        n_failed = 0
        latencies = []
        first_keys: List[str] = []
        first_errors: List[str] = []
        for breadcrumb in breadcrumb_records:
            bid = breadcrumb.breadcrumb_id()
            r = db.remote_result_for(bid)
            if r is None or r.status_code == -1:
                n_pending += 1
                continue
            if r.success:
                n_success += 1
                latencies.append(r.latency_ms)
                if len(first_keys) < 2 and r.key_hex:
                    first_keys.append(r.key_hex)
            else:
                n_failed += 1
                if len(first_errors) < 2 and r.error:
                    first_errors.append(r.error[:80])
        avg_latency = (sum(latencies) // len(latencies)) if latencies else 0
        remote_summary = {
            "remote_success":      n_success,
            "remote_failed":       n_failed,
            "remote_pending":      n_pending,
            "remote_latency_avg":  avg_latency,
            "remote_sample_keys":  first_keys,
            "remote_sample_errors": first_errors,
        }

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
        "mode": mode,
        "inference": inference,
        "hive_inference_calls": hive_inference_calls,
        "lab_hai_fingerprint": lab_hai.fingerprint if lab_hai else None,
        **remote_summary,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run the LAB welding-cell simulator")
    parser.add_argument(
        "--mode", choices=["local", "hybrid", "dryrun"], default="local",
        help="Storage mode: 'local' (SQLite only), 'hybrid' (SQLite + production mirror), 'dryrun' (SQLite + mocked remote)",
    )
    parser.add_argument(
        "--inference", choices=["synthetic", "hive"], default="synthetic",
        help="Model layer: 'synthetic' (default, no external calls), 'hive' (real Hive inference signed by @lab-hai)",
    )
    parser.add_argument("--units", type=int, default=3,
                        help="Number of units to weld (default 3)")
    parser.add_argument("--seams", type=int, default=5,
                        help="Seams per unit (default 5)")
    args = parser.parse_args()

    summary = run_demo(
        n_units=args.units,
        seams_per_unit=args.seams,
        mode=args.mode,
        inference=args.inference,
    )
    print("LAB demo complete.")
    print(f"  mode:                   {summary['mode']}")
    print(f"  inference:              {summary['inference']}")
    print(f"  breadcrumbs written:    {summary['breadcrumbs_written']}")
    print(f"  epochs sealed:          {summary['epochs_sealed']}")
    print(f"  WFM inference calls:    {summary['wfm_inference_calls']}")
    print(f"  epoch id:               {summary['epoch_id']}")
    print(f"  merkle root:            {summary['merkle_root']}")
    print(f"  operator fingerprint:   {summary['operator_fingerprint']}")
    if summary["inference"] == "hive":
        print(f"  --- Hive inference layer ---")
        print(f"  @lab-hai fingerprint:   {summary['lab_hai_fingerprint']}")
        print(f"  Hive calls:             {len(summary['hive_inference_calls'])}")
        if summary["hive_inference_calls"]:
            calls = summary["hive_inference_calls"]
            total_tokens = sum(c["tokens"] for c in calls)
            total_latency = sum(c["latency_ms"] for c in calls)
            workers = {c["worker_h3_cell"] for c in calls}
            print(f"  Total tokens:           {total_tokens}")
            print(f"  Total Hive latency:     {total_latency}ms")
            print(f"  Workers used:           {', '.join(sorted(workers))}")
    if summary["mode"] in ("hybrid", "dryrun"):
        print(f"  --- remote integration ---")
        print(f"  remote success:         {summary['remote_success']}")
        print(f"  remote failed:          {summary['remote_failed']}")
        print(f"  remote pending:         {summary['remote_pending']}")
        print(f"  remote avg latency ms:  {summary['remote_latency_avg']}")
        if summary.get("remote_sample_keys"):
            print(f"  sample composite key:   {summary['remote_sample_keys'][0][:32]}...")
        if summary.get("remote_sample_errors"):
            print(f"  sample error:           {summary['remote_sample_errors'][0]}")
