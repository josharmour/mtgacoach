"""Exact single-face printed facts used by the local library census.

These are potential direct removal effects of individual cards, never
recommendations to cast or assertions that spells will resolve. Every listed
digest covers name, cost, type, and complete printed text. Native and Oracle
versions are separate reviewed inputs; neither is normalized into the other.
"""

import re

from arenamcp.ability_synthesizer import Bounce, Damage, Destroy, Exile
from arenamcp.card_facts.source_identity import legacy_lookup_hash

# These abilities do not alter toughness, targetability, destroy/exile/bounce
# legality or spell damage. Other public board effects are checked separately.
_SUPPORTED_CREATURES = frozenset(
    {
        # Meloku: Flying; return a land to create a flying Illusion.
        "09c5de70b93ac0afe75a010ebd813c15c2c0532069afbf87d41d11f283682ef6",
        # Hinterland Sanctifier: gain one life when another friendly creature enters.
        "dd77807b1a57da4e95db9cd5a1da80ec23a3be1591f851d7dc01dfcf5fc1b93a",
        # Greedy Freebooter: when it dies, scry and make a Treasure.
        "c2825f8421f4c9dd415c93c58beb52c9893cc97e6ed957ad670fa9c6c0f58099",
        "97948baece0ee357ec2c8a2a524865e721c726675939b6970aab7b78b47f44de",
        # Tinybones: deathtouch; combat damage to a player permits a graveyard cast.
        "19c494337741224cdf2368235003b07ecfc64b3d958ff058e9bfc48cedf3bc55",
    }
)
_ABRADE = (Damage(amount=3, target="creature"), Destroy("artifact"))
_PRINTED_REMOVAL = {
    # Abrade's artifact mode also removes a high-toughness artifact creature.
    "ffbff8d51203f41c6661214f5bcd91ed2ee6fdba918a6a1bac3437cc411867ee": _ABRADE,
    "0562b4cca9d74585bf4a09da8b1c87bb52a505fbacb438d7a67abba80fbdc68e": _ABRADE,
    # Infernal Grasp (caster loses life); Swords (controller gains life).
    "84af1e772c643d0ef247f7bbd2fbb07bb25b90aa9fc7e0951f3b696ac545ad4c": (Destroy("creature"),),
    "51dab6c9037fe13704afbfd43fc5d314732485bdc0601691112c573d096bd575": (Exile("creature"),),
    # Scorching Dragonfire: its exile replacement requires the creature to die.
    "410664388f41bbfe1d6263716ccb37ab7ac3ec30712d9a898e9b942ca89d6d62": (
        Damage(amount=3, target="creature"),
    ),
    # Play with Fire scries only on player damage. Lightning Helix also gains life.
    "a7b2ed6aa1f04d4fd2e5fc5cd510dd19bb51c7b7c6e49b3e7e0c4bc4ff865b4a": (
        Damage(amount=2, target="creature"),
    ),
    "86d375002afc80dca56623786656c12085ac43a0fecf432e1aba88af96097c1a": (
        Damage(amount=3, target="creature"),
    ),
    # Get Lost creates Maps. Fateful Absence investigates.
    "3273df904f4407d410021f72d0ac1a352de57edead580375d639c3a8fe97f389": (Destroy("creature"),),
    "45503af49a78daa43dd282ec7b1d2bbbf57d7e0c620ce3cc1b185091b3ac634a": (Destroy("creature"),),
    "9fa52231c7189e603f946cff82acc0691504f6d825520c29f5ef82b370c098a3": (Destroy("creature"),),
    "94647e4943df689d12c0faa8dd72ba270c941e43b783c43e30c57cda8ac8b379": (Destroy("creature"),),
    # Leyline Binding's domain reduces its casting cost. Static Prison
    # can later be sacrificed; neither contradicts the initial exile.
    "7dae02d9c185f7899409af8a3e557c883505d1b373cd79e0163fdd9879c1f89a": (Exile("nonland permanent"),),
    "e478f77a1999b19246d4548517427a6815251b872b4e1caf9bee53f705b3d3b2": (Exile("nonland permanent"),),
    # Seam Rip and Portable Hole stop at MV 2 and cannot hit a land.
    "032295c606d200ebf833f55aa34e1e4cb5ab42423632013f6ac300c810239c90": (
        Exile("nonland permanent with mana value 2 or less"),
    ),
    "6962b6a4bfde98551cfa9c9d6133928b54d3884fd8c066b4698145a92cf56a08": (
        Exile("nonland permanent with mana value 2 or less"),
    ),
    # Glass Casket targets a creature with MV 3 or less, including a land creature.
    "ab1e278095faa948810163527d1ba94c54d739fe64b718fe1522215453945b73": (
        Exile("creature with mana value 3 or less"),
    ),
    # Banishing Light and Seal from Existence exile an opposing nonland
    # permanent when they enter, until their own source leaves.
    "a581354a0624846a8ac75fbb2fa417fec0138870746fd50ee1cd77168864ec86": (Exile("nonland permanent"),),
    "04e90d83ee67c9a1ad890cb1f498f7ef774e9220effc7a431993e24a7450b0b1": (Exile("nonland permanent"),),
    "6614fc1114bc9e1c8cf2c9575c7ac97573b74f9f9533993fb2562ea841378e52": (Exile("nonland permanent"),),
}


def supported_public_creature(digest):
    return digest in _SUPPORTED_CREATURES


_REMOVAL = re.compile(
    r"(Destroy|Exile) target (creature(?: with flying| or planeswalker)?|nonartifact creature|nonlegendary creature|tapped creature|nonland permanent|permanent)\."
)


def printed_removal_effects(card, *, local_creature=False, local_basic=False):
    """Only precision-matched typed effects are exposed; unknown prints abstain."""
    if not isinstance(card, dict):
        return None
    digest = legacy_lookup_hash(card)
    if not digest:
        return None
    # These Auras must have a legal local permanent to enchant before their
    # separate opposing exile target matters. A missing prerequisite abstains.
    if digest in {
        "d1de18dd17a5c63946d5315afd3678378fa7791ee98b6bff554ab47003cc3c3b",  # Sheltered by Ghosts
        "d3554a9824614060b32904dc2ff499cdaa8877bbc940d7f784084fc23ba9d7d4",
    }:
        return (Exile("nonland permanent"),) if local_creature else None
    if digest == "7366d34a0093e2c2f693c227184d9a8c2a72cd3fbfec32a54c6bc3d00eea1256":  # Ossification
        return (Exile("creature"),) if local_basic else None
    effects = _PRINTED_REMOVAL.get(digest)
    if effects:
        return effects
    if card["type_line"] not in ("Instant", "Sorcery"):
        return None
    text = card["oracle_text"]
    match = _REMOVAL.fullmatch(text)
    if match:
        kind, target = match.groups()
        effects = (Destroy(target) if kind == "Destroy" else Exile(target),)
    elif text == "Return target creature to its owner's hand.":
        effects = (Bounce(target="creature"),)
    else:
        damage = re.fullmatch(
            rf"(?:{re.escape(card['name'])}|This spell) deals ([1-9][0-9]?) damage to (any target|target creature(?: with flying| or planeswalker)?)\.",
            text,
        )
        if not damage:
            return None
        effects = (Damage(amount=int(damage[1]), target=damage[2]),)
    return effects
