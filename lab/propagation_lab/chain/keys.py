"""
Ed25519 identity primitives for the propagation chain.

Each layer (operator, model, runtime, actuator) carries an Ed25519 keypair.
Public keys are 32 bytes; signatures are 64 bytes; both are exchanged as
hexadecimal strings throughout the codebase. The "fingerprint" is the
first 8 bytes (16 hex chars) of the public key, used for human-readable
references in the dashboard and the verifier.

This module is intentionally library-thin: it wraps the cryptography
library's Ed25519 primitives and adds nothing else. All higher-level
constructs (delegation certificates, breadcrumbs, etc.) build on this.
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization


@dataclass
class Keypair:
    """An Ed25519 keypair held in memory.

    The private key is stored as the cryptography object; the public key
    is exposed both as the object and as a hex string for serialization.
    """

    private: Ed25519PrivateKey
    public: Ed25519PublicKey

    @classmethod
    def generate(cls) -> "Keypair":
        sk = Ed25519PrivateKey.generate()
        return cls(private=sk, public=sk.public_key())

    @property
    def public_hex(self) -> str:
        raw = self.public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return raw.hex()

    @property
    def fingerprint(self) -> str:
        # First 16 hex chars of the public key — used for human references
        return self.public_hex[:16]

    def sign(self, payload: bytes) -> str:
        return self.private.sign(payload).hex()


def verify(public_hex: str, payload: bytes, signature_hex: str) -> bool:
    """Verify a signature against a hex-encoded public key.

    Returns True iff the signature is valid for the payload under the
    given public key. Catches all signature-validation exceptions and
    returns False; callers do not need to handle exceptions.
    """
    try:
        pk_bytes = bytes.fromhex(public_hex)
        pk = Ed25519PublicKey.from_public_bytes(pk_bytes)
        sig_bytes = bytes.fromhex(signature_hex)
        pk.verify(sig_bytes, payload)
        return True
    except Exception:
        return False
