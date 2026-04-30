# LAB Demo — 3-Minute Video Script

A timed walk-through of the Propagation Identity LAB. Total runtime: 3:00.
Shoot the screen, narrate the script verbatim or in the operator's voice.

The script is timed for a deliberate pace — slower than typical product demos,
because the audience (regulators, notified bodies, partnership leads) reads
along with the screen.

---

## Pre-flight (do not record)

```bash
cd propagation_lab
rm -f lab.mobydb.sqlite
clear
```

Have two terminal windows open: a left "command" window, a right "browser"
window with http://localhost:5050 ready to refresh.

---

## 0:00 — 0:20  Opening frame (terminal left, blank browser right)

> **Narrator:**
> "This is Propagation Identity — the Layer 5 attestation primitive for
> physical AI defined in our April 2026 whitepaper. What you're about to
> see is a working proof of concept of the entire chain: a robot signs
> every action through four cryptographic layers, the chain anchors to an
> H3 spatial cell and a Merkle-rooted epoch, and an external auditor
> verifies the result offline."

[Show the project tree briefly with `ls propagation_lab/`.]

---

## 0:20 — 0:50  Run the simulator

```bash
python -m propagation_lab.demo.run_demo
```

> **Narrator:**
> "Step one: the simulator. This models a Comau-style welding cell at a
> German automotive plant. Three units, five seams each — fifteen
> physical actions. Each action is signed by four parties: the factory
> operator who issued the delegation, the planner model that produced
> the inference, the runtime that scheduled the skill, and the actuator
> that executed the motion."

[The terminal prints:]
```
LAB demo complete.
  breadcrumbs written:    15
  epochs sealed:          1
  WFM inference calls:    3
  epoch id:               bmw-cell-7-shift-1-0001
  merkle root:            <hex>
```

> **Narrator:**
> "Fifteen breadcrumbs, one sealed epoch, three calls to NVIDIA Cosmos
> Reason 2 for mid-action spatial reasoning — those are the WFM hooks
> from Addendum A."

---

## 0:50 — 1:30  Open the dashboard (switch to browser)

```bash
python -m propagation_lab.dashboard.app &
```

[Switch to browser, refresh http://localhost:5050.]

> **Narrator:**
> "The dashboard. Top of the page: fifteen breadcrumbs, one sealed
> epoch, three world-model invocations, one operator. Three rows are
> tagged WFM — those are the actions where the planner consulted Cosmos
> at runtime."

[Click row #002 — one with the amber WFM tag.]

> **Narrator:**
> "Inside any breadcrumb, the four-layer chain renders top to bottom.
> L5 — operator delegation. Notice the training-time WFM provenance
> right here: the VLA executing this skill was distilled from
> cosmos-predict-2.5-2b under the NVIDIA Open Model License. That's
> recorded once, in the delegation certificate, per Addendum A.
> L4 — model inference, with the parent hash chained back to the
> delegation. L3 — runtime invocation. L0 — actuator action with the
> H3 cell, the sensor digest, and the SPIFFE workload identity."

[Scroll down to the WFM invocations section.]

> **Narrator:**
> "Below the chain: the per-action WFM call. Provider, model, version,
> prompt hash, output hash, attestation type. The call itself is a
> pointer — the actual Cosmos output is stored in object storage; only
> the hash is on the chain."

---

## 1:30 — 2:10  Run the verifier

[Switch back to terminal.]

```bash
python -m propagation_lab.verify.run_verify
```

> **Narrator:**
> "Now the auditor's view. The verifier walks every breadcrumb's chain
> offline — no contact with the operator, no trust in the operator's
> infrastructure. It checks eleven independent properties per
> breadcrumb: every signature, the territory containment, the scope
> match, the expiry window, the model authorization, the chain
> continuity, the epoch seal, the Merkle inclusion proof."

[Wait for the table.]

```
Aggregate: 15/15 breadcrumbs verified end-to-end
```

> **Narrator:**
> "Fifteen out of fifteen verified. Every signature correct, every
> spatial constraint satisfied, every Merkle inclusion proof resolved
> against the sealed epoch root."

---

## 2:10 — 2:45  Run the tamper tests

```bash
python -m propagation_lab.verify.tamper_tests
```

> **Narrator:**
> "The verifier passing on clean data is a typing exercise. The
> property that matters is whether it fails closed under attack. Six
> tamper tests, mapping to the five concrete failure modes in §3 of
> the whitepaper plus epoch tampering."

[Wait for output.]

```
T1 motion primitive altered               FAIL (correctly caught)
T2 skill outside delegation scope         FAIL (correctly caught)
T3 H3 cell outside territory              FAIL (correctly caught)
T4 unauthorized model_pk                  FAIL (correctly caught)
T5 sensor readings altered                FAIL (correctly caught)
T6 forged Merkle root in sealed epoch     FAIL (correctly caught)

All tamper categories correctly rejected. Verifier fails closed.
```

> **Narrator:**
> "Six out of six. VLA hallucination caught. Delegation escape caught.
> Spatial violation caught. Model substitution caught. Sensor spoofing
> caught. Epoch tampering caught."

---

## 2:45 — 3:00  Closing

[Switch to browser, navigate to /audit-export.]

> **Narrator:**
> "And the audit-export package — the file an external assessor
> downloads to verify the deployment offline. Every signed breadcrumb,
> every sealed epoch, every verification result. Self-contained,
> deterministic, ready for a notified-body assessment under the
> Machinery Regulation 2023/1230."

[End on the export page or the README.]

> **Narrator:**
> "Propagation Identity. The journey is the proof."

---

## Notes on delivery

- **Pace.** Three minutes is short. Read the script aloud once before
  recording; it should land at 2:55–3:05 with no rushing.
- **Voice.** First-person plural ("our", "we") if recorded by Camilo;
  third-person ("the GNS Foundation has built") if recorded by an
  ULISSY engineer.
- **No fluff.** No "thanks for watching", no "as you can see", no
  rhetorical questions. Every sentence should carry a fact.
- **Aspect ratio.** 1920×1080 for partnership and regulator audiences;
  1080×1920 portrait if cutting a 60-second LinkedIn version.
- **Captions.** Burn-in English captions; the regulatory and
  notified-body audience often watches muted.

## A 60-second cut

If a 60-second LinkedIn version is needed, drop the dashboard tour and
keep only:

- 0:00–0:15  Run the simulator. Show the summary line.
- 0:15–0:35  Run the verifier. Show 15/15 PASS.
- 0:35–0:55  Run the tamper tests. Show six FAIL-correctly-caught lines.
- 0:55–1:00  "Propagation Identity. The journey is the proof."
