"""Python port of mage.player.ai.encoder.Features hashing (WillWroble/mage fork).

Verified against xmage/FeatureTable.txt: 3,730 of 3,730 root-level entries and
nested probes across Player/Opponent/Battlefield/Hand/Stack/Exile subtrees
(see magezero-integration-review.md, section 3.1).
"""

from __future__ import annotations

M64 = (1 << 64) - 1
GLOBAL_SEED = -7046029288634856825 & M64
TABLE_SIZE = 2_000_000


def _s(x: int) -> int:
    """Convert unsigned 64-bit int to signed 64-bit int."""
    x &= M64
    return x - (1 << 64) if x >> 63 else x


def mix64(z: int) -> int:
    """MurmurHash3 64-bit finalizer / mixing function."""
    z &= M64
    z = ((z ^ (z >> 30)) * (-4658895280553007687 & M64)) & M64
    z = ((z ^ (z >> 27)) * (-7723592293110705685 & M64)) & M64
    return z ^ (z >> 31)


def rotl(x: int, r: int) -> int:
    """Rotate left 64-bit unsigned integer."""
    x &= M64
    return ((x << r) | (x >> (64 - r))) & M64


def hash64(s: str, seed: int) -> int:
    """XMage 64-bit string hasher seeded with parent namespace seed."""
    data = s.encode("utf-8")
    h = mix64((seed ^ (len(data) * GLOBAL_SEED)) & M64)
    i = 0
    while len(data) - i >= 8:
        k = int.from_bytes(data[i : i + 8], "little", signed=False)
        h ^= mix64(k)
        h = (rotl(h, 27) * GLOBAL_SEED + (1609587929392839161 & M64)) & M64
        i += 8
    tail = 0
    for j, b in enumerate(data[i:]):
        tail ^= (b & 0xFF) << (8 * j)
    h ^= mix64(tail & M64)
    h ^= h >> 33
    h = (h * (-49064778989728563 & M64)) & M64
    h ^= h >> 33
    h = (h * (-4265267296055464877 & M64)) & M64
    h ^= h >> 33
    return h & M64


def index_for(h: int) -> int:
    """Map 64-bit hash into [0, TABLE_SIZE - 1] matching Java abs(hash) % 2_000_000."""
    v = _s(h)
    if v < 0:
        v = -v
        v = _s(v)
    return int(abs(v) % TABLE_SIZE) if v >= 0 else -((-v) % TABLE_SIZE)


def path_index(path: list[str]) -> int:
    """Compute leaf index by walking seeded namespace chain from GLOBAL_SEED.

    Example:
        path_index(["Player#1", "LifeTotal@10#1"]) -> 147844
        path_index(["Player#1", "Battlefield#1", "Malcolm, Alluring Scoundrel#1", "Tapped#1"]) -> 1951888
    """
    seed = GLOBAL_SEED
    for name in path[:-1]:
        seed = hash64(name, seed)
    return index_for(hash64(path[-1], seed))
