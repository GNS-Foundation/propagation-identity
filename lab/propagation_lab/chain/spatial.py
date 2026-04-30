"""
Spatial and temporal anchors: H3 cells and sealed epochs.

This module implements §9 and §10 of the Propagation Identity whitepaper.
The H3 hexagonal grid provides spatial binding; the epoch sealing
provides temporal immutability via a Merkle root.

GEP genesis hash (constant): every breadcrumb in the chain references
this hash, fixing the H3 grid to a specific Earth datum and a specific
genesis time. The genesis hash is non-secret and public-domain.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import List

import h3


GEP_GENESIS_HASH = "26acb5d998b63d54f2ed92851c5c565db9fe0930fc06b06091d05c0ce4ff8289"


def cell_at(latitude: float, longitude: float, resolution: int = 12) -> str:
    """Return the H3 cell containing (latitude, longitude) at the given resolution.

    Resolution 12 corresponds to roughly 240 m² hexagons — appropriate for
    factory-floor scope. Resolution 15 (~0.9 m²) is the recommended default
    for action-level spatial binding in the whitepaper, but resolution 12
    keeps the demo readable.
    """
    return h3.latlng_to_cell(latitude, longitude, resolution)


def cell_contains(parent_cell: str, child_cell: str) -> bool:
    """Return True iff child_cell is contained in parent_cell.

    Uses H3's native hierarchical containment relation. A delegation
    certificate authorized at resolution 9 is enforceable at resolution
    15 by repeated parent traversal.
    """
    if parent_cell == child_cell:
        return True
    parent_res = h3.get_resolution(parent_cell)
    child_res = h3.get_resolution(child_cell)
    if child_res < parent_res:
        return False
    walker = child_cell
    while h3.get_resolution(walker) > parent_res:
        walker = h3.cell_to_parent(walker)
    return walker == parent_cell


@dataclass
class Breadcrumb:
    """In-memory representation of a propagation breadcrumb.

    The full signed structure of the breadcrumb is built in
    chain/breadcrumb.py; this dataclass is the storage-side view used
    by the epoch sealer.
    """

    breadcrumb_id: str  # SHA-256 of the canonical signed breadcrumb
    h3_cell: str
    timestamp: float
    actor_pk: str
    payload_hash: str  # for the Merkle root


@dataclass
class SealedEpoch:
    """A sealed epoch — an immutable batch of breadcrumbs.

    The Merkle root binds every breadcrumb in the epoch into a single
    32-byte commitment. Once sealed, no breadcrumb in the epoch can be
    modified without invalidating the inclusion proof of every other
    breadcrumb.
    """

    epoch_id: str
    t_open: float
    t_close: float
    merkle_root: str  # 64 hex chars
    breadcrumb_ids: List[str] = field(default_factory=list)
    sealer_pk: str = ""
    sealer_signature: str = ""


def _merkle_pair(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(left + right).digest()


def merkle_root(leaves: List[bytes]) -> bytes:
    """Compute a binary-tree Merkle root over leaves.

    Empty input returns an all-zero hash. Odd numbers of nodes at any
    level are handled by duplicating the last node — the common
    Bitcoin/RFC-9162 convention.
    """
    if not leaves:
        return b"\x00" * 32
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [_merkle_pair(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def merkle_inclusion_proof(leaves: List[bytes], target_index: int) -> List[str]:
    """Return the sibling hashes needed to prove leaf inclusion.

    The returned list is a sequence of hex-encoded sibling hashes,
    bottom-up. A verifier walks the leaf hash up the tree, hashing
    against each sibling in turn, and checks the result against the
    Merkle root.
    """
    proof: List[str] = []
    layer = list(leaves)
    idx = target_index
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        sibling_idx = idx ^ 1  # flip the low bit
        proof.append(layer[sibling_idx].hex())
        layer = [_merkle_pair(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)]
        idx //= 2
    return proof


def verify_inclusion(
    leaf_hash: bytes,
    leaf_index: int,
    proof: List[str],
    expected_root: str,
) -> bool:
    """Verify a Merkle inclusion proof.

    Walks the leaf up the tree using the sibling hashes in proof, and
    returns True iff the resulting root matches expected_root.
    """
    walker = leaf_hash
    idx = leaf_index
    for sibling_hex in proof:
        sibling = bytes.fromhex(sibling_hex)
        if idx % 2 == 0:
            walker = _merkle_pair(walker, sibling)
        else:
            walker = _merkle_pair(sibling, walker)
        idx //= 2
    return walker.hex() == expected_root


class EpochSealer:
    """Accumulates breadcrumbs and seals them into Merkle-rooted epochs.

    The sealer pre-allocates the epoch_id on `open_epoch()`, before any
    breadcrumb is signed. This is the property that makes the actuator
    signatures and the Merkle leaves stable across the seal operation:
    by the time a breadcrumb is signed, its `epoch_id` is final.
    """

    def __init__(self, sealer_keypair, epoch_label: str = "default"):
        self.sealer_keypair = sealer_keypair
        self.epoch_label = epoch_label
        self._buffer: List[Breadcrumb] = []
        self._t_open = time.time()
        self._epoch_counter = 0
        self._current_epoch_id: str = self._next_epoch_id()

    def _next_epoch_id(self) -> str:
        self._epoch_counter += 1
        return f"{self.epoch_label}-{self._epoch_counter:04d}"

    @property
    def current_epoch_id(self) -> str:
        """The id of the open (not-yet-sealed) epoch.

        Breadcrumbs being signed during this epoch must include this id
        in their actuator-signed body, so that the signature remains
        valid when the epoch is later sealed.
        """
        return self._current_epoch_id

    def add(self, breadcrumb: Breadcrumb) -> None:
        self._buffer.append(breadcrumb)

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    def seal(self) -> SealedEpoch:
        """Seal the buffered breadcrumbs into an immutable epoch.

        Resets the buffer, the open-time, and pre-allocates the next
        epoch_id for subsequent breadcrumbs. The sealed epoch record is
        signed by the sealer.
        """
        from .canonical import canonicalize

        t_close = time.time()
        leaves = [bytes.fromhex(b.payload_hash) for b in self._buffer]
        root = merkle_root(leaves)

        unsigned = {
            "epoch_id": self._current_epoch_id,
            "t_open": self._t_open,
            "t_close": t_close,
            "merkle_root": root.hex(),
            "breadcrumb_count": len(self._buffer),
            "sealer_pk": self.sealer_keypair.public_hex,
            "gep_genesis": GEP_GENESIS_HASH,
        }
        sig = self.sealer_keypair.sign(canonicalize(unsigned))

        sealed = SealedEpoch(
            epoch_id=self._current_epoch_id,
            t_open=self._t_open,
            t_close=t_close,
            merkle_root=root.hex(),
            breadcrumb_ids=[b.breadcrumb_id for b in self._buffer],
            sealer_pk=self.sealer_keypair.public_hex,
            sealer_signature=sig,
        )

        self._buffer = []
        self._t_open = t_close
        self._current_epoch_id = self._next_epoch_id()
        return sealed
