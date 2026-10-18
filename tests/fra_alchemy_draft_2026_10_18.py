"""The 2026-10-18 Alchemy FRA Premier Draft (draft 99914101), drafted by hand by a pro.

Event PremierDraft_YFRA_20261013: FRA plus the Y26 Alchemy cards (17lands files it as
Y26FRA). The drafter went Selesnya around Emrakul, the Exigent Doom and Garruk, Curse
Breaker: a ramp engine (Heartwood Crafter's mana casts Emrakul from exile; Loot,
Garruk), a lifegain package of Alchemy cards (Dina, Valgavoth, Flourishing Forces) and
"prepared" re-use (Hexhaven Dueling Arena, Rescue Girl). The deck won its first match.

Packs and picks are decoded from Draft.Notify / EventPlayerDraftMakePick, the 40 from
EventSetDeckV3. Card rows are the Y26FRA 17lands snapshot of 2026-10-18 (pick order
for every card; win rates hidden below ~500 games) merged with the paper FRA snapshot
of 2026-10-12 for win rates (draftstats.merge_alchemy_rows), with Arena mana costs and
rules text. Pair stats are Y26FRA (splash decks counted in their pair). Archetypes and
synergy notes reuse the FRA primer inlined in fra_contender_draft_2026_10_13.
"""

from __future__ import annotations

from tests import fra_contender_draft_2026_10_13 as fra

from arenamcp.set_primer import SetCard, SetPrimer

EVENT_NAME = "PremierDraft_YFRA_20261013"

# Draft.Notify packs, in pick order.
PACKS: dict[str, list[int]] = {
    "P1p1": [
        106283,
        106303,
        106231,
        106411,
        106349,
        106394,
        106250,
        106391,
        106347,
        106225,
        106492,
        106515,
        106502,
        106427,
        107776,
    ],
    "P1p2": [
        106247,
        106298,
        106229,
        106376,
        106354,
        106272,
        106375,
        106426,
        106282,
        106432,
        106467,
        106504,
        106527,
        107781,
    ],
    "P1p3": [
        106252,
        106327,
        106294,
        106246,
        106418,
        106344,
        106269,
        106297,
        106403,
        106425,
        106507,
        106448,
        106420,
    ],
    "P1p4": [106263, 106397, 106235, 106419, 106416, 106350, 106261, 106510, 106449, 106481, 106526, 107786],
    "P1p5": [106352, 106416, 106237, 106397, 106344, 106311, 106336, 106385, 106458, 106437, 107768],
    "P1p6": [106407, 106246, 106327, 106268, 106433, 106358, 106396, 106501, 106447, 106422],
    "P1p7": [106397, 106399, 106246, 106349, 106343, 106413, 106519, 106518, 106535],
    "P1p8": [106331, 106235, 106276, 106315, 106231, 106352, 106518, 106428],
    "P1p9": [106231, 106411, 106349, 106394, 106515, 106502, 106427],
    "P1p10": [106298, 106229, 106376, 106354, 106375, 106527],
    "P1p11": [106327, 106246, 106418, 106344, 106420],
    "P1p12": [106235, 106419, 106350, 106526],
    "P1p13": [106352, 106416, 106437],
    "P1p14": [106246, 106268],
    "P1p15": [106535],
    "P2p1": [
        106330,
        106237,
        106299,
        106433,
        106226,
        106394,
        106317,
        106312,
        106291,
        106436,
        106492,
        106525,
        106467,
        106529,
        107787,
    ],
    "P2p2": [
        106354,
        106325,
        106280,
        106226,
        106315,
        106255,
        106375,
        106385,
        106301,
        106230,
        106520,
        106448,
        106521,
        106427,
    ],
    "P2p3": [
        106299,
        106229,
        106417,
        106330,
        106268,
        106305,
        106231,
        106400,
        106339,
        106442,
        106505,
        106468,
        106435,
    ],
    "P2p4": [106229, 106332, 106294, 106360, 106349, 106252, 106426, 106312, 106499, 106449, 106481, 106420],
    "P2p5": [106296, 106305, 106248, 106365, 106268, 106298, 106373, 106488, 106479, 106494, 106423],
    "P2p6": [106235, 106289, 106418, 106298, 106350, 106325, 106447, 106513, 106483, 106535],
    "P2p7": [106285, 106311, 106295, 106255, 106343, 106396, 106494, 106508, 106532],
    "P2p8": [106252, 106411, 106294, 106246, 106419, 106426, 106234, 106429],
    "P2p9": [106299, 106433, 106226, 106394, 106312, 106525, 106529],
    "P2p10": [106280, 106226, 106315, 106255, 106521, 106427],
    "P2p11": [106299, 106229, 106417, 106268, 106231],
    "P2p12": [106332, 106252, 106426, 106499],
    "P2p13": [106296, 106298, 106488],
    "P2p14": [106418, 106535],
    "P2p15": [106532],
    "P3p1": [
        106433,
        106360,
        106289,
        106394,
        106399,
        106280,
        106403,
        106314,
        106381,
        106318,
        106452,
        106484,
        106520,
        106422,
        107789,
    ],
    "P3p2": [
        106311,
        106231,
        106269,
        106335,
        106371,
        106320,
        106361,
        106339,
        106384,
        106515,
        106502,
        106499,
        106526,
        107772,
    ],
    "P3p3": [
        106416,
        106360,
        106411,
        106397,
        106330,
        106265,
        106287,
        106368,
        106509,
        106461,
        106515,
        106502,
        106428,
    ],
    "P3p4": [106317, 106419, 106315, 106349, 106235, 106369, 106233, 106336, 106345, 106512, 106488, 106528],
    "P3p5": [106227, 106397, 106303, 106376, 106240, 106299, 106394, 106395, 106448, 106531, 107769],
    "P3p6": [106259, 106299, 106305, 106352, 106344, 106241, 106339, 106243, 106473, 106527],
    "P3p7": [106394, 106295, 106306, 106354, 106246, 106339, 106451, 106510, 106534],
    "P3p8": [106246, 106355, 106344, 106360, 106318, 106510, 106519, 106439],
    "P3p9": [106433, 106360, 106394, 106399, 106280, 106381, 106484],
    "P3p10": [106311, 106231, 106515, 106502, 106499, 106526],
    "P3p11": [106416, 106360, 106411, 106509, 106515],
    "P3p12": [106419, 106349, 106235, 106528],
    "P3p13": [106299, 106394, 106531],
    "P3p14": [106352, 106527],
    "P3p15": [106534],
}

# EventPlayerDraftMakePick, per pack.
PICKS: dict[str, int] = {
    "P1p1": 106225,
    "P1p2": 106504,
    "P1p3": 106507,
    "P1p4": 106510,
    "P1p5": 107768,
    "P1p6": 106447,
    "P1p7": 106343,
    "P1p8": 106518,
    "P1p9": 106502,
    "P1p10": 106376,
    "P1p11": 106420,
    "P1p12": 106350,
    "P1p13": 106437,
    "P1p14": 106246,
    "P1p15": 106535,
    "P2p1": 107787,
    "P2p2": 106230,
    "P2p3": 106505,
    "P2p4": 106449,
    "P2p5": 106365,
    "P2p6": 106513,
    "P2p7": 106508,
    "P2p8": 106234,
    "P2p9": 106226,
    "P2p10": 106521,
    "P2p11": 106229,
    "P2p12": 106426,
    "P2p13": 106488,
    "P2p14": 106418,
    "P2p15": 106532,
    "P3p1": 107789,
    "P3p2": 106339,
    "P3p3": 106461,
    "P3p4": 106233,
    "P3p5": 107769,
    "P3p6": 106243,
    "P3p7": 106339,
    "P3p8": 106439,
    "P3p9": 106394,
    "P3p10": 106502,
    "P3p11": 106509,
    "P3p12": 106349,
    "P3p13": 106394,
    "P3p14": 106527,
    "P3p15": 106534,
}

# EventSetDeckV3: grp_id -> copies.
MAIN_DECK: dict[int, int] = {
    106225: 1,
    106504: 1,
    106507: 1,
    106510: 1,
    107768: 1,
    106447: 1,
    106502: 1,
    106376: 1,
    106350: 1,
    106535: 8,
    107787: 1,
    106230: 1,
    106505: 1,
    106449: 1,
    106365: 1,
    106508: 1,
    106234: 1,
    106426: 1,
    107789: 1,
    106339: 2,
    107769: 1,
    106243: 1,
    106439: 1,
    106509: 1,
    106349: 1,
    106527: 7,
}
SIDEBOARD: dict[int, int] = {
    106343: 1,
    106518: 1,
    106502: 1,
    106420: 1,
    106437: 1,
    106246: 1,
    106513: 1,
    106226: 1,
    106521: 1,
    106229: 1,
    106488: 1,
    106418: 1,
    106461: 1,
    106394: 2,
    106233: 1,
}

BASICS: dict[int, str] = {
    106526: "Plains",
    106527: "Plains",
    106528: "Island",
    106529: "Island",
    106531: "Swamp",
    106532: "Mountain",
    106534: "Forest",
    106535: "Forest",
}

# grp_id|name|colors|rarity|types|cmc|arena cost|GIH|baseline z|ATA|ALSA|games|play rate|game WR|Y26FRA games|rules
# (games and the win rates are paper FRA's where Y26FRA hides them; "Y26FRA games" is then set)
CARD_TABLE = r"""
106225|Emrakul, the Exigent Doom||M|Legendary Creature - Eldrazi|10|{10}|||||11||||When you cast this spell, untap all lands you control.\nFlying\nTrample\nWard—Sacrifice three permanents.\n{o3}, Exile this card from your hand: Target land gains "{oT}: Add {oCoC}" until this card is cast from exile. You may cast this card for as long as it remains exiled.
106226|Academic Ascent|W|C|Instant|2|{1}{W}||||8.6605|22|0.0972|||Target creature gets +2/+2 and gains flying until end of turn.\nEmpower Jace 2.
106227|Blossom-Blessed Angel|W|C|Creature - Angel Cleric|4|{3}{W}|0.5668|0.336||5.5861|2768|0.7273|0.5458|179|Flying\nVigilance\nThis creature enters prepared.
106229|Campus Crier|W|C|Creature - Human Advisor|2|{1}{W}|0.5565|0.050||6.0688|3258|0.6571|0.5537|274|{o1}, Exile this card from your graveyard: Empower Jace 2.
106230|Enlightened Confidant|W|M|Creature - Kor Cleric|2|{1}{W}|||||37||||Lifelink\nAt the beginning of your end step, if you gained life this turn, surveil 1. If you put a card with mana value less than or equal to the amount of life you gained this turn into your graveyard this way, put that card into your hand.
106231|Fateshaper Aspirant|W|C|Creature - Rhino Cleric|5|{4}{W}|0.5132|-1.147||7.9750|1025|0.1504|0.5216|47|When this creature enters, choose one — \n•<indent=4%>Return target legendary card from your graveyard to your hand. </indent>\n•<indent=4%>Put a +1/+1 counter on target creature. It gains vigilance and indestructible until end of turn.</indent>
106232|Flickering Hound|W|R|Creature - Dog|4|{3}{W}|||||25||||Whenever you cast a creature spell, exile up to one other target creature you control, then return that card to the battlefield under its owner's control.
106233|Generous Revival|W|U|Sorcery|3|{2}{W}|0.5740|0.535||4.5797|1216||0.5505|116|Return target creature card with mana value 3 or less from your graveyard to the battlefield with an additional +1/+1 counter on it.\nFlashback {o4oW}
106234|Germinate Recruits|W|R|Instant|3|{2}{W}|||||6||||Create X 2/2 colorless Wizard Soldier creature tokens named Cadet, where X is the amount of life you gained this turn.
106235|Graft Surgeon|W|C|Creature - Human Cleric|3|{2}{W}|0.5050|-1.373||8.2886|600|0.1207|0.5037|36|This creature enters with a +1/+1 counter on it.\nWhen this creature dies, put its counters on up to one target creature you control.
106236|Guiding Hydra|W|R|Creature - Hydra Horror|1|{X}{W}|0.5882|0.926|||692||0.5631|43|This creature enters with X +1/+1 counters on it.\nAt the beginning of combat on your turn, you may remove a +1/+1 counter from this creature. If you do, put a +1/+1 counter on each other creature you control.
106237|Hexhaven Battalion|W|C|Sorcery|6|{4}{W}{W}|0.5755|0.575||4.4986|4466|0.8045|0.5553|333|Create three 2/2 colorless Wizard Soldier creature tokens named Cadet. Empower Jace 2.\nBasic landcycling {o2}
106238|Kindred Judgment|W|R|Sorcery|7|{5}{W}{W}|0.6034|1.347|||880||0.5660|73|Choose a creature type. Destroy all creatures that aren't of the chosen type.
106239|Loyal Tutor|W|R|Instant|1|{W}|||||2||||Search your library for a planeswalker card, reveal it, then shuffle and put that card on top.
106240|Memory Trap|W|C|Enchantment|3|{2}{W}|0.5528|-0.052||3.4580|3656|0.8907|0.5394|431|When this enchantment enters, exile target nonland permanent an opponent controls until this enchantment leaves the battlefield.
106241|Predictive Preparations|W|C|Sorcery|2|{1}{W}||||8.5657|16|0.0787|||Put a +1/+1 counter on each of one or two target creatures.\nFlashback {o3oW}
106242|Prophesied End|W|U|Instant|2|{1}{W}|0.5306|-0.666||3.8835|1455||0.5392|151|Destroy target creature. If it wasn't attacking, its controller draws a card.
106243|Refute Destiny|W|U|Sorcery|2|{1}{W}||||8.2564|8||||Exile target creature or planeswalker that's green or blue. Surveil 1.
106244|Repurposed Enforcer|W|R|Creature - Human Soldier|2|{1}{W}|0.5620|0.204|||669||0.5343|68|Whenever this creature attacks, empower Jace X, where X is the number of creatures you control.
106245|Return to the Light Realms|W|M|Sorcery|9|{7}{W}{W}|||||23||||Return all nonland permanent cards from your graveyard to the battlefield.
106246|Shatterwing Pegasus|W|C|Creature - Pegasus|3|{2}{W}|0.5100|-1.234||7.7761|1647|0.2941|0.5284|73|Flying\n{o4oW}: Creatures you control get +1/+1 until end of turn.
106247|Surgical Precision|W|C|Sorcery|2|{1}{W}|0.5663|0.322||4.4191|3777|0.8574|0.5471|256|Choose one — \n•<indent=4%><indent=4%>Destroy target creature with toughness 4 or greater. You gain 1 life. </indent></indent>\n•<indent=4%><indent=4%>You draw a card and gain 2 life.</indent></indent>
106248|Unflinching Hortimancer|W|C|Creature - Human Cleric|2|{1}{W}|0.5193|-0.977||5.5292|1862|0.7188|0.5262|147|Ward {o1}\nWhenever you gain life, put a +1/+1 counter on this creature.
106249|Your Fate Ends Here|W|U|Instant|3|{2}{W}|0.5728|0.501||3.2273|1868||0.5446|146|Destroy target creature or planeswalker with mana value 3 or greater. Surveil 1.
106250|Countersculpt|U|U|Instant|2|{U}{U}|0.6062|1.424||3.8848|3103|0.9267|0.5654|262|As an additional cost to cast this spell, behold a Jace or pay {o1}.\nCounter target spell. Empower Jace 1.
106251|Cruel Calculations|U|R|Sorcery|3|{2}{U}|||||6||||Draw X cards, where X is the number of cards that were put into target player's graveyard from their library this turn.
106252|Cryotheory Adept|U|C|Creature - Human Wizard|2|{1}{U}|0.5303|-0.675||7.8606|1454|0.2012|0.5350|68|Prowess\n{o3oU}, Exile this card from your graveyard: Tap target creature and put a stun counter on it. Activate only as a sorcery.
106253|Diviner of Victory|U|R|Creature - Dwarf Wizard|1|{U}|0.6038|1.358|||949||0.5767|59|This creature enters prepared.\nWhenever you scry or surveil, this creature gets +1/+1 until end of turn.
106255|Divining Duelist|U|C|Creature - Merfolk Wizard|3|{2}{U}|0.5261|-0.791||7.9250|1401|0.2763|0.5182|105|Flash\nWhen this creature enters, choose one — \n•<indent=4%>Tap target creature. </indent>\n•<indent=4%>Untap target creature. </indent>\n•<indent=4%>Draw a card, then discard a card.</indent>
106256|Icy Reception|U|C|Instant|2|{1}{U}|0.5781|0.649||5.9910|5336|0.7565|0.5620|462|Choose one — \n•<indent=4%><indent=4%>Counter target creature or legendary spell unless its controller pays {o3}. </indent></indent>\n•<indent=4%><indent=4%>Target creature gets -5/-0 until end of turn.</indent></indent>
106257|Infinite Coursework|U|C|Enchantment - Aura|3|{2}{U}|0.5737|0.525||4.7259|4466|0.8187|0.5519|363|Enchant creature\nWhen this Aura enters, tap enchanted creature. It becomes unprepared.\nEnchanted creature loses all abilities and doesn't untap during its controller's untap step.
106258|Jace's Machinations|U|R|Instant|3|{2}{U}|0.5895|0.963|||838||0.5376|63|Until end of turn, you may activate loyalty abilities of Jace planeswalkers you control on any player's turn any time you could cast an instant.\nEmpower Jace 8.
106259|Mindseeker Oculus|U|C|Creature - Homunculus|3|{2}{U}|0.6519|2.688|4.3150|3.5180|655|0.9607|0.6108||When this creature enters, empower Jace 4.
106260|Perfected Theory|U|U|Instant|1|{U}||||7.4472|15||||Choose one — \n•<indent=4%><indent=4%>Target creature has base power and toughness 1/1 until end of turn. </indent></indent>\n•<indent=4%><indent=4%>Target creature has base power and toughness 4/5 until end of turn.</indent></indent>
106261|Plan for All Outcomes|U|U|Enchantment|4|{3}{U}|0.6075|1.459|||2978|0.9399|0.5732|286|When this enchantment enters, the owner of up to one other target nonland permanent puts it on their choice of the top or bottom of their library.\nWhenever you cast your first noncreature spell each turn, empower Jace 1.
106262|Precise Redaction|U|U|Instant|2|{1}{U}||||9.4100|2||||Counter target white or black spell.
106263|Protege's Awakening|U|C|Sorcery|4|{3}{U}|0.5882|0.927||5.3877|5340|0.7598|0.5615|467|Empower Jace 6.\nDraw a card.
106264|Seasoned Cryomancer|U|M|Creature - Human Wizard|3|{1}{U}{U}|||||45||||When this creature enters, draw two cards, then discard two cards. When you discard one or more nonland cards this way, tap up to that many target creatures and put a stun counter on each of them.\n{o3oUoU}, Exile this card from your graveyard: Draw two cards.
106265|Semester Foreseer|U|C|Creature - Human Wizard|4|{3}{U}|0.5228|-0.881||6.6266|2502|0.4788|0.5185|168|This creature enters prepared.\nWhen this creature enters, surveil 1.
106267|Sphinx of False Conclusions|U|R|Creature - Sphinx Illusion|4|{2}{U}{U}|0.6353|2.229|||1105||0.5925|78|Flash\nFlying\nWhenever this creature attacks, draw a card, then discard a card.\nWhen this creature dies, if it isn't a token, create a token that's a copy of it.
106268|Sphinx's Approach|U|C|Instant|3|{1}{U}{U}|0.5334|-0.589||8.1302|2608|0.3172|0.5306|158|Draw two cards. Then you may exile this spell and four cards named Sphinx's Approach from your graveyard. If you do, search your library for a Sphinx creature card, put it onto the battlefield, then shuffle.\nA deck can have any number of cards named Sphinx's Approach.
106269|Surveillance Phantasm|U|C|Creature - Bird Illusion|2|{1}{U}|0.5700|0.424||5.5231|5442|0.8467|0.5593|406|Defender\nFlying\nVigilance\nAs long as you've scried or surveilled this turn, this creature can attack as though it didn't have defender.\n{o3oU}: Surveil 1.
106270|The Theorist, Jace Beleren|U|M|Legendary Planeswalker - Jace|4|{2}{U}{U}|0.6904|3.752|||520||0.5957|43|At the beginning of each opponent's draw step, you draw a card.\nCreate a 1/1 blue Illusion creature token.\nFor each opponent, return up to one target artifact or creature that player controls to its owner's hand.\nDraw three cards. Then put X +1/+1 counters on each creature you control, where X is the number of cards in your hand.
106271|Theorist's Proxy|U|R|Creature - Illusion|2|{1}{U}|0.5795|0.687|||811||0.5556|96|Flash\nWhen this creature enters, empower Jace 3.\n{oU}, Sacrifice this creature: The next spell you cast this turn can't be countered.
106272|Undulating Witness|U|C|Creature - Serpent|5|{4}{U}|0.5564|0.048||5.7284|5052|0.8284|0.5495|470|Flying\n{o2}: This creature gets +1/-1 until end of turn.\nBasic landcycling {o2}
106273|Unsummon|U|C|Instant|1|{U}|0.5750|0.563||5.6231|5010|0.7541|0.5559|393|Return target creature to its owner's hand.
106274|Variable Chaser|U|R|Creature - Human Wizard|3|{2}{U}|0.5622|0.208|||788||0.5376|65|Flying\nProwess\nThis creature enters prepared.
106276|Apex Witchstalker|B|C|Creature - Wolf|6|{4}{B}{B}|0.5588|0.115||5.5915|4334|0.7737|0.5519|387|Menace\nWhen this creature enters or dies, you gain 2 life.\nBasic landcycling {o2}
106277|Bloodline Recollector|B|M|Creature - Vampire Warlock|2|{1}{B}|||||10||||At the beginning of each end step, if three or more creatures died this turn, this creature becomes prepared.
106279|Break Under Pressure|B|U|Instant|3|{2}{B}|0.5739|0.532||3.3824|2307|0.9469|0.5466|239|Target opponent sacrifices a creature or planeswalker with the greatest mana value among creatures and planeswalkers they control. You gain 2 life.
106280|Cast Away Doubt|B|C|Sorcery|3|{2}{B}|0.5250|-0.819||7.8629|1078|0.2295|0.5262|60|Draw two cards. Cast Away Doubt deals 2 damage to each player.
106281|Dark Matter Manipulator|B|R|Creature - Human Warlock|1|{B}|0.6076|1.463|||632||0.5729|31|When this creature enters, mill three cards.\nThis creature gets +2/+0 for every seven cards in your graveyard.
106282|Darklight Phoenix|B|M|Creature - Phoenix|4|{3}{B}|||||18||||Flying\nHaste\nAt the beginning of combat on your turn, if two or more creatures died this turn, return this card from your graveyard to the battlefield.
106283|Extended Absence|B|C|Instant|4|{3}{B}|0.5977|1.191||3.3953|5392|0.9404|0.5682|491|Exile target creature or planeswalker. Extended Absence deals 1 damage to each opponent and you gain 1 life.
106284|Extrapolate the Impossible|B|R|Sorcery|2|{1}{B}|||||2||||You may reveal exactly two cards you own with different names from outside the game. An opponent chooses one of them. You put that card into your hand.
106285|Last Gasp|B|C|Instant|2|{1}{B}|0.5589|0.118||4.3644|4514|0.8653|0.5495|402|Target creature gets -3/-3 until end of turn.
106286|Lich's Relic|B|R|Artifact - Equipment|1|{B}|0.6171|1.725|||914||0.5782|111|When this Equipment enters, you may pay {o2}. When you do, for each opponent, destroy up to one target creature or planeswalker that player controls.\nEquipped creature gets +2/+1.\nEquip {o2}
106287|Multiply by Zero|B|U|Instant|2|{1}{B}|0.5989|1.222|||2291|0.9308|0.5612|230|Target creature has base power and toughness 0/0 until end of turn.
106288|Overwrite the Multiverse|B|M|Sorcery|6|{4}{B}{B}|||||40||||Exile all creatures. Empower Jace X, where X is the number of creatures exiled this way.
106289|Rampart Hunter|B|C|Creature - Horror|4|{3}{B}||||8.9761|5|0.0196|||Deathtouch\nWhen this creature enters, target creature gets +2/+2 and gains deathtouch until end of turn.
106290|Rank Rat|B|C|Creature - Zombie Rat|2|{1}{B}|0.5565|0.050||6.8140|2647|0.5596|0.5447|183|When this creature enters, each opponent discards a card.
106291|Rewrite Regrets|B|U|Sorcery|4|{3}{B}|0.6001|1.255||4.0840|2658|0.8713|0.5646|208|Return target creature or planeswalker card with mana value 6 or less from your graveyard to the battlefield.\nEmpower Jace 2.
106292|Rise of the Deathbringer|B|R|Instant|5|{4}{B}|0.5789|0.671|||741||0.5432|55|Choose one — \n•<indent=4%><indent=4%><indent=4%>Draw cards equal to the greatest power among creatures you control. You lose life equal to the number of cards drawn this way. </indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%>All creatures get -3/-3 until end of turn.</indent></indent></indent>
106293|Sanctum Lurker|B|R|Creature - Horror|3|{2}{B}|0.6341|2.196|||962||0.5761|70|When this creature enters, empower Jace 1.\nPlaneswalkers you control aren't put into their owners' graveyards for having 0 loyalty.\nPlaneswalkers you control have "[+2]: This planeswalker deals 1 damage to each opponent and you gain 1 life."
106294|Screeching Soulbreaker|B|C|Creature - Siren Bard|3|{2}{B}|0.5308|-0.660||7.1459|1396|0.3671|0.5273|93|Flying\nWhenever this creature attacks, it deals 1 damage to each opponent and you gain 1 life.
106295|Silence the Echo|B|C|Sorcery|2|{1}{B}|0.5221|-0.901||6.2870|2377|0.4698|0.5108|154|As an additional cost to cast this spell, sacrifice a creature or planeswalker or pay {o3}.\nDestroy target creature or planeswalker.
106296|Solve for Disappointment|B|C|Sorcery|2|{1}{B}|0.5209|-0.932||8.2833|764|0.1622|0.5117|48|Target opponent reveals their hand. You choose a nonland permanent card from it. That player discards that card.\nEmpower Jace 1.
106297|Terminal Criticism|B|U|Instant|2|{1}{B}||||8.2780|5||||Destroy target creature or planeswalker that's blue or red. You gain 1 life.
106298|Theoretical Necromancer|B|C|Creature - Vampire Warlock|3|{2}{B}|0.5638|0.252||7.1184|3535|0.5936|0.5626|284|{o3oB}, Exile this card from your graveyard: Return another target creature card from your graveyard to your hand.
106299|Void Extrapolator|B|C|Creature - Aetherborn Warlock|2|{1}{B}|0.5485|-0.171||8.3357|2454|0.3441|0.5487|144|This creature enters prepared.\n<i>Threshold</i> — This creature gets +1/+1 as long as there are seven or more cards in your graveyard.
106301|Vraska's Final Mercy|B|R|Sorcery|2|{B}{B}|0.5213|-0.922|||516||0.5258|51|Choose one — \n•<indent=4%><indent=4%><indent=4%>You lose 2 life. Destroy target creature or planeswalker. </indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%>You lose 2 life. Empower Jace 6.</indent></indent></indent>
106302|Ajani's Anguish|R|R|Enchantment|1|{X}{R}|0.5687|0.388|||633||0.5532|45|When this enchantment enters, it deals X damage to any target.\nCreatures you control have trample.
106303|Artifist Acumen|R|C|Sorcery|1|{R}||||9.1866|32|0.1188|||Creatures you control gain first strike until end of turn.\nDraw a card.
106304|Awaken the Inferno|R|C|Sorcery|5|{4}{R}|0.5753|0.570||5.2190|3845|0.8821|0.5539|355|Awaken the Inferno deals 6 damage to target creature or planeswalker an opponent controls. Put a +1/+1 counter on up to one target creature you control.\nBasic landcycling {o2}
106305|Blazing Crescendo|R|C|Instant|2|{1}{R}||||9.0088|22|0.0758|||Target creature gets +3/+1 until end of turn. Exile the top card of your library. Until the end of your next turn, you may play that card.
106306|Chandra's Emberling|R|C|Creature - Gremlin Elemental|3|{2}{R}|0.4865|-1.883||7.9585|929|0.2544|0.5175|69|Haste\nWhenever you cast a noncreature spell, put a +1/+1 counter on this creature.
106307|Command the Stage|R|U|Sorcery|3|{2}{R}||||6.1059|35||||Create a 2/2 colorless Wizard Soldier creature token named Cadet, then put a +1/+1 counter on each other Wizard token you control.\nAt the beginning of each upkeep, if an opponent was dealt noncombat damage last turn, return this card from your graveyard to your hand.
106308|Craterclaw Colossus|R|M|Artifact Creature - Beast Construct|7|{4}{R}{R}{R}|||||22||||Haste\nWhen this creature enters, creatures you control gain trample and get +X/+0 until end of turn, where X is the number of artifacts you control.
106309|Curse-Marred Demon|R|R|Creature - Demon|4|{2}{R}{R}|0.5664|0.325|||572||0.5411|42|Flying\nTrample\nWhen this creature enters, search your library for a card, put it into your hand, shuffle, then discard a card at random.
106310|Draconic Visitor|R|R|Creature - Dragon|5|{3}{R}{R}|0.5095|-1.248|||526||0.5068|35|Flying\nIf one or more artifact tokens would be created under your control, that many 5/5 red Dragon creature tokens with flying are created instead.
106311|Eardrum Rattler|R|C|Creature - Human Bard|2|{1}{R}||||9.4908|15|0.0723|||{o1}, {oT}: Another target creature you control with power 2 or less can't be blocked this turn.
106312|Essence Burn|R|U|Instant|2|{1}{R}||||8.4255|2||||Essence Burn deals 5 damage to target black or green creature or planeswalker. If that permanent would die this turn, exile it instead.
106313|Face Yourself|R|R|Sorcery|7|{5}{R}{R}|||||17||||For each creature target player controls, create a token that's a copy of that creature, except it has haste and "At the beginning of the end step, if you don't control a planeswalker, sacrifice this creature."
106314|Fulminous Forte|R|U|Instant|3|{2}{R}|0.5653|0.293||3.7248|1808||0.5527|137|Choose one — \n•<indent=4%>Fulminous Forte deals 1 damage to each creature and planeswalker your opponents control. </indent>\n•<indent=4%>Fulminous Forte deals 5 damage to target creature or planeswalker.</indent>
106315|Hallway Heckler|R|C|Creature - Elemental Sorcerer|3|{2}{R}|0.5232|-0.871||8.1596|1036|0.2892|0.5171|75|This creature enters prepared.\n{oT}, Discard a card: Draw a card.
106317|Heartstring Puller|R|C|Creature - Elf Sorcerer|4|{3}{R}|0.5643|0.266||7.5633|2364|0.4635|0.5453|135|Trample\nWhen this creature enters, create a 2/2 colorless Wizard Soldier creature token named Cadet.
106318|Identity Echo|R|R|Enchantment|3|{2}{R}|||||0||||{o3oR}: Exile target creature or planeswalker you control. Reveal cards from the top of your library until you reveal a creature or planeswalker card. Put that card onto the battlefield and the rest on the bottom of your library in a random order. Activate only as a sorcery.
106319|Master of Barbs|R|R|Creature - Lizard Bard|2|{1}{R}|||||13||||Menace\nWhenever one or more opponents are dealt noncombat damage, creatures you control get +1/+0 until end of turn.
106320|No Admittance|R|C|Sorcery|2|{1}{R}|0.5623|0.210||4.0161|4016|0.8594|0.5508|380|No Admittance deals 3 damage to any target.\nEmpower Jace 1.
106321|Pompous Battlemage|R|R|Creature - Goblin Sorcerer|1|{R}|||||33||||Prowess\nThis creature enters prepared.
106323|Pyre Rhymer|R|R|Creature - Elemental Sorcerer|3|{1}{R}{R}|||||26||||Prowess\nThis creature enters prepared.
106325|Skilled Battlecarver|R|C|Creature - Human Warrior|2|{1}{R}|0.5138|-1.129||8.0915|1989|0.3532|0.5333|111|During your turn, this creature has first strike.\n{o1oR}: This creature gets +1/+0 until end of turn.
106326|Stingcaster Mage|R|M|Creature - Human Wizard|2|{1}{R}|||||14||||Haste\nWhen this creature enters, target instant or sorcery card in your graveyard gains flashback until end of turn. The flashback cost is equal to its mana cost.
106327|Tether Technician|R|C|Creature - Minotaur Artificer|5|{4}{R}|0.5000|-1.511||8.8363|682|0.1099|0.5121|27|Reach\nWhen this creature enters, you may discard a card. When you do, this creature deals 2 damage to any target.
106328|Violent Echoes|R|U|Instant|4|{2}{R}{R}|0.5926|1.049||3.4130|2116||0.5689|201|Violent Echoes deals 6 damage to target creature or planeswalker. If excess damage was dealt to that permanent this way, empower Jace X, where X is that excess damage.
106329|Wrath of the Bloodmane|R|C|Instant|3|{2}{R}|0.5520|-0.074||4.6426|3799|0.8385|0.5446|252|This spell costs {o1} less to cast if you control a legendary creature.\nWrath of the Bloodmane deals 4 damage to target creature or planeswalker.
106330|Arcane Amphisbaena|G|C|Creature - Snake|2|{1}{G}|0.5570|0.063||4.9309|3661|0.7791|0.5451|272|Deathtouch\nWhen this creature enters, empower Jace 2.
106331|Bestial Incursion|G|C|Sorcery|4|{3}{G}|0.5437|-0.304||5.6619|3743|0.6678|0.5392|252|Create a 4/4 green Beast creature token with trample.\nFlashback {o5oG}
106332|Budding Insurgent|G|C|Creature - Dryad Scout|3|{2}{G}|0.4711|-2.310||7.7091|675|0.2768|0.4848|73|Vigilance\nSacrifice this creature: Destroy target artifact or enchantment. If that permanent was a legendary enchantment, draw a card. Activate only as a sorcery.
106333|Carnivorous Cultivator|G|R|Creature - Elf Warlock|2|{1}{G}|0.5693|0.403|||722||0.5465|70|Deathtouch\nThis creature enters prepared.\nWhenever this creature deals combat damage to a player, return target land card from your graveyard to your hand.
106335|Compel Brutality|G|C|Instant|2|{1}{G}|0.5327|-0.607||4.9862|3287|0.7805|0.5227|235|Choose one — \n•<indent=4%><indent=4%>Target creature you control deals damage equal to its power to target creature or planeswalker an opponent controls. </indent></indent>\n•<indent=4%><indent=4%>Target planeswalker you control deals damage equal to its loyalty to target creature or planeswalker an opponent controls.</indent></indent>
106336|Flourishing Grapple|G|U|Instant|1|{G}||||8.9775|3||||Target creature or planeswalker an opponent controls that's red or white loses all abilities until end of turn. Target creature you control deals damage equal to its power to that permanent.
106337|Gardenize|G|R|Enchantment|3|{1}{G}{G}|||||4||||Whenever a creature you control dies, put a charge counter on this enchantment.\nAt the beginning of your first main phase, add {oG} for each charge counter on this enchantment.
106338|Greenhouse Propagator|G|C|Creature - Cat Druid|3|{2}{G}|0.5527|-0.055||4.6961|2477|0.7598|0.5316|186|Whenever another creature you control enters, you gain 1 life.\n{oT}: Add {oG}.
106339|Heartwood Crafter|G|U|Creature - Elf Artificer|1|{G}|0.5818|0.751||3.8791|1662||0.5481|164|This creature enters prepared.\n{oT}: Add {oC}. This mana can't be spent to cast spells from your hand.
106341|Hexhaven Invigorator|G|M|Creature - Chimera Horror|4|{G}{G}{G}{G}|||||4||||Vigilance\nWhenever this creature is dealt damage, you may search your library for up to that many land cards, put them onto the battlefield tapped, then shuffle.
106342|Hungering Puppetbeast|G|R|Artifact Creature - Beast Construct|5|{3}{G}{G}|0.6150|1.668|||787||0.5673|61|When this creature enters, create a Heartwood token.\n{o1}, Sacrifice another artifact: Put a +1/+1 counter on this creature. It gains your choice of trample, hexproof, or haste until end of turn.
106343|Hunter's Axe|G|U|Artifact - Equipment|1|{G}||||6.9722|26||||Equipped creature gets +2/+0 and has "Whenever this creature attacks, it gains your choice of trample or deathtouch until end of turn."\nEquip {o2}
106344|Inspired Tethermage|G|C|Creature - Elf Warrior|3|{2}{G}|0.5003|-1.502||7.3489|1475|0.3347|0.5028|109|Whenever you put one or more loyalty counters on a planeswalker, put a +1/+1 counter on this creature.\n{o6}: Empower Jace 2.
106345|Omnipresence|G|M|Enchantment|8|{5}{G}{G}{G}|||||0||||You may cast spells with mana value less than or equal to the number of creatures you control from your hand without paying their mana costs.
106346|Puppet Crafting|G|R|Enchantment - Aura|2|{1}{G}|||||16||||Enchant artifact or non-Aura enchantment\nEnchanted permanent is a Construct creature with base power and toughness 5/5 in addition to its other types.\n{o4oG}: Return this card from your graveyard to your hand.
106347|Restore with Empathy|G|U|Instant|3|{2}{G}|0.5214|-0.920||5.4667|1053||0.5188|113|Return target permanent card from your graveyard to your hand. You gain 4 life.
106348|Simulacrum Shaper|G|R|Creature - Elf Druid|3|{1}{G}{G}|0.5718|0.475|||696||0.5327|71|When this creature enters, you may search your library for a basic land card, put that card onto the battlefield tapped, then shuffle.\nWhen this creature dies, draw a card.
106349|Something Worth Saving|G|C|Instant|2|{1}{G}|0.5555|0.022||7.5848|2632|0.5096|0.5429|273|Mill four cards. You may put a permanent card from among them into your hand. You gain 1 life.
106350|Sureshot Sower|G|C|Creature - Human Archer|2|{1}{G}|0.5156|-1.079||7.3531|2015|0.3794|0.5270|115|Reach\n{o3oG}, Discard this card: Destroy target creature with flying.
106351|Tarmogoyf|G|M|Creature - Lhurgoyf|2|{1}{G}|||||27||||Tarmogoyf's power is equal to the number of card types among cards in all graveyards and its toughness is equal to that number plus 1.
106352|Tethermage's Advantage|G|C|Instant|1|{G}||||9.1172|7|0.0370|||Target creature gets +2/+2 and gains reach until end of turn. Untap it.
106353|Verdant Kraken|G|R|Creature - Plant Kraken|7|{4}{G}{G}{G}|0.5870|0.893|||707||0.5407|59|At the beginning of each player's upkeep, you create a 3/3 green Forest Tentacle land creature token.
106354|Vinelasher Adept|G|C|Creature - Rhino Soldier|6|{4}{G}{G}|0.5309|-0.657||5.6559|3511|0.7652|0.5306|293|Reach\nWhen this creature enters, put three +1/+1 counters on target creature.\nBasic landcycling {o2}
106355|Wrecking Gecko|G|C|Artifact Creature - Lizard Construct|5|{4}{G}|0.5477|-0.192||7.6147|1802|0.2823|0.5353|88|Ward {o2}\n{o6oGoG}: This creature gets +4/+4 and gains trample until end of turn.
106356|Aerid Konstrari|RG|M|Legendary Creature - Elder Sphinx|4|{1}{R}{G}{G}|||||25||||Flying\nWhen Aerid Konstrari enters or dies, create a Heartwood token.\n{o6}: Create a Heartwood token. Then Aerid Konstrari gets +X/+0 until end of turn, where X is the number of artifacts you control.
106357|Avatar of Burgeoning Echoes|UG|M|Creature - Avatar|2|{G}{U}|||||25||||<i>Landfall</i> — Whenever a land you control enters, empower Jace 2.\nPlaneswalkers you control have "[-10]: Put a +1/+1 counter on target creature for each land you control."
106358|Blessed Ghoul|WB|C|Creature - Zombie Cleric|1|{W/B}|0.5430|-0.322||6.1848|1720|0.5009|0.5358|106|Lifelink\n{o2o(W/B)}: Return this card from your graveyard to your hand.
106359|Bloombrute|WG|U|Creature - Plant Elemental|4|{2}{G}{W}|0.5712|0.457||3.9675|1348||0.5465|139|Whenever you gain life, draw a card. This ability triggers only once each turn.\n{o4oGoW}: Target creature gains trample and lifelink until end of turn.
106360|Charge the Sanctum|WR|C|Instant|3|{2}{R/W}||||9.1380|14|0.0674|||Choose one — \n•<indent=4%><indent=4%>Creatures you control get +2/+0 until end of turn. </indent></indent>\n•<indent=4%><indent=4%>Target creature gets +2/+0 and gains first strike until end of turn. Put a +1/+1 counter on it.</indent></indent>
106361|Clash of Elements|UR|U|Instant|3|{1}{U}{R}|0.5612|0.179||5.8956|1668||0.5471|162|Choose target nonland permanent. Its owner may put it on top of their library. If they do, Clash of Elements deals 2 damage to them. If they didn't put the card on top of their library, they put it on the bottom.
106362|Craftwork Crusher|RG|U|Artifact Creature - Boar Construct|7|{3}{R}{R}{G}{G}|0.6152|1.674||3.6651|1801||0.5672|134|Trample\nWhen this creature enters, choose two — \n•<indent=4%>This creature deals 4 damage to target creature or planeswalker. </indent>\n•<indent=4%>Create a 2/2 colorless Wizard Soldier creature token named Cadet. </indent>\n•<indent=4%>Draw a card.</indent>
106363|Denzilore Fatehold|WU|M|Legendary Creature - Elder Sphinx|4|{1}{W}{U}{U}|||||17||||Flash\nFlying\nWhenever you scry or surveil, put a +1/+1 counter on each creature you control.
106364|Desperate Futurescribe|WU|U|Creature - Kor Scout|4|{2}{W}{U}|0.5701|0.428||4.8182|1775||0.5531|164|Flying\nAt the beginning of combat on your turn, another target creature you control gets +1/+1 until end of turn. If you've scried or surveilled this turn, put a +1/+1 counter on that creature instead.
106365|Emergency Phytomedic|WG|C|Creature - Dryad Cleric|1|{G/W}|0.5304|-0.671||5.8375|1695|0.6047|0.5287|179|This creature enters prepared.
106367|Entrust the Spark|UG|R|Sorcery|5|{3}{G}{U}|||||2||||You may sacrifice a planeswalker. If you do, search your library for a planeswalker card, put it onto the battlefield, then shuffle.
106368|Fatehold Charm|WU|U|Instant|2|{W}{U}|0.5709|0.447||5.3516|1743||0.5603|158|Choose one — \n•<indent=4%><indent=4%>Draw a card. Empower Jace 2. </indent></indent>\n•<indent=4%><indent=4%>Return target spell or creature to its owner's hand. </indent></indent>\n•<indent=4%><indent=4%>Creatures you control get +1/+2 until end of turn.</indent></indent>
106369|Fatehold Chronologist|WU|C|Creature - Bird Wizard|2|{1}{W/U}|0.5696|0.414||4.8490|4854|0.8162|0.5533|350|Flying\nThis creature enters prepared.
106371|Ferocity of the Hunt|BG|C|Enchantment - Aura|2|{1}{B/G}|0.5181|-1.010||6.6933|1324|0.3088|0.5029|76|Flash\nEnchant creature\nEnchanted creature gets +1/+0 and has deathtouch.\nWhen enchanted creature dies, return that card to the battlefield tapped under its owner's control.
106372|Frostbite Pyromental|UR|R|Creature - Elemental|3|{U}{R}{R}|||||5||||Trample\nHaste\nWhenever this creature deals combat damage to a player, draw two cards.\nAt the beginning of the end step, sacrifice this creature.
106373|Grim Repriser|BR|U|Creature - Zombie Bard|2|{B}{R}|0.5226|-0.887||7.1792|731||0.5458|51|Prowess\n{oBoR}: Return this card from your graveyard to the battlefield with a finality counter on it. Activate only if an opponent has been dealt noncombat damage this turn.
106374|Ingris Stingerquill|BR|M|Legendary Creature - Elder Sphinx|3|{B}{R}{R}|||||24||||Flying\nWhenever a creature you control attacks, that creature deals 1 damage to each opponent.\n{o4}: Create a 2/2 colorless Wizard Soldier creature token named Cadet. Then creatures you control gain haste until end of turn.
106375|Konstrari Charm|RG|U|Instant|2|{R}{G}|0.5332|-0.592||6.9761|707||0.5189|39|Choose one — \n•<indent=4%>Konstrari Charm deals 6 damage to target creature with flying. </indent>\n•<indent=4%>Put two +1/+1 counters on target creature. It gains trample until end of turn. </indent>\n•<indent=4%>Add {oCoCoC}.</indent>
106376|Konstrari Improviser|RG|C|Creature - Human Artificer|2|{1}{R/G}|0.5340|-0.571||4.6011|2910|0.7484|0.5292|248|This creature enters prepared.
106378|Kwia Vigorbloom|WG|M|Legendary Creature - Elder Sphinx|6|{3}{G}{W}{W}|||||28||||Flying\nVigilance\nLifelink\nWard {o2}\nWhenever you gain life, create a colorless artifact token named Lotus with "{oT}, Sacrifice this token: Add three mana of any one color." This ability triggers only once each turn.
106379|Mind Meanderer|UG|U|Creature - Bird Fish Illusion|6|{3}{G}{U}{U}|0.5716|0.469||5.5000|2038|0.6439|0.5494|171|Flying\nThis creature has vigilance as long as you control a Jace planeswalker.\nWhen this creature enters, it fights up to one target creature an opponent controls.
106380|Null Summoner|UB|R|Creature - Human Warlock|4|{2}{U}{B}|0.6377|2.295|||1035||0.5807|92|When this creature enters, if you cast it, target opponent reveals their hand. You choose a nonland card from it. Exile that card.\n<i>Threshold</i> — As long as there are seven or more cards in your graveyard, you may cast the exiled card, and mana of any type can be spent to cast that spell.
106381|Paradox Shaper|UB|U|Creature - Octopus Wizard|2|{1}{U/B}|0.5498|-0.134||5.4855|2088|0.7328|0.5414|212|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.\n{o2}: Put target card from your graveyard on the bottom of your library.
106383|Primal Witchstalker|BG|U|Creature - Wolf|3|{1}{B}{G}|0.6020|1.308||5.7444|1829|0.6623|0.5682|193|Menace\nWhen this creature enters, mill four cards. When you do, return target land card from your graveyard to the battlefield tapped.
106384|Proctor of Potential|WU|R|Creature - Human Cleric|2|{W}{U}|0.6187|1.769|||632||0.5846|53|Whenever this creature or another creature you control enters, surveil 1.\n{oWoU}: Return this card from your graveyard to the battlefield with a finality counter on it. Activate only if you've scried or surveilled this turn.
106385|Prudent Fateseer|WU|U|Creature - Dwarf Wizard|3|{1}{W/U}{W/U}|0.5490|-0.156||4.4156|1969||0.5479|173|This creature enters prepared.\nWhenever you scry or surveil, creatures you control get +1/+0 until end of turn. This ability triggers only once each turn.
106387|Recursive Recruitment|UB|U|Sorcery|4|{2}{U}{B}|0.5982|1.204||3.5080|2917|0.9009|0.5639|304|Create two 2/2 colorless Wizard Soldier creature tokens named Cadet. If this spell was cast from a graveyard, put a +1/+1 counter on each of them for every three cards in your graveyard.\nFlashback {o6oUoB}
106388|Solarium Sentry|WG|R|Creature - Cat Soldier|2|{G}{W}|||||39||||Whenever an opponent casts a spell with mana value 2 or less, you gain 2 life.
106389|Solitary Cell|WR|R|Artifact|2|{R}{W}|||||8||||When this artifact enters, exile target nonland permanent an opponent controls with mana value 3 or less until this artifact leaves the battlefield.\n{o1}, {oT}, Discard a legendary card: Draw a card.
106390|Stingerquill Charm|BR|U|Instant|2|{B}{R}|0.5546|-0.002||5.8494|1035||0.5523|114|Choose one — \n•<indent=4%>Stingerquill Charm deals 3 damage to any target. </indent>\n•<indent=4%>Target creature gains first strike and deathtouch until end of turn. </indent>\n•<indent=4%>Create a 2/2 colorless Wizard Soldier creature token named Cadet. It gains haste until end of turn.</indent>
106391|Stingerquill Voxmancer|BR|U|Creature - Goblin Sorcerer|1|{B/R}|0.5250|-0.820||4.9365|920||0.5321|71|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.
106393|Stinging Vitriol|BR|R|Sorcery|2|{B}{R}|||||22||||Stinging Vitriol deals 2 damage to target opponent. That player reveals their hand. You choose a nonland card from it. They discard that card.
106394|Tam's Resistance|UG|C|Sorcery|2|{1}{G/U}|0.5570|0.065||6.1447|3131|0.5314|0.5440|265|Put a +1/+1 counter on up to one target creature. It gains vigilance until end of turn.\nEmpower Jace 4.
106395|Tenured Tethermage|RG|R|Creature - Human Artificer|3|{1}{R}{G}|0.5763|0.598|||616||0.5523|62|When this creature enters, you may sacrifice a land. If you do, create two tapped Heartwood tokens.\nTap two untapped artifacts you control: Put two +1/+1 counters on this creature.
106396|Theorix Charm|UB|U|Instant|2|{U}{B}|0.5852|0.845||5.6549|1854||0.5699|164|Choose one — \n•<indent=4%><indent=4%>Counter target noncreature spell unless its controller pays {o2}. </indent></indent>\n•<indent=4%><indent=4%>Target creature gets -2/-2 until end of turn. </indent></indent>\n•<indent=4%><indent=4%>Mill three cards, then draw a card.</indent></indent>
106397|Theorix Metamage|UB|C|Creature - Shade Wizard|3|{2}{U/B}|0.5488|-0.163||7.4595|2768|0.3322|0.5422|144|This creature enters prepared.\n<i>Threshold</i> — This creature gets +1/+0 and has flying as long as there are seven or more cards in your graveyard.
106399|Twinned Vision|UR|C|Instant|2|{1}{U/R}|0.5824|0.765||5.4871|5179|0.8123|0.5707|424|Draw a card. If this spell wasn't cast from your hand, draw two cards instead.\nFlashback—{o1o(U/R)o(U/R)}, Discard a card.
106400|Twisted Fates|WB|U|Sorcery|5|{2}{W}{W}{B}|0.5577|0.083||5.9104|1413||0.5421|113|Destroy target nonland permanent. Put a +1/+1 counter on each creature target player controls.
106401|Uldaros Theorix|UB|M|Legendary Creature - Elder Sphinx|6|{3}{U}{B}{B}|||||69||||Flying\nWhen Uldaros Theorix enters, if you cast him, exile up to one target nonland card of each card type from your graveyard. Copy those cards. You may cast any number of spells with total mana value 6 or less from among the copies without paying their mana costs. <i>(Permanent spells cast this way become tokens.)</i>
106402|Vigorbloom Charm|WG|U|Instant|2|{G}{W}|0.5726|0.497||4.9069|1060||0.5420|116|Choose one — \n•<indent=4%><indent=4%>Target permanent you control gains hexproof and indestructible until end of turn. </indent></indent>\n•<indent=4%><indent=4%>You draw a card and gain 3 life. </indent></indent>\n•<indent=4%><indent=4%>Put a +1/+1 counter on target creature you control. Then it fights target creature an opponent controls.</indent></indent>
106403|Vigorbloom Vanguard|WG|U|Creature - Troll Druid|2|{1}{G/W}|0.5497|-0.137||3.9549|1368||0.5334|122|This creature enters prepared.\nEach creature you control with a +1/+1 counter on it has vigilance.
106405|Vindictive Triumph|WB|R|Instant|3|{W}{B}{B}|||||27||||Exile target creature or planeswalker. If that permanent's mana value was 3 or less, return it to the battlefield tapped under your control. Exile it at the beginning of the next end step.
106406|Warrior's Blades|WR|U|Artifact - Equipment|4|{2}{R}{W}|0.5563|0.046||5.4987|861||0.5337|93|When this Equipment enters, it deals 3 damage to any target and you gain 3 life.\nEquipped creature gets +2/+1.\nEquip {o3}. This ability costs {o1} less to activate for each +1/+1 counter on the creature it targets.
106407|Whiplash Wordsmith|BR|C|Creature - Vampire Sorcerer|4|{3}{B/R}|0.5198|-0.964||8.1764|606|0.1986|0.5203|37|This creature enters prepared.\nAs long as an opponent was dealt noncombat damage this turn, this creature has flying and haste.
106409|Woodwork Prodigy|RG|U|Creature - Cat Druid|3|{2}{R/G}|0.5194|-0.975||3.6822|1134||0.5067|141|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.
106411|Afterthought Sentry||C|Artifact Creature - Gargoyle|2|{2}|0.5244|-0.838||8.7707|698|0.0749|0.5248|18|{o2}: This creature gains flying until end of turn.\nWhenever this creature attacks, exile up to one target card from a graveyard.
106412|Archive Arbiter||U|Artifact Creature - Sphinx|6|{6}|0.5597|0.138||6.0897|1324||0.5256|98|Flying\nWhen this creature enters, choose one — \n•<indent=4%>Destroy target noncreature, nonland permanent. </indent>\n•<indent=4%>You gain 4 life.</indent>
106413|Codie, Ravenous Codex||R|Legendary Artifact Creature - Book Construct|3|{3}|||||5||||Whenever you cast a prepare spell, copy it. You may choose new targets for the copy.\n{oWoUoBoRoG}, {oT}: Each creature you control becomes prepared. <i>(Only creatures with prepare spells can become prepared.)</i>
106414|The Echoverse Fulcrum||M|Legendary Artifact|2|{2}|||||43||||When The Echoverse Fulcrum enters, draw a card, then discard a card.\n{o5}, {oT}, Exile The Echoverse Fulcrum: Destroy all creatures. Activate only as a sorcery.
106415|Eye of Jace||U|Artifact|1|{1}||||6.6320|16||||At the beginning of your upkeep, surveil 1. Then if there are seven or more cards in your graveyard, sacrifice this artifact, it deals 2 damage to each opponent, and you gain 2 life.
106416|Keeper of the Quiet Hour||C|Artifact Creature - Chimera|3|{3}|0.5662|0.318||7.1978|3264|0.5221|0.5581|225|When this creature enters, empower Jace 2.
106417|Living Library||C|Artifact Creature - Book Illusion|2|{2}|0.4892|-1.811||9.2409|507|0.0752|0.5061|23|{o6}, Sacrifice this creature: Choose target creature or planeswalker an opponent controls. Its owner shuffles it into their library.
106418|Medic's Kitesail||C|Artifact - Equipment|2|{2}|0.5240|-0.848||7.3121|1105|0.3868|0.5135|118|Equipped creature gets +1/+0 and has flying and "Whenever this creature attacks, you gain 1 life."\nEquip {o2}
106419|Murmuring Volume||C|Artifact - Book|3|{3}|0.5607|0.168||5.5806|3663|0.7979|0.5503|349|{oT}: Add one mana of any color.\n{o2}, {oT}, Discard a card: Draw a card.
106420|Dedicated Commons||C|Land|||0.5248|-0.826||7.2415|1008|0.5392|0.5294|134|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oR} or {oW}.
106421|Deserted Beach||R|Land|||||||58||||This land enters tapped unless you control two or more other lands.\n{oT}: Add {oW} or {oU}.
106422|Fatehold Annex||C|Land|||0.5554|0.021||5.9299|1921|0.7426|0.5489|218|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oW} or {oU}.
106423|Formidable Commons||C|Land|||0.5443|-0.287||6.5551|2179|0.6932|0.5508|190|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oB} or {oG}.
106424|Hall of Echoes||R|Land|||||||31||||{oT}: Add {oC}.\n{o5}: This land becomes a copy of target creature you control until end of turn. The "legend rule" doesn't apply to permanents you control this turn.
106425|Haunted Ridge||R|Land|||||||65||||This land enters tapped unless you control two or more other lands.\n{oT}: Add {oB} or {oR}.
106426|Hexhaven Dueling Arena||U|Land||||||7.9433|34||||{oT}: Add {oC}.\n{o2}, {oT}: Target creature that attacked this turn becomes prepared. Activate only as a sorcery. <i>(Only creatures with prepare spells can become prepared.)</i>\n{o4}, {oT}: Target creature becomes prepared.
106427|Innovative Commons||C|Land|||0.5739|0.533||6.1962|1880|0.7523|0.5572|212|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oU} or {oR}.
106428|Konstrari Annex||C|Land|||0.5302|-0.677||6.2445|1524||0.5318|138|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oR} or {oG}.
106429|Meticulous Commons||C|Land|||0.5418|-0.357||7.2037|1401|0.5382|0.5383|132|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oW} or {oB}.
106430|Overgrown Farmland||R|Land|||||||53||||This land enters tapped unless you control two or more other lands.\n{oT}: Add {oG} or {oW}.
106431|Rockfall Vale||R|Land|||||||41||||This land enters tapped unless you control two or more other lands.\n{oT}: Add {oR} or {oG}.
106432|Roiling Canopy||R|Land|||||||14||||This land enters tapped.\nWhenever a Forest you control enters, if you control at least five other Forests, target creature you control gets +3/+3 until end of turn.\n{oT}: Add {oG}.
106433|Room of Refuge||C|Land|||0.6076|1.464||5.3263|525|0.9669|0.5911||This land enters tapped. As it enters, choose a color.\n{oT}: Add one mana of the chosen color.\n{o5}, {oT}, Sacrifice this land: Put two +1/+1 counters on target creature. Activate only as a sorcery.
106434|Shipwreck Marsh||R|Land|||0.5598|0.141|||736||0.5515|73|This land enters tapped unless you control two or more other lands.\n{oT}: Add {oU} or {oB}.
106435|Stingerquill Annex||C|Land|||0.5396|-0.417||6.4491|1516|0.7268|0.5568|166|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oB} or {oR}.
106436|Theorist's Sanctum||R|Land - Island|||0.5875|0.908|||834||0.5582|122|As this land enters, you may behold a Jace. If you don't, this land enters tapped.\n{o2oU}, {oT}: Empower Jace 2.\n{oT}: Add {oU}.
106437|Theorix Annex||C|Land|||0.5724|0.491||5.9167|2706|0.8155|0.5621|280|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oU} or {oB}.
106438|Transformative Commons||C|Land|||0.5475|-0.198||6.1700|2011|0.7788|0.5454|232|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oG} or {oU}.
106439|Vigorbloom Annex||C|Land|||0.5284|-0.726||6.1475|1302||0.5322|152|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oG} or {oW}.
106440|Ajani Resolute|W|M|Legendary Planeswalker - Ajani|2|{1}{W}|||||18||||Whenever you gain life, put a loyalty counter on Ajani.\nYou gain 1 life.\nCreate a 2/2 white Cat Soldier creature token named Ajani's Pridemate with "Whenever you gain life, put a +1/+1 counter on this token."\nYou get an emblem with "Creatures you control get +2/+2."
106441|Danitha, Sword of Hope|W|U|Legendary Creature - Human Knight|3|{2}{W}||||7.2861|10||||First strike\nWhenever you cast an Equipment spell or a spell that targets a creature you control, draw a card. This ability triggers only once each turn.
106442|Ghalta the Immovable|W|U|Legendary Creature - Elder Dinosaur|9|{8}{W}||||7.2384|12||||This spell costs {oX} less to cast, where X is the greatest toughness among creatures you control.\nCreatures you control can attack as though they didn't have defender.\nEach creature you control with toughness greater than its power assigns combat damage equal to its toughness rather than its power.
106443|Gideon's Memorial|W|R|Legendary Artifact|2|{1}{W}|||||28||||Creature tokens you control get +1/+0 and have vigilance.\n{oT}: Add one mana of any color. Spend this mana only to cast a planeswalker spell.\n{o1oW}, Discard this card: It deals 4 damage to target attacking or blocking creature.
106444|Koth of the Homestead|W|U|Legendary Creature - Human Citizen|3|{2}{W}|0.5276|-0.748||3.5848|906||0.5257|94|<i>Landfall</i> — Whenever a land you control enters, you gain 1 life.\nWhenever a Plains you control enters, put a +1/+1 counter on target creature.
106445|Liliana the Faultless|W|R|Legendary Creature - Human Cleric|1|{W}|0.5825|0.771|||527||0.5226|28|Whenever another creature or planeswalker you control enters, you gain 1 life.\n{o1}, {oT}, Discard a card: Another target creature or planeswalker you control gains hexproof until end of turn.
106446|Lyra, Archangel of Dawn|W|R|Legendary Creature - Angel Knight|3|{2}{W}|0.5604|0.158|||530||0.5413|38|Flying\nWhenever you gain life, put a +1/+1 counter on each Angel you control.
106447|Rescue Girl, First Responder|W|U|Legendary Creature - Human Cleric|3|{2}{W}|0.5073|-1.308||5.3263|1157||0.5153|98|Flying\n{oT}: Return another target permanent you control to its owner's hand. Activate only during your turn.
106448|Saheeli, Consul of Oversight|W|U|Legendary Creature - Human Advisor|5|{3}{W}{W}|0.5667|0.332||4.7279|1320||0.5561|106|Flying\nWhenever you scry or surveil, create a 1/1 colorless Thopter artifact creature token with flying. This ability triggers only once each turn.
106449|Teyo, Lightshield Expert|W|U|Legendary Creature - Human Cleric|2|{1}{W}|0.5727|0.500||5.0064|1306||0.5468|119|Flash\nWhen Teyo enters, target permanent you control gains hexproof until end of turn. Put a +1/+1 counter on it if it's a creature. Put a loyalty counter on it if it's a planeswalker.
106450|Thalia, the Survivor|W|U|Legendary Creature - Human Soldier|4|{3}{W}|0.5328|-0.604||4.4175|1006||0.5277|53|Lifelink\nNoncreature spells your opponents cast cost {o1} more to cast.
106451|Tomik, Orzhov Lawmage|W|U|Legendary Creature - Human Advisor|2|{1}{W}|0.5531|-0.044||5.5702|1083||0.5440|68|Flying\nPlaneswalkers you control have "No more than one creature can attack this planeswalker each combat."\n{oT}: Target creature with a +1/+1 counter on it gains flying until end of turn.
106452|Way of the Healer|W|U|Legendary Enchantment|4|{3}{W}|0.6005|1.268||3.3498|1890||0.5588|170|When Way of the Healer enters, empower Jace 5.\nPlaneswalkers you control have "[-2]: Create a 2/2 colorless Wizard Soldier creature token named Cadet. Surveil 1."
106453|Way of the Mentor|W|U|Legendary Enchantment|3|{2}{W}||||5.4212|27||||When Way of the Mentor enters, empower Jace 5.\nWhenever you gain life, put a loyalty counter on each planeswalker you control.
106454|Yoshimaru, Beloved Companion|W|U|Legendary Creature - Dog|3|{2}{W}|0.4706|-2.324||5.4942|612||0.4986|28|If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead.\n{o6}: Put a +1/+1 counter on target legendary creature.
106455|Yuriko, Blade of the Mighty|W|U|Legendary Creature - Human Samurai|4|{3}{W}|0.5568|0.058||5.5397|546||0.5082|53|During combat, players can't cast spells or activate abilities that aren't mana abilities.\nWhenever a creature you control attacks a player alone, it gains double strike until end of turn.
106456|Arni, Humble Scribe|U|U|Legendary Creature - Human Wizard|3|{2}{U}|0.5324|-0.616||5.9613|1544||0.5320|112|Whenever another nontoken creature you control enters, untap Arni.\n{oT}: Draw a card, then discard a card.
106457|Chandra, Chill of Compliance|U|M|Legendary Planeswalker - Chandra|3|{1}{U}{U}|||||42||||Surveil 1. If you put a noncreature, nonland card into your graveyard this way, put that card into your hand.\nAdd {oU}. Spend this mana only to cast a noncreature spell.\nTap target artifact or creature. Put X stun counters on it.\nYou get an emblem with "Whenever you cast a spell, draw a card."
106458|Fblthp, Impossibly Lost|U|U|Legendary Creature - Homunculus|2|{1}{U}|0.5965|1.157||4.1417|3237|0.9011|0.5536|311|When one or more of your opponents are dealt combat damage during your turn, draw two cards. If your library has no cards in it, you win the game. Fblthp's owner shuffles him into their library. <i>(If you draw from an empty library this way, you still win the game.)</i>
106459|Geist of Saint Thalia|U|U|Legendary Creature - Spirit Cleric|2|{1}{U}|0.5708|0.446||4.2741|2020||0.5654|142|Flying\nNoncreature spells you cast cost {o1} less to cast.
106460|Hapatra, the Desert Frost|U|U|Legendary Creature - Human Wizard|4|{3}{U}|0.5667|0.332||6.2124|840||0.5308|41|When Hapatra enters, for each opponent, tap up to one target creature that player controls. Put a stun counter on each of those creatures.\n{o2oU}: Untap target creature.
106461|Jace, Reality Sculptor|U|R|Legendary Planeswalker - Jace|5|{3}{U}{U}|0.5841|0.812|||803||0.5442|99|Empower Jace X, where X is the number of Islands you control.\nUntil your next turn, whenever a creature attacks you or a planeswalker you control, it gets -5/-0 until end of turn.\nExile all but the bottom card of each opponent's library. Activate only if there are twenty-five or more loyalty counters among Jaces you control.
106462|Lyra, Tolarian Archangel|U|R|Legendary Creature - Angel Wizard|3|{1}{U}{U}|0.6158|1.689|||989||0.5678|72|Flying\nAt the beginning of each end step, if you've drawn three or more cards this turn, create a 3/3 blue Angel creature token with flying.\n{o3oUoU}: Until end of turn, whenever Lyra deals combat damage to a player, draw two cards.
106463|Proft, Consulting Detective|U|U|Legendary Creature - Human Detective|2|{1}{U}|0.5699|0.421||4.0807|2439|0.8619|0.5638|207|Whenever you scry or surveil, you may pay {o2}. If you do, put a +1/+1 counter on Proft and draw a card. <i>(Draw after you scry or surveil.)</i>
106464|Ruric Thar, Biomagus|U|U|Legendary Creature - Ogre Crab Wizard|6|{4}{U}{U}|0.5545|-0.005||4.6644|1643||0.5333|112|Flying\nProwess\nWhenever Ruric Thar becomes the target of a spell or ability an opponent controls, draw a card.
106465|Samut, Tyrant of Naktamun|U|R|Legendary Creature - Human Wizard|2|{1}{U}|||||5||||Instant and sorcery spells you control have split second.
106466|Tetsuko Umezawa, Fugitive|U|U|Legendary Creature - Human Rogue|2|{1}{U}|0.5424|-0.338||6.6047|1296||0.5241|65|Creatures you control with power or toughness 1 or less can't be blocked.
106467|Traxos, Academy Guardian|U|U|Legendary Artifact Creature - Dragon Construct|4|{3}{U}|0.5468|-0.217||4.8997|1719||0.5453|103|This spell costs {o2} less to cast if you've cast a noncreature spell this turn.\nFlying\nVigilance\nProwess
106468|Way of the Cryomancer|U|U|Legendary Enchantment|3|{2}{U}|0.5222|-0.897||5.4826|923||0.5216|79|When Way of the Cryomancer enters, empower Jace 5.\nPlaneswalkers you control have "[-3]: When you next cast an instant or sorcery spell this turn, copy that spell. You may choose new targets for the copy."
106469|Way of the Mind Sculptor|U|U|Legendary Enchantment|5|{4}{U}|0.5836|0.799||3.2924|2656|0.9073|0.5554|226|When Way of the Mind Sculptor enters, empower Jace 5.\nWhenever you activate a loyalty ability, if you removed two or more loyalty counters to activate it, draw a card.
106470|Yargle, Goliath of Otaria|U|U|Legendary Creature - Frog Spirit|5|{4}{U}||||8.4344|0||||
106471|Yuriko, Hope from the Shadows|U|U|Legendary Creature - Human Ninja|1|{U}|0.5439|-0.297||7.1735|717||0.5332|27|Flash\nWhen Yuriko enters, choose one — \n•<indent=4%>Target creature gets -X/-0 until end of turn, where X is the number of cards in your graveyard. </indent>\n•<indent=4%>Surveil 2.</indent>
106472|Danitha, Spear of Agony|B|U|Legendary Creature - Human Knight|3|{2}{B}|0.4792|-2.086||6.1717|649||0.4923|49|First strike\nWhenever you cast a spell that targets an opponent or a creature an opponent controls, put a +1/+1 counter on Danitha.
106473|Gallia, Tragic Host|B|U|Legendary Creature - Zombie Satyr|2|{1}{B}|0.5493|-0.147||5.7383|1591||0.5535|80|Menace\n{o4oB}, Exile another creature card from your graveyard: Return this card from your graveyard to the battlefield tapped with a +1/+1 counter on her.
106474|Garruk, Veiled Butcher|B|M|Legendary Planeswalker - Garruk|5|{3}{B}{B}|||||31||||If a creature an opponent controls would die, exile it instead.\nUp to one target creature gets -4/-1 until your next turn.\nEach player sacrifices a creature of their choice. If you sacrificed a creature this way, create a 4/4 green Beast creature token with trample.\nEach opponent discards two cards. For each opponent who didn't discard two nonland cards this way, you draw a card.
106475|Gideon the Oathless|B|R|Legendary Creature - Human Mercenary|3|{2}{B}|0.5556|0.025|||756||0.5328|63|Ward—Discard a card.\nWhenever a creature an opponent controls enters, Gideon deals 1 damage to that player.\nWhenever an opponent activates a loyalty ability, Gideon deals 1 damage to that player.
106476|Liliana the Repentant|B|R|Legendary Creature - Human Warlock|2|{1}{B}|0.5572|0.069|||761||0.5665|66|Whenever another creature or planeswalker you control enters, mill two cards.\nExhaust — {o5oB}: Return target creature or planeswalker card from your graveyard to the battlefield. Put a +1/+1 counter on Liliana. Activate only as a sorcery.
106477|Loot, the Anomaly|B|U|Legendary Creature - Beast Horror|3|{2}{B}||||8.1090|5||||If Loot's power is negative, he assigns combat damage as though his power were positive.\n<i>Threshold</i> — Sacrifice another creature or planeswalker: Loot gets -2/-0 until end of turn. Activate only if there are seven or more cards in your graveyard.
106478|Mabel, Bitter Recluse|B|U|Legendary Creature - Mouse Warlock|1|{B}|0.5372|-0.483||6.2375|1022||0.5399|41|Deathtouch\nWhen Mabel enters, remove up to three counters from another target creature or planeswalker.
106479|Massacre Girl, Most Wanted|B|U|Legendary Creature - Human Assassin|5|{4}{B}|0.4782|-2.114||5.5277|665||0.5010|57|Whenever another creature or planeswalker you control dies, Massacre Girl deals 1 damage to target opponent and you gain 1 life.\nWhenever an opponent is dealt noncombat damage, put a +1/+1 counter on Massacre Girl.
106480|Proft, Sinister Mastermind|B|U|Legendary Creature - Human Rogue|3|{2}{B}|0.5491|-0.155||5.9456|1284||0.5319|82|<i>Threshold</i> — You can't cast this spell unless there are seven or more cards in your graveyard.\nMenace\n{oB}, Discard this card: Target creature gets -3/-1 until end of turn.
106481|Teyo, Diamondblade Mage|B|U|Legendary Creature - Human Warlock|4|{3}{B}||||6.8568|25||||Flash\nWhen Teyo enters, target permanent you control gains deathtouch until end of turn. Put a +1/+1 counter on it if it's a creature. Put a loyalty counter on it if it's a planeswalker.
106482|Tinybones, Pocket Nuisance|B|U|Legendary Creature - Skeleton Rogue|3|{2}{B}|0.5275|-0.751||5.7027|1109||0.5130|77|When Tinybones enters, each opponent discards a card.\nWhenever a player discards one or more cards, Tinybones deals 1 damage to each opponent.
106483|Way of the Deathbringer|B|U|Legendary Enchantment|3|{2}{B}||||6.2916|23||||When Way of the Deathbringer enters, empower Jace 5.\nPlaneswalkers you control have "[-2]: You may sacrifice a creature. If you do, create a 4/4 green Beast creature token with trample."
106484|Way of the Necromancer|B|U|Legendary Enchantment|2|{1}{B}||||7.8983|9||||When Way of the Necromancer enters, empower Jace 2.\nWhenever a creature you control dies, put a loyalty counter on each planeswalker you control.
106485|Winter, Tormented Loner|B|U|Legendary Creature - Human Warlock|3|{2}{B}|0.5173|-1.034||5.4075|984||0.5252|49|When Winter enters, you may sacrifice a creature or planeswalker. When you do, each opponent sacrifices a creature of their choice.\nWinter gets +1/+0 for each creature and planeswalker card in your graveyard.
106486|Yargle, Glutton of Urborg|B|U|Legendary Creature - Frog Spirit|5|{4}{B}||||9.0661|5||||
106487|Ajani Unrelenting|R|M|Legendary Planeswalker - Ajani|6|{4}{R}{R}|||||30||||Whenever you activate a loyalty ability, create a 2/2 colorless Wizard Soldier creature token named Cadet.\nCreatures you control get +1/+0 and gain haste until end of turn.\nDiscard your hand, then draw a card for each creature you control.\nAjani deals 4 damage to each creature except for tokens you control.
106488|Arni, Renowned Champion|R|U|Legendary Creature - Human Berserker|4|{3}{R}||||8.0197|6||||Trample\nWhenever another creature you control enters, Arni gets +X/+0 until end of turn, where X is that creature's power.
106489|Chandra, Torch of Defiance|R|M|Legendary Planeswalker - Chandra|4|{2}{R}{R}|||||17||||Exile the top card of your library. You may cast that card. If you don't, Chandra deals 2 damage to each opponent.\nAdd {oRoR}.\nChandra deals 4 damage to target creature.\nYou get an emblem with "Whenever you cast a spell, this emblem deals 5 damage to any target."
106490|Gallia, the Merrymaker|R|U|Legendary Creature - Satyr|2|{1}{R}|0.5081|-1.289||6.6392|559||0.5428|49|Haste\nEach other creature you control with a +1/+1 counter on it has haste.\n{o1oR}, {oT}: Put a +1/+1 counter on target creature that entered this turn.
106491|Jiang Yanggu, Alone|R|U|Legendary Creature - Human Berserker|5|{4}{R}|0.4923|-1.725||7.0793|583||0.5022|38|Menace\nWhenever a creature you control attacks a player alone, discard a card, then draw a card. Then put a +1/+1 counter on that creature for each card you've discarded this turn.
106492|Kiora of Fire and Ashes|R|U|Legendary Creature - Merfolk Noble|6|{4}{R}{R}|0.6128|1.608|||2304||0.5644|189|When Kiora enters, create a 5/5 red Dragon creature token with flying.\n{o8}: Create a 5/5 red Dragon creature token with flying.
106493|Koth, the Geomancer|R|U|Legendary Creature - Human Warrior|3|{2}{R}|0.5393|-0.425||5.8343|827||0.5250|67|Reach\n<i>Landfall</i> — Whenever a land you control enters, Koth deals 1 damage to each opponent. If that land is a Mountain, add {oR}.
106494|Marwyn, the Clearcutter|R|U|Legendary Creature - Elf Warrior|1|{R}|0.5502|-0.123||7.0365|936||0.5645|58|{o2}, {oT}, Sacrifice an artifact or land: Draw a card.
106495|Pia, Determined Rebuilder|R|U|Legendary Creature - Human Artificer|3|{2}{R}|0.5542|-0.013||4.7122|1467||0.5495|139|When Pia enters, create a 1/1 colorless Thopter artifact creature token with flying.\n{o5oR}: Target creature gets +X/+0 until end of turn, where X is the number of artifacts you control.
106496|Samut, Hazoret's Champion|R|R|Legendary Creature - Human Warrior Cleric|2|{1}{R}|0.5637|0.249|||534||0.5442|32|Creatures you control have haste.
106497|Tetsuko Umezawa, Pursuer|R|U|Legendary Creature - Human Mercenary|4|{3}{R}|0.5183|-1.004||6.2238|627||0.5335|42|Double strike\nProwess\nWhenever a creature an opponent controls with power or toughness 1 or less blocks, Tetsuko Umezawa deals 1 damage to that creature's controller.
106498|Tomik, Izzet Sparkmage|R|U|Legendary Creature - Human Wizard|2|{1}{R}|0.5061|-1.342||5.9714|818||0.5215|51|Prowess\nIf a source you control would deal noncombat damage to an opponent or a permanent an opponent controls, it deals that much damage plus 1 instead.
106499|Way of the Pyromancer|R|U|Legendary Enchantment|2|{1}{R}|0.4970|-1.594||4.9554|831||0.4982|82|When Way of the Pyromancer enters, empower Jace 2.\nPlaneswalkers you control have "[+1]: Add {oR}."
106500|Way of the Warlord|R|U|Legendary Enchantment|3|{2}{R}|0.5211|-0.929||4.9009|950||0.5166|65|When Way of the Warlord enters, empower Jace 5.\nPlaneswalkers you control have "[-4]: This planeswalker deals 2 damage to up to one target creature or planeswalker and 2 damage to target player."
106501|Winter, Team Player|R|U|Legendary Creature - Human Warrior|5|{4}{R}||||7.9360|23||||Convoke\nWhenever you cast a noncreature spell, creatures you control get +1/+0 until end of turn.
106502|Edgar, Moonlit Sovereign|G|U|Legendary Creature - Werewolf Noble|5|{3}{G}{G}|0.4642|-2.502||5.7012|558||0.4772|55|Flash\nAt the beginning of your end step, if you didn't cast a spell this turn, put two +1/+1 counters on Edgar.\n{o4oG}: Put a +1/+1 counter on each creature you control with a +1/+1 counter on it.
106503|Fblthp, Knows the Way|G|U|Legendary Creature - Homunculus Scout|2|{X}{G}{G}|0.5861|0.869||4.2190|1829||0.5626|147|<i>Domain</i> — Fblthp's power is equal to the number of basic land types among lands you control.\nWhen Fblthp enters, search your library for up to X basic land cards with different names, reveal them, put them into your hand, then shuffle.
106504|Garruk, Curse Breaker|G|M|Legendary Planeswalker - Garruk|5|{3}{G}{G}|||||53||||Whenever a creature you control with power 4 or greater enters, draw a card.\nUntap up to two target lands.\nCreate a 4/4 green Beast creature token with trample.\nUntil your next turn, whenever one or more creatures attack one of your opponents, those creatures get +2/+2 and gain trample until end of turn.
106505|Ghalta the Unstoppable|G|U|Legendary Creature - Elder Dinosaur|9|{8}{G}|0.4984|-1.555||4.5074|935||0.4998|83|This spell costs {oX} less to cast, where X is the greatest power among creatures you control.\nTrample\nOther creatures you control have trample.
106506|Jiang Yanggu, Never Alone|G|U|Legendary Creature - Human Druid|4|{3}{G}|0.5456|-0.251||4.9027|1393||0.5337|101|When Jiang Yanggu enters, create Mowu, a legendary 3/3 green Dog creature token.\nAt the beginning of your end step, untap all tokens you control.
106507|Loot, the Nexus|G|U|Legendary Creature - Beast Noble|3|{2}{G}|0.5181|-1.012||4.6324|996||0.5316|123|{oT}: Choose a color. Add one mana of that color for each different power among creatures you control.
106508|Marwyn, the Preserver|G|U|Legendary Creature - Elf Druid|2|{1}{G}|0.5281|-0.733||5.7846|1066||0.5412|108|Lands you control have hexproof.\n{o2}: Return target land card from your graveyard to your hand.
106509|Pia, Aether Ascetic|G|U|Legendary Creature - Human Druid|3|{2}{G}||||7.3379|37||||When Pia enters, you may discard a card. If you do, search your library for an enchantment card, reveal it, put it into your hand, then shuffle.
106510|Ruric Thar, Magecrusher|G|U|Legendary Creature - Ogre Warrior|7|{5}{G}{G}|0.5336|-0.583||5.3971|1117||0.5156|120|This spell can't be countered.\nReach\nVigilance\nTrample\nRuric Thar has hexproof as long as they haven't dealt combat damage yet.
106511|Titanbones, Towering Heart|G|U|Legendary Creature - Skeleton Druid|4|{3}{G}|0.4919|-1.736||4.8652|799||0.5014|85|Reach\nWhenever you gain life, put two +1/+1 counters on Titanbones.\nWhen you discard this card, you gain 3 life.
106512|Way of the Paradox|G|U|Legendary Enchantment|3|{2}{G}|0.5567|0.055||4.2994|1218||0.5365|144|When Way of the Paradox enters, empower Jace 5.\nWhenever you activate a loyalty ability, you gain 1 life. You may play an additional land this turn.
106513|Way of the Wildspeaker|G|U|Legendary Enchantment|5|{4}{G}|0.5309|-0.657||4.1623|1407||0.5298|86|When Way of the Wildspeaker enters, empower Jace 7.\nPlaneswalkers you control have "[-4]: Create a 4/4 green Beast creature token with trample."
106514|Yoshimaru, Scrappy Stray|G|U|Legendary Creature - Dog|2|{1}{G}|0.5353|-0.536||4.7256|1233||0.5132|80|When Yoshimaru enters, another target creature you control fights up to one target creature an opponent controls.\n{o6}: Put a +1/+1 counter on target nonlegendary creature.
106515|Edgar, Ancient Bloodlord|WB|U|Legendary Creature - Vampire Noble|2|{W}{B}||||7.5758|24||||Whenever another creature or planeswalker you control dies, you gain 1 life.\n{o2}, Sacrifice another creature or planeswalker: Put a +1/+1 counter on Edgar. He gains menace until end of turn.
106516|Hapatra, the Desert Fang|BG|U|Legendary Creature - Human Cleric|5|{2}{B}{B}{G}|0.5890|0.950||4.8762|2241|0.8081|0.5601|245|When Hapatra enters, for each opponent, put X -1/-1 counters on up to one target creature that player controls, where X is the greatest mana value among cards in your graveyard.
106517|Karn, Gilded Guardian|WUBRG|R|Legendary Artifact Creature - Golem|5|{2/W}{2/U}{2/B}{2/R}{2/G}|||||8||||Vigilance\nTrample\nWhen Karn enters, draw a card for each color among other artifacts you control.
106518|Kiora of Salt and Sand|UG|U|Legendary Creature - Merfolk Noble|3|{1}{G}{U}|0.5434|-0.312||7.1846|611||0.5133|48|Whenever you attack, if you've activated a loyalty ability this turn, untap target attacking creature. It can't be blocked this turn.\nPlaneswalkers you control have "[-8]: Create an 8/8 blue Leviathan creature token with hexproof."
106519|Mabel, Valley Hero|WR|U|Legendary Creature - Mouse Soldier|3|{1}{R}{W}||||7.1018|20||||Whenever Mabel or another creature you control enters, put a +1/+1 counter on target creature that entered this turn.
106520|Saheeli, Jewel of Avishkar|UR|U|Legendary Creature - Human Artificer|4|{2}{U}{R}|0.5726|0.496||5.0072|1591||0.5424|182|Thopters you control have haste.\nWhenever you cast a noncreature spell, create a 1/1 colorless Thopter artifact creature token with flying.
106521|Tam, the Possibility|UG|R|Legendary Creature - Gorgon Wizard|3|{1}{G}{U}|||||1||||Planeswalker spells you cast cost {o1} less to cast.\n{oWoUoBoRoG}, {oT}: Proliferate X times, where X is the number of planeswalker types among planeswalkers you control.
106522|Vraska, Soul of Stone|WUR|R|Legendary Creature - Gorgon Wizard|3|{U}{R}{W}|||||33||||Artifact creatures you control have vigilance.\nWhenever you cast a noncreature spell, create a 1/1 colorless Sculpture Treasure artifact creature token with "{oT}, Sacrifice this token: Add one mana of any color."
106523|Vraska, the Cutting Glare|BG|R|Legendary Creature - Gorgon Assassin|3|{B}{B}{G}|0.6398|2.354|||758||0.5699|81|Deathtouch\nWhen Vraska enters, if you control six or more lands, destroy target permanent an opponent controls. They create a Treasure token.
106524|Karn, Argent Defender||R|Legendary Artifact Creature - Golem|2|{2}|||||5||||Artifacts and creatures entering the battlefield don't cause abilities to trigger.
106525|Traxos, Scourge Eternal||U|Legendary Artifact Creature - Dragon Construct|4|{4}||||7.4351|15||||Trample\nTraxos doesn't untap during your untap step.\nWhenever you cast an artifact or creature spell, untap Traxos.
106566|Eye of Ugin||M|Legendary Land|||||||0||||Colorless Eldrazi spells you cast cost {o2} less to cast.\n{o7}, {oT}: Search your library for a colorless creature card, reveal it, put it into your hand, then shuffle.
106567|Austere Command|W|R|Sorcery|6|{4}{W}{W}|||||7||||Choose two — \n•<indent=4%><indent=4%>Destroy all artifacts. </indent></indent>\n•<indent=4%><indent=4%>Destroy all enchantments. </indent></indent>\n•<indent=4%><indent=4%>Destroy all creatures with mana value 3 or less. </indent></indent>\n•<indent=4%><indent=4%>Destroy all creatures with mana value 4 or greater.</indent></indent>
106568|Flesh Duplicate|U|R|Creature - Shapeshifter Rebel|2|{U}{U}|||||1||||You may have this creature enter as a copy of any creature on the battlefield, except it has vanishing 3 if that creature doesn't have vanishing.
106569|Consign to Memory|U|U|Instant|1|{U}|||||0||||Replicate {o1}\nCounter target triggered ability or colorless spell.
106570|Sublime Epiphany|U|R|Instant|6|{4}{U}{U}|||||19||||Choose one or more — \n•<indent=4%><indent=4%><indent=4%><indent=4%>Counter target spell. </indent></indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%><indent=4%>Counter target activated or triggered ability. </indent></indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%><indent=4%>Return target nonland permanent to its owner's hand. </indent></indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%><indent=4%>Create a token that's a copy of target creature you control. </indent></indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%><indent=4%>Target player draws a card.</indent></indent></indent></indent>
106571|Consider|U|C|Instant|1|{U}|||||8||||Surveil 1.\nDraw a card.
106572|Necrodominance|B|M|Legendary Enchantment|3|{B}{B}{B}|||||0||||Skip your draw step.\nAt the beginning of your end step, you may pay any amount of life. If you do, draw that many cards.\nYour maximum hand size is five.\nIf a card or token would be put into your graveyard from anywhere, exile it instead.
106573|Mind Twist|B|R|Sorcery|1|{X}{B}|||||19||||Target player discards X cards at random.
106574|Splinter Twin|R|R|Enchantment - Aura|4|{2}{R}{R}|||||7||||Enchant creature\nEnchanted creature has "{oT}: Create a token that's a copy of this creature, except it has haste. Exile that token at the beginning of the next end step."
106575|Root Maze|G|M|Enchantment|1|{G}|||||0||||Artifacts and lands enter tapped.
107767|Oko, Lord of the Fae|W|M|Legendary Planeswalker - Oko|3|{1}{W}{W}|||||40||||Creatures can't attack planeswalkers you control unless their controller pays {o1} for each of those creatures.\nUp to one target artifact or creature loses all abilities until your next turn.\nCreate a 2/2 white Ox creature token.\nYou get an emblem with "At the beginning of your upkeep, conjure a random planeswalker card into exile. You may cast that card this turn without paying its mana cost."
107768|Prophet of the Planeswalker|W|U|Creature - Human Cleric|2|{1}{W}||||5.2872|77||||When this creature enters, you may behold a planeswalker. If you do, it perpetually gains "Loyalty abilities of this planeswalker cost an additional [+1] to activate."
107769|Valgavoth, the Lifeline|W|R|Legendary Creature - Insect Angel|3|{2}{W}|||||46||||Flying\nWhenever you gain life, creature cards in your hand and graveyard perpetually get +1/+1.\nAs long as you've gained life this turn, you may cast creature spells from your graveyard. If you cast a spell this way, that creature enters with a finality counter on it.
107770|Crux of the Theory|U|U|Enchantment|2|{1}{U}|||||264|0.9225|0.6328||When this enchantment enters, heist target opponent's library.\n<i>Actualize</i> — Whenever you cast a spell that isn't from your starting deck, empower Jace 2.
107771|Echogoyfs|U|R|Creature - Lhurgoyf|1|{U}|||||88||||Double team\n<i>Actualize</i> — Echogoyfs's power is equal to the number of spells you've cast this game that weren't from your starting deck and its toughness is equal to that number plus 1.
107772|Jace's Saga||R|Enchantment Land - Saga|||||||137||||This Saga gains "{oT}: Add {oU}."\nThis Saga gains "{oU}, {oT}: Conjure a card named Jace's Sentinel onto the battlefield."\nEmpower Jace 3.
107773|Paradox Kraken|U|R|Creature - Kraken|6|{4}{U}{U}|||||88||||When this card is put into your graveyard from anywhere, conjure a card named Kraken Hatchling into your hand.\n{o1oU}: Return this card from your graveyard to the battlefield. Activate only if there are more cards in your graveyard than your library and only as a sorcery.
107774|Remnants of Beleren|U|M|Artifact|2|{1}{U}|||||64||||If you would empower Jace, you may instead put that many loyalty counters on a Jace you control.\n{oT}: Empower Jace 1.\n{o1oU}, {oT}: Draft a card from the Beleren Spellbook.
107775|Avoided Stairwurm|B|R|Artifact Creature - Wurm|1|{B}||||5.9507|1||||Menace\nThis creature gets -1/-1 for each creature card in your library.
107776|Bend Mortal Limits|B|U|Sorcery|1|{B}||||5.2940|96||||Look at all creature and planeswalker cards in target player's hand. You may exile one of those cards. If you do, conjure a duplicate of that card into your graveyard.
107777|Boisterous Clique|B|R|Creature - Faerie Warlock|3|{1}{B}{B}|||||91||||Flash\nFlying\nWhen this creature enters, look at all nonland cards in target player's hand. You may exile one of those cards. If you do, that player conjures a card named Mind Stone into their hand.
107778|Searing Soloist|R|R|Creature - Orc Bard|4|{2}{R}{R}|||||47||||When this creature enters, conjure a card named Aria of Flame onto the battlefield.\nIf a red source you control would deal damage to a permanent or player, it deals that much damage plus 1 instead.
107779|Sparks Ablaze|R|U|Instant|1|{R}||||4.3003|147||||Sparks Ablaze deals 2 damage to target creature or planeswalker. If that creature or planeswalker would die this turn, exile it instead.\nThe next planeswalker spell you cast costs {o1} less to cast.
107780|Etali, Primal Garden|G|R|Legendary Creature - Elder Dinosaur|7|{6}{G}|||||118||||<i>Actualize</i> — This spell costs {o1} less to cast for each spell you've cast this game that wasn't from your starting deck.\nTrample\nWhenever Etali enters or attacks, conjure a random Dinosaur card into exile. You may cast that card this turn without paying its mana cost.
107781|Selfless Germinator|G|U|Creature - Treefolk Druid|1|{G}||||3.8171|200|0.8524|||Exhaust — {o1}: Conjure a card named Seed of Hope into your hand.
107782|Nylea's Oracle|G|R|Creature - Centaur Druid|2|{G}{G}|||||76||||When this creature enters, conjure X cards named Nylea's Oracle into your library, where X is your devotion to green, then shuffle. Then if there are more cards in your library than your starting deck, creatures you control get +X/+X and gain haste until end of turn.
107783|Banescale Serpent|UR|R|Creature - Serpent|5|{3}{U}{R}|||||57||||Vigilance\nHaste\nProtection from instants\nAs this creature enters, for each opponent, note the most prevalent creature type in their library.\nThis creature has protection from each of the noted creature types.
107784|The Bloodbile Swarm|BR|R|Legendary Creature - Elemental Insect|5|{3}{B}{R}|||||131||||Ward—Sacrifice a Blood artifact.\nOther creatures are Blood artifacts in addition to their other types and have "{o1}, {oT}, Discard a card, Sacrifice this creature: Draw a card."\nWhenever a player sacrifices a nontoken creature or discards a creature card, exile it. It perpetually gains prismatic. You may cast it until the end of your next turn.
107785|Change the Narrative|WU|R|Enchantment|3|{1}{W}{U}|||||136||||Planeswalker spells you cast cost {o1} less to cast.\nAt the beginning of your upkeep, conjure a random planeswalker card into your hand. It perpetually gains prismatic.
107786|Diabolic Formula|UB|R|Enchantment|2|{U}{B}|||||31||||Whenever one or more instant and/or sorcery cards leave your graveyard, conjure a random creature card with mana value X onto the battlefield, where X is the greatest mana value among those cards.
107787|Dina, Flesh Sower|WG|U|Legendary Creature - Dryad Cleric|2|{G}{W}||||3.4805|137||||Lifelink\nWhenever you gain life, Dina perpetually gets +1/+1.\nWhen Dina dies, choose a creature card in your hand. It perpetually gains trample and gets +X/+X, where X is Dina's power.
107788|Enter the Eternities|UG|M|Sorcery|2|{G}{U}|||||47||||Exile target creature you control. Draft a [+1] ability, a [-2] ability, and a [-8] ability. Create a legendary planeswalker token with the exiled creature's name and colors and the drafted abilities. Put a number of loyalty counters on it equal to the exiled creature's power.
107789|Flourishing Forces|WG|R|Sorcery|3|{G}{W}{W}|||||40||||Create a 2/2 colorless Wizard Soldier creature token named Cadet. For every 3 life you gained this game, create an additional one of those tokens.
107790|Killian, Spotlight Chaser|BR|U|Legendary Creature - Human Bard|2|{B}{R}||||6.2382|34||||Whenever a source you control deals noncombat damage to an opponent, seek a card with mana value equal to the damage dealt. Discard that card at the beginning of the next end step.
107791|Quintorius, Fate Splitter|WU|M|Legendary Planeswalker - Quintorius|5|{3}{W}{U}|||||55||||As long as Quintorius entered this turn, you may activate the loyalty abilities of planeswalkers you control twice this turn rather than only once.\nProliferate. You gain 3 life.\nSeek a non-Quintorius planeswalker card.\nCreate a 2/2 colorless Wizard Soldier creature token named Cadet. Then put a +1/+1 counter on each creature you control.
107792|Rootha, Ardent Artisan|RG|U|Legendary Creature - Orc Artificer|2|{R}{G}||||4.5613|88||||Sacrifice an artifact: Rootha becomes prepared. Activate only as a sorcery and only once each turn.
107794|Salvaged Imitations|RG|R|Sorcery|2|{X}{R}{G}|||||95||||For each number between 1 and X, conjure a random artifact card with that mana value onto the battlefield. X can't be 0.
107795|Zimone the Calculating|UB|U|Legendary Creature - Human Warlock|2|{U}{B}||||4.8835|164|0.6527|||Zimone assigns combat damage as though her power were positive.\nWhenever one or more cards are put into your graveyard from your library, Zimone becomes prepared.
107797|Willbound Quill||M|Artifact|4|{4}|||||19||||{oT}: Add one mana of any color.\n<i>Actualize</i> — Whenever you cast a spell that isn't from your starting deck, copy that spell. You may choose new targets for the copy.
107798|Potential Horizon||R|Land|||||||98||||If you were the starting player, this land enters tapped.\n{oT}, Sacrifice this land: Search your library for a land card with a basic land type, put it onto the battlefield, then shuffle.
"""

PAIR_STATS = {
    "BG": {"win_rate": 0.5702, "games": 684, "share": 0.0661},
    "BR": {"win_rate": 0.5479, "games": 794, "share": 0.0767},
    "RG": {"win_rate": 0.5783, "games": 1117, "share": 0.1079},
    "UB": {"win_rate": 0.5355, "games": 2407, "share": 0.2325},
    "UG": {"win_rate": 0.576, "games": 625, "share": 0.0604},
    "UR": {"win_rate": 0.5707, "games": 1146, "share": 0.1107},
    "WB": {"win_rate": 0.509, "games": 279, "share": 0.0269},
    "WG": {"win_rate": 0.5589, "games": 1578, "share": 0.1524},
    "WR": {"win_rate": 0.5189, "games": 185, "share": 0.0179},
    "WU": {"win_rate": 0.5763, "games": 1539, "share": 0.1486},
}


def _num(value: str, kind=float):
    return kind(value) if value else None


def primer() -> SetPrimer:
    cards = {}
    for line in CARD_TABLE.strip().splitlines():
        (
            grp,
            name,
            colors,
            rarity,
            types,
            cmc,
            _cost,
            gih,
            base,
            ata,
            alsa,
            games,
            play,
            game_wr,
            own,
            rules,
        ) = line.split("|")
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
            alsa=_num(alsa),
            games=int(games),
            play_rate=_num(play),
            game_wr=_num(game_wr),
            format_games=_num(own, int),
        )
    return SetPrimer(
        set_code="Y26FRA",
        source="model",
        speed=fra.SPEED,
        archetypes=[dict(entry) for entry in fra.ARCHETYPES],
        synergy_notes=fra.SYNERGY_NOTES,
        pair_stats=PAIR_STATS,
        traps=fra.TRAPS,
        cards=cards,
    )


COSTS = {int(line.split("|")[0]): line.split("|")[6] for line in CARD_TABLE.strip().splitlines()}
NAMES = {
    **{int(line.split("|")[0]): line.split("|")[1] for line in CARD_TABLE.strip().splitlines()},
    **BASICS,
}
ID = {name: grp_id for grp_id, name in NAMES.items()}
ORDER = list(PACKS)


def pool_before(key: str) -> list[int]:
    """What the drafter had taken before this pick."""
    return [PICKS[k] for k in ORDER[: ORDER.index(key)]]


def pool() -> list[int]:
    return [PICKS[k] for k in ORDER]
