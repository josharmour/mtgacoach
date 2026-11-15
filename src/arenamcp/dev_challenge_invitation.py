"""Owner-enrolled, read-only comparison of the issuer’s room and the Arena invitation tile."""

import hmac
import os
from pathlib import Path

from arenamcp import dev_challenge_pair as gate
from arenamcp.dev_challenge_identity import write_private_new
from arenamcp.mac_bridge_adapter import _Ops, field, guid

_FORMATS = (
    "DirectGame",
    "DirectGameBrawl",
    "DirectGameLimited",
    "DirectGameAlchemy",
    "DirectGameTournamentMode",
    "DirectGameTournamentLimited",
    "DirectGameTournamentHistoric",
    "DirectGameTournamentAlchemy",
    "DirectGameTournamentExplorer",
    "DirectGameTournamentTimeless",
)
_TILE = "SocialV2_IncomingChallengeRequestTile"


def _config():
    pair = gate.configured_pair()
    path = os.environ.get("MTGACOACH_DEV_DC_ROOM_TICKET")
    match_type = os.environ.get("MTGACOACH_DEV_DC_MATCH_TYPE")
    length = os.environ.get("MTGACOACH_DEV_DC_BEST_OF")
    if (
        not pair.pins
        or not path
        or not Path(path).is_absolute()
        or match_type not in _FORMATS
        or length not in {"1", "3"}
    ):
        raise gate.PairUnavailable(gate.PairStatus.CONFIG_TICKET)
    return pair, Path(path), match_type, length == "3"


def _ticket(obs, match_type, best_of_three):
    room = obs.room
    if (
        not room
        or gate._native_state(field(room, "MatchType"), _FORMATS) != match_type
        or field(room, "IsBestOf3") is not best_of_three
    ):
        raise gate.PairUnavailable(gate.PairStatus.TICKET)
    members, invites = gate._people(room)
    owner = obs.local_id if obs.pair.role == "issuer" else obs.peer_id
    if set(members) != {owner} or len(invites) != 1 or invites[0][3] != "Sent":
        raise gate.PairUnavailable(gate.PairStatus.TICKET_ROLE)
    selected_deck = field(members[owner], "DeckId")
    if obs.pair.role == "issuer" and (
        not gate._valid_id(selected_deck) or guid(selected_deck) != obs.pair.deck
    ):
        raise gate.PairUnavailable(gate.PairStatus.ACTIVE_DECK)
    room_id = guid(field(room, "ChallengeId"))
    scope = (
        "dev-challenge-offer-v1",
        obs.pair.pins.issuer,
        obs.pair.pins.acceptor,
        room_id,
        match_type,
        str(best_of_three),
    )
    expected = hmac.digest(obs.pair.pins.key, "\0".join(scope).encode(), "sha256").hex()
    return room_id, expected


def _check_active_tile(adapter, obs, room_id, timeout):
    """Read the active native tile's public identity. Arena handles all user input."""
    ops = _Ops()
    tile = ops.add("find", **{"class": _TILE}, depth=0)
    ops.expect_class(tile, _TILE)
    ops.expect_member(tile, "isActiveAndEnabled", True)
    ops.expect_member(ops.get(tile, "gameObject"), "activeInHierarchy", True)
    ops.expect_member(tile, "ChallengeSenderFullDisplayName", obs.pair.peer)
    ops.expect_member(tile, "ChallengeSenderPlayerId", obs.peer_id)
    value = ops.add("get", target=tile, member="IncomingChallengeId", depth=1)
    # Both bridges can encode a Guid into a read result. The Windows plugin
    # cannot retain the boxed value as the target of a later operation.
    values = adapter._run(ops, timeout)
    if not gate._valid_id(values[value["ref"]]) or guid(values[value["ref"]]) != room_id:
        raise gate.PairUnavailable(gate.PairStatus.TICKET)


def invitation_action(adapter, timeout, *, record):
    pair, path, match_type, best_of_three = _config()
    if pair.role != ("issuer" if record else "acceptor"):
        raise gate.PairUnavailable(gate.PairStatus.TICKET_ROLE)
    obs = gate.observe(adapter, timeout, pair)
    expected_status = gate.PairStatus.ACTIVE if record else gate.PairStatus.INVITED
    if obs.status is not expected_status:
        raise gate.PairUnavailable(gate.PairStatus.TICKET_ROLE)
    room_id, expected = _ticket(obs, match_type, best_of_three)
    if path.is_dir():
        path = path / (expected + ".ticket")
    encoded = expected.encode("ascii")
    if record:
        try:
            write_private_new(path, encoded)
            return gate.PairStatus.RECORDED
        except FileExistsError:
            pass
    try:
        with path.open("rb") as stream:
            if not hmac.compare_digest(stream.read(65), encoded):
                raise gate.PairUnavailable(gate.PairStatus.TICKET)
    except FileNotFoundError:
        raise gate.PairUnavailable(gate.PairStatus.TICKET) from None
    if record:
        return gate.PairStatus.RECORDED
    _check_active_tile(adapter, obs, room_id, timeout)
    return gate.PairStatus.VERIFIED


def record_offer(adapter, timeout):
    return invitation_action(adapter, timeout, record=True)


def verify_offer(adapter, timeout):
    return invitation_action(adapter, timeout, record=False)
