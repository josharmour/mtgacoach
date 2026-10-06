"""Draft strategy fixes from the 2026-10-06 FRA traditional draft (draft d789225a).

The fallback ranking made 40 of 42 picks after the model's synergy claims were
rejected wholesale, took 8 unrated, rarely played cards on a rarity prior, and
stayed in a weak UR lane while black flowed. Packs and picks below are decoded
from that draft's Player.log; card rows are the FRA primer / 17lands snapshot of
the day, inlined so the tests need no local cache.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.draft_advisor import DraftAdvisor
from arenamcp.draft_autopick import is_removal, pool_lane, rank_pack
from arenamcp.limited_rules import rules_profile, synergy_evidence
from arenamcp.set_primer import SetCard, SetPrimer

# grp_id|name|colors|rarity|types|cmc|arena cost|GIH|baseline z|ATA|games in hand|play rate|game WR|rules
CARD_TABLE = r"""
106226|Academic Ascent|W|C|Instant|2.0|{1}{W}|0.5091|-1.318|11.19|660|0.162|0.5175|Target creature gets +2/+2 and gains flying until end of turn.\nEmpower Jace 2.
106227|Blossom-Blessed Angel|W|C|Creature - Angel Cleric|4.0|{3}{W}|0.558|-0.027|7.23|3310|0.737|0.5445|Flying\nVigilance\nThis creature enters prepared.
106252|Cryotheory Adept|U|C|Creature - Human Wizard|2.0|{1}{U}|0.5282|-0.813|9.83|2109|0.389|0.5324|Prowess\n{o3oU}, Exile this card from your graveyard: Tap target creature and put a stun counter on it. Activate only as a sorcery.
106255|Divining Duelist|U|C|Creature - Merfolk Wizard|3.0|{2}{U}|0.5474|-0.307|10.0|1920|0.346|0.5431|Flash\nWhen this creature enters, choose one —\n•Tap target creature.\n•Untap target creature.\n•Draw a card, then discard a card.
106256|Icy Reception|U|C|Instant|2.0|{1}{U}|0.5962|0.981|6.84|6484|0.793|0.5742|Choose one —\n•Counter target creature or legendary spell unless its controller pays {o3}.\n•Target creature gets -5/-0 until end of turn.
106259|Mindseeker Oculus|U|C|Creature - Homunculus|3.0|{2}{U}|0.6092|1.324|4.05|9020|0.929|0.5738|When this creature enters, empower Jace 4.
106265|Semester Foreseer|U|C|Creature - Human Wizard|4.0|{3}{U}|0.546|-0.343|8.55|3377|0.552|0.5407|This creature enters prepared.\nWhen this creature enters, surveil 1.
106267|Sphinx of False Conclusions|U|R|Creature - Sphinx Illusion|4.0|{2}{U}{U}|0.6415|2.175|1.61|1297|0.932|0.5873|Flash\nFlying\nWhenever this creature attacks, draw a card, then discard a card.\nWhen this creature dies, if it isn't a token, create a token that's a copy of it.
106268|Sphinx's Approach|U|C|Instant|3.0|{1}{U}{U}|0.5849|0.681|10.85|1361|0.205|0.5642|Draw two cards. Then you may exile this spell and four cards named Sphinx's Approach from your graveyard. If you do, search your library for a Sphinx creature card, put i
106272|Undulating Witness|U|C|Creature - Serpent|5.0|{4}{U}|0.5767|0.467|7.61|6008|0.784|0.5589|Flying\n{o2}: This creature gets +1/-1 until end of turn.\nBasic landcycling {o2}
106273|Unsummon|U|C|Instant|1.0|{U}|0.5832|0.637|7.21|6125|0.752|0.5678|Return target creature to its owner's hand.
106279|Break Under Pressure|B|U|Instant|3.0|{2}{B}|0.598|1.029|3.32|3356|0.926|0.5581|Target opponent sacrifices a creature or planeswalker with the greatest mana value among creatures and planeswalkers they control. You gain 2 life.
106283|Extended Absence|B|C|Instant|4.0|{3}{B}|0.5862|0.717|3.87|7040|0.935|0.5594|Exile target creature or planeswalker. Extended Absence deals 1 damage to each opponent and you gain 1 life.
106289|Rampart Hunter|B|C|Creature - Horror|4.0|{3}{B}|||11.66|393|0.093|0.4468|Deathtouch\nWhen this creature enters, target creature gets +2/+2 and gains deathtouch until end of turn.
106290|Rank Rat|B|C|Creature - Zombie Rat|2.0|{1}{B}|0.5564|-0.069|7.81|4139|0.709|0.5448|When this creature enters, each opponent discards a card.
106291|Rewrite Regrets|B|U|Sorcery|4.0|{3}{B}|0.6044|1.198|4.73|3160|0.844|0.5659|Return target creature or planeswalker card with mana value 6 or less from your graveyard to the battlefield.\nEmpower Jace 2.
106299|Void Extrapolator|B|C|Creature - Aetherborn Warlock|2.0|{1}{B}|0.5717|0.335|9.57|3122|0.494|0.5592|This creature enters prepared.\nThreshold — This creature gets +1/+1 as long as there are seven or more cards in your graveyard.
106303|Artifist Acumen|R|C|Sorcery|1.0|{R}|||12.08|387|0.1|0.542|Creatures you control gain first strike until end of turn.\nDraw a card.
106305|Blazing Crescendo|R|C|Instant|2.0|{1}{R}|0.5217|-0.984|11.57|667|0.167|0.5247|Target creature gets +3/+1 until end of turn. Exile the top card of your library. Until the end of your next turn, you may play that card.
106318|Identity Echo|R|R|Enchantment|3.0|{2}{R}|||9.32|26|0.035||{o3oR}: Exile target creature or planeswalker you control. Reveal cards from the top of your library until you reveal a creature or planeswalker card. Put that card onto
106327|Tether Technician|R|C|Creature - Minotaur Artificer|5.0|{4}{R}|0.5369|-0.585|10.98|1207|0.275|0.5315|Reach\nWhen this creature enters, you may discard a card. When you do, this creature deals 2 damage to any target.
106355|Wrecking Gecko|G|C|Artifact Creature - Lizard Construct|5.0|{4}{G}|0.542|-0.449|9.45|2321|0.49|0.5285|Ward {o2}\n{o6oGoG}: This creature gets +4/+4 and gains trample until end of turn.
106358|Blessed Ghoul|WB|C|Creature - Zombie Cleric|1.0|{W/B}|0.5477|-0.299|8.89|1992|0.495|0.5315|Lifelink\n{o2o(W/B)}: Return this card from your graveyard to your hand.
106361|Clash of Elements|UR|U|Instant|3.0|{1}{U}{R}|0.5824|0.616|6.66|2179|0.613|0.563|Choose target nonland permanent. Its owner may put it on top of their library. If they do, Clash of Elements deals 2 damage to them. If they didn't put the card on top of
106368|Fatehold Charm|WU|U|Instant|2.0|{W}{U}|0.6004|1.091|6.48|2600|0.71|0.5806|Choose one —\n•Draw a card. Empower Jace 2.\n•Return target spell or creature to its owner's hand.\n•Creatures you control get +1/+2 until end of turn.
106369|Fatehold Chronologist|WU|C|Creature - Bird Wizard|2.0|{1}{W/U}|0.5683|0.244|6.01|6041|0.872|0.5568|Flying\nThis creature enters prepared.
106373|Grim Repriser|BR|U|Creature - Zombie Bard|2.0|{B}{R}|0.5235|-0.937|8.6|978|0.504|0.546|Prowess\n{oBoR}: Return this card from your graveyard to the battlefield with a finality counter on it. Activate only if an opponent has been dealt noncombat damage this t
106376|Konstrari Improviser|RG|C|Creature - Human Artificer|2.0|{1}{R/G}|0.5341|-0.659|5.86|4198|0.848|0.5425|This creature enters prepared.
106379|Mind Meanderer|UG|U|Creature - Bird Fish Illusion|6.0|{3}{G}{U}{U}|0.5958|0.969|6.25|2459|0.643|0.555|Flying\nThis creature has vigilance as long as you control a Jace planeswalker.\nWhen this creature enters, it fights up to one target creature an opponent controls.
106381|Paradox Shaper|UB|U|Creature - Octopus Wizard|2.0|{1}{U/B}|0.5596|0.015|7.78|2139|0.611|0.5543|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.\n{o2}: Put target card from your graveyard on the bottom of your library.
106390|Stingerquill Charm|BR|U|Instant|2.0|{B}{R}|0.5748|0.417|6.98|1256|0.596|0.5405|Choose one —\n•Stingerquill Charm deals 3 damage to any target.\n•Target creature gains first strike and deathtouch until end of turn.\n•Create a 2/2 colorless Wizard Soldie
106391|Stingerquill Voxmancer|BR|U|Creature - Goblin Sorcerer|1.0|{B/R}|0.5408|-0.482|6.01|1361|0.738|0.5364|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.
106394|Tam's Resistance|UG|C|Sorcery|2.0|{1}{G/U}|0.5677|0.229|8.36|3211|0.523|0.5394|Put a +1/+1 counter on up to one target creature. It gains vigilance until end of turn.\nEmpower Jace 4.
106397|Theorix Metamage|UB|C|Creature - Shade Wizard|3.0|{2}{U/B}|0.5352|-0.63|9.45|2915|0.45|0.5317|This creature enters prepared.\nThreshold — This creature gets +1/+0 and has flying as long as there are seven or more cards in your graveyard.
106399|Twinned Vision|UR|C|Instant|2.0|{1}{U/R}|0.5906|0.832|7.13|5823|0.746|0.5793|Draw a card. If this spell wasn't cast from your hand, draw two cards instead.\nFlashback—{o1o(U/R)o(U/R)}, Discard a card.
106407|Whiplash Wordsmith|BR|C|Creature - Vampire Sorcerer|4.0|{3}{B/R}|0.5428|-0.428|10.61|1028|0.288|0.5373|This creature enters prepared.\nAs long as an opponent was dealt noncombat damage this turn, this creature has flying and haste.
106409|Woodwork Prodigy|RG|U|Creature - Cat Druid|3.0|{2}{R/G}|0.5223|-0.968|4.65|1679|0.826|0.5312|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.
106411|Afterthought Sentry||C|Artifact Creature - Gargoyle|2.0|{2}|0.5493|-0.257|11.07|1045|0.2|0.5367|{o2}: This creature gains flying until end of turn.\nWhenever this creature attacks, exile up to one target card from a graveyard.
106416|Keeper of the Quiet Hour||C|Artifact Creature - Chimera|3.0|{3}|0.5719|0.34|9.32|3990|0.544|0.5606|When this creature enters, empower Jace 2.
106417|Living Library||C|Artifact Creature - Book Illusion|2.0|{2}|||11.99|441|0.092|0.5126|{o6}, Sacrifice this creature: Choose target creature or planeswalker an opponent controls. Its owner shuffles it into their library.
106420|Dedicated Commons||C|Land|||0.5392|-0.523|9.91|1415|0.473|0.5377|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oR} or {oW}.
106423|Formidable Commons||C|Land|||0.5543|-0.126|8.9|2699|0.683|0.5525|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oB} or {oG}.
106433|Room of Refuge||C|Land|||0.5656|0.173|7.39|6485|0.925|0.5566|This land enters tapped. As it enters, choose a color.\n{oT}: Add one mana of the chosen color.\n{o5}, {oT}, Sacrifice this land: Put two +1/+1 counters on target creature.
106435|Stingerquill Annex||C|Land|||0.5397|-0.509|9.22|1875|0.558|0.5428|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oB} or {oR}.
106446|Lyra, Archangel of Dawn|W|R|Legendary Creature - Angel Knight|3.0|{2}{W}|0.5833|0.641|2.57|792|0.873|0.5424|Flying\nWhenever you gain life, put a +1/+1 counter on each Angel you control.
106449|Teyo, Lightshield Expert|W|U|Legendary Creature - Human Cleric|2.0|{1}{W}|0.5626|0.094|5.31|1669|0.82|0.5341|Flash\nWhen Teyo enters, target permanent you control gains hexproof until end of turn. Put a +1/+1 counter on it if it's a creature. Put a loyalty counter on it if it's a
106458|Fblthp, Impossibly Lost|U|U|Legendary Creature - Homunculus|2.0|{1}{U}|0.629|1.845|5.34|4121|0.809|0.5698|When one or more of your opponents are dealt combat damage during your turn, draw two cards. If your library has no cards in it, you win the game. Fblthp's owner shuffles
106464|Ruric Thar, Biomagus|U|U|Legendary Creature - Ogre Crab Wizard|6.0|{4}{U}{U}|0.5636|0.12|4.37|2383|0.839|0.5348|Flying\nProwess\nWhenever Ruric Thar becomes the target of a spell or ability an opponent controls, draw a card.
106466|Tetsuko Umezawa, Fugitive|U|U|Legendary Creature - Human Rogue|2.0|{1}{U}|0.5651|0.159|8.08|1598|0.53|0.5438|Creatures you control with power or toughness 1 or less can't be blocked.
106470|Yargle, Goliath of Otaria|U|U|Legendary Creature - Frog Spirit|5.0|{4}{U}|||11.4|116|0.05||
106477|Loot, the Anomaly|B|U|Legendary Creature - Beast Horror|3.0|{2}{B}|||10.67|205|0.106||If Loot's power is negative, he assigns combat damage as though his power were positive.\nThreshold — Sacrifice another creature or planeswalker: Loot gets -2/-0 until end
106488|Arni, Renowned Champion|R|U|Legendary Creature - Human Berserker|4.0|{3}{R}|||10.3|355|0.205|0.4852|Trample\nWhenever another creature you control enters, Arni gets +X/+0 until end of turn, where X is that creature's power.
106501|Winter, Team Player|R|U|Legendary Creature - Human Warrior|5.0|{4}{R}|||10.07|485|0.271|0.4986|Convoke\nWhenever you cast a noncreature spell, creatures you control get +1/+0 until end of turn.
106522|Vraska, Soul of Stone|WUR|R|Legendary Creature - Gorgon Wizard|3.0|{U}{R}{W}|||6.35|304|0.449|0.5161|Artifact creatures you control have vigilance.\nWhenever you cast a noncreature spell, create a 1/1 colorless Sculpture Treasure artifact creature token with "{oT}, Sacrif
"""


ARCHETYPES = [
    {
        "colors": "WU",
        "name": "Azorius Scry & Surveil Tempo",
        "tier": 1,
        "payoffs": [],
        "enablers": ["Semester Foreseer", "Fatehold Chronologist"],
        "key_commons": [
            "Semester Foreseer",
            "Fatehold Chronologist",
            "Icy Reception",
            "Unsummon",
            "Sphinx's Approach",
            "Fatehold Charm",
        ],
        "key_uncommons": ["Mindseeker Oculus", "Fblthp, Impossibly Lost"],
    },
    {
        "colors": "WB",
        "name": "Orzhov Removal & Reanimator",
        "tier": 2,
        "payoffs": ["Rewrite Regrets", "Blessed Ghoul", "Lyra, Archangel of Dawn"],
        "enablers": ["Break Under Pressure", "Extended Absence"],
        "key_commons": ["Break Under Pressure", "Extended Absence", "Blessed Ghoul"],
        "key_uncommons": ["Rewrite Regrets", "Lyra, Archangel of Dawn"],
    },
    {
        "colors": "WR",
        "name": "Boros Tokens & Burn",
        "tier": 2,
        "payoffs": ["Winter, Team Player"],
        "enablers": [],
        "key_commons": ["Keeper of the Quiet Hour"],
        "key_uncommons": [],
    },
    {
        "colors": "WG",
        "name": "Selesnya Lifegain Counters",
        "tier": 2,
        "payoffs": ["Lyra, Archangel of Dawn"],
        "enablers": [],
        "key_commons": [],
        "key_uncommons": ["Lyra, Archangel of Dawn", "Teyo, Lightshield Expert", "Blossom-Blessed Angel"],
    },
    {
        "colors": "UB",
        "name": "Dimir Threshold Mill",
        "tier": 1,
        "payoffs": ["Void Extrapolator", "Theorix Metamage"],
        "enablers": ["Semester Foreseer", "Mindseeker Oculus"],
        "key_commons": ["Break Under Pressure", "Extended Absence", "Theorix Metamage", "Void Extrapolator"],
        "key_uncommons": ["Mindseeker Oculus", "Fblthp, Impossibly Lost", "Sphinx of False Conclusions"],
    },
    {
        "colors": "UR",
        "name": "Izzet Spells & Jace Empowerment",
        "tier": 2,
        "payoffs": ["Mindseeker Oculus"],
        "enablers": ["Twinned Vision", "Unsummon", "Icy Reception"],
        "key_commons": [
            "Twinned Vision",
            "Mindseeker Oculus",
            "Keeper of the Quiet Hour",
            "Unsummon",
            "Icy Reception",
            "Cryotheory Adept",
        ],
        "key_uncommons": ["Sphinx of False Conclusions"],
    },
    {
        "colors": "UG",
        "name": "Simic Ramp & Big Stuffs",
        "tier": 2,
        "payoffs": ["Mind Meanderer"],
        "enablers": [],
        "key_commons": ["Tam's Resistance"],
        "key_uncommons": ["Mind Meanderer"],
    },
    {
        "colors": "BR",
        "name": "Rakdos Noncombat Damage / Discard",
        "tier": 2,
        "payoffs": ["Grim Repriser", "Whiplash Wordsmith"],
        "enablers": ["Rank Rat"],
        "key_commons": ["Rank Rat", "Void Extrapolator", "Whiplash Wordsmith"],
        "key_uncommons": [
            "Stingerquill Charm",
            "Break Under Pressure",
            "Extended Absence",
            "Rewrite Regrets",
        ],
    },
    {
        "colors": "BG",
        "name": "Golgari Self-Mill Threshold",
        "tier": 1,
        "payoffs": ["Void Extrapolator", "Rewrite Regrets"],
        "enablers": [],
        "key_commons": ["Void Extrapolator", "Extended Absence"],
        "key_uncommons": ["Rewrite Regrets"],
    },
    {
        "colors": "RG",
        "name": "Gruul Heartwood Ramp",
        "tier": 2,
        "payoffs": [],
        "enablers": ["Konstrari Improviser"],
        "key_commons": ["Wrecking Gecko"],
        "key_uncommons": [],
    },
]
SYNERGY_NOTES = [
    {
        "cards": ["Sphinx of False Conclusions", "Twinned Vision"],
        "why": "Sphinx loots on attack to fill the graveyard with instants/s",
    }
]
PAIR_STATS = {
    "WU": {"win_rate": 0.5596},
    "WB": {"win_rate": 0.5339},
    "WR": {"win_rate": 0.5226},
    "WG": {"win_rate": 0.5343},
    "UB": {"win_rate": 0.5666},
    "UR": {"win_rate": 0.5391},
    "UG": {"win_rate": 0.5375},
    "BR": {"win_rate": 0.5257},
    "BG": {"win_rate": 0.5671},
    "RG": {"win_rate": 0.5433},
}
TRAPS = [
    {
        "card": "Grim Repriser",
        "why": "-3.9 IWD; requires opponent damage plus mana investment for a conditional reanimation body",
    }
]
BASICS = {
    106526: "Plains",
    106527: "Plains",
    106528: "Island",
    106529: "Island",
    106530: "Swamp",
    106531: "Swamp",
    106533: "Mountain",
    106534: "Forest",
    106535: "Forest",
}
# Every pick of the draft, in order (P1p1..P3p14).
ACTUAL_PICKS = [
    106268,
    106273,
    106361,
    106390,
    106433,
    106361,
    106501,
    106449,
    106416,
    106535,
    106327,
    106394,
    106535,
    106355,
    106267,
    106256,
    106416,
    106272,
    106458,
    106379,
    106488,
    106416,
    106470,
    106265,
    106303,
    106305,
    106289,
    106530,
    106259,
    106272,
    106272,
    106272,
    106399,
    106368,
    106433,
    106255,
    106501,
    106265,
    106417,
    106305,
    106289,
    106305,
]
PACKS = {
    "P1p7": [106411, 106407, 106355, 106358, 106417, 106227, 106501, 106527],
    "P2p7": [106303, 106299, 106252, 106355, 106291, 106488, 106522, 106423],
    "P3p3": [106272, 106283, 106305, 106369, 106417, 106376, 106279, 106409, 106318, 106446, 106477, 106435],
    "P3p9": [106265, 106358, 106417, 106373, 106501, 106420],
}
# What the new ranking would have drafted through P3p2 (replayed on the same packs).
UB_LEANING_POOL_P3P3 = [
    106268,
    106273,
    106361,
    106273,
    106394,
    106416,
    106411,
    106449,
    106416,
    106397,
    106227,
    106394,
    106226,
    106355,
    106267,
    106256,
    106464,
    106272,
    106458,
    106466,
    106291,
    106381,
    106290,
    106265,
    106391,
    106305,
    106289,
    106530,
    106259,
    106272,
]


def _num(value: str, kind=float):
    return kind(value) if value else None


def fra_primer(play_data: bool = True) -> SetPrimer:
    cards = {}
    for line in CARD_TABLE.strip().splitlines():
        grp, name, colors, rarity, types, cmc, _cost, gih, base, ata, games, play, game_wr, rules = (
            line.split("|")
        )
        cards[int(grp)] = SetCard(
            int(grp),
            name,
            colors,
            rarity,
            types,
            cmc=_num(cmc),
            oracle=rules.replace("\\n", "\n"),
            gih_wr=_num(gih),
            baseline=_num(base),
            ata=_num(ata),
            games=int(games),
            play_rate=_num(play) if play_data else None,
            game_wr=_num(game_wr) if play_data else None,
        )
    return SetPrimer(
        set_code="FRA",
        source="model",
        archetypes=ARCHETYPES,
        synergy_notes=SYNERGY_NOTES,
        pair_stats=PAIR_STATS,
        traps=TRAPS,
        cards=cards,
    )


COSTS = {int(line.split("|")[0]): line.split("|")[6] for line in CARD_TABLE.strip().splitlines()}
NAMES = {
    **{int(line.split("|")[0]): line.split("|")[1] for line in CARD_TABLE.strip().splitlines()},
    **BASICS,
}
ID = {name: grp_id for grp_id, name in NAMES.items()}


def ranked(key: str, pool: list[int], primer: SetPrimer | None = None):
    pack_number, pick_number = (int(part) for part in key[1:].split("p"))
    return rank_pack(
        PACKS[key],
        pool,
        primer or fra_primer(),
        pack_number=pack_number,
        pick_number=pick_number,
        names=NAMES,
        mana_costs=COSTS,
    )


def position(picks, name: str) -> int:
    return [pick.name for pick in picks].index(name)


# ---------------------------------------------------------------------------
# The live picks that went wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("play_data", [True, False])
def test_p1p7_rated_two_drop_beats_unrated_rarely_played_payoff(play_data):
    # Live: Winter, Team Player (unrated; 27% played, 49.9% game WR) on a rarity
    # prior plus the primer's "Boros payoff" role. Without play data (an older
    # primer cache) its games in hand versus typical uncommons give it away.
    picks = ranked("P1p7", ACTUAL_PICKS[:6], fra_primer(play_data))
    assert picks[0].name == "Afterthought Sentry"
    winter = picks[position(picks, "Winter, Team Player")]
    assert any("rarely played" in reason for reason in winter.reasons)
    assert not any(reason.startswith("payoff for") for reason in winter.reasons)
    assert position(picks, "Winter, Team Player") > position(picks, "Blossom-Blessed Angel")


def test_p2p7_open_black_bomb_uncommon_beats_unrated_red_filler():
    # Live: Arni, Renowned Champion (unrated; 20% played) over Rewrite Regrets
    # (60.4% GIH) seventh pick, with red only a sliver of the pool.
    picks = ranked("P2p7", ACTUAL_PICKS[:20])
    assert picks[0].name == "Rewrite Regrets"
    assert any("second color still open" in reason for reason in picks[0].reasons)
    assert position(picks, "Arni, Renowned Champion") > position(picks, "Cryotheory Adept")


def test_p3p9_in_lane_playable_beats_unrated_five_drop():
    picks = ranked("P3p9", ACTUAL_PICKS[:36])
    assert picks[0].name == "Semester Foreseer"
    assert position(picks, "Winter, Team Player") > position(picks, "Dedicated Commons")


def test_p3p3_third_five_drop_loses_and_open_lane_takes_cheap_removal():
    # Live: a third Undulating Witness over Break Under Pressure / Extended Absence.
    picks = ranked("P3p3", ACTUAL_PICKS[:30])
    assert picks[0].name != "Undulating Witness"
    assert position(picks, "Break Under Pressure") < position(picks, "Undulating Witness")
    # Having drafted toward the open blue-black lane, the removal is the pick.
    assert pool_lane(UB_LEANING_POOL_P3P3, fra_primer(), COSTS).colors == "UB"
    picks = ranked("P3p3", UB_LEANING_POOL_P3P3)
    assert picks[0].name == "Break Under Pressure"
    assert any("pool needs removal" in reason for reason in picks[0].reasons)


def test_actual_pool_lane_reads_mono_blue_with_an_open_second_color():
    lane = pool_lane(ACTUAL_PICKS[:28], fra_primer(), COSTS)
    assert lane.main == "U"
    assert lane.second_hold < 0.2  # red was never a real second color


# ---------------------------------------------------------------------------
# Unrated cards
# ---------------------------------------------------------------------------


def rated_set(**extra: SetCard) -> SetPrimer:
    cards = {
        i: SetCard(
            i,
            f"Rated {i}",
            "U",
            "C",
            "Creature",
            cmc=3,
            gih_wr=0.55,
            baseline=0.0,
            games=2000,
            game_wr=0.54 + (i % 5) * 0.004,
            play_rate=0.7,
        )
        for i in range(1, 13)
    }
    cards.update({card.grp_id: card for card in extra.values()})
    return SetPrimer(set_code="TST", cards=cards)


def base_reason(primer: SetPrimer, grp_id: int):
    pick = rank_pack([grp_id], [], primer)[0]
    return pick.score, pick.reasons[0]


def test_unrated_cards_default_below_average_but_scarce_strong_rares_do_not():
    primer = rated_set(
        a=SetCard(50, "Unknown Uncommon", "U", "U", "Creature", games=900),
        b=SetCard(51, "Chaff Uncommon", "U", "U", "Creature", games=900, play_rate=0.1),
        c=SetCard(52, "Scarce Mythic", "U", "M", "Creature", games=400),
        d=SetCard(53, "Played Mythic", "U", "M", "Creature", games=400, play_rate=0.9, game_wr=0.6),
        e=SetCard(54, "Unplayed Rare", "U", "R", "Creature", games=50),
        **{
            f"rare{i}": SetCard(
                60 + i, f"Rated Rare {i}", "U", "R", "Creature", gih_wr=0.57, baseline=0.5, games=900
            )
            for i in range(3)
        },
    )
    assert base_reason(primer, 50) == (-1.0, "unrated; assumed below average")
    score, reason = base_reason(primer, 51)
    assert score == -1.4 and "rarely played (10% of drafted copies played)" in reason
    assert base_reason(primer, 52) == (0.2, "unrated rare; rarity prior")
    score, reason = base_reason(primer, 53)
    assert score >= 1.0 and "game win rate 60.0%" in reason
    score, reason = base_reason(primer, 54)  # 50 games vs a typical 2000: nobody plays it
    assert score == -1.4 and "rarely played" in reason


def test_primer_roles_cannot_lift_an_unrated_card_over_a_rated_playable():
    primer = rated_set(a=SetCard(50, "Signpost Chaff", "U", "U", "Creature", games=300, play_rate=0.2))
    primer.cards[1].baseline = -0.3
    primer.archetypes = [{"colors": "UR", "name": "Izzet", "tier": 1, "payoffs": ["Signpost Chaff"]}]
    picks = rank_pack([50, 1], [], primer)
    assert picks[0].grp_id == 1
    assert not any("payoff" in reason for reason in picks[1].reasons)


# ---------------------------------------------------------------------------
# Removal need
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Target creature gets -3/-3 until end of turn.",
        "Target opponent sacrifices a creature or planeswalker with the greatest mana value.",
        "Exile target creature or planeswalker. You gain 1 life.",
        "Choose one —\n• This spell deals 3 damage to any target.\n• Create a 2/2 token.",
        "This spell deals 5 damage to target black or green creature or planeswalker.",
        "When this enchantment enters, exile target nonland permanent an opponent controls.",
        "Target creature has base power and toughness 0/0 until end of turn.",
        "When this creature enters, it fights up to one target creature an opponent controls.",
    ],
)
def test_removal_is_read_from_rules_text(text):
    assert is_removal(SimpleNamespace(oracle=text))


@pytest.mark.parametrize(
    "text",
    [
        "Return target creature to its owner's hand.",
        "Choose one —\n• Counter target creature spell unless its controller pays {3}.\n"
        "• Target creature gets -5/-0 until end of turn.",
        "{3}{R}: Exile target creature or planeswalker you control. Reveal cards until a creature.",
        "Whenever this creature attacks, exile up to one target card from a graveyard.",
        "Choose target nonland permanent. Its owner may put it on top of their library.",
    ],
)
def test_bounce_tucks_and_self_targets_are_not_removal(text):
    assert not is_removal(SimpleNamespace(oracle=text))


def removal_primer() -> SetPrimer:
    cards = {
        i: SetCard(i, f"Bear {i}", "B", "C", "Creature", cmc=2, gih_wr=0.55, baseline=0.0, games=2000)
        for i in range(1, 9)
    }
    cards[20] = SetCard(20, "Body", "B", "C", "Creature", cmc=4, gih_wr=0.56, baseline=0.3, games=2000)
    cards[21] = SetCard(
        21,
        "Cheap Kill",
        "B",
        "C",
        "Instant",
        cmc=2,
        oracle="Destroy target creature.",
        gih_wr=0.55,
        baseline=0.0,
        games=2000,
    )
    return SetPrimer(set_code="TST", cards=cards)


def test_pool_short_on_removal_takes_cheap_removal_until_it_has_enough():
    primer = removal_primer()
    picks = rank_pack([20, 21], [1, 2, 3, 4, 5, 6, 7, 8], primer)
    assert picks[0].grp_id == 21
    assert "pool needs removal (0 so far)" in picks[0].reasons
    primer.cards.update(
        {
            30 + i: SetCard(
                30 + i,
                f"Kill {i}",
                "B",
                "C",
                "Instant",
                cmc=2,
                oracle="Destroy target creature.",
                gih_wr=0.55,
                baseline=0.0,
                games=2000,
            )
            for i in range(2)
        }
    )
    picks = rank_pack([20, 21], [1, 2, 3, 4, 5, 6, 7, 8, 30, 31], primer)
    assert picks[0].grp_id == 20
    assert not any("removal" in reason for pick in picks for reason in pick.reasons)


# ---------------------------------------------------------------------------
# Lane: what the mana actually requires, and a second color that stays open
# ---------------------------------------------------------------------------


def lane_primer() -> SetPrimer:
    cards = {
        i: SetCard(i, f"Blue {i}", "U", "C", "Creature", cmc=3, gih_wr=0.56, baseline=0.3, games=2000)
        for i in range(1, 11)
    }
    cards[20] = SetCard(
        20, "Hybrid Spell", "UR", "C", "Instant", cmc=2, gih_wr=0.56, baseline=0.3, games=2000
    )
    cards[21] = SetCard(21, "Red Filler", "R", "C", "Creature", cmc=3, gih_wr=0.53, baseline=-0.6, games=2000)
    cards[22] = SetCard(22, "Black Star", "B", "U", "Instant", cmc=3, gih_wr=0.60, baseline=1.2, games=2000)
    cards[23] = SetCard(
        23, "Black Common", "B", "C", "Creature", cmc=3, gih_wr=0.56, baseline=0.3, games=2000
    )
    cards[30] = SetCard(
        30, "Artifact", "", "C", "Artifact Creature", cmc=3, gih_wr=0.56, baseline=0.3, games=2000
    )
    return SetPrimer(
        set_code="TST",
        cards=cards,
        pair_stats={"UR": {"win_rate": 0.53}, "UB": {"win_rate": 0.57}, "BG": {"win_rate": 0.55}},
    )


def test_lane_weights_follow_required_mana_not_printed_colors():
    primer = lane_primer()
    pool = [1, 2, 3, 20, 20, 30]
    # {1}{U/R} is castable with blue alone, so it does not vote for red.
    lane = pool_lane(pool, primer, {20: "{1}{U/R}"})
    assert lane.weights["R"] == 0 and lane.colors == "U"
    # Without an Arena cost both printed colors count, as before.
    assert pool_lane(pool, primer).weights["R"] > 0
    # A generic artifact adds no color.
    assert pool_lane([30, 30], primer).weights == dict.fromkeys("WUBRG", 0.0)


def test_weak_second_color_stays_open_in_pack_two_but_not_late_pack_three():
    primer = lane_primer()
    pool = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1, 2, 3, 21]  # blue with one red filler
    lane = pool_lane(pool, primer)
    assert lane.colors == "UR" and lane.second_hold < 0.2
    assert rank_pack([21, 22], pool, primer, pack_number=2, pick_number=5)[0].name == "Black Star"
    late = rank_pack([21, 22], pool, primer, pack_number=3, pick_number=10)
    assert late[0].name == "Red Filler" and any("off lane" in r for r in late[1].reasons)


def test_lane_follows_an_open_pair_once_its_cards_are_taken():
    primer = lane_primer()
    pool = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 21, 22, 23]
    assert pool_lane(pool, primer).colors == "UB"
    # Equal second-color weight: the set's stronger pair breaks the tie.
    primer.cards[24] = SetCard(
        24, "Red Twin", "R", "C", "Creature", cmc=3, gih_wr=0.56, baseline=0.3, games=2000
    )
    assert pool_lane([1, 2, 3, 4, 23, 24], primer).colors == "UB"


# ---------------------------------------------------------------------------
# Rules text: current Oracle wording for creature-entry payoffs
# ---------------------------------------------------------------------------


def rules_card(grp_id: int, text: str, type_line: str = "Creature") -> dict:
    return {"grp_id": grp_id, "name": f"Card {grp_id}", "type_line": type_line, "oracle_text": text}


@pytest.mark.parametrize(
    "text",
    [
        "Trample\nWhenever another creature you control enters, this creature gets +X/+0 until end of turn.",
        "Whenever a creature you control enters, you gain 1 life.",
        "Whenever one or more other creatures you control enter, draw a card.",
        "Whenever another creature enters the battlefield under your control, scry 1.",
    ],
)
def test_creature_entry_payoff_wordings(text):
    payoff = rules_card(1, text)
    assert "creature_entry" in rules_profile(payoff)["payoffs"]
    assert synergy_evidence(rules_card(2, "Flying"), payoff)[0]["mechanic"] == "creature_entry"


@pytest.mark.parametrize(
    "text",
    [
        "Whenever another red creature you control enters, draw a card.",
        "Whenever a creature an opponent controls enters, you gain 1 life.",
        "Whenever another creature enters, you gain 1 life.",
    ],
)
def test_restricted_or_symmetric_entry_triggers_are_not_generic_payoffs(text):
    assert "creature_entry" not in rules_profile(rules_card(1, text))["payoffs"]


# ---------------------------------------------------------------------------
# Draft advisor: synergy claims are explanations, not vetoes
# ---------------------------------------------------------------------------


def advisor_pack() -> dict:
    return {
        "event_name": "TradDraft_FRA",
        "pack_number": 3,
        "pick_number": 1,
        "picks_per_pack": 1,
        "cards": [
            rules_card(10, "When this creature enters, empower Jace 4."),
            rules_card(11, "Flying"),
        ],
        "picked_cards": [rules_card(20, "When this creature enters, empower Jace 2.")],
    }


def answer(**pick) -> str:
    return json.dumps(
        {
            "picks": [{"grp_id": 10, "reason": "Empowers Jace again.", "synergy_with": [20], **pick}],
            "plan": "Blue Jace empowerment.",
            "needs": [],
        }
    )


@pytest.mark.parametrize("claimed", [[20], [999], ["Card 20"], 20])
def test_unsupported_synergy_is_stripped_and_logged_but_the_pick_stands(claimed, caplog):
    backend = Mock()
    backend.complete.return_value = answer(synergy_with=claimed)
    with caplog.at_level(logging.WARNING, logger="arenamcp.draft_advisor"):
        result = DraftAdvisor(backend).recommend(advisor_pack(), {"spoken_advice": "fallback"})
    assert result["reasoning_source"] == "card_rules"
    pick = result["recommendations"][0]
    assert pick["grp_id"] == 10 and pick["synergy_with"] == [] and pick["synergy_evidence"] == []
    assert pick["reason"].endswith("(synergy unverified)")
    assert any("Draft synergy claim stripped: Card 10" in message for message in caplog.messages)


def test_supported_synergy_is_kept_with_its_evidence():
    pack = advisor_pack()
    pack["picked_cards"] = [rules_card(20, "Whenever another creature you control enters, scry 1.")]
    backend = Mock()
    backend.complete.return_value = answer(synergy_with=[20])
    pick = DraftAdvisor(backend).recommend(pack, {})["recommendations"][0]
    assert pick["synergy_with"] == [20] and pick["synergy_evidence"][0]["mechanic"] == "creature_entry"
    assert "unsupported_synergy" not in pick


def test_empty_synergy_graph_tells_the_model_and_requests_json_mode():
    backend = Mock()
    backend.complete.return_value = answer(synergy_with=[])
    DraftAdvisor(backend).recommend(advisor_pack(), {})
    message = json.loads(backend.complete.call_args.args[1])
    assert message["supported_synergies"] == []
    assert "synergy_with must be []" in message["synergy_rule"]
    assert backend.complete.call_args.kwargs["response_format"] == {"type": "json_object"}


def test_rejected_answers_are_logged_in_card_names(caplog):
    backend = Mock()
    backend.complete.return_value = answer(grp_id=999)
    with caplog.at_level(logging.WARNING, logger="arenamcp.draft_advisor"):
        result = DraftAdvisor(backend).recommend(advisor_pack(), {})
    assert result["reasoning_source"] == "heuristic"
    logged = next(message for message in caplog.messages if message.startswith("Rejected draft answer"))
    assert '"synergy_with": ["Card 20"]' in logged and "Empowers Jace again." in logged
