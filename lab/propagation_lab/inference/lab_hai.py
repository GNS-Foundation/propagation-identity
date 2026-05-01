"""
@lab-hai — the LAB's signed-inference actor.

This module wraps HiveClient with persistent identity and signing, so the
LAB's model layer produces structurally identical signed inferences to
production @hai. The differences are exactly two:

    1. The signing key is @lab-hai, not @hai (different fingerprint,
       clearly tagged as demo / development).
    2. Records are tagged with a 'lab' marker in the envelope so a
       downstream observer can distinguish LAB demo data from production
       @hai data.

Everything else — the canonical JSON shape, the Ed25519 signing, the
hash conventions, the H3 cell / epoch / written_at_ms triple — matches
what gns-backend/src/services/ai_bot.ts produces today.

The keypair is persisted at ~/.propagation-lab/keys/lab-hai.key with
0600 permissions. First run generates it; subsequent runs load it. The
LAB's dashboard always shows the same @lab-hai fingerprint, which is
what we want for demos.
"""
from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from ..chain.canonical import canonicalize
from ..chain.keys import Keypair
from ..chain.mobydb_format import current_epoch
from .hive_client import ChatMessage, HiveClient, HiveError, HiveInferenceResult


log = logging.getLogger(__name__)


DEFAULT_KEY_DIR = Path.home() / ".propagation-lab" / "keys"
DEFAULT_KEY_FILE = DEFAULT_KEY_DIR / "lab-hai.key"


@dataclass
class SignedHaiInference:
    """A Hive inference plus the signed envelope @lab-hai stamped on it.

    This is the structure the LAB's demo runner consumes when wiring the
    model layer of the propagation chain. The fields mirror the `cd`
    block in ai_bot.ts's writeInferenceToMobyDB, plus the worker H3
    cell that Hive reports.
    """
    # The inference itself
    prompt:           str
    response:         str
    model:            str
    tokens:           int

    # The envelope, signed by @lab-hai
    cd:               Dict[str, Any]
    signature:        str  # hex Ed25519 signature over canonical(cd)
    lab_hai_pk:       str  # hex public key of @lab-hai

    # Hive-side metadata
    worker_h3_cell:   Optional[str]
    job_id:           Optional[str]
    latency_ms:       int


class LabHai:
    """Persistent @lab-hai actor.

    Use:
        lab_hai = LabHai.load_or_create(h3_cell='871e9a0ecffffff')  # Rome
        signed = lab_hai.sign_inference("Plan five welds for unit SKU 1842")
        # signed.signature is verifiable under signed.lab_hai_pk
    """

    def __init__(
        self,
        keypair: Keypair,
        hive_client: HiveClient,
        jurisdiction: str = "IT",
        site_h3_cell: str = None,
    ):
        self.keypair = keypair
        self.hive = hive_client
        self.jurisdiction = jurisdiction
        # site_h3_cell is the LAB's own location anchor for the inference;
        # this is distinct from worker_h3_cell, which is where Hive's
        # worker physically ran. The chain records both.
        self.site_h3_cell = site_h3_cell or "871e9a0ecffffff"  # Rome — matches ai_bot.ts default

    # ---- factories ----

    @classmethod
    def load_or_create(
        cls,
        key_path: Path = None,
        hive_client: HiveClient = None,
        jurisdiction: str = "IT",
        site_h3_cell: str = None,
    ) -> "LabHai":
        """Load the persistent @lab-hai key, generating it if missing.

        Args:
            key_path: Override the default storage path.
            hive_client: If provided, use this client; else build one
                from environment variables, bound to @lab-hai's PK.
            jurisdiction: ISO country code for the cd.jurisdiction field.
            site_h3_cell: H3 cell of the calling site. Defaults to Rome.
        """
        key_path = key_path or DEFAULT_KEY_FILE
        keypair = cls._load_or_generate_keypair(key_path)
        if hive_client is None:
            hive_client = HiveClient(public_key_hex=keypair.public_hex)
        else:
            # Caller-provided client; verify it's bound to the right PK.
            if hive_client.public_key_hex != keypair.public_hex:
                raise ValueError(
                    "HiveClient public_key_hex does not match @lab-hai's keypair; "
                    "construct the HiveClient with lab_hai.public_hex"
                )
        return cls(
            keypair=keypair,
            hive_client=hive_client,
            jurisdiction=jurisdiction,
            site_h3_cell=site_h3_cell,
        )

    @staticmethod
    def _load_or_generate_keypair(key_path: Path) -> Keypair:
        """Load an existing keypair from disk, or generate and persist one.

        The on-disk format is the raw 32-byte Ed25519 private key, written
        with 0600 permissions. The directory is created with 0700.
        """
        if key_path.exists():
            with key_path.open("rb") as f:
                raw = f.read()
            if len(raw) != 32:
                raise ValueError(
                    f"@lab-hai key file at {key_path} is not 32 bytes; "
                    f"refusing to load. Delete the file and re-run to regenerate."
                )
            sk = Ed25519PrivateKey.from_private_bytes(raw)
            kp = Keypair(private=sk, public=sk.public_key())
            log.info("Loaded @lab-hai keypair (fp: %s)", kp.fingerprint)
            return kp

        # Generate, persist with strict permissions
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kp = Keypair.generate()
        raw = kp.private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with key_path.open("wb") as f:
            f.write(raw)
        # 0600 (owner read/write only). Run chmod explicitly because
        # umask might broaden the implicit mode at create time.
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        log.info("Generated @lab-hai keypair (fp: %s) → %s", kp.fingerprint, key_path)
        return kp

    @property
    def public_hex(self) -> str:
        return self.keypair.public_hex

    @property
    def fingerprint(self) -> str:
        return self.keypair.fingerprint

    # ---- core operation ----

    def sign_inference(
        self,
        prompt: str,
        requester_pk: str = None,
        session_id: str = None,
        system_prompt: str = None,
        max_tokens: int = 256,
    ) -> SignedHaiInference:
        """Run a real Hive inference and produce a signed envelope.

        Calls Hive over HTTP, hashes the prompt and response, builds the
        envelope in the canonical shape ai_bot.ts uses, signs it with
        @lab-hai's key, returns the result.

        Raises HiveError if Hive cannot serve the request (timeout,
        no workers, HTTP error). The LAB caller decides what to do.

        Args:
            prompt:        the user message that drives the inference.
            requester_pk:  optional GNS PK of the entity asking for the
                           inference. Defaults to a synthetic LAB requester.
            session_id:    optional session correlator. Defaults to a hash
                           of the prompt for determinism in tests.
            system_prompt: optional system message. None means no system msg.
            max_tokens:    cap on response length.
        """
        messages: List[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role="system", content=system_prompt))
        messages.append(ChatMessage(role="user", content=prompt))

        # ---- Real Hive call ----
        result: HiveInferenceResult = self.hive.chat(
            messages=messages,
            max_tokens=max_tokens,
        )

        # ---- Build the envelope (matches ai_bot.ts cd shape) ----
        prompt_hash = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        response_hash = "sha256:" + hashlib.sha256(result.content.encode("utf-8")).hexdigest()

        if session_id is None:
            session_id = "lab-" + hashlib.sha256(
                (prompt + str(result.tokens)).encode("utf-8")
            ).hexdigest()[:16]
        if requester_pk is None:
            # Synthetic requester used by the LAB demo runner. Real demos
            # can override this to attribute the inference to a specific
            # GNS identity.
            requester_pk = "lab-requester-" + self.fingerprint

        cd = {
            "data": {
                "jurisdiction":    self.jurisdiction,
                "model":           result.model,
                "prompt_hash":     prompt_hash,
                "requester_pk":    requester_pk,
                "response_hash":   response_hash,
                "session_id":      session_id,
                "tokens":          result.tokens,
                # LAB-specific tags
                "lab_marker":      "@lab-hai",
                "worker_h3_cell":  result.worker_h3_cell,
                "hive_job_id":     result.job_id,
            },
            "epoch":         current_epoch(),
            "h3_cell":       self.site_h3_cell,
            "payload_type":  "lab/inference",
            "public_key":    self.public_hex,
            "written_at_ms": int(__import__("time").time() * 1000),
        }

        signature = self.keypair.sign(canonicalize(cd))

        return SignedHaiInference(
            prompt=prompt,
            response=result.content,
            model=result.model,
            tokens=result.tokens,
            cd=cd,
            signature=signature,
            lab_hai_pk=self.public_hex,
            worker_h3_cell=result.worker_h3_cell,
            job_id=result.job_id,
            latency_ms=result.latency_ms,
        )
