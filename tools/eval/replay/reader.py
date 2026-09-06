"""Reader for MTGA's TimedReplay (`.rply`) format.

Format: a header line `#Version2` followed by a JSON metadata blob (one
line, with player + opponent info, deck, battlefield), followed by one
GRE message per line in the form:

    <DIRECTION>-<TIMESTAMP_US>:<JSON_PAYLOAD>

Where DIRECTION is `IN` (server -> client = GREToClientMessage) or `OUT`
(client -> server = ClientToGREMessage), TIMESTAMP_US is microseconds
since match start, and JSON_PAYLOAD is the proto message rendered to JSON
with proto3-style camelCase field names (matches what
`Wotc.Mtga.Replays.ReplayUtilities.SerializeProtoMessage` produces).

Decompiled reference: `re-output/Core/Wotc.Mtga.Replays/ReplayUtilities.cs`
and `re-output/Core/Wotc.Mtga.TimedReplays/ReplayReader.cs`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Anchored prefix: prevents accidental matches if a JSON payload contains
# a substring that looks like our own prefix. The colon after the
# timestamp is the separator.
_LINE_RE = re.compile(r"^(IN|OUT)-(\d+):(.*)$", re.DOTALL)


@dataclass
class ReplayMessage:
    """One GRE message extracted from a replay line."""

    direction: str  # 'IN' (server->client) or 'OUT' (client->server)
    timestamp_us: int
    payload: dict  # parsed JSON of the GRE message

    @property
    def msg_type(self) -> str:
        """`type` field on the proto, e.g. 'GREMessageType_GameStateMessage'."""
        return str(self.payload.get("type") or "")

    @property
    def msg_id(self) -> Optional[int]:
        v = self.payload.get("msgId")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def is_request(self) -> bool:
        """True if this is a GREToClient request prompting user action.

        The set of request types comes from re-output/SharedClientCore/
        GreClient.Rules/RequestTranslation.cs — every GREMessageType that
        builds a BaseUserRequest. Excludes informational types
        (UIMessage, TimerStateMessage, etc.) that don't ask for input.
        """
        return self.direction == "IN" and self.msg_type in _REQUEST_TYPES


_REQUEST_TYPES = frozenset(
    {
        "GREMessageType_ActionsAvailableReq",
        "GREMessageType_DeclareAttackersReq",
        "GREMessageType_DeclareBlockersReq",
        "GREMessageType_AssignDamageReq",
        "GREMessageType_MulliganReq",
        "GREMessageType_ChooseStartingPlayerReq",
        "GREMessageType_SearchReq",
        "GREMessageType_SelectNReq",
        "GREMessageType_GroupReq",
        "GREMessageType_OrderReq",
        "GREMessageType_DistributionReq",
        "GREMessageType_NumericInputReq",
        "GREMessageType_PayCostsReq",
        "GREMessageType_SelectTargetsReq",
        "GREMessageType_AutoTapActionsReq",
        "GREMessageType_OptionalActionMessage",
        "GREMessageType_CastingTimeOptionsReq",
        "GREMessageType_StringInputReq",
        "GREMessageType_IntermissionReq",
        "GREMessageType_GatherReq",
        "GREMessageType_SelectReplacementReq",
        "GREMessageType_SelectCountersReq",
        "GREMessageType_SubmitDeckReq",
    }
)


@dataclass
class ReplayMetadata:
    """The pre-message header line from a `.rply`."""

    raw: dict
    local_screen_name: str
    opponent_screen_name: str
    battlefield_id: Optional[str]


def parse_replay_path(path: Path) -> tuple[ReplayMetadata, list[ReplayMessage]]:
    """Read an entire `.rply` into memory.

    Yields a (metadata, messages) tuple. Lines that don't match the prefix
    pattern or contain invalid JSON are skipped silently — we have plenty
    of replays so robustness > strictness.
    """
    metadata: Optional[ReplayMetadata] = None
    messages: list[ReplayMessage] = []
    with open(path, encoding="utf-8") as f:
        first = f.readline().rstrip("\r\n")
        if not first.startswith("#Version"):
            raise ValueError(f"unexpected replay header: {first!r}")
        meta_line = f.readline().rstrip("\r\n")
        if meta_line:
            try:
                meta_obj = json.loads(meta_line)
                local = meta_obj.get("Local") or {}
                opp = meta_obj.get("Opponent") or {}
                metadata = ReplayMetadata(
                    raw=meta_obj,
                    local_screen_name=str(local.get("ScreenName") or ""),
                    opponent_screen_name=str(opp.get("ScreenName") or ""),
                    battlefield_id=meta_obj.get("BattlefieldId"),
                )
            except json.JSONDecodeError:
                metadata = ReplayMetadata(
                    raw={}, local_screen_name="", opponent_screen_name="", battlefield_id=None
                )
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            direction, ts, payload_str = m.group(1), int(m.group(2)), m.group(3)
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                continue
            messages.append(ReplayMessage(direction=direction, timestamp_us=ts, payload=payload))
    if metadata is None:
        metadata = ReplayMetadata(raw={}, local_screen_name="", opponent_screen_name="", battlefield_id=None)
    return metadata, messages


def iter_decision_points(
    messages: list[ReplayMessage],
) -> Iterator[tuple[ReplayMessage, Optional[ReplayMessage]]]:
    """Yield (request, response) pairs.

    For each `IN` request message, find the next `OUT` message (the
    player's response). We pair lazily: many requests are immediately
    followed by their response, but UI messages can be interleaved. The
    "response" is just the next OUT message after a request (modulo
    intervening IN messages, which are state updates, not responses).

    Yields (request, response) where response may be None if the replay
    ended before the player answered.
    """
    n = len(messages)
    for i, msg in enumerate(messages):
        if not msg.is_request:
            continue
        # Walk forward to find the next OUT message that's not a UI
        # heartbeat. Skip UIMessage on the OUT side too — they are
        # cosmetic events, not request answers.
        j = i + 1
        response: Optional[ReplayMessage] = None
        while j < n:
            nxt = messages[j]
            if nxt.direction == "OUT" and nxt.msg_type != "ClientMessageType_UIMessage":
                response = nxt
                break
            j += 1
        yield msg, response


def main():
    """CLI: dump a replay's decision points for ad-hoc inspection."""
    import argparse

    p = argparse.ArgumentParser(description="Inspect an MTGA .rply file")
    p.add_argument("path", type=Path)
    p.add_argument("--limit", type=int, default=20, help="How many decisions to show")
    args = p.parse_args()

    meta, messages = parse_replay_path(args.path)
    print(f"=== {args.path.name} ===")
    print(f"local: {meta.local_screen_name!r}  opp: {meta.opponent_screen_name!r}")
    print(f"battlefield: {meta.battlefield_id}")
    print(
        f"messages: {len(messages)} (IN={sum(1 for m in messages if m.direction == 'IN')}, OUT={sum(1 for m in messages if m.direction == 'OUT')})"
    )

    decisions = list(iter_decision_points(messages))
    print(f"decision points: {len(decisions)}\n")
    for k, (req, resp) in enumerate(decisions[: args.limit]):
        rt = req.msg_type.replace("GREMessageType_", "")
        rp = resp.msg_type.replace("ClientMessageType_", "") if resp else "<none>"
        print(f"  [{k:3d}] t={req.timestamp_us}us  {rt:35s} -> {rp}")


if __name__ == "__main__":
    main()
