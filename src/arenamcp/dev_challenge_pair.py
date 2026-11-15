"""Local read-only readiness for a developer's two explicitly configured Arena accounts."""

import os
import sys
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from arenamcp.dev_challenge_identity import EnrolledPair, IdentityUnavailable, load_enrollment
from arenamcp.mac_bridge_adapter import AdapterError, H, _Ops, field, guid, handle, items


class PairStatus(StrEnum):
    INCOMPLETE = "Arena did not provide complete challenge evidence on this client."
    CONTROLLER = "Arena did not supply its signed-in challenge controller."
    MEMBERSHIP = "Arena did not supply the complete lobby membership."
    AMBIGUOUS = "Arena returned ambiguous lobby membership."
    LIST = "Arena returned an incomplete participant list."
    DECK_ID = "Arena returned an unreadable deck identity."
    REFRESHING = "Arena is refreshing its empty friends list. Check the pairing again after it loads."
    SOCIAL = "Arena has not confirmed its current connected friends list."
    UNBOUND = "The local lobby has not established the configured peer."
    ACTIVE_DECK = "Arena has selected a different or unreadable deck for this lobby."
    CONFIG_DECK = "Configure an exact native deck ID on this machine."
    CONFIG_ROLE = "Configure the issuer or acceptor role on this machine."
    CONFIG_NAMES = "Configure two distinct, exact Arena full names on this machine."
    DISABLED = "Direct-challenge checks require an enabled developer source checkout."
    CONFIG_PINS = "The selected local native pairing profile could not be verified."
    MULTIPLE = "Arena reported ambiguous current challenges or invitations."
    STATUS = "The challenge or invitation is no longer in setup."
    ACTIVE_ROLE = "The active lobby differs from the configured participant pair or role."
    ACTIVE_LOCAL = "The active lobby does not have unambiguous local membership."
    IDLE = "The configured account, friend and deck agree. No invitation is pending."
    ACTIVE = "The active lobby matches both configured participants. The configured deck is available."
    INVITED = "The configured account, friend, deck and incoming invitation agree."
    DECK_NOT_FOUND = "The configured deck was not found uniquely in Arena’s local cache."
    FRIEND = "The configured peer does not resolve to one distinct Arena friend."
    INVITE_ROLE = "The incoming invitation does not match this machine’s acceptor role."
    RECIPIENT = "The invitation does not have one unambiguous recipient."
    OWNER = "The invitation owner is missing from the native lobby."
    FOREIGN_INVITE = "The lobby contains a different invitation."
    WRONG_ACCOUNT = "The signed-in account does not match this machine’s configured role."


class PairUnavailable(ValueError):
    """The message describes a check and never contains a participant or deck identifier."""


def source_enabled():
    from arenamcp.settings import is_developer_mode

    checkout = Path(__file__).resolve().parents[2]
    return (
        not getattr(sys, "frozen", False)
        and (checkout / ".git").exists()
        and (checkout / "pyproject.toml").is_file()
        and os.environ.get("MTGACOACH_DEV_DIRECT_CHALLENGE") == "1"
        and is_developer_mode()
    )


@dataclass(frozen=True, repr=False)
class Pair:
    role: str
    local: str
    peer: str
    deck: str
    pins: EnrolledPair | None = None


def configured_pair():
    if not source_enabled():
        raise PairUnavailable(PairStatus.DISABLED)
    role, local, peer, deck = (
        os.environ.get("MTGACOACH_DEV_DC_" + k, "")
        for k in ("ROLE", "LOCAL_FULL_NAME", "OPPONENT_FULL_NAME", "DECK_ID")
    )
    profile = os.environ.get("MTGACOACH_DEV_DC_PAIR_PROFILE")
    pins = None
    if profile:
        try:
            pins = load_enrollment(profile)
        except (OSError, IdentityUnavailable):
            raise PairUnavailable(PairStatus.CONFIG_PINS) from None
    if role not in ("issuer", "acceptor"):
        raise PairUnavailable(PairStatus.CONFIG_ROLE)
    if (not pins or local or peer) and (
        any(not v or len(v) > 100 or v.strip() != v or not v.isprintable() for v in (local, peer))
        or local == peer
    ):
        raise PairUnavailable(PairStatus.CONFIG_NAMES)
    try:
        identity = uuid.UUID(deck)
    except ValueError:
        identity = None
    if not identity or not identity.int or str(identity) != deck:
        raise PairUnavailable(PairStatus.CONFIG_DECK)
    return Pair(role, local, peer, deck, pins)


def _all_items(node):
    rows = items(node)
    if (
        not isinstance(node, dict)
        or type(node.get("$items")) is not list
        or type(node.get("$n")) is not int
        or node["$n"] != len(rows)
        or node.get("$more")
    ):
        raise PairUnavailable(PairStatus.LIST)
    return rows


def _entries(node, key):
    table = field(node, key)
    if not isinstance(table, dict):
        raise PairUnavailable(PairStatus.MEMBERSHIP)
    # Arena dictionaries expose an allocated backing array, including free
    # slots. Count only live key/value pairs, and check against the actual count.
    count = field(table, "count")
    free = field(table, "freeCount", 0)
    if type(count) is int and type(free) is int and count == free == 0 and field(table, "entries") is None:
        return []
    records = []
    for slot in _all_items(field(table, "entries")):
        if not isinstance(slot, dict) or slot.get("$more") or slot.get("$ref"):
            raise PairUnavailable(PairStatus.AMBIGUOUS)
        missing = object()
        k, v = field(slot, "key", missing), field(slot, "value", missing)
        if k is None and v is None:
            continue
        if not isinstance(k, str) or not k or not isinstance(v, dict):
            raise PairUnavailable(PairStatus.AMBIGUOUS)
        records.append((k, v))
    if (
        type(count) is not int
        or type(free) is not int
        or not 0 <= free <= count
        or count - free != len(records)
        or len({k for k, _ in records}) != len(records)
    ):
        raise PairUnavailable(PairStatus.AMBIGUOUS)
    return records


def _valid_id(value):
    try:
        raw = guid(value)
        return bool(uuid.UUID(raw).int)
    except (ValueError, AdapterError):
        return False


_CHALLENGE_STATES = ("None", "Setup", "Starting", "WaitingForMatch", "InGame", "Removed")
_INVITE_STATES = ("None", "Sent", "Accepted", "Rejected", "Cancelled")


def _native_state(node, names):
    if not isinstance(node, dict):
        return None
    index = node.get("v")
    if type(index) is int and 0 <= index < len(names) and node.get("e") == names[index]:
        return names[index]
    return None


def _people(row):
    members = _entries(row, "ChallengePlayers")
    invites = _entries(row, "Invites")
    # The encoder emits the same ChallengePlayer only once per root. An
    # issuer’s native invite Sender is the existing local ChallengePlayer.
    people = [person for _, person in members]
    for _, invitation in invites:
        people.extend((field(invitation, "Sender"), field(invitation, "Recipient")))
    by_handle = {}
    for person in people:
        identity = handle(person)
        if type(identity) is int and identity > 0 and not person.get("$ref"):
            if identity in by_handle and by_handle[identity] != person:
                raise PairUnavailable(PairStatus.AMBIGUOUS)
            by_handle[identity] = person

    def resolve(person):
        if isinstance(person, dict) and person.get("$ref") is True:
            return by_handle.get(handle(person))
        return person

    def identity(person):
        evidence = resolve(person)
        return field(evidence, "PlayerId"), field(evidence, "FullDisplayName")

    return {key: resolve(value) for key, value in members}, [
        (
            recipient,
            identity(field(invite, "Sender")),
            identity(field(invite, "Recipient")),
            _native_state(field(invite, "Status"), _INVITE_STATES),
        )
        for recipient, invite in invites
    ]


def classify(pair, name, local_id, friends, active, incoming, deck_found):
    if pair.pins:
        if not pair.pins.matches(pair.role, name, local_id) or (pair.local and pair.local != name):
            raise PairUnavailable(PairStatus.WRONG_ACCOUNT)
        peer_role = "acceptor" if pair.role == "issuer" else "issuer"
        names = [
            field(row, "FullName")
            for row in _all_items(friends)
            if pair.pins.matches(peer_role, field(row, "FullName"), field(row, "PlayerId"))
        ]
        if len(names) != 1 or (pair.peer and pair.peer != names[0]):
            raise PairUnavailable(PairStatus.FRIEND)
        pair = Pair(pair.role, name, names[0], pair.deck, pair.pins)
    if name != pair.local or not isinstance(local_id, str) or not local_id:
        raise PairUnavailable(PairStatus.WRONG_ACCOUNT)
    friend_rows = _all_items(friends)
    peers = [field(f, "PlayerId") for f in friend_rows if field(f, "FullName") == pair.peer]
    if (
        len(peers) != 1
        or not isinstance(peers[0], str)
        or not peers[0]
        or peers[0] == local_id
        or sum(field(f, "PlayerId") == peers[0] for f in friend_rows) != 1
    ):
        raise PairUnavailable(PairStatus.FRIEND)
    if not deck_found:
        raise PairUnavailable(PairStatus.DECK_NOT_FOUND)
    peer_id = peers[0]
    invites = _all_items(incoming)
    if len(invites) > 1:
        raise PairUnavailable(PairStatus.MULTIPLE)
    if active is None and not invites:
        return PairStatus.IDLE
    row = active if active is not None else invites[0]
    owner = local_id if pair.role == "issuer" else peer_id
    if (
        field(row, "LocalPlayerId") != local_id
        or field(row, "LocalPlayerDisplayName") != pair.local
        or field(row, "ChallengeOwnerId") != owner
        or not _valid_id(field(row, "ChallengeId"))
    ):
        raise PairUnavailable(PairStatus.ACTIVE_ROLE)
    # The unified challenge blade accepts an incoming native room in None;
    # Setup is the owner’s open lobby. Both still need a strictly Sent invite.
    states = {"Setup"} if active is not None else {"None", "Setup"}
    if _native_state(field(row, "Status"), _CHALLENGE_STATES) not in states:
        raise PairUnavailable(PairStatus.STATUS)
    members, invitations = _people(row)
    local, peer = (local_id, pair.local), (peer_id, pair.peer)
    if any(
        key != field(person, "PlayerId") or (key, field(person, "FullDisplayName")) not in (local, peer)
        for key, person in members.items()
    ):
        raise PairUnavailable(PairStatus.MEMBERSHIP)
    if owner not in members:
        raise PairUnavailable(PairStatus.OWNER)
    issuer, acceptor = (local, peer) if pair.role == "issuer" else (peer, local)
    if any(
        (recipient, sender, receiver) != (acceptor[0], issuer, acceptor)
        or status not in {"None", "Sent", "Accepted"}
        for recipient, sender, receiver, status in invitations
    ):
        raise PairUnavailable(PairStatus.FOREIGN_INVITE)
    if active is not None:
        if invites and not (
            pair.role == "acceptor"
            and type(handle(active)) is int
            and handle(active) > 0
            and handle(active) == handle(invites[0])
            and guid(field(active, "ChallengeId")) == guid(field(invites[0], "ChallengeId"))
            and len(invitations) == 1
            and invitations[0][3] == "Accepted"
        ):
            raise PairUnavailable(PairStatus.MULTIPLE)
        if peer_id not in members and not invitations:
            raise PairUnavailable(PairStatus.UNBOUND)
        if local_id not in members:
            raise PairUnavailable(PairStatus.ACTIVE_LOCAL)
        try:
            selected = uuid.UUID(guid(field(members[local_id], "DeckId")))
        except (ValueError, AdapterError):
            raise PairUnavailable(PairStatus.ACTIVE_DECK) from None
        if selected.int and str(selected) != pair.deck:
            raise PairUnavailable(PairStatus.ACTIVE_DECK)
        return PairStatus.ACTIVE
    if pair.role != "acceptor" or local_id in members:
        raise PairUnavailable(PairStatus.INVITE_ROLE)
    if len(invitations) != 1 or invitations[0][3] != "Sent":
        raise PairUnavailable(PairStatus.RECIPIENT)
    return PairStatus.INVITED


def check(adapter, timeout):
    pair = configured_pair()
    ops = _Ops()
    home = ops.add("find", **{"class": "HomePageContentController"}, depth=0)
    ctrl = ops.get(home, "_pvpChallengeController")
    wrapper = ops.get(ctrl, "_challengeCommunicationWrapper")
    account = ops.get(ops.get(ctrl, "_accountClient"), "AccountInformation")
    current = ops.get(account, "DisplayName")
    owner_id = ops.get(account, "PersonaID")
    friends = ops.add("call", target=wrapper, method="GetFriends", depth=2, max_items=256)
    social = ops.get(wrapper, "_socialManager")
    connected = ops.get(social, "Connected")
    enabled = ops.get(social, "IsSocialEnabled")
    friend_count = ops.get(friends, "Count")
    all_rooms = ops.add("call", target=ctrl, method="GetAllChallenges", depth=1)
    lobby = ops.add("call", target=ctrl, method="GetActiveCurrentChallengeData", depth=6, max_items=32)
    incoming = ops.add("call", target=ctrl, method="GetIncomingChallengeRequests", depth=7, max_items=32)
    values = adapter._run(ops, timeout)
    if len(values) != len(ops.ops) or handle(values[ctrl["ref"]]) is None:
        raise PairUnavailable(PairStatus.CONTROLLER)
    if (
        values[connected["ref"]] is not True
        or values[enabled["ref"]] is not True
        or type(values[friend_count["ref"]]) is not int
        or values[friend_count["ref"]] != len(_all_items(values[friends["ref"]]))
    ):
        raise PairUnavailable(PairStatus.SOCIAL)
    current_name, persona = values[current["ref"]], values[owner_id["ref"]]
    if (
        not isinstance(current_name, str)
        or not current_name
        or not isinstance(persona, str)
        or not persona
        or (pair.local and current_name != pair.local)
        or (pair.pins and not pair.pins.matches(pair.role, current_name, persona))
    ):
        raise PairUnavailable(PairStatus.WRONG_ACCOUNT)
    if values[friend_count["ref"]] == 0:
        # The live Windows client can report an enabled, connected but empty
        # cache even after reaching Home. Use Arena's normal roster refresh;
        # a subsequent observation still has to match this exact peer.
        target = handle(values[social["ref"]])
        if type(target) is not int or target <= 0:
            raise PairUnavailable(PairStatus.SOCIAL)
        refresh = _Ops()
        refresh.call(H(target), "RefreshFriendsData")
        adapter._run(refresh, timeout)
        raise PairUnavailable(PairStatus.REFRESHING)
    # GetActiveCurrentChallengeData returns only the first native lobby.
    # Check source dictionary cardinality independently before reporting a pair.
    rooms = values[all_rooms["ref"]]
    count, free = field(rooms, "count"), field(rooms, "freeCount")
    observed = int(values[lobby["ref"]] is not None or bool(_all_items(values[incoming["ref"]])))
    if type(count) is not int or type(free) is not int or not 0 <= free <= count or count - free != observed:
        raise PairUnavailable(PairStatus.MULTIPLE)
    # Read only cached deck identities. The challenge screen does not need
    # card names, piles or any player's collection to preflight its role.
    listing = _Ops()
    controller = listing.add("static", **{"class": "WrapperController"}, member="Instance", depth=0)
    decks = listing.get(controller, "DecksManager")
    records = listing.add("call", target=decks, method="GetAllCachedDecks", depth=1, max_items=512)
    nodes = _all_items(adapter._run(listing, timeout)[records["ref"]])
    matches = 0
    for offset in range(0, len(nodes), 16):
        ids = _Ops()
        identifiers = []
        for node in nodes[offset : offset + 16]:
            if not handle(node):
                raise PairUnavailable(PairStatus.DECK_ID)
            summary = ids.get(H(handle(node)), "Summary")
            identifiers.append(ids.get(summary, "DeckId", depth=1))
        values_ids = adapter._run(ids, timeout)
        matches += sum(guid(values_ids[r["ref"]]) == pair.deck for r in identifiers)
    found = matches == 1
    return classify(pair, *(values[x["ref"]] for x in (current, owner_id, friends, lobby, incoming)), found)
