"""
Tamper tests — confirm the verifier fails closed under attack.

A signing system that always passes is no system at all. This module
exercises every category of attack the propagation chain claims to
defend against, and asserts that the verifier produces FAIL for each.

The categories mirror the §3 failure modes of the whitepaper:

    T1  -- VLA hallucination     -> motion primitive altered after signing
    T2  -- Delegation escape     -> skill_id outside scope
    T3  -- Spatial violation     -> H3 cell outside authorized territory
    T4  -- Model substitution    -> inference signed by unauthorized key
    T5  -- Sensor spoofing       -> sensor_readings altered after signing
    T6  -- Epoch tampering       -> a breadcrumb claims to be in the
                                    epoch but was not Merkle-sealed in it

Run:
    python -m propagation_lab.verify.tamper_tests
"""
from __future__ import annotations

import copy
import hashlib
import sys
from typing import List, Tuple

from ..chain.canonical import canonicalize
from ..chain.spatial import SealedEpoch
from ..storage.local_mobydb import LocalMobyDB
from .verifier import verify_audit_package, VerificationResult


def _load_clean() -> Tuple[List[dict], List[SealedEpoch]]:
    db = LocalMobyDB("lab.mobydb.sqlite")
    breadcrumbs = db.list_breadcrumbs()
    epochs = db.list_epochs()
    db.close()
    if not breadcrumbs:
        print("Run the demo first: python -m propagation_lab.demo.run_demo")
        sys.exit(1)
    return breadcrumbs, epochs


def _expect_failed(label: str, results: List[VerificationResult],
                   target_index: int) -> bool:
    target = results[target_index]
    if target.all_ok:
        print(f"  {label:<40}  UNEXPECTED PASS  (verifier did not catch attack)")
        return False
    print(f"  {label:<40}  FAIL (correctly caught)")
    return True


def main() -> int:
    print("Tamper tests — verifier should reject each tampered breadcrumb.")
    print("-" * 78)

    all_caught = True

    # ------ T1: motion primitive altered after signing ------
    breadcrumbs, epochs = _load_clean()
    tampered = copy.deepcopy(breadcrumbs)
    tampered[0]["motion_primitive"]["seam_id"] = "WRONG-SEAM"
    results = verify_audit_package(tampered, epochs)
    all_caught &= _expect_failed("T1 motion primitive altered", results, 0)

    # ------ T2: skill outside scope (delegation escape) ------
    breadcrumbs, epochs = _load_clean()
    tampered = copy.deepcopy(breadcrumbs)
    tampered[0]["invocation"]["skill_id"] = "disassemble"  # not in scope
    results = verify_audit_package(tampered, epochs)
    all_caught &= _expect_failed("T2 skill outside delegation scope", results, 0)

    # ------ T3: H3 cell outside authorized territory ------
    breadcrumbs, epochs = _load_clean()
    tampered = copy.deepcopy(breadcrumbs)
    # Pick a cell halfway across Europe
    import h3
    rogue_cell = h3.latlng_to_cell(40.4168, -3.7038, 12)  # Madrid
    tampered[0]["h3_cell"] = rogue_cell
    results = verify_audit_package(tampered, epochs)
    all_caught &= _expect_failed("T3 H3 cell outside territory", results, 0)

    # ------ T4: model not in authorized_model_pks ------
    breadcrumbs, epochs = _load_clean()
    tampered = copy.deepcopy(breadcrumbs)
    rogue_pk = "00" * 32  # an obviously unauthorized key
    tampered[0]["inference"]["model_pk"] = rogue_pk
    results = verify_audit_package(tampered, epochs)
    all_caught &= _expect_failed("T4 unauthorized model_pk", results, 0)

    # ------ T5: sensor reading altered after signing ------
    breadcrumbs, epochs = _load_clean()
    tampered = copy.deepcopy(breadcrumbs)
    tampered[0]["sensor_readings"]["current_A"] = 999.99
    results = verify_audit_package(tampered, epochs)
    all_caught &= _expect_failed("T5 sensor readings altered", results, 0)

    # ------ T6: epoch tampering — fake Merkle root ------
    breadcrumbs, epochs = _load_clean()
    tampered_epochs = [
        SealedEpoch(
            epoch_id=epochs[0].epoch_id,
            t_open=epochs[0].t_open,
            t_close=epochs[0].t_close,
            merkle_root="ff" * 32,  # invalid root
            sealer_pk=epochs[0].sealer_pk,
            sealer_signature=epochs[0].sealer_signature,
            breadcrumb_ids=epochs[0].breadcrumb_ids,
        )
    ]
    results = verify_audit_package(breadcrumbs, tampered_epochs)
    all_caught &= _expect_failed(
        "T6 forged Merkle root in sealed epoch", results, 0
    )

    print("-" * 78)
    if all_caught:
        print("All tamper categories correctly rejected. Verifier fails closed.")
        return 0
    print("FAILURE: at least one tamper category passed. Verifier is broken.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
