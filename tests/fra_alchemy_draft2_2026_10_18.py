"""The pro's second 2026-10-18 Alchemy FRA Premier Draft (course c3c94ec0).

Same event as fra_alchemy_draft_2026_10_18 (PremierDraft_YFRA_20261013, 17lands
Y26FRA), drafted by hand the same day; the deck was saved at 12:51 and the course
went 5-1 (some later games were played by our autopilot, so only the build is the
pro's). Pick order is lost (Player.log rotated); the pool and his 40 come from the
course's CourseDeck (main) and sideboard. He built Azorius planeswalkers: Chandra,
Ajani, Karn, Saheeli, Samut, Hapatra and Yuriko with the walker payoffs (Traxos,
Gideon's Memorial, Repurposed Enforcer, ...), on 7 Plains + 8 Island + 1 Forest +
Vigorbloom Annex.

Card rows (rules text, costs, ratings) are the first fixture's: every card in this
pool is in its CARD_TABLE.
"""

from __future__ import annotations

from tests import fra_alchemy_draft_2026_10_18 as first

EVENT_NAME = first.EVENT_NAME
BASICS = first.BASICS
COSTS = first.COSTS
NAMES = first.NAMES
ID = first.ID
primer = first.primer

# CourseDeck main (EventSetDeckV3 equivalent), basics included.
MAIN_DECK: dict[int, int] = {
    106358: 1,  # Blessed Ghoul
    106467: 1,  # Traxos, Academy Guardian
    106439: 1,  # Vigorbloom Annex
    106242: 1,  # Prophesied End
    106457: 1,  # Chandra, Chill of Compliance
    106384: 1,  # Proctor of Potential
    106440: 1,  # Ajani Resolute
    106234: 1,  # Germinate Recruits
    106527: 7,  # Plains
    106237: 1,  # Hexhaven Battalion
    106379: 1,  # Mind Meanderer
    106418: 1,  # Medic's Kitesail
    106229: 1,  # Campus Crier
    106465: 1,  # Samut, Tyrant of Naktamun
    106253: 1,  # Diviner of Victory
    106524: 1,  # Karn, Argent Defender
    106233: 1,  # Generous Revival
    106443: 1,  # Gideon's Memorial
    106529: 8,  # Island
    106272: 1,  # Undulating Witness
    106267: 1,  # Sphinx of False Conclusions
    106227: 1,  # Blossom-Blessed Angel
    106471: 1,  # Yuriko, Hope from the Shadows
    106460: 1,  # Hapatra, the Desert Frost
    106535: 1,  # Forest
    106244: 1,  # Repurposed Enforcer
    106448: 1,  # Saheeli, Consul of Oversight
}

# The course's card pool (main + sideboard, basics excluded), as Arena lists it.
POOL: list[int] = [
    106244, 106405, 106389, 106457, 106471, 106465, 106460, 106448, 106467, 106439, 106272,
    106241, 106262, 106303, 106241, 106367, 106253, 106234, 106242, 106379, 106384, 106233,
    106502, 106350, 106231, 106237, 106229, 106358, 106327, 106226, 106440, 106267, 106476,
    106443, 106227, 106517, 106429, 106524, 106231, 106525, 106262, 106418, 106418, 106344,
    106332,
]  # fmt: skip


def pool() -> list[int]:
    return list(POOL)
