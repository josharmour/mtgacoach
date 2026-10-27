"""Static card-name selections use TitleId, not GrpId or instance id.

Anointed Peacekeeper, 2026-10-25: Arena rejected Channel's GrpId 77529
and Ugin's instance id 219 with InvalidCardName. The manual response
21415 succeeded: it is Channel's Cards.TitleId in the local database.
"""

from typing import Any

from arenamcp.color_choice import enum_short


def is_card_name_list(list_type: Any, static_list: Any) -> bool:
    return enum_short(list_type) in {"Static", "StaticSubset"} and enum_short(static_list) == "CardNames"


def _database():
    try:
        from arenamcp.card_db import get_card_database

        return get_card_database().get_raw_mtgadb()
    except Exception:
        return None


def card_title_id(grp_id: int) -> int:
    db = _database()
    return db.get_card_title_id(grp_id) if db is not None else 0


def is_card_title_id(title_id: int) -> bool:
    db = _database()
    return db.is_card_title_id(title_id) if db is not None else False


def card_title_name(title_id: int) -> str:
    db = _database()
    return db.get_card_title_name(title_id) if db is not None else ""
