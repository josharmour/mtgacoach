"""Live MTG Arena event discovery, entry, Table Ready!, rewards, and deck/packet actions.

All operations run through MacBridgeAdapter's Unity main-thread reflect batch
protocol so macOS IL2CPP, Android IL2CPP, and Windows/Linux BepInEx (reflect-1)
share the exact same implementation.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from arenamcp.mac_bridge_adapter import (
    EVENT_PAGE_CONTROLLER,
    AdapterError,
    B,
    E,
    H,
    I,
    _Ops,
    enum_name,
    field,
    guid,
    handle,
    items,
    num,
)

TABLE_DRAFT_QUEUE_CONTROLLER = "Wotc.Mtga.Wrapper.Draft.TableDraftQueueContentController"
PACKET_SELECT_CONTROLLER = "Wotc.Mtga.Wrapper.PacketSelect.PacketSelectContentController"
CONSTRUCTED_DECK_SELECT_CONTROLLER = "ConstructedDeckSelectController"
REWARDS_PANEL_CONTROLLER = "Core.Meta.MainNavigation.Store.ContentControllerRewards"

_EXCLUDED_EVENT_PREFIXES = ("DirectGame", "NPE", "ColorChallenge", "AIBotMatch", "BotMatch", "Tutorial")
_COLOR_CHALLENGE_EVENT_RE = re.compile(r"^(?:white|blue|black|red|green)0[1-5](?:_|$)", re.IGNORECASE)
_EXCLUDED_BLADE_BEHAVIORS = frozenset(
    {"Queue", "Hidden", "ColorChallenge", "Tutorial", "DirectChallenge", "BotMatch", "None"}
)
_UNPAID_MODULES = frozenset({"Join", "Pay", "PayEntry"})
_POST_DECK_SUBMIT_MODULES = frozenset(
    {
        "CreateMatch",
        "TransitionToMatches",
        "WinLossGate",
        "WinNoGate",
        "ClaimPrize",
        "Complete",
        "MatchResults",
    }
)
_FREE_CURRENCIES = frozenset({"Free", "None"})


def _read_wallet(adapter: Any, wrapper_handle: int, timeout: float | None) -> dict[str, Any]:
    ops = _Ops()
    inv_mgr = ops.get(H(wrapper_handle), "InventoryManager", optional=True)
    inv = ops.get(inv_mgr, "Inventory", depth=1, optional=True)
    custom_ref = ops.get(inv, "CustomTokens", depth=3, optional=True)
    values = adapter._run(ops, timeout)
    inv_node = values[inv["ref"]] if isinstance(values, list) else None
    custom_raw = values[custom_ref["ref"]] if isinstance(values, list) else None
    if custom_raw is None:
        custom_raw = field(inv_node, "CustomTokens")
    custom_tokens: dict[str, int] = {}
    if isinstance(custom_raw, dict):
        for entry in items(field(custom_raw, "entries")) or items(custom_raw):
            k = field(entry, "key")
            v = field(entry, "value")
            if isinstance(k, str) and k:
                custom_tokens[k] = num(v)
        if not custom_tokens:
            for k, v in custom_raw.items():
                if not str(k).startswith("$") and isinstance(v, (int, float)):
                    custom_tokens[str(k)] = int(v)
    return {
        "gold": num(field(inv_node, "gold")),
        "gems": num(field(inv_node, "gems")),
        "draft_tokens": num(field(inv_node, "draftTokens")),
        "sealed_tokens": num(field(inv_node, "sealedTokens")),
        "custom_tokens": custom_tokens,
    }


def _normalize_currency_type(currency_type: str) -> str:
    norm = str(currency_type or "").strip().removeprefix("CurrencyType_")
    if norm == "Gems":
        return "Gem"
    return norm or "None"


def _fee_affordable(
    currency_type: str, quantity: int, uses_remaining: bool, reference_id: str, wallet: dict[str, Any]
) -> bool:
    if not uses_remaining:
        return False
    norm = _normalize_currency_type(currency_type)
    if norm in _FREE_CURRENCIES:
        return quantity == 0
    if norm == "Gold":
        return int(wallet.get("gold", 0)) >= quantity
    if norm == "Gem":
        return int(wallet.get("gems", 0)) >= quantity
    if norm == "DraftToken":
        return int(wallet.get("draft_tokens", 0)) >= quantity
    if norm == "SealedToken":
        return int(wallet.get("sealed_tokens", 0)) >= quantity
    if norm == "EventToken":
        return int((wallet.get("custom_tokens") or {}).get(reference_id, 0)) >= quantity
    return False


def _normalize_entry_fees(fees_node: Any, wallet: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, fee in enumerate(items(fees_node)):
        currency = enum_name(field(fee, "CurrencyType")) or "None"
        qty = num(field(fee, "Quantity"))
        uses_remaining = bool(field(fee, "UsesRemaining"))
        ref_id = str(field(fee, "ReferenceId") or "")
        is_free = _normalize_currency_type(currency) in _FREE_CURRENCIES and qty == 0 and uses_remaining
        normalized.append(
            {
                "index": index,
                "currency_type": currency,
                "quantity": qty,
                "uses_remaining": uses_remaining,
                "reference_id": ref_id,
                "is_free": is_free,
                "affordable": _fee_affordable(currency, qty, uses_remaining, ref_id, wallet),
            }
        )
    return normalized


def _extract_prize_and_loss_info(comp_data_node: Any, ux_prizes_node: Any) -> dict[str, Any]:
    loss_node = field(comp_data_node, "LossDetailsDisplay")
    loss_type = enum_name(field(loss_node, "LossDetailsType"))
    loss_games = num(field(loss_node, "Games"))

    max_prize_wins = 0
    is_cumulative_track = False

    def _max_win_from_track(track_node: Any) -> int:
        if not isinstance(track_node, dict):
            return 0
        chests = field(track_node, "ChestDescriptions")
        best = 0
        for chest in items(chests):
            for key in ("Wins", "RequiredWins", "WinCount", "TargetWins", "ObjectiveCount", "Threshold"):
                val = field(chest, key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    best = max(best, int(val))
        return best

    cum_track = field(comp_data_node, "CumulativeObjectiveTrack")
    cum_max = _max_win_from_track(cum_track)
    if cum_max > 0:
        max_prize_wins = max(max_prize_wins, cum_max)
        is_cumulative_track = True

    course_track = field(comp_data_node, "ByCourseObjectiveTrack")
    course_max = _max_win_from_track(course_track)
    if course_max > 0:
        max_prize_wins = max(max_prize_wins, course_max)

    if isinstance(ux_prizes_node, dict):
        for entry in items(field(ux_prizes_node, "entries")) or items(ux_prizes_node):
            k = field(entry, "key")
            if isinstance(k, (int, float)) and not isinstance(k, bool):
                max_prize_wins = max(max_prize_wins, int(k))
        for k in ux_prizes_node:
            if str(k).isdigit():
                max_prize_wins = max(max_prize_wins, int(str(k)))

    return {
        "loss_details_type": loss_type,
        "loss_details_games": loss_games,
        "max_prize_wins": max_prize_wins,
        "is_cumulative_track": is_cumulative_track,
    }


def _humanize_event_name(internal_name: str) -> str:
    cleaned = str(internal_name or "").replace("_", " ").strip()
    return cleaned or "MTGA Event"


def _classify_event(event_name: str, format_type: str, module: str, is_precon: bool) -> str:
    lower = event_name.lower()
    compact = lower.replace("_", "").replace("-", "")
    if module == "Jumpstart" or "jumpstart" in compact or "jumpin" in compact:
        return "jumpstart"
    if format_type == "Sealed" or "sealed" in lower:
        return "sealed"
    if format_type == "Draft" or "draft" in lower or "cube" in lower:
        if module == "Draft" or "quickdraft" in compact or "botdraft" in compact:
            return "bot_draft"
        return "human_draft"
    if is_precon or module == "Choice":
        return "precon_event"
    return "constructed_event"


def _extract_event_page_details(
    *,
    event_name: str,
    module: str,
    format_type: str,
    is_precon: bool,
    deck_select_format: str,
    joinability: str,
    win_condition: str,
    current_wins: int,
    current_losses: int,
    games_played: int,
    has_unclaimed_rewards: bool,
    fees: list[dict[str, Any]],
    prize_loss: dict[str, Any],
    public_name: str = "",
    title_loc_key: str = "",
    skip_deck_validation: bool = False,
) -> dict[str, Any]:
    is_free = any(f["is_free"] for f in fees)
    in_progress = module not in {"", "None", "Join", "Pay", "PayEntry", "Complete"}
    return {
        "event_name": event_name,
        "display_name": public_name.strip() or _humanize_event_name(event_name),
        "title_loc_key": title_loc_key,
        "category": _classify_event(event_name, format_type, module, is_precon),
        "format_type": format_type,
        "is_precon": is_precon,
        "skip_deck_validation": skip_deck_validation,
        "deck_select_format": deck_select_format,
        "win_condition": win_condition,
        "module": module,
        "in_progress": in_progress,
        "joinability": joinability,
        "current_wins": current_wins,
        "current_losses": current_losses,
        "games_played": games_played,
        "has_unclaimed_rewards": has_unclaimed_rewards,
        "is_free": is_free,
        "entry_fees": fees,
        **prize_loss,
    }


def read_event_page(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Read the currently open EventPageContentController with rich course/fee metadata."""
    found = adapter._find(EVENT_PAGE_CONTROLLER, timeout)
    if not found:
        return {"ok": True, "is_open": False}
    ops = _Ops()
    context = ops.get(H(found), "_currentEventContext")
    player_event = ops.get(context, "PlayerEvent")
    course = ops.get(player_event, "CourseData")
    module = ops.get(course, "CurrentModule")
    info = ops.get(player_event, "EventInfo")
    name = ops.get(info, "InternalEventName")
    fmt_type = ops.get(info, "FormatType", optional=True)
    is_precon = ops.get(info, "IsPreconEvent", optional=True)
    skip_val = ops.get(info, "SkipDeckValidation", optional=True)
    fees_ref = ops.get(info, "EntryFees", depth=2, optional=True)
    ux = ops.get(player_event, "EventUXInfo", optional=True)
    pub_name = ops.get(ux, "PublicEventName", optional=True)
    deck_fmt = ops.get(ux, "DeckSelectFormat", optional=True)
    joinability = ops.get(ux, "Joinability", optional=True)
    prizes = ops.get(ux, "Prizes", depth=3, optional=True)
    comp_data = ops.get(ux, "EventComponentData", depth=4, optional=True)
    wins = ops.get(player_event, "CurrentWins", optional=True)
    losses = ops.get(player_event, "CurrentLosses", optional=True)
    games = ops.get(player_event, "GamesPlayed", optional=True)
    unclaimed = ops.get(player_event, "HasUnclaimedRewards", optional=True)
    win_cond = ops.get(player_event, "WinCondition", optional=True)
    values = adapter._run(ops, timeout)

    raw_fees = values[fees_ref["ref"]]
    wallet = {"gold": 0, "gems": 0, "draft_tokens": 0, "sealed_tokens": 0, "custom_tokens": {}}
    if items(raw_fees):
        try:
            ops_w = _Ops()
            w_ref = ops_w.add(
                "static", **{"class": "WrapperController"}, member="Instance", depth=0, optional=True
            )
            w_vals = adapter._run(ops_w, timeout)
            wrapper_h = handle(w_vals[w_ref["ref"]]) if isinstance(w_vals, list) else None
            if wrapper_h:
                wallet = _read_wallet(adapter, wrapper_h, timeout)
        except Exception:
            pass
    fees = _normalize_entry_fees(raw_fees, wallet)
    prize_loss = _extract_prize_and_loss_info(values[comp_data["ref"]], values[prizes["ref"]])
    event_name = str(values[name["ref"]] or "")
    mod_name = enum_name(values[module["ref"]])
    details = _extract_event_page_details(
        event_name=event_name,
        module=mod_name,
        format_type=enum_name(values[fmt_type["ref"]]),
        is_precon=bool(values[is_precon["ref"]]),
        deck_select_format=str(values[deck_fmt["ref"]] or ""),
        joinability=enum_name(values[joinability["ref"]]),
        win_condition=enum_name(values[win_cond["ref"]]),
        current_wins=num(values[wins["ref"]]),
        current_losses=num(values[losses["ref"]]),
        games_played=num(values[games["ref"]]),
        has_unclaimed_rewards=bool(values[unclaimed["ref"]]),
        fees=fees,
        prize_loss=prize_loss,
        public_name=str(values[pub_name["ref"]] or ""),
        skip_deck_validation=bool(values[skip_val["ref"]]),
    )
    return {"ok": True, "is_open": True, **details}


_KNOWN_60_FORMATS = frozenset(
    {
        "standard",
        "traditionalstandard",
        "alchemy",
        "traditionalalchemy",
        "explorer",
        "pioneer",
        "traditionalpioneer",
        "historic",
        "traditionalhistoric",
        "timeless",
        "traditionaltimeless",
        "pauper",
        "artisan",
        "singleton",
        "standardpauper",
        "standardartisan",
        "historicpauper",
        "historicartisan",
        "alchemyartisan",
        "artisanfuture",
        "tcsingleton",
    }
)

_VALIDATION_COUNTER_LABELS: tuple[tuple[str, str], ...] = (
    ("NumberBannedAsCommanderCards", "banned commander"),
    ("NumberBannedCards", "banned card"),
    ("NumberEmergencyBannedCards", "emergency-banned card"),
    ("NumberNonFormatCard", "non-format card"),
    ("NumberTooManyCardsByTitle", "excess copies"),
    ("NumberLessThanRequiredInMain", "missing from main"),
    ("NumberMoreThanRequiredInMain", "over main limit"),
    ("NumberMoreThanXInSideboard", "over sideboard limit"),
    ("NumberOfUnownedUncraftableCards", "unowned uncraftable"),
    ("NumberUnknownCards", "unknown card"),
    ("NumberOfInvalidCards", "invalid card"),
)


def _summarize_validation_result(result: Any) -> tuple[int, str, dict[str, int]]:
    """Return ``(issue_count, summary, counters)`` from a DeckValidationResult dict."""
    if not isinstance(result, dict):
        return 0, "not legal for format", {}
    counters: dict[str, int] = {}
    parts: list[str] = []
    specific_card_issues = False
    for member, singular in _VALIDATION_COUNTER_LABELS:
        val = field(result, member)
        if type(val) is int and val > 0:
            capped = min(val, 999999)
            counters[member] = capped
            if member in {
                "NumberBannedAsCommanderCards",
                "NumberBannedCards",
                "NumberEmergencyBannedCards",
                "NumberNonFormatCard",
                "NumberTooManyCardsByTitle",
                "NumberUnknownCards",
            }:
                specific_card_issues = True
            if member == "NumberOfInvalidCards" and specific_card_issues:
                continue
            noun = (
                singular
                if capped == 1 or singular.endswith(("s", "limit", "main", "uncraftable"))
                else f"{singular}s"
            )
            parts.append(f"{capped} {noun}")

    has_unowned = field(result, "HasUnOwnedCards") is True
    if has_unowned and "NumberOfUnownedUncraftableCards" not in counters:
        counters["HasUnOwnedCards"] = 1
        parts.append("unowned cards")

    # Prefer the sum of specific counters when present; avoid double-counting NumberOfInvalidCards.
    specific_total = sum(
        v for k, v in counters.items() if k not in {"NumberOfInvalidCards", "HasUnOwnedCards"}
    )
    if specific_total > 0:
        issue_count = specific_total + (
            1 if has_unowned and "NumberOfUnownedUncraftableCards" not in counters else 0
        )
    else:
        issue_count = counters.get("NumberOfInvalidCards", 0) + (
            1 if has_unowned and "NumberOfUnownedUncraftableCards" not in counters else 0
        )
    summary = ", ".join(parts[:3]) if parts else "not legal for format"
    return issue_count, summary, counters


def _read_deck_summaries(
    adapter: Any,
    deck_handles: list[int],
    timeout: float | None,
    *,
    include_format: bool = True,
) -> list[dict[str, Any]]:
    """Read ``DeckId``, ``Name``, and (via property getter) ``Format`` for deck handles."""
    summaries: list[dict[str, Any]] = []
    for start in range(0, len(deck_handles), 4):
        chunk = deck_handles[start : start + 4]
        ops_s = _Ops()
        refs: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
        for dh in chunk:
            s_ref = ops_s.get(H(dh), "Summary", depth=2, optional=True)
            fmt_ref = ops_s.get(s_ref, "Format", optional=True) if include_format else None
            refs.append((dh, s_ref, fmt_ref))
        vals_s = adapter._run(ops_s, timeout)
        for dh, s_ref, fmt_ref in refs:
            summary = vals_s[s_ref["ref"]]
            if not isinstance(summary, dict):
                continue
            try:
                did = guid(field(summary, "DeckId"))
            except AdapterError:
                continue
            fmt_val = vals_s[fmt_ref["ref"]] if fmt_ref is not None else None
            fmt_str = str(fmt_val if fmt_val is not None else (field(summary, "Format") or ""))
            summaries.append(
                {
                    "handle": dh,
                    "deck_id": did,
                    "name": str(field(summary, "Name") or ""),
                    "format": fmt_str,
                }
            )
    return summaries


def _attach_eligible_decks(
    adapter: Any, wrapper_h: int, events: list[dict[str, Any]], timeout: float | None
) -> None:
    """Validate cached decks per constructed event format and attach ``eligible_decks`` & ``near_decks``."""
    from arenamcp.event_queue import event_needs_deck, format_matches, normalize_format_key

    needed_formats: list[str] = []
    seen_fmt: set[str] = set()
    for ev in events:
        if not event_needs_deck(ev):
            continue
        fmt = str(ev.get("deck_select_format") or "").strip()
        if fmt and fmt not in seen_fmt:
            seen_fmt.add(fmt)
            needed_formats.append(fmt)
    if not needed_formats:
        return

    ops_dm = _Ops()
    dm = ops_dm.get(H(wrapper_h), "DecksManager", optional=True)
    cached_ref = ops_dm.add("call", target=dm, method="GetAllCachedDecks", depth=1, optional=True)
    vals_dm = adapter._run(ops_dm, timeout)
    deck_handles = [handle(node) for node in items(vals_dm[cached_ref["ref"]]) if handle(node)]
    if vals_dm[dm["ref"]] is None or vals_dm[cached_ref["ref"]] is None:
        return

    cached_decks = _read_deck_summaries(adapter, deck_handles, timeout, include_format=True)

    eligible_by_format: dict[str, list[dict[str, str]]] = {}
    near_by_format: dict[str, list[dict[str, Any]]] = {}
    for fmt in needed_formats:
        fmt_key = normalize_format_key(fmt)
        if "brawl" in fmt_key:
            candidates = [d for d in cached_decks if "brawl" in normalize_format_key(d["format"])]
        elif fmt_key in _KNOWN_60_FORMATS:
            candidates = [d for d in cached_decks if "brawl" not in normalize_format_key(d["format"])]
        else:
            candidates = list(cached_decks)

        qual: list[dict[str, str]] = []
        near: list[dict[str, Any]] = []
        if candidates:
            try:
                for start in range(0, len(candidates), 4):
                    chunk = candidates[start : start + 4]
                    ops_val = _Ops()
                    refs = []
                    for deck in chunk:
                        view = ops_val.new("Wizards.Mtga.Decks.DeckViewInfo")
                        ops_val.set(view, "deck", H(deck["handle"]))
                        display = ops_val.call(view, "GetValidationForFormat", {"str": fmt})
                        validation = ops_val.get(display, "ValidationResult", depth=1)
                        valid = ops_val.get(validation, "IsValid")
                        refs.append((validation, valid))
                    val_results = adapter._run(ops_val, timeout)
                    for deck, (val_ref, valid_ref) in zip(chunk, refs, strict=True):
                        if val_results[valid_ref["ref"]] is True:
                            qual.append(
                                {
                                    "deck_id": deck["deck_id"],
                                    "name": deck["name"],
                                    "format": deck["format"],
                                }
                            )
                        elif val_results[valid_ref["ref"]] is False:
                            issue_count, summary_text, counters = _summarize_validation_result(
                                val_results[val_ref["ref"]]
                            )
                            near.append(
                                {
                                    "deck_id": deck["deck_id"],
                                    "name": deck["name"],
                                    "format": deck["format"],
                                    "issue_count": issue_count,
                                    "summary": summary_text,
                                    "counters": counters,
                                }
                            )
            except AdapterError:
                qual = [
                    {
                        "deck_id": d["deck_id"],
                        "name": d["name"],
                        "format": d["format"],
                    }
                    for d in cached_decks
                    if format_matches(fmt, d["format"])
                ]
                near = []
        near.sort(
            key=lambda d: (
                d["issue_count"] if d["issue_count"] > 0 else 9999,
                str(d.get("name") or "").lower(),
            )
        )
        eligible_by_format[fmt] = qual
        near_by_format[fmt] = near[:8]

    for ev in events:
        if not event_needs_deck(ev):
            continue
        fmt = str(ev.get("deck_select_format") or "").strip()
        if fmt in eligible_by_format:
            ev["eligible_decks"] = list(eligible_by_format[fmt])
            ev["has_eligible_deck"] = bool(ev["eligible_decks"])
            ev["near_decks"] = list(near_by_format.get(fmt) or [])


def list_available_events(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Enumerate all active events from Arena's EventManager with wallet balances."""
    ops = _Ops()
    wrapper = ops.add("static", **{"class": "WrapperController"}, member="Instance", depth=0)
    event_mgr = ops.get(wrapper, "EventManager", optional=True)
    contexts = ops.get(event_mgr, "EventContexts", depth=1, optional=True)
    home_ref = ops.add("find", **{"class": "HomePageContentController"}, depth=0, optional=True)
    values = adapter._run(ops, timeout)
    wrapper_h = handle(values[wrapper["ref"]])
    if wrapper_h is None:
        raise AdapterError("Arena's event list is only readable outside a match (Home screen)")

    on_home = bool(handle(values[home_ref["ref"]]))
    wallet = _read_wallet(adapter, wrapper_h, timeout)
    ctx_handles = [handle(node) for node in items(values[contexts["ref"]]) if handle(node)]
    events: list[dict[str, Any]] = []

    chunk_size = 4
    for start in range(0, len(ctx_handles), chunk_size):
        chunk = ctx_handles[start : start + chunk_size]
        ops = _Ops()
        refs = []
        for ctx_h in chunk:
            pe = ops.get(H(ctx_h), "PlayerEvent")
            info = ops.get(pe, "EventInfo")
            ux = ops.get(pe, "EventUXInfo", optional=True)
            course = ops.get(pe, "CourseData", optional=True)
            refs.append(
                {
                    "name": ops.get(info, "InternalEventName"),
                    "state": ops.get(info, "EventState", optional=True),
                    "fmt": ops.get(info, "FormatType", optional=True),
                    "precon": ops.get(info, "IsPreconEvent", optional=True),
                    "skip_val": ops.get(info, "SkipDeckValidation", optional=True),
                    "fees": ops.get(info, "EntryFees", depth=2, optional=True),
                    "pub_name": ops.get(ux, "PublicEventName", optional=True),
                    "loc_key": ops.get(ux, "TitleLocKey", optional=True),
                    "behavior": ops.get(ux, "EventBladeBehavior", optional=True),
                    "deck_fmt": ops.get(ux, "DeckSelectFormat", optional=True),
                    "joinability": ops.get(ux, "Joinability", optional=True),
                    "prizes": ops.get(ux, "Prizes", depth=3, optional=True),
                    "comp": ops.get(ux, "EventComponentData", depth=4, optional=True),
                    "module": ops.get(course, "CurrentModule", optional=True),
                    "wins": ops.get(pe, "CurrentWins", optional=True),
                    "losses": ops.get(pe, "CurrentLosses", optional=True),
                    "games": ops.get(pe, "GamesPlayed", optional=True),
                    "unclaimed": ops.get(pe, "HasUnclaimedRewards", optional=True),
                    "win_cond": ops.get(pe, "WinCondition", optional=True),
                }
            )
        chunk_vals = adapter._run(ops, timeout)
        for r in refs:
            event_name = str(chunk_vals[r["name"]["ref"]] or "").strip()
            if (
                not event_name
                or any(event_name.startswith(p) for p in _EXCLUDED_EVENT_PREFIXES)
                or _COLOR_CHALLENGE_EVENT_RE.match(event_name)
            ):
                continue
            ev_state = enum_name(chunk_vals[r["state"]["ref"]])
            if ev_state == "NotActive":
                continue
            mod_name = enum_name(chunk_vals[r["module"]["ref"]])
            behavior = enum_name(chunk_vals[r["behavior"]["ref"]])
            joinability = enum_name(chunk_vals[r["joinability"]["ref"]])
            in_progress = mod_name not in {"", "None", "Join", "Pay", "PayEntry", "Complete"}
            if not in_progress:
                if behavior in _EXCLUDED_BLADE_BEHAVIORS:
                    continue
                if joinability == "Closed":
                    continue
            fees = _normalize_entry_fees(chunk_vals[r["fees"]["ref"]], wallet)
            if not in_progress and fees and not any(f["uses_remaining"] for f in fees):
                continue
            prize_loss = _extract_prize_and_loss_info(
                chunk_vals[r["comp"]["ref"]], chunk_vals[r["prizes"]["ref"]]
            )
            events.append(
                _extract_event_page_details(
                    event_name=event_name,
                    module=mod_name,
                    format_type=enum_name(chunk_vals[r["fmt"]["ref"]]),
                    is_precon=bool(chunk_vals[r["precon"]["ref"]]),
                    deck_select_format=str(chunk_vals[r["deck_fmt"]["ref"]] or ""),
                    joinability=joinability,
                    win_condition=enum_name(chunk_vals[r["win_cond"]["ref"]]),
                    current_wins=num(chunk_vals[r["wins"]["ref"]]),
                    current_losses=num(chunk_vals[r["losses"]["ref"]]),
                    games_played=num(chunk_vals[r["games"]["ref"]]),
                    has_unclaimed_rewards=bool(chunk_vals[r["unclaimed"]["ref"]]),
                    fees=fees,
                    prize_loss=prize_loss,
                    public_name=str(chunk_vals[r["pub_name"]["ref"]] or ""),
                    title_loc_key=str(chunk_vals[r["loc_key"]["ref"]] or ""),
                    skip_deck_validation=bool(chunk_vals[r["skip_val"]["ref"]]),
                )
            )

    _attach_eligible_decks(adapter, wrapper_h, events, timeout)
    return {"ok": True, "on_home": on_home, "wallet": wallet, "events": events}


def get_table_draft_state(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Read TableDraftQueueContentController (the 'Table Ready!' pod queue screen)."""
    found = adapter._find(TABLE_DRAFT_QUEUE_CONTROLLER, timeout)
    if not found:
        return {"ok": True, "is_open": False}
    ops = _Ops()
    ctrl = H(found)
    draft_id_ref = ops.get(ctrl, "_draftId", optional=True)
    ready_flag_ref = ops.get(ctrl, "_isLocalPlayerReady", optional=True)
    pod_num_ref = ops.get(ctrl, "_prevNumInPod", optional=True)
    btn_ref = ops.get(ctrl, "_readyButton", optional=True)
    interactable_ref = ops.get(btn_ref, "Interactable", optional=True)
    btn_go_ref = ops.get(btn_ref, "gameObject", optional=True)
    active_ref = ops.get(btn_go_ref, "activeInHierarchy", optional=True)
    values = adapter._run(ops, timeout)

    draft_id = str(values[draft_id_ref["ref"]] or "").strip()
    is_local_ready = bool(values[ready_flag_ref["ref"]])
    interactable = bool(values[interactable_ref["ref"]])
    active_raw = values[active_ref["ref"]]
    btn_active = True if active_raw is None else bool(active_raw)
    ready_interactable = bool(interactable and btn_active)
    return {
        "ok": True,
        "is_open": True,
        "controller": found,
        "draft_id": draft_id,
        "is_local_ready": is_local_ready,
        "ready_interactable": ready_interactable,
        "num_in_pod": num(values[pod_num_ref["ref"]]),
        "needs_ready": bool(draft_id and not is_local_ready and ready_interactable),
    }


def ready_table_draft(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Click Ready on the TableDraftQueueContentController ('Table Ready!') screen."""
    state = get_table_draft_state(adapter, timeout)
    if not state.get("is_open"):
        raise AdapterError("Table draft queue screen is not open")
    if state.get("is_local_ready"):
        return {"ok": True, "already_ready": True, "draft_id": state["draft_id"]}
    if not state.get("draft_id") or not state.get("ready_interactable"):
        return {"ok": True, "waiting": True, "draft_id": state.get("draft_id", "")}

    ops = _Ops()
    ctrl = H(state["controller"])
    ops.expect_member(ctrl, "_isLocalPlayerReady", False)
    ops.expect_member(ctrl, "_draftId", state["draft_id"])
    ops.call(ctrl, "HandleReadyButtonClicked")
    adapter._run(ops, timeout)
    return {"ok": True, "submitted_type": "TableDraftReady", "draft_id": state["draft_id"]}


def _resolve_event_context_handle(adapter: Any, event_name: str, timeout: float | None) -> tuple[int, int]:
    """Return (player_event_handle, wrapper_handle) for *event_name*."""
    ops = _Ops()
    ep_ref = ops.add("find", **{"class": EVENT_PAGE_CONTROLLER}, depth=0, optional=True)
    wrapper_ref = ops.add(
        "static", **{"class": "WrapperController"}, member="Instance", depth=0, optional=True
    )
    values = adapter._run(ops, timeout)
    ep_h = handle(values[ep_ref["ref"]])
    wrapper_h = handle(values[wrapper_ref["ref"]])

    if ep_h:
        ops2 = _Ops()
        ctx = ops2.get(H(ep_h), "_currentEventContext")
        pe = ops2.get(ctx, "PlayerEvent")
        info = ops2.get(pe, "EventInfo")
        name = ops2.get(info, "InternalEventName")
        vals2 = adapter._run(ops2, timeout)
        if str(vals2[name["ref"]] or "") == event_name and handle(vals2[pe["ref"]]):
            return handle(vals2[pe["ref"]]), wrapper_h or 0

    if not wrapper_h:
        raise AdapterError("Arena's EventManager is unavailable")
    ops3 = _Ops()
    mgr = ops3.get(H(wrapper_h), "EventManager")
    ctx = ops3.call(mgr, "GetEventContext", {"str": event_name})
    pe = ops3.get(ctx, "PlayerEvent")
    vals3 = adapter._run(ops3, timeout)
    pe_h = handle(vals3[pe["ref"]])
    if not pe_h:
        raise AdapterError(f"Event {event_name!r} is not available in EventManager")
    return pe_h, wrapper_h


def enter_event(adapter: Any, command: dict[str, Any], timeout: float | None) -> dict[str, Any]:
    """Enter an event via IPlayerEvent.JoinAndPay after strict fee and wallet checks."""
    event_name = str(command.get("event_name") or "").strip()
    currency_type = str(command.get("currency_type") or "Free").strip()
    expected_qty = int(command.get("expected_quantity") or 0)
    event_choice = str(command.get("event_choice") or "")
    require_free = command.get("require_free", True) is not False

    norm_currency = _normalize_currency_type(currency_type)
    if not event_name:
        raise AdapterError("An event_name is required to enter an event")
    if require_free and (norm_currency not in _FREE_CURRENCIES or expected_qty != 0):
        raise AdapterError("Refusing paid event entry without explicit approval")

    pe_h, wrapper_h = _resolve_event_context_handle(adapter, event_name, timeout)
    wallet = (
        _read_wallet(adapter, wrapper_h, timeout)
        if wrapper_h
        else {"gold": 0, "gems": 0, "draft_tokens": 0, "sealed_tokens": 0, "custom_tokens": {}}
    )

    ops = _Ops()
    pe = H(pe_h)
    course = ops.get(pe, "CourseData")
    mod = ops.get(course, "CurrentModule")
    info = ops.get(pe, "EventInfo")
    name = ops.get(info, "InternalEventName")
    fees_ref = ops.get(info, "EntryFees", depth=2)
    ux = ops.get(pe, "EventUXInfo", optional=True)
    joinability = ops.get(ux, "Joinability", optional=True)
    values = adapter._run(ops, timeout)

    actual_name = str(values[name["ref"]] or "")
    if actual_name != event_name:
        raise AdapterError(f"Event mismatch: expected {event_name!r}, got {actual_name!r}")
    mod_name = enum_name(values[mod["ref"]])
    if mod_name not in _UNPAID_MODULES:
        raise AdapterError(f"Event {event_name} is in module {mod_name!r}, not Join/Pay/PayEntry")
    if enum_name(values[joinability["ref"]]) == "Closed":
        raise AdapterError(f"Event {event_name} is Closed")

    fees = _normalize_entry_fees(values[fees_ref["ref"]], wallet)
    matched = next(
        (
            f
            for f in fees
            if _normalize_currency_type(f["currency_type"]) == norm_currency
            and f["quantity"] == expected_qty
            and f["uses_remaining"]
        ),
        None,
    )
    if matched is None:
        raise AdapterError(
            f"Requested fee {currency_type} ({expected_qty}) not found or no uses remaining for {event_name}"
        )
    if require_free and not matched["is_free"]:
        raise AdapterError("Event entry is not verified free")
    if not matched["affordable"]:
        raise AdapterError(
            f"Insufficient balance for {event_name}: {currency_type} {expected_qty} is unaffordable"
        )

    # Guarded submission frame re-verifying event name, module, and exact fee entry.
    ops_sub = _Ops()
    pe_sub = H(pe_h)
    info_sub = ops_sub.get(pe_sub, "EventInfo")
    ops_sub.expect_member(info_sub, "InternalEventName", event_name)
    course_sub = ops_sub.get(pe_sub, "CourseData")
    ops_sub.expect_member(course_sub, "CurrentModule", mod_name)
    fees_sub = ops_sub.get(info_sub, "EntryFees")
    fee_item = ops_sub.call(fees_sub, "get_Item", I(matched["index"]))
    ops_sub.expect_member(fee_item, "CurrencyType", matched["currency_type"])
    ops_sub.expect_member(fee_item, "Quantity", expected_qty)
    ops_sub.expect_member(fee_item, "UsesRemaining", True)
    ops_sub.call(pe_sub, "JoinAndPay", E(matched["currency_type"]), {"str": event_choice}, B(False))
    adapter._run(ops_sub, timeout)
    return {
        "ok": True,
        "submitted_type": "EventEnter",
        "event_name": event_name,
        "currency_type": matched["currency_type"],
        "quantity": expected_qty,
    }


def is_rewards_panel_open(adapter: Any, timeout: float | None) -> bool:
    """True when ContentControllerRewards is present and its container is active."""
    found = adapter._find(REWARDS_PANEL_CONTROLLER, timeout)
    if not found:
        return False
    ops = _Ops()
    container = ops.get(H(found), "_container", optional=True)
    active = ops.get(container, "activeInHierarchy", optional=True)
    values = adapter._run(ops, timeout)
    if values[container["ref"]] is None:
        return True
    return bool(values[active["ref"]])


def dismiss_rewards_panel(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Click the standard claim button on ContentControllerRewards when visible."""
    found = adapter._find(REWARDS_PANEL_CONTROLLER, timeout)
    if not found:
        return {"ok": True, "is_open": False}
    ops = _Ops()
    ctrl = H(found)
    container = ops.get(ctrl, "_container", optional=True)
    container_active = ops.get(container, "activeInHierarchy", optional=True)
    btn = ops.get(ctrl, "_standardClaimButton", optional=True)
    btn_interactable = ops.get(btn, "Interactable", optional=True)
    btn_go = ops.get(btn, "gameObject", optional=True)
    btn_active = ops.get(btn_go, "activeInHierarchy", optional=True)
    onclick = ops.get(btn, "OnClick", optional=True)
    values = adapter._run(ops, timeout)

    if values[container["ref"]] is not None and not values[container_active["ref"]]:
        return {"ok": True, "is_open": False}
    btn_h = handle(values[btn["ref"]])
    onclick_h = handle(values[onclick["ref"]])
    active_ok = True if values[btn_active["ref"]] is None else bool(values[btn_active["ref"]])
    if not btn_h or not onclick_h or not values[btn_interactable["ref"]] or not active_ok:
        return {"ok": True, "waiting": True}

    ops_click = _Ops()
    ops_click.call(H(onclick_h), "Invoke")
    adapter._run(ops_click, timeout)
    return {"ok": True, "submitted_type": "RewardsClaimDismiss"}


def get_constructed_deck_select(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Read ConstructedDeckSelectController's available decks and event context."""
    found = adapter._find(CONSTRUCTED_DECK_SELECT_CONTROLLER, timeout)
    if not found:
        return {"ok": True, "is_open": False}
    ops = _Ops()
    ctrl = H(found)
    decks_ref = ops.get(ctrl, "_decks", depth=1, optional=True)
    ev_ctx = ops.get(ctrl, "_event", optional=True)
    pe = ops.get(ev_ctx, "PlayerEvent", optional=True)
    course = ops.get(pe, "CourseData", optional=True)
    mod = ops.get(course, "CurrentModule", optional=True)
    info = ops.get(pe, "EventInfo", optional=True)
    ev_name = ops.get(info, "InternalEventName", optional=True)
    is_precon = ops.get(info, "IsPreconEvent", optional=True)
    ux = ops.get(pe, "EventUXInfo", optional=True)
    deck_fmt = ops.get(ux, "DeckSelectFormat", optional=True)
    values = adapter._run(ops, timeout)

    deck_handles = [handle(node) for node in items(values[decks_ref["ref"]]) if handle(node)]
    deck_list: list[dict[str, str]] = [
        {"deck_id": d["deck_id"], "name": d["name"], "format": d["format"]}
        for d in _read_deck_summaries(adapter, deck_handles, timeout, include_format=True)
    ]
    return {
        "ok": True,
        "is_open": True,
        "controller": found,
        "event_name": str(values[ev_name["ref"]] or ""),
        "module": enum_name(values[mod["ref"]]),
        "is_precon": bool(values[is_precon["ref"]]),
        "deck_select_format": str(values[deck_fmt["ref"]] or ""),
        "decks": deck_list,
    }


def close_constructed_deck_select(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Close ConstructedDeckSelectController via OnBack to return to the event page."""
    found = adapter._find(CONSTRUCTED_DECK_SELECT_CONTROLLER, timeout)
    if not found:
        return {"ok": True, "is_open": False}
    ops = _Ops()
    ops.call(H(found), "OnBack")
    adapter._run(ops, timeout)
    return {"ok": True, "submitted_type": "CloseDeckSelect"}


def select_event_deck(adapter: Any, command: dict[str, Any], timeout: float | None) -> dict[str, Any]:
    """Submit a validated constructed/precon deck to IPlayerEvent.SubmitEventDeck."""
    event_name = str(command.get("event_name") or "").strip()
    deck_id = str(command.get("deck_id") or "").strip()
    if not event_name or not deck_id:
        raise AdapterError("event_name and deck_id are required")
    try:
        identifier = uuid.UUID(deck_id)
    except ValueError as exc:
        raise AdapterError(f"Invalid deck_id GUID: {deck_id!r}") from exc
    if not identifier.int or str(identifier) != deck_id:
        raise AdapterError(f"Expected canonical non-empty deck GUID, got {deck_id!r}")

    pe_h, wrapper_h = _resolve_event_context_handle(adapter, event_name, timeout)
    ops = _Ops()
    pe = H(pe_h)
    course = ops.get(pe, "CourseData", optional=True)
    mod = ops.get(course, "CurrentModule", optional=True)
    info = ops.get(pe, "EventInfo")
    is_precon = ops.get(info, "IsPreconEvent", optional=True)
    skip_val = ops.get(info, "SkipDeckValidation", optional=True)
    ux = ops.get(pe, "EventUXInfo", optional=True)
    deck_fmt = ops.get(ux, "DeckSelectFormat", optional=True)
    ds_ctrl = ops.add("find", **{"class": CONSTRUCTED_DECK_SELECT_CONTROLLER}, depth=0, optional=True)
    values = adapter._run(ops, timeout)

    mod_name = enum_name(values[mod["ref"]])
    if mod_name in _POST_DECK_SUBMIT_MODULES:
        ds_h = handle(values[ds_ctrl["ref"]])
        if ds_h:
            try:
                ops_back = _Ops()
                ops_back.call(H(ds_h), "OnBack")
                adapter._run(ops_back, timeout)
            except Exception:
                pass
        return {
            "ok": True,
            "already_submitted": True,
            "module": mod_name,
            "event_name": event_name,
            "deck_id": deck_id,
        }

    fmt_name = str(values[deck_fmt["ref"]] or "")
    precon = bool(values[is_precon["ref"]])
    skip_validation = bool(values[skip_val["ref"]])

    deck_handle: int | None = None
    ds_h = handle(values[ds_ctrl["ref"]])
    if ds_h:
        ops_ds = _Ops()
        decks_node = ops_ds.get(H(ds_h), "_decks", depth=1, optional=True)
        ds_vals = adapter._run(ops_ds, timeout)
        ds_handles = [handle(node) for node in items(ds_vals[decks_node["ref"]]) if handle(node)]
        for rec in _read_deck_summaries(adapter, ds_handles, timeout, include_format=False):
            if rec["deck_id"] == deck_id:
                deck_handle = rec["handle"]
                break

    if deck_handle is None and wrapper_h:
        ops_dm = _Ops()
        dm = ops_dm.get(H(wrapper_h), "DecksManager", optional=True)
        cached = ops_dm.add("call", target=dm, method="GetAllCachedDecks", depth=1, optional=True)
        dm_vals = adapter._run(ops_dm, timeout)
        dm_handles = [handle(node) for node in items(dm_vals[cached["ref"]]) if handle(node)]
        for rec in _read_deck_summaries(adapter, dm_handles, timeout, include_format=False):
            if rec["deck_id"] == deck_id:
                deck_handle = rec["handle"]
                break

    if deck_handle is None:
        raise AdapterError(f"Deck {deck_id} not found in Arena cache or deck selector")

    if not precon and not skip_validation and fmt_name:
        ops_val = _Ops()
        view = ops_val.new("Wizards.Mtga.Decks.DeckViewInfo")
        ops_val.set(view, "deck", H(deck_handle))
        display = ops_val.call(view, "GetValidationForFormat", {"str": fmt_name})
        validation = ops_val.get(display, "ValidationResult", depth=1)
        valid = ops_val.get(validation, "IsValid")
        val_results = adapter._run(ops_val, timeout)
        if val_results[valid["ref"]] is not True:
            raise AdapterError(f"Deck {deck_id} failed validation for event format {fmt_name!r}")

    ops_sub = _Ops()
    ops_sub.call(H(pe_h), "SubmitEventDeck", H(deck_handle))
    adapter._run(ops_sub, timeout)
    return {"ok": True, "submitted_type": "EventDeckSubmit", "event_name": event_name, "deck_id": deck_id}


def submit_event_choice(adapter: Any, command: dict[str, Any], timeout: float | None) -> dict[str, Any]:
    """Submit a choice string on an open Choice-module event."""
    event_name = str(command.get("event_name") or "").strip()
    choice = str(command.get("choice") or "").strip()
    if not event_name or not choice:
        raise AdapterError("event_name and choice are required")
    pe_h, _ = _resolve_event_context_handle(adapter, event_name, timeout)
    ops = _Ops()
    course = ops.get(H(pe_h), "CourseData")
    ops.expect_member(course, "CurrentModule", "Choice")
    ops.call(H(pe_h), "SetChoice", {"str": choice})
    adapter._run(ops, timeout)
    return {"ok": True, "submitted_type": "EventChoice", "event_name": event_name, "choice": choice}


def get_packet_options(adapter: Any, timeout: float | None) -> dict[str, Any]:
    """Read PacketSelectContentController options and already-submitted Jump In packets."""
    found = adapter._find(PACKET_SELECT_CONTROLLER, timeout)
    if not found:
        return {"ok": True, "is_open": False}
    ops = _Ops()
    state_ref = ops.get(H(found), "_currentState", depth=3, optional=True)
    values = adapter._run(ops, timeout)
    state_node = values[state_ref["ref"]]

    def _normalize_packets(raw_list: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pkt in items(raw_list):
            pid = str(field(pkt, "PacketId") or "")
            if not pid:
                continue
            raw_colors = field(pkt, "RawColors")
            colors = (
                [str(c) for c in items(raw_colors)]
                if isinstance(raw_colors, dict)
                else (list(raw_colors) if isinstance(raw_colors, list) else [])
            )
            out.append(
                {
                    "packet_id": pid,
                    "name": str(field(pkt, "Name") or pid),
                    "colors": colors,
                }
            )
        return out

    return {
        "ok": True,
        "is_open": True,
        "controller": found,
        "options": _normalize_packets(field(state_node, "PacketOptions")),
        "submitted": _normalize_packets(field(state_node, "SubmittedPackets")),
    }


def submit_packet(adapter: Any, command: dict[str, Any], timeout: float | None) -> dict[str, Any]:
    """Submit a Jump In / Jumpstart packet choice via PacketSelectContentController.SubmitPack."""
    packet_id = str(command.get("packet_id") or "").strip()
    if not packet_id:
        raise AdapterError("packet_id is required")
    state = get_packet_options(adapter, timeout)
    if not state.get("is_open"):
        raise AdapterError("Packet select screen is not open")
    if packet_id not in {opt["packet_id"] for opt in state.get("options") or []}:
        raise AdapterError(f"Packet {packet_id!r} is not in current options")
    ops = _Ops()
    ops.call(H(state["controller"]), "SubmitPack", {"str": packet_id})
    adapter._run(ops, timeout)
    return {"ok": True, "submitted_type": "JumpstartPacket", "packet_id": packet_id}


def resign_event(adapter: Any, command: dict[str, Any], timeout: float | None) -> dict[str, Any]:
    """Resign from an active free event (used only when free auto-reenter is explicitly enabled)."""
    event_name = str(command.get("event_name") or "").strip()
    require_free = command.get("require_free", True) is not False
    if not event_name:
        raise AdapterError("event_name is required to resign an event")
    pe_h, wrapper_h = _resolve_event_context_handle(adapter, event_name, timeout)
    wallet = (
        _read_wallet(adapter, wrapper_h, timeout)
        if wrapper_h
        else {"gold": 0, "gems": 0, "draft_tokens": 0, "sealed_tokens": 0, "custom_tokens": {}}
    )
    ops = _Ops()
    pe = H(pe_h)
    course = ops.get(pe, "CourseData")
    mod = ops.get(course, "CurrentModule")
    info = ops.get(pe, "EventInfo")
    name = ops.get(info, "InternalEventName")
    fees_ref = ops.get(info, "EntryFees", depth=2)
    values = adapter._run(ops, timeout)

    if str(values[name["ref"]] or "") != event_name:
        raise AdapterError("Event name mismatch during resign")
    mod_name = enum_name(values[mod["ref"]])
    if mod_name not in {"CreateMatch", "WinNoGate", "TransitionToMatches", "WinLossGate"}:
        raise AdapterError(f"Cannot resign event in module {mod_name!r}")
    fees = _normalize_entry_fees(values[fees_ref["ref"]], wallet)
    if require_free and not any(f["is_free"] for f in fees):
        raise AdapterError("Refusing to automatically resign a paid event")

    ops_resign = _Ops()
    ops_resign.expect_member(ops_resign.get(pe, "EventInfo"), "InternalEventName", event_name)
    ops_resign.call(pe, "ResignFromEvent")
    adapter._run(ops_resign, timeout)
    return {"ok": True, "submitted_type": "EventResign", "event_name": event_name}


def validate_deck_for_format(adapter: Any, command: dict[str, Any], timeout: float | None) -> dict[str, Any]:
    """Validate one cached deck against *format_name* using DeckViewInfo.GetValidationForFormat."""
    deck_id = str(command.get("deck_id") or "").strip()
    format_name = str(command.get("format_name") or "").strip()
    if not deck_id or not format_name:
        raise AdapterError("deck_id and format_name are required")
    try:
        identifier = uuid.UUID(deck_id)
    except ValueError as exc:
        raise AdapterError(f"Invalid deck_id GUID: {deck_id!r}") from exc
    if not identifier.int or str(identifier) != deck_id:
        raise AdapterError(f"Expected canonical non-empty deck GUID, got {deck_id!r}")

    ops_dm = _Ops()
    wrapper = ops_dm.add("static", **{"class": "WrapperController"}, member="Instance", depth=0)
    dm = ops_dm.get(wrapper, "DecksManager", optional=True)
    cached = ops_dm.add("call", target=dm, method="GetAllCachedDecks", depth=1, optional=True)
    vals_dm = adapter._run(ops_dm, timeout)
    if handle(vals_dm[wrapper["ref"]]) is None or vals_dm[dm["ref"]] is None:
        raise AdapterError("Arena DecksManager is unavailable")

    dm_handles = [handle(node) for node in items(vals_dm[cached["ref"]]) if handle(node)]
    deck_handle: int | None = None
    for rec in _read_deck_summaries(adapter, dm_handles, timeout, include_format=False):
        if rec["deck_id"] == deck_id:
            deck_handle = rec["handle"]
            break
    if deck_handle is None:
        raise AdapterError(f"Deck {deck_id} not found in Arena cache")

    ops_val = _Ops()
    view = ops_val.new("Wizards.Mtga.Decks.DeckViewInfo")
    ops_val.set(view, "deck", H(deck_handle))
    display = ops_val.call(view, "GetValidationForFormat", {"str": format_name})
    validation = ops_val.get(display, "ValidationResult", depth=1)
    valid = ops_val.get(validation, "IsValid")
    val_results = adapter._run(ops_val, timeout)
    is_valid = val_results[valid["ref"]] is True
    if is_valid:
        return {
            "ok": True,
            "deck_id": deck_id,
            "format_name": format_name,
            "is_valid": True,
            "issue_count": 0,
            "summary": "Valid",
            "counters": {},
        }
    issue_count, summary_text, counters = _summarize_validation_result(val_results[validation["ref"]])
    return {
        "ok": True,
        "deck_id": deck_id,
        "format_name": format_name,
        "is_valid": False,
        "issue_count": issue_count,
        "summary": summary_text,
        "counters": counters,
    }
