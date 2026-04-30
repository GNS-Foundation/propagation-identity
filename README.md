# Propagation Identity

The Layer 5 attestation primitive for Physical AI.

This repository accompanies the paper [*Propagation Identity — The Missing Primitive for Physical AI*](docs/Propagation_Identity_Whitepaper_v0.1_FINAL.pdf) (GNS Foundation / ULISSY S.r.l., April 2026, v0.1.0). It contains the whitepaper, an addendum on World Foundation Models, an operational intelligence brief, an outreach package for the planned Q3 2026 companion paper, and a working reference implementation of the propagation primitive.

## What is propagation identity?

Propagation identity is the property of an agentic physical system whereby every action in the physical world is cryptographically traceable back through the full chain of decisions that produced it — human intent, model inference, runtime planning, actuator command — with each layer signing its own contribution and inheriting constrained authority from the layer above.

The primitive is positioned as **Layer 5** in the existing provenance stack, composing rather than displacing C2PA (L1), SLSA (L2), SPIFFE (L3) and AARM/AP2 (L4). Addendum A introduces a sub-layer L1.5 for World Foundation Model attestation (NVIDIA Cosmos, DeepMind Genie, World Labs Marble).

## Why it exists

Three regulatory deadlines in the European Union compose a forcing function for the primitive:

- **2 August 2026** — EU AI Act Annex III obligations on high-risk standalone AI systems
- **20 January 2027** — Machinery Regulation 2023/1230, classifying AI safety components as subject to traceability of all interactions
- **2 August 2027** — EU AI Act Annex I obligations on AI embedded in regulated machinery

The primitive can be defined and implemented entirely as a composition of existing components — Ed25519, H3, Merkle trees, TPM 2.0, the GNS Protocol, MobyDB, and the ULissy language. The whitepaper documents the construction; the LAB demonstrates it.

## Repository layout

| Path | Contents |
|------|----------|
| [`docs/`](docs/) | The four published artefacts (whitepaper, addendum, intelligence brief, outreach package) |
| [`lab/`](lab/) | Working reference implementation in Python — runs the four-layer chain end to end |
| [`samples/`](samples/) | A signed audit-export package produced by the LAB, suitable for offline verification |
| [`screenshots/`](screenshots/) | Dashboard captures from the LAB |

## The published artefacts

| Document | Audience | Pages |
|----------|----------|-------|
| [Propagation Identity Whitepaper v0.1.0](docs/Propagation_Identity_Whitepaper_v0.1_FINAL.pdf) | Public — regulators, notified bodies, partnership leads | 39 |
| [Addendum A — World Foundation Models](docs/Propagation_Identity_Addendum_A_WFM.pdf) | Public — extends v0.1.0 to v0.1.1 | 11 |

> Operational intelligence and outreach materials are maintained separately for the GNS Foundation and ULISSY S.r.l. operating teams. Inquire via the contact channels of the parent organizations.

## Run the LAB

```bash
cd lab
pip install -r requirements.txt

python -m propagation_lab.demo.run_demo          # simulates a Comau-style welding cell
python -m propagation_lab.verify.run_verify      # verifies the audit package offline
python -m propagation_lab.verify.tamper_tests    # confirms the verifier rejects six attack categories
python -m propagation_lab.dashboard.app          # http://localhost:5050
```

Or with Docker:

```bash
cd lab && docker build -t prop-lab . && docker run -p 5050:5050 prop-lab
```

See [`lab/README.md`](lab/README.md) for full architecture and the production swap-in path.

## Standardization track

This work is intended to be standardized through the IETF as a follow-on Internet-Draft to [`draft-ayerbe-trip-protocol-03`](https://datatracker.ietf.org/doc/draft-ayerbe-trip-protocol/). The proposed identifier is `draft-ayerbe-prop-identity-00`. Coordination is sought with the Cloud Security Alliance TAISE programme, OASIS C2PA, ISO/IEC JTC 1/SC 42, CEN-CENELEC JTC 21, the EU AI Office, and NIST AI Identity.

## License

- Code (the LAB and any reference implementation): **Apache 2.0**
- Documentation (the whitepaper, addendum, briefs): **Creative Commons BY 4.0**

The patent posture, declared in line with the GNS Foundation's Commercial Open Source Software model, is non-assertion against open-source implementations and FRAND licensing for commercial implementations. USPTO Provisional Patent Application #63/948,788 covers the parent Proof-of-Trajectory invention.

## Companion projects

- [GNS Protocol](https://github.com/GNS-Foundation) — the human-identity primitive on which propagation identity composes
- MobyDB — geospatial-native database with H3 cell as primary key
- ULissy — the domain-specific language with `propagates from` as a first-class construct

## Contact

GNS Foundation (Switzerland) and ULISSY S.r.l. (Roma, Italy). For partnership, standardization, or notified-body conversations, open an issue on this repository or use the contact channels of the parent organizations.

> *"The journey is the proof."*
> — GNS Protocol Design Principle
