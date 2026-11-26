"""Clone and adapt near-qualifying Arena decks for events using owned cards only.

Never mutates any pre-existing deck and never redeems wildcards. Format rules
(sets, bans, commander constraints, deck sizes, singleton limits, rarity quotas)
come from Arena's own ``GetFormats`` log payload and local CardDatabase SQLite;
final legality of the cloned deck is verified natively via Arena's
``DeckViewInfo.GetValidationForFormat(event_format)``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arenamcp.event_queue import normalize_format_key

logger = logging.getLogger(__name__)

_COLOR_NUM_TO_LETTER = {"1": "W", "2": "U", "3": "B", "4": "R", "5": "G"}
_COLOR_ORDER = ("W", "U", "B", "R", "G")
_BASIC_NAMES = ("Plains", "Island", "Swamp", "Mountain", "Forest")
_BASIC_COLOR_BY_NAME = {
    "Plains": "W",
    "Island": "U",
    "Swamp": "B",
    "Mountain": "R",
    "Forest": "G",
}
# Standard fallback printings used only when the player owns zero copies of a basic grp_id.
_DEFAULT_BASIC_GRP_IDS = {
    "Plains": 91301,
    "Island": 91303,
    "Swamp": 91305,
    "Mountain": 91307,
    "Forest": 91309,
}
_MTGA_RARITY_NAMES = {
    1: "common",
    2: "common",
    3: "uncommon",
    4: "rare",
    5: "mythic",
}
# Format RarityPerCardQuotasInternal keys: 0=Common, 1=Uncommon, 2=Rare, 3=Mythic.
_FORMAT_RARITY_KEYS = {
    "0": "common",
    "1": "uncommon",
    "2": "rare",
    "3": "mythic",
}
_MANA_PIP_RE = re.compile(r"\{([^}]+)\}")
_REMINDER_RE = re.compile(r"\([^)]*\)")


@dataclass(frozen=True)
class EventFormatRules:
    """Format construction rules decoded from Arena's GetFormats response."""

    name: str
    legal_sets: frozenset[str]
    allowed_title_ids: frozenset[int] = frozenset()
    banned_title_ids: frozenset[int] = frozenset()
    banned_as_commander_title_ids: frozenset[int] = frozenset()
    allowed_commander_title_ids: frozenset[int] = frozenset()
    main_min: int = 60
    main_max: int = 250
    sideboard_max: int = 15
    command_zone_min: int = 0
    command_zone_max: int = 0
    singleton: bool = False
    use_rebalanced: bool = False
    restrict_color_identity: bool = False
    rarity_max_copies: dict[str, int] = field(default_factory=dict)
    individual_card_quotas: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class EventCardInfo:
    """Metadata for one candidate Arena printing used in deck adaptation."""

    grp_id: int
    title_id: int
    name: str
    rarity: str
    expansion_code: str
    color_identity: frozenset[str]
    colors: frozenset[str]
    cmc: int
    mana_cost: str
    type_line: str
    oracle_text: str
    is_land: bool
    is_creature: bool
    is_planeswalker: bool
    is_legendary: bool
    is_basic_land: bool
    is_primary: bool
    is_token: bool
    is_rebalanced: bool
    rebalanced_grp_id: int = 0
    alternate_deck_limit: int = 0
    power_num: int = 0
    toughness_num: int = 0


@dataclass
class AdaptedDeckPlan:
    """Complete owned-only deck build plan plus cut/add summary."""

    ok: bool
    error: str = ""
    main: list[tuple[int, int]] = field(default_factory=list)
    sideboard: list[tuple[int, int]] = field(default_factory=list)
    command_zone: list[tuple[int, int]] = field(default_factory=list)
    companions: list[tuple[int, int]] = field(default_factory=list)
    kept_count: int = 0
    added_count: int = 0
    cut_cards: list[tuple[int, str, int, str]] = field(default_factory=list)
    added_cards: list[tuple[int, str, int]] = field(default_factory=list)
    commander_swapped: bool = False
    old_commander_name: str = ""
    new_commander_name: str = ""


def find_player_log_candidates(explicit_path: Path | str | None = None) -> list[Path]:
    """Return existing MTGA Player.log / Player-prev.log paths, most recent first."""
    candidates: list[Path] = []
    if explicit_path:
        p = Path(explicit_path)
        candidates.extend([p, p.with_name("Player-prev.log")])
    try:
        from arenamcp.settings import get_settings

        setting_path = get_settings().get("mtga_log_path")
        if setting_path:
            sp = Path(setting_path)
            candidates.extend([sp, sp.with_name("Player-prev.log")])
    except Exception:
        pass

    home = Path.home()
    defaults = [
        home / "Library/Logs/Wizards Of The Coast/MTGA/Player.log",
        home / "Library/Logs/Wizards Of The Coast/MTGA/Player-prev.log",
        home / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log",
        home / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player-prev.log",
        home
        / ".local/share/Steam/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log",
        home
        / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log",
    ]
    candidates.extend(defaults)
    seen: set[Path] = set()
    existing: list[tuple[float, Path]] = []
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            existing.append((path.stat().st_mtime, path))
        except OSError:
            continue
    existing.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in existing]


def parse_event_format_rules(entry: dict[str, Any]) -> EventFormatRules:
    """Convert one Arena ``GetFormats`` JSON entry into ``EventFormatRules``."""
    main_quota = entry.get("mainDeckQuota") or {}
    side_quota = entry.get("sideBoardQuota") or {}
    cmd_quota = entry.get("commandZoneQuota") or {}
    cmd_min = int(cmd_quota.get("min") or 0)
    cmd_max = int(cmd_quota.get("max") or cmd_min)

    banned = (
        {int(x) for x in entry.get("bannedTitleIds") or [] if int(x) > 0}
        | {int(x) for x in entry.get("suspendedTitleIds") or [] if int(x) > 0}
        | {int(x) for x in entry.get("supressedTitleIds") or [] if int(x) > 0}
    )

    rarity_caps: dict[str, int] = {}
    raw_rarity = entry.get("RarityPerCardQuotasInternal")
    if isinstance(raw_rarity, dict):
        for raw_key, quota in raw_rarity.items():
            rarity_name = _FORMAT_RARITY_KEYS.get(str(raw_key))
            if not rarity_name or not isinstance(quota, dict):
                continue
            rarity_caps[rarity_name] = int(quota.get("max") or 0)

    indiv: dict[int, int] = {}
    raw_indiv = entry.get("individualCardQuotas")
    if isinstance(raw_indiv, dict):
        for raw_tid, quota in raw_indiv.items():
            try:
                tid = int(raw_tid)
            except (TypeError, ValueError):
                continue
            if tid > 0 and isinstance(quota, dict):
                indiv[tid] = int(quota.get("max") or 0)

    sideboard_max = int(side_quota["max"]) if "max" in side_quota else (0 if cmd_min >= 1 else 15)

    return EventFormatRules(
        name=str(entry.get("name") or ""),
        legal_sets=frozenset(str(s) for s in entry.get("legalSets") or [] if s),
        allowed_title_ids=frozenset(int(x) for x in entry.get("allowedTitleIds") or [] if int(x) > 0),
        banned_title_ids=frozenset(banned),
        banned_as_commander_title_ids=frozenset(
            int(x) for x in entry.get("BannedAsCommanderTitleIds") or [] if int(x) > 0
        ),
        allowed_commander_title_ids=frozenset(
            int(x) for x in entry.get("AllowedCommanderTitleIds") or [] if int(x) > 0
        ),
        main_min=int(main_quota.get("min") or 60),
        main_max=int(main_quota.get("max") or 250),
        sideboard_max=sideboard_max,
        command_zone_min=cmd_min,
        command_zone_max=cmd_max,
        singleton=int(entry.get("CardCountRestrictionInternal") or 0) == 1,
        use_rebalanced=bool(entry.get("useRebalancedCards", False)),
        restrict_color_identity=cmd_min >= 1 or bool(entry.get("RestrictToColorIdentityInternal", False)),
        rarity_max_copies=rarity_caps,
        individual_card_quotas=indiv,
    )


def load_event_format_rules(format_name: str, log_path: Path | str | None = None) -> EventFormatRules | None:
    """Load *format_name*'s rules from the newest ``<== GetFormats(`` payload in Arena logs."""
    wanted = str(format_name or "").strip()
    if not wanted:
        return None
    wanted_norm = normalize_format_key(wanted)
    decoder = json.JSONDecoder()
    for path in find_player_log_candidates(log_path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pos = len(text)
        while True:
            idx = text.rfind("<== GetFormats(", 0, pos)
            if idx < 0:
                break
            brace = text.find("{", idx)
            if brace < 0:
                pos = idx
                continue
            try:
                payload, _ = decoder.raw_decode(text, brace)
            except ValueError:
                pos = idx
                continue
            formats = payload.get("Formats") if isinstance(payload, dict) else None
            if isinstance(formats, list):
                exact = next(
                    (f for f in formats if isinstance(f, dict) and str(f.get("name") or "") == wanted),
                    None,
                )
                if exact is not None:
                    return parse_event_format_rules(exact)
                approx = next(
                    (
                        f
                        for f in formats
                        if isinstance(f, dict)
                        and normalize_format_key(str(f.get("name") or "")) == wanted_norm
                    ),
                    None,
                )
                if approx is not None:
                    return parse_event_format_rules(approx)
            pos = idx
    return None


def describe_format_rules(rules: EventFormatRules) -> str:
    """Short player-readable summary of *rules*."""
    parts: list[str] = []
    if rules.command_zone_min >= 1:
        parts.append(f"{rules.main_min} + {rules.command_zone_min} commander")
    elif rules.main_min == rules.main_max:
        parts.append(f"{rules.main_min} cards")
    else:
        parts.append(f"{rules.main_min}+ cards")
    if rules.singleton:
        parts.append("singleton")
    if rules.rarity_max_copies:
        disallowed = {r for r, lim in rules.rarity_max_copies.items() if lim <= 0}
        allowed = [r for r in ("common", "uncommon", "rare", "mythic") if r not in disallowed]
        if allowed == ["common"]:
            parts.append("commons only")
        elif allowed == ["common", "uncommon"]:
            parts.append("commons & uncommons only")
        elif allowed and disallowed:
            parts.append("/".join(allowed) + " only")
    return ", ".join(parts)


def _parse_color_nums(raw: Any) -> frozenset[str]:
    out: set[str] = set()
    for token in str(raw or "").split(","):
        ch = _COLOR_NUM_TO_LETTER.get(token.strip())
        if ch:
            out.add(ch)
    return frozenset(out)


def _parse_mana_pips(mana_cost: str, oracle_text: str = "") -> Counter[str]:
    pips: Counter[str] = Counter()
    sources = [mana_cost]
    if oracle_text:
        sources.append(_REMINDER_RE.sub("", oracle_text))
    for text in sources:
        for group in _MANA_PIP_RE.findall(text or ""):
            for sym in group.upper():
                if sym in _COLOR_ORDER:
                    pips[sym] += 1
    return pips


def _parse_cmc(raw_cmc: Any, mana_cost: str) -> int:
    if isinstance(raw_cmc, (int, float)) and raw_cmc >= 0:
        return int(raw_cmc)
    total = 0
    for group in _MANA_PIP_RE.findall(mana_cost or ""):
        token = group.strip().upper()
        if token.isdigit():
            total += int(token)
        elif token not in {"X", "Y", "Z"}:
            total += 1
    return total


def _parse_stat(raw: str) -> int:
    try:
        return int(str(raw or "").strip())
    except ValueError:
        return 0


def _decode_mana_text(raw: Any) -> str:
    symbols = re.findall(r"o(\([^()]+\)|\d+|[WUBRGCXYZS])", str(raw or ""))
    return "".join("{" + sym.strip("()") + "}" for sym in symbols)


def load_event_card_catalog(
    collection: dict[int, int],
    extra_grp_ids: set[int],
    rules: EventFormatRules,
    mtgadb: Any,
) -> tuple[dict[int, EventCardInfo], set[int]]:
    """Load owned/deck/basic card metadata and legal TitleIds from local ``MTGADatabase``.

    Returns ``(catalog_by_grp_id, legal_title_ids)``.
    """
    if mtgadb is None or not getattr(mtgadb, "available", False):
        return {}, set()
    wanted_raw = {int(g) for g, c in collection.items() if int(g) > 0 and int(c) > 0} | {
        int(g) for g in extra_grp_ids if int(g) > 0
    }
    wanted_raw.update(_DEFAULT_BASIC_GRP_IDS.values())

    with mtgadb._conn_lock:
        conn = getattr(mtgadb, "_conn", None)
        if conn is None:
            return {}, set()
        cols = set(getattr(mtgadb, "_card_columns", set()))
        select_cols = [
            "GrpId",
            "TitleId",
            "Rarity",
            "ExpansionCode",
            "IsPrimaryCard",
            "IsToken",
            "IsRebalanced",
            "RebalancedCardGrpId",
            "AlternateDeckLimit",
            "Colors",
            "ColorIdentity",
            "Supertypes",
            "Types",
            "Subtypes",
            "LinkedFaceGrpIds",
            "OldSchoolManaText",
            "Order_CMCWithXLast",
        ]
        sql_cols = ", ".join(c if c in cols else f"NULL AS {c}" for c in select_cols)
        rows = conn.execute(f"SELECT {sql_cols} FROM Cards").fetchall()  # noqa: S608 - fixed col names

    row_by_gid: dict[int, Any] = {}
    legal_titles: set[int] = set(rules.allowed_title_ids)
    basic_gids: set[int] = set()
    legal_sets = set(rules.legal_sets)

    for row in rows:
        gid = int(row["GrpId"] or 0)
        if gid <= 0:
            continue
        is_token = bool(row["IsToken"])
        if is_token:
            continue
        row_by_gid[gid] = row
        is_primary = row["IsPrimaryCard"] in (1, None, True)
        is_basic = (
            str(row["Supertypes"] or "") == "1"
            and str(row["Types"] or "") == "5"
            and str(row["Subtypes"] or "").isdigit()
            and int(row["Subtypes"] or 0) > 0
        )
        if is_basic and gid in wanted_raw:
            basic_gids.add(gid)
        tid = int(row["TitleId"] or 0)
        if (
            tid > 0
            and is_primary
            and (
                is_basic
                or (not legal_sets and not rules.allowed_title_ids)
                or str(row["ExpansionCode"] or "") in legal_sets
            )
        ):
            legal_titles.add(tid)

    # Ensure every basic land type (Plains/Island/Swamp/Mountain/Forest) is represented.
    seen_basic_titles: set[int] = {int(row_by_gid[g]["TitleId"] or 0) for g in basic_gids if g in row_by_gid}
    if len(seen_basic_titles) < 5:
        for gid, row in row_by_gid.items():
            if (
                str(row["Supertypes"] or "") == "1"
                and str(row["Types"] or "") == "5"
                and str(row["Subtypes"] or "").isdigit()
                and int(row["Subtypes"] or 0) > 0
            ):
                tid = int(row["TitleId"] or 0)
                if tid not in seen_basic_titles:
                    seen_basic_titles.add(tid)
                    basic_gids.add(gid)
                    if len(seen_basic_titles) >= 5:
                        break

    legal_titles -= set(rules.banned_title_ids)

    # Expand to include linked faces for color-identity resolution and any rebalanced twins.
    fetch_ids: set[int] = (wanted_raw | basic_gids) & set(row_by_gid)
    extra_faces: set[int] = set()
    for gid in list(fetch_ids):
        row = row_by_gid[gid]
        reb = int(row["RebalancedCardGrpId"] or 0)
        if reb and reb in row_by_gid and reb in wanted_raw:
            extra_faces.add(reb)
        for token in str(row["LinkedFaceGrpIds"] or "").split(","):
            if token.strip().isdigit():
                fgid = int(token.strip())
                if fgid in row_by_gid:
                    extra_faces.add(fgid)
    fetch_ids |= extra_faces

    card_objs: dict[int, Any] = {}
    sorted_ids = sorted(fetch_ids)
    for offset in range(0, len(sorted_ids), 500):
        card_objs.update(mtgadb.get_cards_batch(sorted_ids[offset : offset + 500]))

    catalog: dict[int, EventCardInfo] = {}
    for gid in sorted(wanted_raw | basic_gids):
        row = row_by_gid.get(gid)
        if row is None:
            continue
        card_obj = card_objs.get(gid)
        name = str(getattr(card_obj, "name", "") or f"Card_{gid}")
        type_line = str(getattr(card_obj, "type_line", "") or "")
        oracle_text = str(getattr(card_obj, "oracle_text", "") or "")
        mana_cost = str(getattr(card_obj, "mana_cost", "") or "") or _decode_mana_text(
            row["OldSchoolManaText"]
        )

        supertypes = {s.strip() for s in str(row["Supertypes"] or "").split(",") if s.strip()}
        types_set = {t.strip() for t in str(row["Types"] or "").split(",") if t.strip()}
        is_basic = (
            "1" in supertypes
            and "5" in types_set
            and name in _BASIC_NAMES
            and (not type_line or "Basic Land" in type_line)
        )
        is_land = "5" in types_set or "Land" in type_line
        is_creature = "2" in types_set or "Creature" in type_line
        is_planeswalker = "8" in types_set or "Planeswalker" in type_line
        is_legendary = "2" in supertypes or "Legendary" in type_line

        ci: set[str] = set(_parse_color_nums(row["ColorIdentity"]) or _parse_color_nums(row["Colors"]))
        for token in str(row["LinkedFaceGrpIds"] or "").split(","):
            if not token.strip().isdigit():
                continue
            face_gid = int(token.strip())
            face_row = row_by_gid.get(face_gid)
            if face_row is not None:
                ci |= _parse_color_nums(face_row["ColorIdentity"]) | _parse_color_nums(face_row["Colors"])
            face_obj = card_objs.get(face_gid)
            if face_obj is not None:
                ci.update(
                    _parse_mana_pips(
                        getattr(face_obj, "mana_cost", ""),
                        getattr(face_obj, "oracle_text", ""),
                    ).keys()
                )
        ci.update(_parse_mana_pips(mana_cost, oracle_text).keys())
        if is_basic and name in _BASIC_COLOR_BY_NAME:
            ci.add(_BASIC_COLOR_BY_NAME[name])

        rarity = _MTGA_RARITY_NAMES.get(int(row["Rarity"] or 0), "") or str(
            getattr(card_obj, "rarity", "") or ""
        )
        catalog[gid] = EventCardInfo(
            grp_id=gid,
            title_id=int(row["TitleId"] or 0),
            name=name,
            rarity=rarity,
            expansion_code=str(row["ExpansionCode"] or ""),
            color_identity=frozenset(ci),
            colors=_parse_color_nums(row["Colors"]),
            cmc=_parse_cmc(row["Order_CMCWithXLast"], mana_cost),
            mana_cost=mana_cost,
            type_line=type_line,
            oracle_text=oracle_text,
            is_land=is_land,
            is_creature=is_creature,
            is_planeswalker=is_planeswalker,
            is_legendary=is_legendary,
            is_basic_land=is_basic,
            is_primary=row["IsPrimaryCard"] in (1, None, True),
            is_token=False,
            is_rebalanced=bool(row["IsRebalanced"]),
            rebalanced_grp_id=int(row["RebalancedCardGrpId"] or 0),
            alternate_deck_limit=int(row["AlternateDeckLimit"] or 0),
            power_num=_parse_stat(getattr(card_obj, "power", "")),
            toughness_num=_parse_stat(getattr(card_obj, "toughness", "")),
        )
    return catalog, legal_titles


def max_copies_in_format(card: EventCardInfo, rules: EventFormatRules, legal_titles: set[int]) -> int:
    """Maximum copies of *card* allowed in *rules*, or 0 if illegal."""
    if card.is_token:
        return 0
    if card.is_basic_land:
        return 250
    if not card.is_primary:
        return 0
    if card.is_rebalanced and not rules.use_rebalanced:
        return 0
    if rules.use_rebalanced and card.rebalanced_grp_id > 0 and not card.is_rebalanced:
        return 0
    if card.title_id <= 0 or card.title_id not in legal_titles:
        return 0
    if card.title_id in rules.banned_title_ids:
        return 0
    if card.rarity in rules.rarity_max_copies and rules.rarity_max_copies[card.rarity] <= 0:
        return 0

    limit = 1 if rules.singleton else 4
    if card.alternate_deck_limit > 0 and not rules.singleton:
        limit = card.alternate_deck_limit
    if card.rarity in rules.rarity_max_copies:
        limit = min(limit, rules.rarity_max_copies[card.rarity])
    if card.title_id in rules.individual_card_quotas:
        limit = min(limit, rules.individual_card_quotas[card.title_id])
    return max(0, limit)


def is_legal_commander(card: EventCardInfo, rules: EventFormatRules, legal_titles: set[int]) -> bool:
    """True when *card* is a legal commander in *rules*."""
    if max_copies_in_format(card, rules, legal_titles) <= 0:
        return False
    if card.is_basic_land or card.title_id in rules.banned_as_commander_title_ids:
        return False
    if rules.allowed_commander_title_ids:
        return card.title_id in rules.allowed_commander_title_ids
    return (card.is_legendary and (card.is_creature or card.is_planeswalker)) or (
        "can be your commander" in card.oracle_text.lower()
    )


def _pile_counts(raw_pile: Any) -> dict[int, int]:
    counts: dict[int, int] = {}
    for entry in raw_pile or []:
        if isinstance(entry, dict):
            gid = int(entry.get("grp_id") or 0)
            qty = int(entry.get("count") or 0)
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            gid = int(entry[0] or 0)
            qty = int(entry[1] or 0)
        else:
            continue
        if gid > 0 and qty > 0:
            counts[gid] = counts.get(gid, 0) + qty
    return counts


def _score_replacement(
    card: EventCardInfo,
    *,
    target_cmc: float,
    want_creature_ratio: float,
    color_weights: Counter[str],
    existing_creatures: int,
    existing_nonlands: int,
) -> tuple[float, int]:
    """Heuristic score (higher is better) preferring playable curve/role fits."""
    score = 0.0
    # Rarity preference: uncommon slightly over common, both good.
    if card.rarity == "uncommon":
        score += 2.0
    elif card.rarity == "common":
        score += 1.0

    # Role balance: match creature vs spell proportion cleanly.
    cur_ratio = (existing_creatures / max(1, existing_nonlands)) if existing_nonlands else 0.5
    need_creature = cur_ratio < want_creature_ratio
    if card.is_creature and need_creature:
        score += 4.0
    elif not card.is_creature and not need_creature:
        score += 3.5

    # Curve match: stay near target CMC (capped around 3.0) and avoid clunky top-end.
    ideal = max(2.0, min(3.5, target_cmc))
    score -= abs(card.cmc - ideal) * 1.4
    if card.cmc >= 6:
        score -= (card.cmc - 5) * 1.8

    text = card.oracle_text.lower()
    if any(kw in text for kw in ("destroy target", "exile target", "deals ", "counter target")):
        score += 2.5
    if any(kw in text for kw in ("draw a card", "draw two", "add {", "search your library for a basic")):
        score += 2.2
    if card.is_creature and card.cmc > 0:
        stats = card.power_num + card.toughness_num
        if stats >= 2 * card.cmc:
            score += 1.5
    elif card.is_creature and card.cmc == 0:
        score += 0.5

    # Slight preference for cards matching dominant deck colors cleanly.
    pip_bonus = sum(color_weights.get(c, 0) for c in card.color_identity)
    score += min(3.0, pip_bonus * 0.08)
    return (score, -card.grp_id)


def _select_commander(
    source_cmd: dict[int, int],
    source_main: dict[int, int],
    rules: EventFormatRules,
    collection: dict[int, int],
    catalog: dict[int, EventCardInfo],
    legal_titles: set[int],
    extra_banned_grp_ids: frozenset[int],
) -> tuple[int | None, str, bool]:
    """Pick existing or replacement owned legal commander. Return ``(gid, err, swapped)``."""
    orig_gid = next(iter(source_cmd), 0)
    orig_card = catalog.get(orig_gid)

    def _owned_legal_twin(gid: int) -> int | None:
        info = catalog.get(gid)
        if info is None:
            return None
        if (
            gid not in extra_banned_grp_ids
            and collection.get(gid, 0) >= 1
            and is_legal_commander(info, rules, legal_titles)
        ):
            return gid
        # Check owned cards sharing TitleId (e.g., Alchemy rebalanced vs paper printing).
        for cand_gid, owned_qty in sorted(collection.items()):
            if owned_qty < 1 or cand_gid in extra_banned_grp_ids:
                continue
            cand = catalog.get(cand_gid)
            if (
                cand is not None
                and cand.title_id == info.title_id
                and is_legal_commander(cand, rules, legal_titles)
            ):
                return cand_gid
        return None

    kept = _owned_legal_twin(orig_gid)
    if kept is not None:
        return kept, "", kept != orig_gid

    # Infer target color identity from original commander or source deck cards.
    target_colors: set[str] = set()
    if orig_card is not None and orig_card.color_identity:
        target_colors.update(orig_card.color_identity)
    else:
        for gid in source_main:
            c = catalog.get(gid)
            if c is not None:
                target_colors.update(c.color_identity)

    best_gid: int | None = None
    best_rank: tuple[int, int, float, int] | None = None
    for cand_gid, owned_qty in sorted(collection.items()):
        if owned_qty < 1 or cand_gid in extra_banned_grp_ids:
            continue
        cand = catalog.get(cand_gid)
        if cand is None or not is_legal_commander(cand, rules, legal_titles):
            continue
        if target_colors:
            exact = 1 if cand.color_identity == frozenset(target_colors) else 0
            superset = 1 if set(target_colors) <= set(cand.color_identity) else 0
            overlap = len(set(cand.color_identity) & target_colors) - len(
                set(cand.color_identity) - target_colors
            )
            color_score = exact * 100 + superset * 30 + overlap * 5
        else:
            color_score = len(cand.color_identity)
        rarity_rank = 2 if cand.rarity == "uncommon" else 1
        curve_rank = -abs(cand.cmc - 3.0)
        rank = (color_score, rarity_rank, curve_rank, -cand.grp_id)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_gid = cand_gid

    if best_gid is None:
        return (
            None,
            f"No owned legal commander found for {rules.name} (no wildcards spent).",
            False,
        )
    return best_gid, "", True


def adapt_deck_for_event(
    source_deck: dict[str, Any],
    rules: EventFormatRules,
    collection: dict[int, int],
    catalog: dict[int, EventCardInfo],
    legal_titles: set[int],
    *,
    extra_banned_grp_ids: frozenset[int] = frozenset(),
) -> AdaptedDeckPlan:
    """Adapt *source_deck* for *rules* using only owned cards in *collection*."""
    source_main = _pile_counts(source_deck.get("main"))
    source_side = _pile_counts(source_deck.get("sideboard"))
    source_cmd = _pile_counts(source_deck.get("command_zone"))

    if not source_main:
        return AdaptedDeckPlan(ok=False, error="Source deck has no readable main-deck cards.")

    used_by_title: Counter[int] = Counter()
    used_by_grp: Counter[int] = Counter()
    cut_cards: list[tuple[int, str, int, str]] = []
    added_cards: list[tuple[int, str, int]] = []
    out_cmd: list[tuple[int, int]] = []
    allowed_colors: frozenset[str] | None = None
    commander_swapped = False
    old_cmd_name = ""
    new_cmd_name = ""

    if rules.command_zone_min >= 1:
        orig_gid = next(iter(source_cmd), 0)
        if orig_gid and orig_gid in catalog:
            old_cmd_name = catalog[orig_gid].name
        cmd_gid, cmd_err, commander_swapped = _select_commander(
            source_cmd, source_main, rules, collection, catalog, legal_titles, extra_banned_grp_ids
        )
        if cmd_gid is None:
            return AdaptedDeckPlan(ok=False, error=cmd_err)
        cmd_info = catalog[cmd_gid]
        new_cmd_name = cmd_info.name
        out_cmd = [(cmd_gid, 1)]
        used_by_title[cmd_info.title_id] += 1
        used_by_grp[cmd_gid] += 1
        if rules.restrict_color_identity:
            allowed_colors = cmd_info.color_identity
        if commander_swapped and orig_gid:
            cut_cards.append((orig_gid, old_cmd_name or str(orig_gid), 1, "illegal commander"))

    # Group owned cards by TitleId so a printing swap (or rebalanced twin) stays owned & legal.
    owned_by_title: dict[int, list[int]] = {}
    owned_basics_by_name: dict[str, int] = {}
    fallback_basics_by_name: dict[str, int] = {}
    for gid, info in catalog.items():
        if info.is_basic_land:
            fallback_basics_by_name.setdefault(info.name, gid)
            if collection.get(gid, 0) > 0:
                owned_basics_by_name.setdefault(info.name, gid)
        elif collection.get(gid, 0) > 0:
            owned_by_title.setdefault(info.title_id, []).append(gid)

    kept: Counter[int] = Counter()
    cut_creatures = 0
    cut_nonlands = 0
    cut_lands = 0
    cut_cmc_sum = 0
    cut_cmc_count = 0

    for orig_gid, qty in source_main.items():
        orig = catalog.get(orig_gid)
        if orig is None:
            cut_cards.append((orig_gid, f"Card {orig_gid}", qty, "unknown card"))
            cut_nonlands += qty
            continue

        target_gid = orig_gid
        target = orig
        if orig.is_basic_land:
            if collection.get(orig_gid, 0) <= 0 and orig.name in owned_basics_by_name:
                target_gid = owned_basics_by_name[orig.name]
                target = catalog[target_gid]
        else:
            # Prefer exact printing if owned & legal; else an owned legal same-TitleId printing.
            if (
                orig_gid in extra_banned_grp_ids
                or collection.get(orig_gid, 0) <= 0
                or max_copies_in_format(orig, rules, legal_titles) <= 0
            ):
                alt_gid = next(
                    (
                        g
                        for g in sorted(owned_by_title.get(orig.title_id, []))
                        if g not in extra_banned_grp_ids
                        and max_copies_in_format(catalog[g], rules, legal_titles) > 0
                        and collection.get(g, 0) > used_by_grp[g]
                    ),
                    0,
                )
                if alt_gid:
                    target_gid = alt_gid
                    target = catalog[alt_gid]

        cap = max_copies_in_format(target, rules, legal_titles)
        reason = ""
        if target_gid in extra_banned_grp_ids:
            keep = 0
            reason = "not format legal"
        elif allowed_colors is not None and not (target.color_identity <= allowed_colors):
            keep = 0
            reason = "outside commander color identity"
        elif cap <= 0:
            keep = 0
            reason = (
                f"{target.rarity} not allowed"
                if (target.rarity in rules.rarity_max_copies and rules.rarity_max_copies[target.rarity] <= 0)
                else "not format legal"
            )
        elif target.is_basic_land:
            keep = min(qty, cap - used_by_title[target.title_id])
        else:
            avail_title = max(0, cap - used_by_title[target.title_id])
            avail_owned = max(0, collection.get(target_gid, 0) - used_by_grp[target_gid])
            keep = min(qty, avail_title, avail_owned)
            if keep < qty:
                if avail_title < qty - keep or avail_title == 0:
                    reason = "copy limit"
                else:
                    reason = "not enough owned copies"

        if keep > 0:
            kept[target_gid] += keep
            if not target.is_basic_land:
                used_by_title[target.title_id] += keep
                used_by_grp[target_gid] += keep
        dropped = qty - keep
        if dropped > 0:
            cut_cards.append((orig_gid, orig.name, dropped, reason or "format restriction"))
            if orig.is_land:
                cut_lands += dropped
            else:
                cut_nonlands += dropped
                if orig.is_creature:
                    cut_creatures += dropped
                cut_cmc_sum += orig.cmc * dropped
                cut_cmc_count += dropped

    # Trim main if downsizing (e.g., 99-card Historic Brawl -> 59-card Standard Brawl).
    while sum(kept.values()) > rules.main_max:
        total_now = sum(kept.values())
        land_now = sum(q for g, q in kept.items() if catalog[g].is_land)
        target_lands = round(rules.main_max * 0.4)
        trim_candidates: list[tuple[tuple[int, int, int], int]] = []
        for g, q in kept.items():
            if q <= 0:
                continue
            info = catalog[g]
            if land_now > target_lands and info.is_land:
                prio = (0 if info.is_basic_land else 1, -info.cmc, g)
            elif not info.is_land:
                prio = (2, -info.cmc, g)
            else:
                prio = (3, -info.cmc, g)
            trim_candidates.append((prio, g))
        if not trim_candidates:
            break
        _, drop_gid = min(trim_candidates, key=lambda item: item[0])
        kept[drop_gid] -= 1
        if kept[drop_gid] <= 0:
            del kept[drop_gid]
        info = catalog[drop_gid]
        if not info.is_basic_land:
            used_by_title[info.title_id] = max(0, used_by_title[info.title_id] - 1)
            used_by_grp[drop_gid] = max(0, used_by_grp[drop_gid] - 1)
        cut_cards.append((drop_gid, info.name, 1, "deck size limit"))
        if total_now <= rules.main_max:
            break

    kept_count = sum(kept.values())
    shortfall = max(0, rules.main_min - kept_count)
    if shortfall > 0:
        # Determine deck color identity from kept cards (or allowed_colors when Brawl).
        if allowed_colors is not None:
            deck_colors = allowed_colors
        else:
            inferred: set[str] = set()
            for g in kept:
                inferred.update(catalog[g].color_identity)
            deck_colors = frozenset(inferred)

        color_weights: Counter[str] = Counter()
        existing_lands = 0
        existing_creatures = 0
        existing_nonlands = 0
        for g, q in kept.items():
            info = catalog[g]
            if info.is_land:
                existing_lands += q
            else:
                existing_nonlands += q
                if info.is_creature:
                    existing_creatures += q
                color_weights.update({c: n * q for c, n in _parse_mana_pips(info.mana_cost, "").items()})
        for c in deck_colors:
            color_weights[c] = max(color_weights[c], 1)

        desired_total_lands = max(existing_lands, min(rules.main_min, round(rules.main_min * 0.4)))
        needed_lands = min(shortfall, max(0, desired_total_lands - existing_lands))
        needed_nonlands = shortfall - needed_lands

        want_creature_ratio = (cut_creatures / cut_nonlands) if cut_nonlands > 0 else 0.55
        target_cmc = (cut_cmc_sum / cut_cmc_count) if cut_cmc_count > 0 else 2.8

        # Build deduplicated owned legal candidates by TitleId (never adding banned/off-color/unowned cards).
        nonland_cands: list[EventCardInfo] = []
        land_cands: list[EventCardInfo] = []
        seen_titles: set[int] = set()
        for gid in sorted(collection):
            if collection.get(gid, 0) <= used_by_grp[gid] or gid in extra_banned_grp_ids:
                continue
            info = catalog.get(gid)
            if info is None or info.is_basic_land:
                continue
            cap = max_copies_in_format(info, rules, legal_titles)
            if cap <= used_by_title[info.title_id]:
                continue
            if allowed_colors is not None and not (info.color_identity <= allowed_colors):
                continue
            if deck_colors and not (info.color_identity <= deck_colors):
                continue
            if not deck_colors and info.color_identity:
                continue
            if info.title_id in seen_titles:
                continue
            seen_titles.add(info.title_id)
            if info.is_land:
                land_cands.append(info)
            else:
                nonland_cands.append(info)

        # Fill nonland slots greedily by score (recomputing creature ratio as we add).
        for _ in range(needed_nonlands):
            best_item: tuple[tuple[float, int], EventCardInfo] | None = None
            for info in nonland_cands:
                cap = max_copies_in_format(info, rules, legal_titles)
                if used_by_title[info.title_id] >= cap:
                    continue
                if collection.get(info.grp_id, 0) <= used_by_grp[info.grp_id]:
                    continue
                sc = _score_replacement(
                    info,
                    target_cmc=target_cmc,
                    want_creature_ratio=want_creature_ratio,
                    color_weights=color_weights,
                    existing_creatures=existing_creatures,
                    existing_nonlands=existing_nonlands,
                )
                if best_item is None or sc > best_item[0]:
                    best_item = (sc, info)
            if best_item is None:
                break
            chosen = best_item[1]
            kept[chosen.grp_id] += 1
            used_by_title[chosen.title_id] += 1
            used_by_grp[chosen.grp_id] += 1
            added_cards.append((chosen.grp_id, chosen.name, 1))
            existing_nonlands += 1
            if chosen.is_creature:
                existing_creatures += 1
            color_weights.update(_parse_mana_pips(chosen.mana_cost, ""))

        # Recompute remaining slots in case we had fewer nonland candidates than desired; lands can still help fill if needed?
        # Wait: never pad with >50% lands unless needed? Let's keep land target reasonable (at most needed_lands from owned nonbasic + basics),
        # and only fill missing nonlands from owned nonlands unless deck needs lands.
        still_need = max(0, rules.main_min - sum(kept.values()))
        max_reasonable_lands = round(rules.main_min * 0.43)
        land_slots = min(still_need, max(needed_lands, max(0, max_reasonable_lands - existing_lands)))

        # Add best owned nonbasic lands in color first (especially duals for multicolor decks).
        if land_slots > 0 and land_cands:
            scored_lands = sorted(
                land_cands,
                key=lambda c: (
                    len(c.color_identity),
                    1 if "enters tapped" not in c.oracle_text.lower() else 0,
                    -c.grp_id,
                ),
                reverse=True,
            )
            for land in scored_lands:
                if land_slots <= 0:
                    break
                cap = max_copies_in_format(land, rules, legal_titles)
                while (
                    land_slots > 0
                    and used_by_title[land.title_id] < cap
                    and collection.get(land.grp_id, 0) > used_by_grp[land.grp_id]
                ):
                    kept[land.grp_id] += 1
                    used_by_title[land.title_id] += 1
                    used_by_grp[land.grp_id] += 1
                    added_cards.append((land.grp_id, land.name, 1))
                    land_slots -= 1

        # Fill remaining land slots with basic lands matching color pip weights.
        if land_slots > 0:
            active_colors = [c for c in _COLOR_ORDER if c in deck_colors]
            if not active_colors:
                active_colors = ["W"]
            basic_for_color = {
                "W": owned_basics_by_name.get("Plains") or fallback_basics_by_name.get("Plains", 0),
                "U": owned_basics_by_name.get("Island") or fallback_basics_by_name.get("Island", 0),
                "B": owned_basics_by_name.get("Swamp") or fallback_basics_by_name.get("Swamp", 0),
                "R": owned_basics_by_name.get("Mountain") or fallback_basics_by_name.get("Mountain", 0),
                "G": owned_basics_by_name.get("Forest") or fallback_basics_by_name.get("Forest", 0),
            }
            basic_added: Counter[int] = Counter()
            total_w = sum(max(1, color_weights[c]) for c in active_colors)
            for _ in range(land_slots):
                best_c = max(
                    active_colors,
                    key=lambda c: (
                        (color_weights[c] / max(1, total_w))
                        - (basic_added.get(basic_for_color[c], 0) / max(1, land_slots)),
                        color_weights[c],
                    ),
                )
                bgid = basic_for_color.get(best_c, 0)
                if bgid <= 0:
                    break
                kept[bgid] += 1
                basic_added[bgid] += 1
            for bgid, qty in sorted(basic_added.items()):
                added_cards.append((bgid, catalog[bgid].name, qty))

    final_total = sum(kept.values())
    if final_total < rules.main_min:
        short = rules.main_min - final_total
        return AdaptedDeckPlan(
            ok=False,
            error=f"Not enough owned legal cards for {rules.name} (short by {short} cards; no wildcards spent).",
        )

    # Adapt sideboard cleanly (Brawl -> clear sideboard & companions so Arena accepts it).
    out_side: list[tuple[int, int]] = []
    if rules.sideboard_max > 0 and rules.command_zone_min == 0:
        side_kept: Counter[int] = Counter()
        for gid, qty in source_side.items():
            if sum(side_kept.values()) >= rules.sideboard_max:
                break
            info = catalog.get(gid)
            if info is None or gid in extra_banned_grp_ids:
                continue
            cap = max_copies_in_format(info, rules, legal_titles)
            avail_title = max(0, cap - used_by_title[info.title_id])
            avail_owned = qty if info.is_basic_land else max(0, collection.get(gid, 0) - used_by_grp[gid])
            take = min(qty, avail_title, avail_owned, rules.sideboard_max - sum(side_kept.values()))
            if take > 0:
                side_kept[gid] += take
                if not info.is_basic_land:
                    used_by_title[info.title_id] += take
                    used_by_grp[gid] += take
        out_side = sorted(side_kept.items())

    out_main = sorted((gid, qty) for gid, qty in kept.items() if qty > 0)
    return AdaptedDeckPlan(
        ok=True,
        main=out_main,
        sideboard=out_side,
        command_zone=out_cmd,
        companions=[],
        kept_count=kept_count,
        added_count=sum(q for _, _, q in added_cards),
        cut_cards=cut_cards,
        added_cards=added_cards,
        commander_swapped=commander_swapped,
        old_commander_name=old_cmd_name,
        new_commander_name=new_cmd_name,
    )


def _unique_clone_name(source_name: str, format_label: str, existing_names: set[str]) -> str:
    tag = format_label.strip() or "Event"
    if len(tag) > 18:
        tag = tag[:18].strip()
    base = f"{source_name[:26].strip()} ({tag})"
    if base not in existing_names:
        return base
    for n in range(2, 50):
        candidate = f"{source_name[:22].strip()} ({tag} {n})"
        if candidate not in existing_names:
            return candidate
    return f"{source_name[:20].strip()} ({tag} {int(time.time()) % 1000})"


def build_event_deck(
    *,
    bridge: Any,
    event_name: str,
    event_format: str,
    source_deck_id: str,
    mtgadb: Any = None,
    log_path: Path | str | None = None,
    display_name: str = "",
    poll_timeout: float = 8.0,
    poll_interval: float = 0.25,
) -> dict[str, Any]:
    """Clone *source_deck_id*, adapt it with owned cards for *event_format*, and validate natively."""
    if bridge is None or not getattr(bridge, "connected", False):
        return {"ok": False, "error": "Arena bridge is not connected; open MTG Arena on the Home screen."}
    if mtgadb is None or not getattr(mtgadb, "available", False):
        return {"ok": False, "error": "Local MTGA card database is unavailable."}

    rules = load_event_format_rules(event_format, log_path)
    if rules is None:
        return {
            "ok": False,
            "error": f"Format rules for '{event_format}' were not found in Arena's log.",
        }

    decks_resp = bridge.draft_command("get_decks", timeout=12.0)
    if not decks_resp.get("ok") or not isinstance(decks_resp.get("decks"), list):
        return {"ok": False, "error": str(decks_resp.get("error") or "Could not read decks from Arena.")}
    decks = decks_resp["decks"]
    source_deck = next((d for d in decks if str(d.get("deck_id") or "") == source_deck_id), None)
    if source_deck is None:
        return {"ok": False, "error": f"Source deck {source_deck_id} was not found in Arena."}

    if rules.command_zone_min >= 1:
        has_cmd = bool(_pile_counts(source_deck.get("command_zone")))
        is_brawl_fmt = "brawl" in normalize_format_key(str(source_deck.get("format") or ""))
        if not has_cmd and not is_brawl_fmt:
            return {
                "ok": False,
                "error": f"{event_format} requires a Brawl source deck with a Commander zone.",
            }

    coll_resp = bridge.draft_command("get_collection", timeout=12.0)
    if not coll_resp.get("ok"):
        return {
            "ok": False,
            "error": str(coll_resp.get("error") or "Could not read your Arena collection."),
        }
    collection: dict[int, int] = {
        int(gid): int(qty)
        for gid, qty in (coll_resp.get("cards") or {}).items()
        if int(gid) > 0 and int(qty) > 0
    }

    extra_ids: set[int] = set()
    for pile_name in ("main", "sideboard", "command_zone", "companions"):
        extra_ids.update(_pile_counts(source_deck.get(pile_name)))

    catalog, legal_titles = load_event_card_catalog(collection, extra_ids, rules, mtgadb)
    if not catalog:
        return {"ok": False, "error": "Could not load MTGA card catalog for deck adaptation."}

    plan = adapt_deck_for_event(source_deck, rules, collection, catalog, legal_titles)
    if not plan.ok:
        return {"ok": False, "error": plan.error}

    source_name = str(source_deck.get("name") or "Deck")
    existing_ids = {str(d.get("deck_id") or "") for d in decks}
    existing_names = {str(d.get("name") or "") for d in decks}
    new_name = _unique_clone_name(source_name, display_name or event_format, existing_names)

    create_resp = bridge.draft_command(
        "create_deck", timeout=15.0, source_deck_id=source_deck_id, name=new_name
    )
    if not create_resp.get("ok"):
        return {
            "ok": False,
            "error": str(create_resp.get("error") or "Arena rejected cloning the deck."),
        }

    # Poll get_decks until the newly cloned deck receives its fresh DeckId GUID.
    new_deck_id = ""
    deadline = time.monotonic() + max(0.2, float(poll_timeout))
    while time.monotonic() <= deadline:
        refreshed = bridge.draft_command("get_decks", timeout=10.0)
        if refreshed.get("ok") and isinstance(refreshed.get("decks"), list):
            matches = [
                d
                for d in refreshed["decks"]
                if str(d.get("deck_id") or "") not in existing_ids and str(d.get("name") or "") == new_name
            ]
            if len(matches) == 1:
                new_deck_id = str(matches[0]["deck_id"])
                break
        if time.monotonic() + poll_interval > deadline:
            break
        time.sleep(max(0.01, float(poll_interval)))

    if not new_deck_id:
        return {
            "ok": False,
            "error": f"Cloned '{new_name}', but its new DeckId has not appeared in Arena yet. Click Refresh.",
        }

    piles = {
        "main": plan.main,
        "sideboard": plan.sideboard,
        "command_zone": plan.command_zone,
        "companions": plan.companions,
    }
    upd_resp = bridge.draft_command("update_deck", timeout=15.0, deck_id=new_deck_id, piles=piles)
    if not upd_resp.get("ok"):
        return {
            "ok": False,
            "deck_id": new_deck_id,
            "deck_name": new_name,
            "error": str(
                upd_resp.get("error") or f"Created '{new_name}', but failed to write adapted cards."
            ),
        }

    val_resp = bridge.draft_command(
        "validate_deck_for_format", timeout=10.0, deck_id=new_deck_id, format_name=event_format
    )
    # Bounded 1-shot structural repair on the clone if Arena complains about sideboard or basic ownership.
    if val_resp.get("ok") and not val_resp.get("is_valid"):
        counters = val_resp.get("counters") or {}
        if counters.get("NumberMoreThanXInSideboard") and piles["sideboard"]:
            piles["sideboard"] = []
            piles["companions"] = []
            bridge.draft_command("update_deck", timeout=15.0, deck_id=new_deck_id, piles=piles)
            val_resp = bridge.draft_command(
                "validate_deck_for_format",
                timeout=10.0,
                deck_id=new_deck_id,
                format_name=event_format,
            )

    if not val_resp.get("ok"):
        return {
            "ok": False,
            "deck_id": new_deck_id,
            "deck_name": new_name,
            "error": str(val_resp.get("error") or f"Could not validate '{new_name}' for {event_format}."),
        }
    if not val_resp.get("is_valid"):
        return {
            "ok": False,
            "deck_id": new_deck_id,
            "deck_name": new_name,
            "error": (
                f"Created '{new_name}', but Arena validation reported: "
                f"{val_resp.get('summary') or 'not legal for format'}."
            ),
        }

    replaced = sum(qty for _, _, qty in plan.added_cards)
    summary_parts = [
        f"Built '{new_name}' ({plan.kept_count} kept, {replaced} owned cards added, 0 wildcards)"
    ]
    if plan.commander_swapped and plan.new_commander_name:
        summary_parts.append(f"Commander -> {plan.new_commander_name}")
    return {
        "ok": True,
        "event_name": event_name,
        "event_format": event_format,
        "deck_id": new_deck_id,
        "deck_name": new_name,
        "source_deck_name": source_name,
        "kept_count": plan.kept_count,
        "replaced_count": replaced,
        "commander_swapped": plan.commander_swapped,
        "new_commander_name": plan.new_commander_name,
        "summary": " — ".join(summary_parts),
    }
