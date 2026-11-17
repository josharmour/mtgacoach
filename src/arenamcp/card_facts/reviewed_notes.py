"""Descriptive public annotations approved for exact Arena prints.

The shipped corpus is independent of the fetched executable card-facts cache.
Both the group ID and all four printed fields must agree. Rebalanced versions
can share the same display name as a reviewed printing.
"""

import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from .source_identity import single_face_source
from .validate import PrintedCard

_CORPUS_SHA256 = "ea260fefecbd70fc9cb60a6513391c8a68c77bc0802771fa718169e0e8188397"


@lru_cache(maxsize=1)
def corpus():
    from arenamcp.commentary.card_annotations import annotation_digest

    ids, records, printed, digests = {}, {}, {}, {}
    try:
        raw = files("arenamcp.card_facts").joinpath("reviewed_annotations.json").read_bytes()
    except OSError:
        return ids, records, printed, digests
    if hashlib.sha256(raw).hexdigest() != _CORPUS_SHA256:
        return ids, records, printed, digests
    for row in json.loads(raw):
        source = PrintedCard(**row["printed"])
        record = row["record"]
        records[source.name] = record
        printed[source.name] = source
        digests[source.name] = annotation_digest(record)
        for gid in row["grp_ids"]:
            ids[gid] = source
    return ids, records, printed, digests


def reviewed_print(card):
    if (
        type(card) is not dict
        or type(card.get("grp_id")) is not int
        or card.get("face_down")
        or not single_face_source(card)
    ):
        return None
    source = corpus()[0].get(card["grp_id"])
    if source and all(
        card.get(field) == getattr(source, field)
        for field in ("name", "type_line", "mana_cost", "oracle_text")
    ):
        return source
    return None
