"""Extract REAL main-phase-1 legal-action menus from MTGA .rply replays.

Ground truth for the synthesized-menu spike. Restricts to ActionsAvailableReq
where the local seat has priority during their own precombat main phase --
the same decision point synthesize_menu.py reconstructs from 17lands rows.

Emits one JSONL record per decision with the menu normalized to
(actionType, grpId) pairs, plus the zone facts needed to replay the menu
model against perfect inputs: `hand_grp_ids`, `untapped_land_grp_ids`,
`all_land_grp_ids`, `board_grp_ids`, `cast_costs`, and the player's `pick`.

Replays live on the Linux box (plex, 10.0.0.100):
    ~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/
        MTGA/MTGA_Data/StreamingAssets/Tests/

Stdlib only, so it runs under the system python there:
    python3 tools/training/extract_real_menus.py --dir <Tests/> --out real_menus.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.eval.replay.decisions import pair_request_response  # noqa: E402
from tools.eval.replay.reader import parse_replay_path  # noqa: E402
from tools.eval.replay.state import snapshot_at_decision  # noqa: E402


def normalize_action(a: dict) -> tuple[str, int | None]:
    """(actionType stripped of ActionType_, grpId or None)."""
    at = (a.get("actionType") or "?").replace("ActionType_", "")
    gid = a.get("grpId")
    try:
        gid = int(gid) if gid is not None else None
    except (TypeError, ValueError):
        gid = None
    return (at, gid)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--seat", type=int, default=2)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    files = sorted(args.dir.glob("*.rply"))
    menu_sizes: list[int] = []
    distinct_sizes: list[int] = []
    type_counter: Counter[str] = Counter()
    pick_type_counter: Counter[str] = Counter()
    n_decisions = 0
    n_files_ok = 0
    n_files_err = 0
    all_aa = 0
    pick_in_menu = 0
    pick_checked = 0
    hand_sizes: list[int] = []
    lands_in_play: list[int] = []

    with args.out.open("w") as fh:
        for path in files:
            try:
                meta, messages = parse_replay_path(path)
            except Exception as exc:  # noqa: BLE001
                n_files_err += 1
                print(f"  !! {path.name}: {exc}", file=sys.stderr)
                continue
            n_files_ok += 1
            by_resp = pair_request_response(messages)
            for m in messages:
                if m.msg_type != "GREMessageType_ActionsAvailableReq" or m.direction != "IN":
                    continue
                all_aa += 1
                snap = snapshot_at_decision(messages, m, local_seat_id=args.seat)
                if snap.phase != "Phase_Main1":
                    continue
                if snap.active_player != args.seat or snap.priority_player != args.seat:
                    continue
                req = m.payload.get("actionsAvailableReq") or {}
                actions = req.get("actions") or []
                norm = [normalize_action(a) for a in actions]
                # There is always an implicit pass; MTGA lists it explicitly
                # as ActionType_Pass in most menus.
                n_decisions += 1
                menu_sizes.append(len(norm))
                distinct_sizes.append(len({(t, g) for t, g in norm}))
                for t, _g in norm:
                    type_counter[t] += 1

                resp = by_resp.get(m.msg_id) if m.msg_id is not None else None
                picked = []
                if resp is not None:
                    picked = [
                        normalize_action(a)
                        for a in ((resp.payload.get("performActionResp") or {}).get("actions") or [])
                    ]
                if picked:
                    pick_checked += 1
                    menu_set = {(t, g) for t, g in norm}
                    if picked[0] in menu_set:
                        pick_in_menu += 1
                    pick_type_counter[picked[0][0]] += 1
                else:
                    pick_type_counter["<none/pass-ack>"] += 1

                bf = snap.battlefield(args.seat)
                hand_sizes.append(snap.hand_size(args.seat))
                lands_in_play.append(len(bf))

                inactive = [normalize_action(a) for a in (req.get("inactiveActions") or [])]
                lands_bf = [o for o in bf if "CardType_Land" in (o.get("cardTypes") or [])]
                untapped_lands = [o for o in lands_bf if not o.get("isTapped")]
                fh.write(
                    json.dumps(
                        {
                            "file": path.name,
                            "msg_id": m.msg_id,
                            "turn": snap.turn_number,
                            "menu": [[t, g] for t, g in norm],
                            "inactive": [[t, g] for t, g in inactive],
                            "cast_costs": [
                                sum(c.get("count", 0) for c in (a.get("manaCost") or []))
                                for a in actions
                                if a.get("actionType") == "ActionType_Cast"
                            ],
                            "n_lands_bf": len(lands_bf),
                            "n_lands_untapped": len(untapped_lands),
                            "untapped_land_grp_ids": [o.get("grpId") for o in untapped_lands],
                            "all_land_grp_ids": [o.get("grpId") for o in lands_bf],
                            "board_grp_ids": [o.get("grpId") for o in bf],
                            "hand_grp_ids": [o.get("grpId") for o in snap.hand(args.seat)],
                            "pick": [[t, g] for t, g in picked],
                        }
                    )
                    + "\n"
                )

    def med(xs):
        return statistics.median(xs) if xs else 0

    print(
        json.dumps(
            {
                "files_ok": n_files_ok,
                "files_err": n_files_err,
                "actions_available_requests_total": all_aa,
                "own_main1_decisions": n_decisions,
                "mean_menu_size": round(sum(menu_sizes) / len(menu_sizes), 2) if menu_sizes else 0,
                "median_menu_size": med(menu_sizes),
                "mean_distinct_menu_size": round(sum(distinct_sizes) / len(distinct_sizes), 2)
                if distinct_sizes
                else 0,
                "max_menu_size": max(menu_sizes) if menu_sizes else 0,
                "menu_action_type_mix": type_counter.most_common(),
                "pick_action_type_mix": pick_type_counter.most_common(),
                "pick_in_menu": pick_in_menu,
                "pick_checked": pick_checked,
                "pick_in_menu_pct": round(100.0 * pick_in_menu / pick_checked, 2) if pick_checked else None,
                "mean_hand_size": round(sum(hand_sizes) / len(hand_sizes), 2) if hand_sizes else 0,
                "mean_battlefield_size": round(sum(lands_in_play) / len(lands_in_play), 2)
                if lands_in_play
                else 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
