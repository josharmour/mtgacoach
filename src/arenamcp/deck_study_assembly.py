"""Assemble a private review copy from current validated on-device jobs."""

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from arenamcp.deck_card_study import _cached, _checked, _context, _directory, card_menu
from arenamcp.deck_local_guidance import TOPICS
from arenamcp.deck_strategy import oracle_rules
from arenamcp.deck_study import STUDY_VERSION, study_source, validate_study


def assemble(book, current, context, directory, digest, links, parts, chapters, usage):
    if (
        not links["discovery"]
        or links["groups"] != links["complete_groups"]
        or len(parts) != links["groups"] + 1
    ):
        raise ValueError("All reviewed clusters and the whole-deck interaction search must be complete")
    if any(set(chapters.get(scope, {})) != set(TOPICS[scope]) for scope in ("sequencing", "switches")):
        raise ValueError("Both four-topic chapters must be complete")
    jobs = list(usage)
    roles, commanders, original_sources = [], {}, {}
    for card in card_menu(current):
        gid = card["card"]
        job = _context(book, gid)
        folder = _directory(directory / "local_card_studies", job)
        try:
            row, metrics = _cached(folder / (digest + ".json"), book, job)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ValueError(
                "Every nonland needs a current local role from these same model weights"
            ) from error
        jobs.append({**metrics, "input_sha256": folder.name})
        # The general study validator has a strict schema. Preserve additional
        # commander prerequisites and their per-action citations alongside it.
        role = {k: v for k, v in row.items() if k not in ("action_rules", "commander_conditions")}
        refs = []
        if not _checked(book, [int(i) for i in job["prints"]]):
            raise ValueError("A per-card study no longer matches its original printed sources")
        for ref in role["evidence"]:
            referenced = ref["card"]
            native = current.catalog[referenced]
            if oracle_rules(book.catalog[referenced]) != oracle_rules(native):
                raise ValueError("The current numbered printed rules changed")
            # Prior books can omit empty optional fields or a redundant face-ID
            # vector. Keep the originally validated citation hash as provenance.
            original_sources[str(referenced)] = ref["source"]
            refs.append({**ref, "source": study_source(native)})
        roles.append({**role, "evidence": refs})
        if gid in current.commanders:
            commanders[str(gid)] = {k: row[k] for k in ("action_rules", "commander_conditions")}
    if any(job.get("thinking_mode") == "disabled" for job in jobs):
        raise ValueError("A no-thinking diagnostic cannot form part of an assembled study")
    # A legacy cache can lack provider measurements. Keep them unreported and
    # copy only metrics which belong to this private review format.
    clean_jobs = []
    for job in jobs:
        clean = {"input_sha256": job["input_sha256"]}
        mode = job.get("thinking_mode")
        clean["thinking_mode"] = (
            mode if mode in ("enabled", "low", "medium", "high", "xhigh", "unspecified") else None
        )
        for name in ("seconds", "generated_tokens", "thinking_tokens"):
            value = job.get(name)
            numeric = type(value) is int or (
                name == "seconds" and type(value) is float and math.isfinite(value)
            )
            clean[name] = value if numeric and value >= 0 else None
        clean_jobs.append(clean)
    study = {
        "version": STUDY_VERSION,
        "cards": roles,
        "interactions": [row for part in parts for row in part["interactions"]],
        "minor_links": [row for part in parts for row in part["minor_links"]],
    }
    counts = validate_study(study, current.catalog)
    if not _checked(current, current.catalog):
        raise ValueError("The native deck prints changed during assembly")
    return {
        "version": 1,
        "status": "local_review_candidate",
        "deck_identity": current.identity,
        "model_digest": digest,
        "commanders": context["commanders"],
        "prints": context["prints"],
        "study": study,
        "commander_prerequisites": commanders,
        "original_role_print_digests": original_sources,
        "chapters": chapters,
        "coverage": counts,
        "jobs": clean_jobs,
    }


def save_review_copy(packet, directory):
    # The source identity, print catalogue and writer evidence are inside the
    # same private file. Old reviews remain distinguishable after prints change.
    data = json.dumps(packet, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2).encode()
    checksum = hashlib.sha256(data).hexdigest()
    folder = Path(directory) / "local_deck_reviews" / packet["deck_identity"] / packet["model_digest"]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (checksum + ".json")
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return target
