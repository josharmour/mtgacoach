"""Descriptive public annotations approved for exact Arena prints.

The shipped corpus is independent of the fetched executable card-facts cache.
Both the group ID and all four printed fields must agree. Rebalanced versions
can share the same display name as a reviewed printing.
"""

import hashlib
import json
from functools import lru_cache
from importlib.resources import files

from .validate import PrintedCard

_CORPUS_SHA256 = "a43fde431a32b1f74f7185609a4016c5132cbba1549ff25cea73753a7ff96763"


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
    if type(card) is not dict or type(card.get("grp_id")) is not int or card.get("face_down"):
        return None
    source = corpus()[0].get(card["grp_id"])
    if source and all(
        card.get(field) == getattr(source, field)
        for field in ("name", "type_line", "mana_cost", "oracle_text")
    ):
        return source
    return None
