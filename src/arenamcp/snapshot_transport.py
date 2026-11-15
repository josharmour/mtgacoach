"""Lossless, self-contained bundle spelling of an already redacted v1 snapshot."""

from __future__ import annotations

import collections
import json
import math

# Match the native bundle endpoint's decompressed JSON ceiling. Packing must
# never make previously oversized native data eligible for upload or replay.
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64


def _key(value):
    if type(value) is str:
        return (str, value) if len(value) >= 16 else None
    if type(value) in (dict, list):
        spelling = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(spelling) >= 128:
            return type(value), spelling
    return None


def pack_snapshot(snapshot):
    """Share object shapes and repeated values within this decision only."""
    if type(snapshot) is not dict or set(snapshot) != {"version", "state"}:
        return snapshot
    if (
        type(snapshot["version"]) is not int
        or snapshot["version"] != 1
        or type(snapshot["state"]) is not dict
    ):
        return snapshot
    counts = collections.Counter()

    def count(value):
        if (key := _key(value)) is not None:
            counts[key] += 1
        if isinstance(value, dict):
            for child in value.values():
                count(child)
        elif isinstance(value, list):
            for child in value:
                count(child)

    shapes, indices, values = {}, {}, []

    def encode(value, allow_ref=True):
        key = _key(value) if allow_ref else None
        share = key is not None and counts[key] > 1
        if share and key in indices:
            return {"=": indices[key]}
        if isinstance(value, dict):
            keys = tuple(value)
            shape = shapes.setdefault(keys, len(shapes))
            result = {str(shape): [encode(value[k], allow_ref and not share) for k in keys]}
        elif isinstance(value, list):
            result = [encode(child, allow_ref and not share) for child in value]
        else:
            result = value
        if share:
            indices[key] = len(values)
            values.append(result)
            return {"=": indices[key]}
        return result

    count(snapshot["state"])
    state = encode(snapshot["state"])
    return {"version": 2, "shapes": [list(keys) for keys in shapes], "table": values, "state": state}


def unpack_snapshot(snapshot, *, remaining=None):
    """Expand a packed snapshot before schema, identity checks, or board replay.

    There are no cross-snapshot references, nested references or discarded
    table fields. A bounded traversal charges repeated materialized values.
    """
    if type(snapshot) is not dict or snapshot.get("version") != 2:
        return snapshot
    if type(snapshot["version"]) is not int or set(snapshot) != {"version", "shapes", "table", "state"}:
        raise ValueError("Invalid snapshot envelope")
    shapes, table = snapshot["shapes"], snapshot["table"]
    if type(shapes) is not list or len(shapes) > 4096 or type(table) is not list or len(table) > 65536:
        raise ValueError("Invalid snapshot table")
    for keys in shapes:
        if (
            type(keys) is not list
            or len(keys) > 1024
            or not all(type(k) is str for k in keys)
            or len(keys) != len(set(keys))
        ):
            raise ValueError("Invalid snapshot object shape")
    used_shapes, used_values = set(), set()
    size = 0

    def charge(n):
        nonlocal size
        size += n
        if size > MAX_SNAPSHOT_BYTES or (remaining is not None and size > remaining[0]):
            raise ValueError("Expanded snapshot exceeds replay budget")

    def expand(value, depth, allow_ref):
        if depth > MAX_DEPTH:
            raise ValueError("Snapshot depth exceeded")
        charge(2)
        if type(value) is list:
            return [expand(child, depth + 1, allow_ref) for child in value]
        if type(value) is dict:
            if len(value) != 1:
                raise ValueError("Invalid snapshot object")
            tag, payload = next(iter(value.items()))
            if tag == "=":
                if not allow_ref or type(payload) is not int or not 0 <= payload < len(table):
                    raise ValueError("Invalid snapshot value reference")
                used_values.add(payload)
                return expand(table[payload], depth + 1, False)
            if type(tag) is not str or len(tag) > 4 or not tag.isascii() or not tag.isdecimal():
                raise ValueError("Invalid snapshot shape reference")
            index = int(tag)
            if str(index) != tag or index >= len(shapes):
                raise ValueError("Invalid snapshot shape index")
            keys = shapes[index]
            if type(payload) is not list or len(payload) != len(keys):
                raise ValueError("Invalid snapshot field count")
            used_shapes.add(index)
            charge(sum(len(k.encode("utf-8")) + 4 for k in keys))
            return {k: expand(child, depth + 1, allow_ref) for k, child in zip(keys, payload, strict=True)}
        if type(value) is str:
            charge(len(value.encode("utf-8")))
        elif value is None or type(value) in (int, bool) or (type(value) is float and math.isfinite(value)):
            charge(len(str(value)))
        else:
            raise ValueError("Invalid snapshot value")
        return value

    state = expand(snapshot["state"], 0, True)
    if type(state) is not dict or len(used_shapes) != len(shapes) or len(used_values) != len(table):
        raise ValueError("Unused snapshot data or invalid state")
    if remaining is not None:
        remaining[0] -= size
    return {"version": 1, "state": state}


def expand_bundle_snapshots(bundle):
    """Expose all packed fields to redaction and whole-bundle identity discovery."""
    packet = bundle.get("packet") if isinstance(bundle, dict) else None
    rows = packet.get("decisions") if isinstance(packet, dict) else None
    if not isinstance(rows, list):
        return bundle
    expanded_rows = None
    remaining = [MAX_JSON_BYTES]
    for index, row in enumerate(rows):
        snapshot = row.get("state_snapshot") if isinstance(row, dict) else None
        if not isinstance(snapshot, dict) or snapshot.get("version") != 2:
            continue
        if expanded_rows is None:
            expanded_rows = rows.copy()
        expanded_rows[index] = {**row, "state_snapshot": unpack_snapshot(snapshot, remaining=remaining)}
    if expanded_rows is None:
        return bundle
    expanded = {**bundle, "packet": {**packet, "decisions": expanded_rows}}
    if len(json.dumps(expanded, ensure_ascii=False, default=str).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("Expanded bundle exceeds native limit")
    return expanded
