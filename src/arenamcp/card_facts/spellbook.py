"""Offline relationship projection of ArenaOnAir's complete Spellbook recipes.

Input is the normalized ``combo_record`` payload from build_card_knowledge.py,
not live inference or a two-card combo assertion. The caller supplies source
metadata from the export. Provenance is recorded, not cryptographically verified.
"""

from __future__ import annotations

import copy
from datetime import datetime

SOURCE_URL = "https://json.commanderspellbook.com/variants.json.gz"
RECIPE_FIELDS = frozenset(
    {
        "id",
        "source_url",
        "pieces",
        "additional_requirements",
        "results",
        "mana_needed",
        "easy_prerequisites",
        "notable_prerequisites",
        "steps",
        "notes",
        "legalities",
    }
)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_recipe(recipe: object) -> list[str]:
    """Require the full normalized recipe; preserve unknown fields verbatim."""
    if not isinstance(recipe, dict) or not RECIPE_FIELDS.issubset(recipe):
        return ["recipe: missing complete recipe fields"]
    errors = []
    identity = recipe["id"]
    if not _nonempty(identity) or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in identity
    ):
        errors.append("recipe: invalid ID")
    if recipe["source_url"] != f"https://commanderspellbook.com/combo/{identity}/":
        errors.append("recipe: source URL does not match ID")
    for key in ("mana_needed", "easy_prerequisites", "notable_prerequisites", "steps", "notes"):
        if not isinstance(recipe[key], str):
            errors.append(f"recipe: {key} must be text")
    if not _nonempty(recipe["steps"]):
        errors.append("recipe: steps required")
    legalities = recipe["legalities"]
    if (
        not isinstance(legalities, dict)
        or not legalities
        or any(not isinstance(k, str) or type(v) is not bool for k, v in legalities.items())
    ):
        errors.append("recipe: explicit format legalities required")
    for key in ("pieces", "additional_requirements"):
        pieces = recipe[key]
        if not isinstance(pieces, list) or (key == "pieces" and not pieces):
            errors.append(f"recipe: invalid {key}")
            continue
        for item in pieces:
            if not isinstance(item, dict) or not _nonempty(item.get("name")):
                errors.append(f"recipe: invalid {key} item")
                continue
            if type(item.get("quantity")) is not int or item["quantity"] < 1:
                errors.append(f"recipe: {key} quantity required")
            zones = item.get("zoneLocations")
            if not isinstance(zones, list) or not zones or any(not _nonempty(z) for z in zones):
                errors.append(f"recipe: {key} zones required")
            identity_key = "oracle_id" if key == "pieces" else "scryfall_query"
            if not _nonempty(item.get(identity_key)):
                errors.append(f"recipe: {key} identity required")
    pieces = recipe["pieces"]
    if isinstance(pieces, list):
        identities = {}
        for piece in pieces:
            if isinstance(piece, dict) and _nonempty(piece.get("oracle_id")) and _nonempty(piece.get("name")):
                oid = piece["oracle_id"]
                if oid in identities and identities[oid] != piece["name"]:
                    errors.append("recipe: conflicting piece identity")
                identities[oid] = piece["name"]
    results = recipe["results"]
    if not isinstance(results, list) or not results:
        errors.append("recipe: results required")
    else:
        for item in results:
            if (
                not isinstance(item, dict)
                or not _nonempty(item.get("name"))
                or type(item.get("quantity")) is not int
                or item["quantity"] < 1
            ):
                errors.append("recipe: invalid result")
    return errors


def project_relationships(card_name: str, oracle_id: str, recipes: list[dict], source: dict) -> dict:
    """Return source-attributed links plus intact recipes for an exact identity.

    All inputs are validated before returning; no partial result on a malformed
    export. A matching name with a different Oracle ID is not treated as a match.
    Format legality is retained; no claim is made that any recipe is Arena-legal,
    available in a deck, executable, or currently live.
    """
    if not _nonempty(card_name) or not _nonempty(oracle_id):
        raise ValueError("exact card identity required")
    if not isinstance(source, dict) or source.get("url") != SOURCE_URL:
        raise ValueError("Spellbook export source required")
    for key in ("updated_at", "retrieved_at"):
        value = source.get(key)
        try:
            stamp = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            stamp = None
        if stamp is None or stamp.tzinfo is None:
            raise ValueError("timezone-aware source timestamps required")
    if not isinstance(recipes, list):
        raise ValueError("recipe list required")
    seen = set()
    result = {
        "version": 1,
        "origin": "spellbook",
        "source": copy.deepcopy(source),
        "recipe_links": [],
        "recipes": [],
    }
    for recipe in recipes:
        problems = validate_recipe(recipe)
        if problems:
            raise ValueError("; ".join(problems))
        if recipe["id"] in seen:
            raise ValueError("duplicate recipe ID")
        seen.add(recipe["id"])
        matches = [p for p in recipe["pieces"] if p["oracle_id"] == oracle_id]
        if not matches:
            continue
        if any(p["name"] != card_name for p in matches):
            raise ValueError("conflicting focal card identity")
        result["recipes"].append(copy.deepcopy(recipe))
        partners = {(p["oracle_id"], p["name"]) for p in recipe["pieces"] if p["oracle_id"] != oracle_id}
        for partner_id, partner_name in sorted(partners):
            result["recipe_links"].append(
                {
                    "card": partner_name,
                    "oracle_id": partner_id,
                    "role": "combo_piece",
                    "recipe_id": recipe["id"],
                    "text": f"Listed with {partner_name} in recipe {recipe['id']}; all recipe requirements apply.",
                }
            )
    return result
