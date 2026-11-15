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
        # Helpful Hunter and Spirited Companion only draw on entry. Hare
        # Apparent creates vanilla tokens on entry. Ball Lightning sacrifices
        # itself in the end step. None grants protection or changes spell damage.
        "94fe6ef39dd2f3002883bb455e9c44d44d638ba07eabe703ec4f1b6cc93a7e82",
        "f2e2152b84adb74e97d167380edccedfb3592bd09bd0a2a27063fd88d46fc55a",
        "5989fd35fa2c2f0904647affd4d1fc7fb6267c8c2f227d20f5cb51cf898fc9b0",
        "4cffd35e8e536281d1c262a77a5d9e1807e0606e1db635f02fd7465659820230",
        # These triggers make the opponent lose life. Vito’s activation only
        # grants lifelink; any actually granted or unverified ability on this
        # board is still excluded by the independent native-state checks.
        "ad5e7730e52e962171372d386567871f9d100e2c78da1a148d4c289abdbcf8b5",
        "79353256574893a9ed98ce8dd34c6b8658ea9a6127c0668e1d9d37a9d4637a27",
        # Greedy Freebooter: when it dies, scry and make a Treasure.
        "c2825f8421f4c9dd415c93c58beb52c9893cc97e6ed957ad670fa9c6c0f58099",
        "97948baece0ee357ec2c8a2a524865e721c726675939b6970aab7b78b47f44de",
        # Tinybones: deathtouch; combat damage to a player permits a graveyard cast.
        "19c494337741224cdf2368235003b07ecfc64b3d958ff058e9bfc48cedf3bc55",
    }
)
_ABRADE = (Damage(amount=3, target="creature"), Destroy("artifact"))
_PRINTED_REMOVAL = {
    # Trapped in the Screen exiles an opposing artifact, creature, or enchantment
    # until it leaves. Its own ward does not restrict its entering ability.
    "1c5451cc2780deb960624c9db060dbb37e946fd80e88e6c7d6219549175ad668": (Exile("creature"),),
    # Cooped Up can exile the creature it enchants through its activated ability.
    "1d319980beeeac19985c3ca7b620f6c537b42c7a02ed4dc110e4d380789354fb": (Exile("creature"),),
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


# Reviewed single-card effects from the public native catalog. This library-only
# classification is deliberately separate from battlefield safety: an Aura may
# change toughness or targeting on the board without itself removing a creature.
_LIBRARY_NONREMOVAL = frozenset(
    {
        # Stat increases, keywords and Aura protection.
        "15bdc2334c07a49529aa65ad1f2263cb1560f3799259afe9f8de9feefd61c4ed",  # Optimistic Scavenger
        "651dc71f5e7798f0698097f68e86a6de5e3c8fd740d5bfe01839de5e6fc567b4",  # Ethereal Armor
        "764cb5fcb43ce20ee8a3cb0b8f57335569fc680723e3574f6df0b46743d0330c",  # Shardmage's Rescue
        "e45dbdb7cb2cf5c93a5164902a233bef1259103a5edb3126af4e4ca60f2059c9",  # All That Glitters
        "f60e98ee476877f31988a6358e407438d9d8248eeee3c9fe1aa4f55ca1838fcd",  # Feather of Flight
        "18f6ba9b8e8f8b1da7f6848fa2054d3dee5ba3f96b19f44395202b215dcf31e6",  # Angelic Gift
        "e4b2e91005978750c7e71ec917a1d7ca45d6ee72191865e9cf6b7e0e666c2bb3",  # Audacity
        "09a57715ef878c4f03a235d983e7b6b534dd7f5245e2259945b0805e5d7055db",  # Dawnwing Marshal
        "fe6c7357958886f49b1fe3ee87d559f461944c942b56d45c11d4997f6daaa568",  # Lionheart Glimmer
        # Card draw, life or card access; these do not remove an opposing permanent.
        "dcbb2db078a7ec03767a819bc6592c3b2d565f040d76647e042369c8091ebc68",  # Sram, Senior Edificer
        "f9faee6e99c6653d89095a1f725ded0cd1da37e26e6a9c3d3dee8104e331ddae",  # Hateful Eidolon
        "cf2de831d1483167e2d78cc450f5c3c75840db94c3edc6785f548a46c04865e2",  # Enduring Innocence
        "aafd46c41d52232f79e48e8c4b86dd33878962dc5bc5f4125acaaff61e932841",  # Enduring Curiosity
        "8c67f64e893aadc4fb136457bb2e0db9d4024197401bd8e3d60e54f04323e0b5",  # Ruin-Lurker Bat
        # Own creature tokens, graveyard targets, mana or enchantment cost reductions.
        "afe1eef388124c6515ffeb8cce8f024c47137946ab0d2ea74ff29944659e02d3",  # Spellbook Vendor
        "e68851da79c5577a4984819ec337836ad182603402666587d0e9bffc8e6136f1",  # Veteran Survivor
        "811a0688c49f91ee4e68ae650660dd210be08123cf25070b29f0c6e8f23c8c46",  # Glimmer Seeker
        "bc8cd51753b8503eaf44d04712f94f9dd648154d8872899f9afc59ea2c5b4da5",  # Knight of Doves
        "a2f7ca2eaf6d85cf76d878da5bf40719388d873432e06599d81cb1d02742863e",  # Inquisitive Glimmer
        "e4560be6a1ce850c5a8d98a9e7f11ded8284fbac281005d3bb2b1e9c8ea3bad9",  # Hopeful Vigil
        "ce234256f2c89b4759645dcb6c565fab7a5fafbb29859da033d2ac13d174ba29",  # Slumbering Keepguard
        "d19221871c5cc5cea945908b1d521f1f09f30ec92958b15e8dce97167d64c153",  # Scoured Barrens
        "c2ab9e0868d3fdf63c33e59713d58204b58d6abfc965dfdb7dd2fd625bd8259e",  # Fortified Village
        "a2034ae14e9194fd47fb626fb63f1147b88985150bceb0540fcf32225d7c52b4",  # Port Town
        "5fda33383feb2f102499c179c0201188604ccddde362c30f4ccf6308005c9fe1",  # Battlefield Forge
        # Creature-type bonuses, tokens, card access and cost reductions.
        "e272b80aec08a780f048d309dd11dc35a74e901204d064d2a58c4cd535b94d96",  # Rat Colony
        "f98720192fb293032e6ebcb183196899c5f7a24c5e6dd8438b2a9e83a184d7fb",  # Seven Dwarves
        "75bcf9cac9ef3fd7d4b68189934e01708a5d02c208649a97ddf5965db00187dd",  # Persistent Petitioners
        "9109c475cb87ba7e19d15cbb34d7bb1d81fe7cf28b59a020cf37b34da323d432",  # Oketra's Monument
        "d9678dba649d575bf72c8d1ce6f0d05389a2955000864927bb1c12ad28283046",  # Bontu's Monument
        "592b20808641339e77a81d96f368db05fb49c01f9983c56881a6484e3eb2cd06",  # Rhonas's Monument
        "52d869612fa2829361351883414b0457467a752e8cc4d2699d2c9905a1e400fb",  # Hazoret's Monument
        "08e05f858ab026870edfc4a3af88db4a7fcb9587e1e3fee6cd2e677134d27aca",  # Herald's Horn
        "0cc7a7d887287a2d0fed4c86df747c62e0bef6cf6330d14208cd9f72a88c9cf0",  # Patchwork Banner
        "91aad1c84863d01706be9c283c5783937a9e7200a5a07d407ed505b09e5f4e98",  # Icon of Ancestry
        "69f0bc42b97c776a9a4f3761c4b91faadc536c67edc45a13804868815434bec8",  # Vanquisher's Banner
        "ee621589be7e3b7b58e5bc06cd93e1f3f7ed83e0f927dce345069d0c87bf58f0",  # Door of Destinies
        "92a68294af28d559a5eeb22ffe763312a304f438e56ed0c4fb2bf47aadcab8f8",  # Regal Bunnicorn
        "921742989b10ca84e6a3c87fd1636ab938380f7d6162565237fc941dce3506dc",  # Valley Questcaller
        "1d98f617e52fd8817e1ba74e7c4e5cdef765fc4d8281b9f8d4b0694d51b2744d",  # Banner of Kinship
    }
)


def reviewed_library_nonremoval(card):
    digest = legacy_lookup_hash(card)
    if not digest:
        return False
    if digest in _LIBRARY_NONREMOVAL:
        return True
    # The Medallion cycle has only a cost reduction. Full-match the entire
    # single-faced printed text so a second ability can never be hidden.
    return card["type_line"] == "Artifact" and bool(
        re.fullmatch(
            r"(?:White|Blue|Black|Red|Green) spells you cast cost \{o?1\} less to cast\.", card["oracle_text"]
        )
    )


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
