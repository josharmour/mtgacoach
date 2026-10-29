"""Read-only native card metadata for the conservative Standard quest builder.

The rule sources are the client's FormatManagerCore.BuildDeckFormats,
SqlDirectDatabaseUtilities.GetTitleIdsLegalForFormatData, FormatManagerUtils,
and DeckValidationCardInfo (see re-output/SharedClientCore). Membership is by
TitleId across printings, not by the requested printing's expansion. Allowed
names are added before banned/suspended names are removed. Non-rebalanced
primary printings and ordinary unlimited basics are supported here.

This is local format evidence, not a replacement for server deck validation.
Unknown format rules or a missing/stale GetFormats response fail closed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
_MAX_AGE = timedelta(hours=24)
_MAX_RESPONSE = 4 * 1024 * 1024
_TIMESTAMP = re.compile(r"(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M)")
_BASIC_NAMES = {"Plains", "Island", "Swamp", "Mountain", "Forest"}
_COLORS = dict(zip("12345", "WUBRG", strict=True))
# Protobuf JSON omits default false/zero/empty values. Only these rules are
# implemented; a new restriction must never silently become positive legality.
_FORMAT_FIELDS = {
    "name",
    "legalSets",
    "filterSets",
    "bannedTitleIds",
    "suspendedTitleIds",
    "allowedTitleIds",
    "supressedTitleIds",
    "FormatTypeInternal",
    "mainDeckQuota",
    "sideBoardQuota",
    "commandZoneQuota",
    "useRebalancedCards",
    "CardCountRestrictionInternal",
    "SideboardBehaviorInternal",
}
_DB_COLUMNS = {
    "GrpId",
    "TitleId",
    "ExpansionCode",
    "IsPrimaryCard",
    "IsRebalanced",
    "IsToken",
    "IsDigitalOnly",
    "Supertypes",
    "Types",
    "Subtypes",
    "LinkedFaceGrpIds",
    "OldSchoolManaText",
    "AlternateDeckLimit",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_standard_format(log_path: Path) -> dict | None:
    """Use the latest response, never an older success behind a broken refresh.

    Unity's network timestamps in Player.log are UTC. Require the response's
    own timestamp as well as a recent file; touching an old log cannot renew
    its authority. The whole response can span lines, as real logs do.
    """
    now = _utc_now()
    try:
        modified = datetime.fromtimestamp(log_path.stat().st_mtime, timezone.utc)
        if not -timedelta(minutes=10) <= now - modified <= _MAX_AGE:
            return None
        timestamp = None
        response_time = None
        response = None
        pending: list[str] | None = None
        length = 0
        with log_path.open(encoding="utf-8", errors="replace") as log:
            for line in log:
                match = _TIMESTAMP.search(line[:160])
                if match:
                    timestamp = datetime.strptime(match[1], "%m/%d/%Y %I:%M:%S %p").replace(
                        tzinfo=timezone.utc
                    )
                if "<== GetFormats(" in line:
                    response = None
                    response_time = timestamp
                    pending = []
                    length = 0
                    continue
                if pending is None:
                    continue
                pending.append(line)
                length += len(line)
                if length > _MAX_RESPONSE:
                    pending = None
                    continue
                try:
                    response = json.loads("".join(pending))
                except ValueError:
                    continue
                pending = None
        if response_time is None or not -timedelta(minutes=10) <= now - response_time <= _MAX_AGE:
            return None
        if not isinstance(response, dict) or not isinstance(response.get("Formats"), list):
            return None
        matches = [f for f in response["Formats"] if isinstance(f, dict) and f.get("name") == "Standard"]
        if len(matches) != 1 or not _supported_standard(matches[0]):
            return None
        return matches[0]
    except (OSError, ValueError, OverflowError):
        return None


def _supported_standard(config: dict) -> bool:
    if set(config) - _FORMAT_FIELDS:
        return False
    if type(config.get("FormatTypeInternal")) is not int or config["FormatTypeInternal"] != 3:
        return False
    if config.get("useRebalancedCards", False) is not False:
        return False
    for field in ("CardCountRestrictionInternal", "SideboardBehaviorInternal"):
        if type(config.get(field, 0)) is not int or config.get(field, 0) != 0:
            return False
    for field, expected in (
        ("mainDeckQuota", {"min": 60, "max": 250}),
        ("sideBoardQuota", {"max": 15}),
        ("commandZoneQuota", {}),
    ):
        quota = config.get(field)
        if not isinstance(quota, dict) or set(quota) - {"min", "max"}:
            return False
        if any(type(v) is not int for v in quota.values()):
            return False
        if any(quota.get(k, 0) != expected.get(k, 0) for k in ("min", "max")):
            return False
    sets = config.get("legalSets")
    if not isinstance(sets, list) or not sets or any(not isinstance(s, str) or not s for s in sets):
        return False
    for field in ("allowedTitleIds", "bannedTitleIds", "suspendedTitleIds", "supressedTitleIds"):
        values = config.get(field, [])
        if not isinstance(values, list) or any(type(v) is not int or v <= 0 for v in values):
            return False
    return True


def _ordinary_basic(row) -> bool:
    # CardPrintingData.IsBasicLandUnlimited: exactly one super/type/subtype.
    # The English name is verified separately after native localization.
    return (
        str(row["Supertypes"]) == "1"
        and str(row["Types"]) == "5"
        and str(row["Subtypes"]).isdigit()
        and int(row["Subtypes"]) > 0
    )


def load_quest_card_metadata(ids: list[int], log_path: Path) -> dict[int, dict]:
    """Enrich requested native IDs and add verified free ordinary basic IDs.

    ``legalities['standard']`` is positive only with fresh, supported server
    GetFormats data and the required native schema. Missing evidence returns
    an empty mapping. No Scryfall/network fallback, bridge, or account writes.
    """
    config = _read_standard_format(Path(log_path))
    if config is None:
        logger.debug("Quest metadata: no fresh supported Standard GetFormats response")
        return {}
    from arenamcp import server

    raw = server._get_mtgadb()
    if raw is None or not raw.available:
        return {}
    try:
        # The adapter's shared connection requires its lock, including queries
        # for native flags not exposed by MTGACard. Never open a writable DB.
        with raw._conn_lock:
            if raw._conn is None or not raw._card_columns >= _DB_COLUMNS:
                return {}
            rows = raw._conn.execute(
                "SELECT GrpId, TitleId, ExpansionCode, IsPrimaryCard, IsRebalanced, "
                "IsToken, IsDigitalOnly, Supertypes, Types, Subtypes, "
                "LinkedFaceGrpIds, OldSchoolManaText, AlternateDeckLimit FROM Cards"
            ).fetchall()
        legal_sets = set(config["legalSets"])
        banned = set(config.get("bannedTitleIds", [])) | set(config.get("suspendedTitleIds", []))
        legal_titles = set(config.get("allowedTitleIds", []))
        eligible = {}
        basic_ids = set()
        for row in rows:
            basic = _ordinary_basic(row)
            # Reject tokens, back faces, digital/rebalanced and limited-count
            # cards even if a malformed/foreign printing shares a legal title.
            if (
                row["IsPrimaryCard"] != 1
                or row["IsRebalanced"] != 0
                or row["IsToken"] != 0
                or row["IsDigitalOnly"] != 0
                or (
                    row["AlternateDeckLimit"] not in (0, None)
                    and not (basic and row["AlternateDeckLimit"] == 250)
                )
            ):
                continue
            gid = row["GrpId"]
            eligible[gid] = row
            if basic:
                basic_ids.add(gid)
            if basic or row["ExpansionCode"] in legal_sets:
                legal_titles.add(row["TitleId"])
        legal_titles -= banned
        requested = {gid for gid in ids if type(gid) is int and gid > 0}
        wanted = sorted((requested | basic_ids) & eligible.keys())
        cards = {}
        for offset in range(0, len(wanted), 500):
            cards.update(raw.get_cards_batch(wanted[offset : offset + 500]))
        result = {}
        for gid, card in cards.items():
            row = eligible[gid]
            basic = gid in basic_ids and card.name in _BASIC_NAMES and "Basic Land" in card.type_line
            if gid not in requested and not basic:
                continue
            tokens = [v.strip() for v in card.colors.split(",") if v.strip()]
            if any(v not in {*_COLORS, "0"} for v in tokens):
                continue
            # Adapter conversion deliberately tolerates unrecognized symbols;
            # don't let a partial conversion look like a cheaper payable spell.
            mana = card.mana_cost
            symbols = re.findall(r"o(\([^()]+\)|\d+|[WUBRGCXYZS])", row["OldSchoolManaText"] or "")
            if "".join("o" + symbol for symbol in symbols) != (row["OldSchoolManaText"] or ""):
                mana = ""
            result[gid] = {
                "name": card.name,
                "type_line": card.type_line,
                "colors": [_COLORS[v] for v in tokens if v in _COLORS],
                "mana_cost": mana,
                "title_id": row["TitleId"],
                "is_token": False,
                "is_primary_card": True,
                "is_rebalanced": False,
                "layout": "multiface" if row["LinkedFaceGrpIds"] else "normal",
                "free_basic": basic,
                "basic_requires_unlock": basic,
                "legalities": {"standard": "legal" if row["TitleId"] in legal_titles else "not_legal"},
                "legality_source": "Player.log:GetFormats:Standard + native Cards.TitleId",
            }
        return result
    except (sqlite3.Error, AttributeError, TypeError, ValueError):
        logger.warning("Quest metadata: native card schema unavailable", exc_info=True)
        return {}
