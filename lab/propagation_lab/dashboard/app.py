"""
Propagation Identity dashboard.

A single-file Flask app that renders the LAB's MobyDB store as a
human-readable web interface. Six routes:

    GET  /              -- summary + breadcrumb stream
    GET  /breadcrumb/<i> -- detail view: 4-layer chain, signatures
    GET  /epochs        -- sealed epoch list with Merkle roots
    GET  /verify        -- run the offline verifier and render results
    GET  /audit-export  -- download the full audit package as JSON
    GET  /api/...       -- JSON endpoints behind the same data

Run:
    python -m propagation_lab.dashboard.app
    open http://localhost:5050

The dashboard is intentionally read-only. It mutates no data; it only
renders what the demo and the sealer have written.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template_string, request, abort, Response

from ..storage.local_mobydb import LocalMobyDB
from ..verify.verifier import verify_audit_package


DB_PATH = os.environ.get("LAB_DB", "lab.mobydb.sqlite")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Stylesheet — applied to every page; matches the GNS / ULISSY palette.
# ---------------------------------------------------------------------------

CSS = """
:root {
    --bg:         #0e1014;
    --bg-card:    #161922;
    --bg-cell:    #1c2030;
    --fg:         #e9eaee;
    --fg-dim:     #8d93a6;
    --accent:     #0099cc;
    --accent-2:   #ffab00;
    --ok:         #00c853;
    --warn:       #ff7043;
    --rule:       #2a2f3d;
    --mono:       'Geist Mono', 'Space Mono', ui-monospace, Menlo, monospace;
    --sans:       'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
}
* { box-sizing: border-box; }
html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--fg);
    font-family: var(--sans); font-weight: 300;
    font-size: 14px; line-height: 1.55;
}
header {
    border-bottom: 1px solid var(--rule);
    padding: 18px 32px;
    display: flex; align-items: center; gap: 16px;
}
header .brand {
    font-weight: 500; letter-spacing: 0.02em; color: var(--fg);
}
header .brand .lab { color: var(--accent); margin-left: 8px; }
header nav { margin-left: auto; display: flex; gap: 24px; }
header nav a {
    color: var(--fg-dim); text-decoration: none; font-size: 13px;
    border-bottom: 1px solid transparent; padding-bottom: 2px;
}
header nav a:hover { color: var(--fg); border-bottom-color: var(--accent); }
header nav a.active { color: var(--fg); border-bottom-color: var(--accent); }

main { padding: 28px 32px; max-width: 1280px; margin: 0 auto; }
h1 { font-size: 24px; font-weight: 400; margin: 0 0 8px; }
h2 { font-size: 16px; font-weight: 500; margin: 28px 0 12px;
     letter-spacing: 0.02em; color: var(--fg); }
h3 { font-size: 13px; font-weight: 500; margin: 16px 0 8px;
     letter-spacing: 0.04em; text-transform: uppercase; color: var(--fg-dim); }
.subtitle { color: var(--fg-dim); font-size: 13px; margin-bottom: 24px; }

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 16px 0 32px; }
.kpi {
    background: var(--bg-card); border: 1px solid var(--rule);
    padding: 16px 20px; border-radius: 6px;
}
.kpi .label { color: var(--fg-dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
.kpi .value { font-size: 28px; font-weight: 300; margin-top: 6px; color: var(--fg); }
.kpi .value.ok { color: var(--ok); }
.kpi .value.accent { color: var(--accent); }

table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--rule); }
th { color: var(--fg-dim); font-weight: 400; text-transform: uppercase; letter-spacing: 0.06em; font-size: 11px; }
tr:hover td { background: var(--bg-cell); }
td a { color: var(--accent); text-decoration: none; }
td a:hover { text-decoration: underline; }

.code { font-family: var(--mono); font-size: 12px; color: var(--fg-dim); word-break: break-all; }
.hex { font-family: var(--mono); font-size: 11px; color: var(--accent); }
.tag {
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-family: var(--mono); font-size: 10px; letter-spacing: 0.04em;
    background: var(--bg-cell); color: var(--fg-dim);
    border: 1px solid var(--rule);
}
.tag.ok { color: var(--ok); border-color: rgba(0,200,83,0.4); }
.tag.warn { color: var(--warn); border-color: rgba(255,112,67,0.4); }
.tag.acc { color: var(--accent); border-color: rgba(0,153,204,0.4); }
.tag.am  { color: var(--accent-2); border-color: rgba(255,171,0,0.4); }

.chain { display: grid; grid-template-columns: 60px 1fr; gap: 8px 16px; margin: 16px 0; }
.chain .layer-name { font-family: var(--mono); font-size: 11px; color: var(--fg-dim);
                     padding-top: 16px; text-transform: uppercase; letter-spacing: 0.04em; }
.chain .card {
    background: var(--bg-card); border: 1px solid var(--rule);
    padding: 14px 18px; border-radius: 6px;
}
.chain .card .top {
    display: flex; align-items: center; gap: 10px; margin-bottom: 8px;
}
.chain .card .who { font-weight: 500; color: var(--fg); }
.chain .arrow { font-family: var(--mono); color: var(--fg-dim); text-align: center;
                font-size: 14px; padding: 4px 0; }

.kv { display: grid; grid-template-columns: 200px 1fr; gap: 6px 16px; }
.kv .k { color: var(--fg-dim); font-size: 12px; }
.kv .v { font-family: var(--mono); font-size: 12px; word-break: break-all; }

pre {
    background: var(--bg-cell); padding: 14px 18px; border-radius: 6px;
    overflow-x: auto; font-family: var(--mono); font-size: 11px;
    line-height: 1.55; color: var(--fg); border: 1px solid var(--rule);
}

.banner {
    margin: 12px 0 24px; padding: 12px 18px;
    border-radius: 6px; border: 1px solid var(--rule);
    background: var(--bg-card);
}
.banner.ok { border-color: rgba(0,200,83,0.4); }
.banner.warn { border-color: rgba(255,112,67,0.4); }
.banner h3 { margin: 0 0 4px; color: var(--fg); }
.banner p { margin: 0; color: var(--fg-dim); font-size: 13px; }

footer {
    border-top: 1px solid var(--rule); padding: 24px 32px;
    color: var(--fg-dim); font-size: 11px; margin-top: 64px;
    display: flex; gap: 24px;
}
footer .epigraph { font-style: italic; }
"""


# ---------------------------------------------------------------------------
# Page templates
# ---------------------------------------------------------------------------

def _layout(title: str, body: str, active: str) -> str:
    nav = ""
    for href, label, key in [
        ("/", "Stream", "stream"),
        ("/epochs", "Epochs", "epochs"),
        ("/verify", "Verify", "verify"),
        ("/audit-export", "Export", "export"),
    ]:
        cls = ' class="active"' if key == active else ""
        nav += f'<a href="{href}"{cls}>{label}</a>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} — Propagation Identity LAB</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&family=Geist+Mono:wght@300;400;500&display=swap" rel="stylesheet">
  <style>{CSS}</style>
</head>
<body>
  <header>
    <span class="brand">Propagation Identity<span class="lab">LAB</span></span>
    <nav>{nav}</nav>
  </header>
  <main>{body}</main>
  <footer>
    <span>GNS Foundation · ULISSY S.r.l.</span>
    <span class="epigraph">"The journey is the proof."</span>
  </footer>
</body>
</html>
"""


def _truncate(s: str, n: int = 16) -> str:
    return s[:n]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    db = LocalMobyDB(DB_PATH)
    breadcrumbs = db.list_breadcrumbs()
    epochs = db.list_epochs()
    db.close()

    if not breadcrumbs:
        body = """
        <h1>No breadcrumbs found</h1>
        <p class="subtitle">The MobyDB store is empty. Run the demo first:</p>
        <pre>python -m propagation_lab.demo.run_demo</pre>
        """
        return _layout("Stream", body, "stream")

    n_wfm = sum(len(b.get("world_model_infer") or []) for b in breadcrumbs)
    operators = {b["delegation"]["operator_pk"] for b in breadcrumbs}

    rows = ""
    for i, b in enumerate(breadcrumbs):
        skill = b["invocation"]["skill_id"]
        seam = b["motion_primitive"].get("seam_id", "")
        wfm_n = len(b.get("world_model_infer") or [])
        wfm_tag = f'<span class="tag am">WFM {wfm_n}</span>' if wfm_n else ""
        rows += f"""
        <tr>
          <td>{i:03d}</td>
          <td><a href="/breadcrumb/{i}"><span class="hex">{_truncate(b['breadcrumb_id'] if 'breadcrumb_id' in b else '')}</span></a></td>
          <td><span class="hex">{_truncate(b['h3_cell'])}</span></td>
          <td>{skill} <span class="tag">{seam}</span></td>
          <td><span class="hex">{_truncate(b['actuator_pk'])}</span> {wfm_tag}</td>
          <td><span class="tag">{b['epoch_id']}</span></td>
        </tr>
        """

    # Compute breadcrumb_id for each row (it isn't stored as a top-level field
    # in the json blob; we recompute on render to match the verifier).
    import hashlib
    from ..chain.canonical import canonicalize as _canon
    rows = ""
    for i, b in enumerate(breadcrumbs):
        bid = hashlib.sha256(_canon(b)).hexdigest()
        skill = b["invocation"]["skill_id"]
        seam = b["motion_primitive"].get("seam_id", "")
        wfm_n = len(b.get("world_model_infer") or [])
        wfm_tag = f'<span class="tag am">WFM x{wfm_n}</span>' if wfm_n else ""
        rows += f"""
        <tr>
          <td>{i:03d}</td>
          <td><a href="/breadcrumb/{i}"><span class="hex">{_truncate(bid)}</span></a></td>
          <td><span class="hex">{_truncate(b['h3_cell'])}</span></td>
          <td>{skill} <span class="tag">{seam}</span></td>
          <td><span class="hex">{_truncate(b['actuator_pk'])}</span> {wfm_tag}</td>
          <td><span class="tag acc">{b['epoch_id']}</span></td>
        </tr>
        """

    body = f"""
    <h1>Breadcrumb stream</h1>
    <p class="subtitle">Live view of signed propagation breadcrumbs in the local MobyDB store.</p>

    <div class="kpi-row">
      <div class="kpi"><div class="label">Breadcrumbs</div><div class="value accent">{len(breadcrumbs)}</div></div>
      <div class="kpi"><div class="label">Sealed epochs</div><div class="value accent">{len(epochs)}</div></div>
      <div class="kpi"><div class="label">WFM inference calls</div><div class="value">{n_wfm}</div></div>
      <div class="kpi"><div class="label">Operators</div><div class="value">{len(operators)}</div></div>
    </div>

    <h2>Stream</h2>
    <table>
      <thead><tr>
        <th>#</th><th>breadcrumb id</th><th>H3 cell</th>
        <th>skill</th><th>actuator</th><th>epoch</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """
    return _layout("Stream", body, "stream")


@app.route("/breadcrumb/<int:idx>")
def breadcrumb_detail(idx: int):
    db = LocalMobyDB(DB_PATH)
    breadcrumbs = db.list_breadcrumbs()
    db.close()
    if idx < 0 or idx >= len(breadcrumbs):
        abort(404)
    b = breadcrumbs[idx]

    import hashlib
    from ..chain.canonical import canonicalize as _canon
    bid = hashlib.sha256(_canon(b)).hexdigest()

    deleg = b["delegation"]
    inf = b["inference"]
    inv = b["invocation"]

    def _layer_card(layer_letter, who_label, pk, hash_value, payload_kv):
        kvs = "".join(
            f'<div class="k">{k}</div><div class="v">{v}</div>'
            for k, v in payload_kv
        )
        return f"""
        <div class="card">
          <div class="top">
            <span class="tag acc">{layer_letter}</span>
            <span class="who">{who_label}</span>
            <span class="hex" style="margin-left:auto">{_truncate(pk)}</span>
          </div>
          <div class="kv">
            <div class="k">payload hash</div><div class="v hex">{_truncate(hash_value, 32)}</div>
            {kvs}
          </div>
        </div>
        """

    operator_card = _layer_card(
        "OP", "Operator delegation", deleg["operator_pk"],
        hashlib.sha256(_canon(deleg)).hexdigest(),
        [
            ("scope skills", ", ".join(deleg["scope_skills"])),
            ("territory cells", str(len(deleg["territory_cells"])) + " cells"),
            ("budget actions", str(deleg["budget_actions"])),
            ("authorized models", str(len(deleg["authorized_model_pks"])) + " keys"),
            ("WFM training",
             (f"{deleg['world_model_train']['provider']} / "
              f"{deleg['world_model_train']['model']} v{deleg['world_model_train']['version']}")
             if deleg.get("world_model_train") else "—"),
        ],
    )
    model_card = _layer_card(
        "MD", "Model inference", inf["model_pk"],
        hashlib.sha256(_canon(inf)).hexdigest(),
        [
            ("parent delegation", _truncate(inf["parent_delegation"], 32)),
            ("prompt hash", _truncate(inf["prompt_hash"], 32)),
            ("plan primitives", str(len(inf["plan_output"].get("primitives", [])))),
            ("reasoning trace", inf["reasoning_trace"][:90] + "…"),
        ],
    )
    runtime_card = _layer_card(
        "RT", "Runtime invocation", inv["runtime_pk"],
        hashlib.sha256(_canon(inv)).hexdigest(),
        [
            ("parent inference", _truncate(inv["parent_inference"], 32)),
            ("skill id", inv["skill_id"]),
            ("preconditions ok", str(inv["preconditions_ok"])),
            ("scope check ok", str(inv["scope_check_ok"])),
            ("parameters", json.dumps(inv["parameters"])),
        ],
    )
    actuator_card = _layer_card(
        "AC", "Actuator action", b["actuator_pk"],
        bid,
        [
            ("parent invocation", _truncate(b["parent_invocation"], 32)),
            ("H3 cell", b["h3_cell"]),
            ("motion", json.dumps(b["motion_primitive"])),
            ("sensor digest", _truncate(b["sensor_digest"], 32)),
            ("epoch", b["epoch_id"]),
            ("TPM quote", _truncate(b.get("tpm_quote") or "—", 32)),
            ("SPIFFE id", b.get("spiffe_id") or "—"),
        ],
    )

    wfm_section = ""
    wfm_calls = b.get("world_model_infer") or []
    if wfm_calls:
        rows = ""
        for c in wfm_calls:
            rows += f"""
            <tr>
              <td><span class="tag am">L1.5</span></td>
              <td>{c['provider']}</td>
              <td>{c['model']}</td>
              <td>{c['version']}</td>
              <td><span class="hex">{_truncate(c['prompt_hash'], 16)}</span></td>
              <td><span class="hex">{_truncate(c['output_hash'], 16)}</span></td>
              <td>{c['attestation']}</td>
            </tr>
            """
        wfm_section = f"""
        <h2>World model invocations <span class="tag am">L1.5</span></h2>
        <p class="subtitle">Per-action WFM calls recorded as part of the propagation chain (Addendum A patch P4).</p>
        <table>
          <thead><tr>
            <th>layer</th><th>provider</th><th>model</th><th>version</th>
            <th>prompt hash</th><th>output hash</th><th>attestation</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        """

    body = f"""
    <h1>Breadcrumb #{idx:03d}</h1>
    <p class="subtitle">Full four-layer signing chain. Click any hex value to inspect upstream.</p>

    <h2>Signing chain</h2>
    <div class="chain">
      <div class="layer-name">L5 OP</div>{operator_card}
      <div class="layer-name"></div><div class="arrow">↓ parent hash chained into L4</div>
      <div class="layer-name">L4 MD</div>{model_card}
      <div class="layer-name"></div><div class="arrow">↓ parent hash chained into L3</div>
      <div class="layer-name">L3 RT</div>{runtime_card}
      <div class="layer-name"></div><div class="arrow">↓ parent hash chained into actuator</div>
      <div class="layer-name">L0 AC</div>{actuator_card}
    </div>

    {wfm_section}

    <h2>Raw record</h2>
    <pre>{json.dumps(b, indent=2, sort_keys=True)}</pre>
    """
    return _layout(f"Breadcrumb {idx:03d}", body, "stream")


@app.route("/epochs")
def epochs_view():
    db = LocalMobyDB(DB_PATH)
    epochs = db.list_epochs()
    db.close()
    rows = ""
    for e in epochs:
        rows += f"""
        <tr>
          <td><span class="tag acc">{e.epoch_id}</span></td>
          <td><span class="hex">{_truncate(e.merkle_root, 32)}…</span></td>
          <td>{len(e.breadcrumb_ids)}</td>
          <td><span class="hex">{_truncate(e.sealer_pk, 16)}</span></td>
          <td><span class="hex">{_truncate(e.sealer_signature, 32)}…</span></td>
        </tr>
        """
    body = f"""
    <h1>Sealed epochs</h1>
    <p class="subtitle">Each epoch is a Merkle-rooted batch of breadcrumbs, signed by the sealer.
       Once sealed, the batch is immutable: editing any breadcrumb invalidates the inclusion proof of every other breadcrumb in the same epoch.</p>
    <table>
      <thead><tr>
        <th>epoch id</th><th>merkle root</th><th>breadcrumbs</th>
        <th>sealer</th><th>sealer signature</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """
    return _layout("Epochs", body, "epochs")


@app.route("/verify")
def verify_view():
    db = LocalMobyDB(DB_PATH)
    breadcrumbs = db.list_breadcrumbs()
    epochs = db.list_epochs()
    db.close()
    results = verify_audit_package(breadcrumbs, epochs)

    n_pass = sum(1 for r in results if r.all_ok)
    banner_class = "ok" if n_pass == len(results) else "warn"
    banner_h = "Audit-export verified" if n_pass == len(results) else "Verification failed"
    banner_p = (f"{n_pass}/{len(results)} breadcrumbs verified end-to-end against the GEP genesis hash and the public keys of the parties."
                if n_pass == len(results)
                else f"{len(results)-n_pass}/{len(results)} breadcrumbs failed verification. See details below.")

    rows = ""
    for i, r in enumerate(results):
        sigs = (
            r.delegation_signature_ok and r.inference_signature_ok
            and r.invocation_signature_ok and r.actuator_signature_ok
        )
        p3 = r.scope_ok and r.expiry_ok and r.model_authorized_ok
        merk = r.epoch_seal_signature_ok and r.merkle_inclusion_ok
        cells = []
        for ok, label in [(sigs, "sigs"), (r.territory_ok, "P2"),
                          (p3, "P3"), (r.chain_continuity_ok, "chain"),
                          (merk, "merkle")]:
            cls = "ok" if ok else "warn"
            cells.append(f'<span class="tag {cls}">{label}</span>')
        verdict_cls = "ok" if r.all_ok else "warn"
        verdict = "PASS" if r.all_ok else "FAIL"
        rows += f"""
        <tr>
          <td>{i:03d}</td>
          <td><a href="/breadcrumb/{i}"><span class="hex">{_truncate(r.breadcrumb_id)}</span></a></td>
          <td>{' '.join(cells)}</td>
          <td><span class="tag {verdict_cls}">{verdict}</span></td>
        </tr>
        """

    body = f"""
    <h1>Offline verification</h1>
    <p class="subtitle">The verifier walks every breadcrumb's chain, checks each signature, enforces scope/territory/expiry, and confirms Merkle inclusion in the sealed epoch.
       It does not contact the operator. Verification is deterministic and constant-time per breadcrumb.</p>

    <div class="banner {banner_class}">
      <h3>{banner_h}</h3>
      <p>{banner_p}</p>
    </div>

    <table>
      <thead><tr>
        <th>#</th><th>breadcrumb</th><th>checks</th><th>verdict</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """
    return _layout("Verify", body, "verify")


@app.route("/audit-export")
def audit_export():
    db = LocalMobyDB(DB_PATH)
    breadcrumbs = db.list_breadcrumbs()
    epochs = db.list_epochs()
    db.close()
    results = verify_audit_package(breadcrumbs, epochs)
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
        "verification_results": [r.to_dict() for r in results],
    }
    body = json.dumps(package, indent=2, sort_keys=True)
    if request.args.get("download") == "1":
        return Response(
            body, mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=audit-export.json"},
        )
    layout_body = f"""
    <h1>Audit-export package</h1>
    <p class="subtitle">The complete signed audit package for an external verifier.
       Contains every breadcrumb, every sealed epoch, and the verification result table.
       <a href="/audit-export?download=1" style="color:var(--accent)">Download as JSON</a></p>
    <pre>{body[:6000]}{('…' if len(body) > 6000 else '')}</pre>
    """
    return _layout("Audit export", layout_body, "export")


@app.route("/api/breadcrumbs")
def api_breadcrumbs():
    db = LocalMobyDB(DB_PATH)
    out = db.list_breadcrumbs()
    db.close()
    return jsonify(out)


@app.route("/api/epochs")
def api_epochs():
    db = LocalMobyDB(DB_PATH)
    epochs = db.list_epochs()
    db.close()
    return jsonify([
        {
            "epoch_id": e.epoch_id, "t_open": e.t_open, "t_close": e.t_close,
            "merkle_root": e.merkle_root, "sealer_pk": e.sealer_pk,
            "sealer_signature": e.sealer_signature,
            "breadcrumb_ids": e.breadcrumb_ids,
        }
        for e in epochs
    ])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5050")), debug=False)
