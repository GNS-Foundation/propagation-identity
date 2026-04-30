"""
CLI driver for the offline verifier.

Reads breadcrumbs and sealed epochs from a LocalMobyDB store, runs
verify_audit_package, and prints a per-breadcrumb result table along
with an aggregate verdict.

Run:
    python -m propagation_lab.verify.run_verify
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List

from ..storage.local_mobydb import LocalMobyDB
from .verifier import verify_audit_package, VerificationResult


def _yes(v: bool) -> str:
    return "OK" if v else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a propagation audit package")
    parser.add_argument("--db", default="lab.mobydb.sqlite",
                        help="Path to the local MobyDB SQLite store")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON results instead of a table")
    parser.add_argument("--export", default=None,
                        help="If set, write the audit-export JSON package to this path")
    args = parser.parse_args()

    db = LocalMobyDB(args.db)
    breadcrumbs = db.list_breadcrumbs()
    epochs = db.list_epochs()
    db.close()

    if not breadcrumbs:
        print(f"No breadcrumbs found in {args.db}.")
        print("Run `python -m propagation_lab.demo.run_demo` first.")
        return 1

    results: List[VerificationResult] = verify_audit_package(breadcrumbs, epochs)

    # Optional audit-export package
    if args.export:
        package = {
            "epochs": [
                {
                    "epoch_id": e.epoch_id,
                    "t_open": e.t_open,
                    "t_close": e.t_close,
                    "merkle_root": e.merkle_root,
                    "sealer_pk": e.sealer_pk,
                    "sealer_signature": e.sealer_signature,
                    "breadcrumb_ids": e.breadcrumb_ids,
                }
                for e in epochs
            ],
            "breadcrumbs": breadcrumbs,
            "results": [r.to_dict() for r in results],
        }
        with open(args.export, "w") as f:
            json.dump(package, f, indent=2, sort_keys=True)
        print(f"Wrote audit-export package to {args.export}")

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True))
        return 0 if all(r.all_ok for r in results) else 2

    # Pretty table
    print()
    print(f"Audit-export verification — {len(results)} breadcrumbs, "
          f"{len(epochs)} sealed epoch(s)")
    print("-" * 95)
    header = f"{'#':>3}  {'breadcrumb id':<16}  {'cell':<16}  {'sigs':<6}  {'P2':<3}  {'P3':<3}  {'merkle':<6}  verdict"
    print(header)
    print("-" * 95)
    for i, r in enumerate(results):
        sigs = (
            r.delegation_signature_ok
            and r.inference_signature_ok
            and r.invocation_signature_ok
            and r.actuator_signature_ok
        )
        p3 = r.scope_ok and r.expiry_ok and r.model_authorized_ok
        merk = r.epoch_seal_signature_ok and r.merkle_inclusion_ok
        verdict = "PASS" if r.all_ok else "FAIL"
        print(
            f"{i:>3}  {r.breadcrumb_id[:16]:<16}  {r.h3_cell[:16]:<16}  "
            f"{_yes(sigs):<6}  {_yes(r.territory_ok):<3}  {_yes(p3):<3}  "
            f"{_yes(merk):<6}  {verdict}"
        )
        for note in r.notes:
            print(f"     note: {note}")

    print("-" * 95)
    n_pass = sum(1 for r in results if r.all_ok)
    print(f"Aggregate: {n_pass}/{len(results)} breadcrumbs verified end-to-end")
    print()
    return 0 if n_pass == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
