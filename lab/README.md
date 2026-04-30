# Propagation Identity LAB

A working proof-of-concept of the Layer-5 propagation identity primitive defined in *Propagation Identity — The Missing Primitive for Physical AI* (GNS Foundation / ULISSY S.r.l., April 2026, v0.1.0) and Addendum A on World Foundation Models.

The LAB simulates a Comau-style welding workcell signing a physical-action chain through four cryptographic layers — operator → model → runtime → actuator — anchoring each action to an H3 hexagonal cell and a Merkle-rooted sealed epoch, and writing the result into a MobyDB-style local store. An offline verifier walks the chain and produces an audit-export package that an external assessor can validate without contacting the operator.

## What the LAB demonstrates

- Four-layer Ed25519 signing chain with parent-hash continuity at every link
- H3 spatial binding and per-cell territory enforcement (whitepaper §9, Property P2)
- Sealed-epoch Merkle commitment with per-breadcrumb inclusion proofs (whitepaper §10)
- WFM hooks per Addendum A: training-time provenance in the delegation certificate, inference-time `world_model_infer` calls per breadcrumb
- Offline verification: the verifier rejects all six tamper categories from the whitepaper's failure-mode catalogue
- Dashboard and audit-export package suitable for regulator or notified-body review

## Quickstart

```bash
# 1. Install dependencies (one-time)
pip install --break-system-packages cryptography h3 flask

# 2. Run the simulator — produces lab.mobydb.sqlite
python -m propagation_lab.demo.run_demo

# 3. Verify the resulting audit package offline
python -m propagation_lab.verify.run_verify

# 4. Run the tamper tests — verifier should reject every attack
python -m propagation_lab.verify.tamper_tests

# 5. Launch the dashboard
python -m propagation_lab.dashboard.app
# open http://localhost:5050
```

## Architecture

```
propagation_lab/
├── chain/
│   ├── keys.py          Ed25519 keypair primitives, signing, verification
│   ├── canonical.py     Sorted-key, null-excluded JSON for cross-language compat
│   ├── spatial.py       H3 cell helpers, Merkle root + inclusion proofs, EpochSealer
│   └── breadcrumb.py    The 4-layer signing chain; WFM hooks (Addendum A)
├── storage/
│   └── local_mobydb.py  SQLite-backed MobyDB-style store; drop-in for Railway prod
├── verify/
│   ├── verifier.py      Offline verifier — checks signatures, P2/P3, Merkle inclusion
│   ├── run_verify.py    CLI driver
│   └── tamper_tests.py  T1–T6 attack tests; verifier must reject all
├── demo/
│   └── run_demo.py      Comau-style welding cell simulator
└── dashboard/
    └── app.py           Single-file Flask UI: stream, detail, epochs, verify, export
```

## The four-layer chain

The whitepaper §8 specifies the chain. The LAB implementation follows it exactly:

```
operator_pk   ──▶  delegation_certificate  (scope, territory, budget, expiry, models)
                                       ↓
model_pk      ──▶  inference            (prompt, scene, plan, reasoning trace)
                                       ↓
runtime_pk    ──▶  skill_invocation     (skill_id, parameters, scope_check_ok)
                                       ↓
actuator_pk   ──▶  physical_action      (motion, sensors, H3 cell, epoch)
```

Each layer signs a payload that includes a hash of the upstream layer's payload. The actuator's signature is therefore an end-to-end commitment to the entire chain. Tampering with any field of any layer invalidates downstream signatures.

## What the verifier checks

For every breadcrumb, the verifier checks eleven independent properties:

1. **delegation signature** valid under operator_pk
2. **inference signature** valid under model_pk
3. **invocation signature** valid under runtime_pk
4. **actuator signature** valid under actuator_pk
5. **territory (P2)**: H3 cell contained in operator's authorized territory
6. **scope (P3)**: skill_id in delegation.scope_skills
7. **expiry**: action timestamp ≤ delegation.expiry_unix
8. **model authorization**: inference.model_pk in delegation.authorized_model_pks
9. **chain continuity**: parent-hash chain resolves
10. **epoch seal**: sealer signature valid over Merkle root
11. **Merkle inclusion**: breadcrumb's leaf hash and inclusion proof verify against the sealed root

A breadcrumb passes verification iff all eleven checks pass.

## The six tamper tests

`python -m propagation_lab.verify.tamper_tests` confirms the verifier rejects each of:

| Test | Attack | Maps to whitepaper |
|------|--------|--------------------|
| T1 | motion primitive altered after signing | §3.1 VLA hallucination |
| T2 | skill outside delegation scope | §3.2 Delegation escape |
| T3 | H3 cell outside authorized territory | §3.3 Spatial violation |
| T4 | unauthorized model_pk | §3.4 Model substitution |
| T5 | sensor readings altered after signing | §3.5 Sensor spoofing |
| T6 | forged Merkle root in sealed epoch | §10 epoch tampering |

## WFM hooks (Addendum A)

The chain carries world-foundation-model provenance in two distinct positions:

**Training-time** (per delegation certificate) — the demo records that the VLA was post-trained on `cosmos-predict-2.5-2b v2.5.0` under the NVIDIA Open Model License. Visible in the dashboard's L5 OP card.

**Inference-time** (per breadcrumb, 0..N entries) — when the planner consults `cosmos-reason-2 v2.0.1` for spatial disambiguation mid-skill, the call is recorded with prompt hash, output hash, output URI and attestation type. Visible as `WFM xN` tags on the stream page.

## MobyDB schema

The LAB ships the schema specified in whitepaper §13 with Addendum A patch P5:

```sql
CREATE TABLE propagation_breadcrumbs (
    h3_cell        TEXT NOT NULL,
    epoch_id       TEXT NOT NULL,
    actor_pk       TEXT NOT NULL,
    timestamp      REAL NOT NULL,
    breadcrumb_id  TEXT NOT NULL,
    record_json    TEXT NOT NULL,
    world_model_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (h3_cell, epoch_id, actor_pk, timestamp)
);
```

The composite primary key matches MobyDB's native 48-byte `(cell, epoch, identity)` layout. To swap in the real Railway-hosted MobyDB at `mobydb-production.up.railway.app`, replace `LocalMobyDB` with a `RemoteMobyDB` adapter exposing the same four methods. No other code changes.

## Audit-export package

The `/audit-export` endpoint and `python -m propagation_lab.verify.run_verify --export package.json` produce a self-contained JSON package containing:

- every sealed epoch (epoch id, t_open, t_close, Merkle root, sealer signature, breadcrumb ids)
- every breadcrumb (full signed envelope with all four signatures and parent hashes)
- the verification result table

A notified body receiving this package can verify it offline against the GEP genesis hash and the public keys of the parties. No contact with the operator is required.

## Production swap-in path

Each adapter in the LAB is designed to be replaced by its production counterpart with no changes to the rest of the code:

| LAB component | Production replacement |
|---------------|------------------------|
| `LocalMobyDB` | `RemoteMobyDB` against `mobydb-production.up.railway.app` |
| `Comau-style demo` | `RealFlowstateAdapter` wrapping an actual Flowstate skill |
| `Cosmos Reason 2 stub` | Real Cosmos Reason 2 inference call against NVIDIA NGC API |
| `software actuator key` | TPM 2.0-rooted hardware attestation key |
| `manual sealer` | Scheduled sealer service with Stellar mainnet anchoring |

## License

Apache 2.0 (matching the Intrinsic SDK and the GNS Foundation default).

## Companion documents

- `Propagation_Identity_Whitepaper_v0.1_FINAL.docx` — the public whitepaper
- `Propagation_Identity_Addendum_A_WFM.docx` — the World Foundation Models addendum
- `Research_v2_Intelligence_Brief.docx` — operational intelligence (internal)
- `Companion_Paper_CoAuthoring_Proposal.docx` — outreach package for the Q3 2026 companion paper
