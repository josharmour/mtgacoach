"""The 2026-10-13 FRA Contender Draft (draft 3f231a57), as recorded in Player.log.

Autoplay drafted Izzet (UR) around Saheeli, Jewel of Avishkar and Kiora of Fire and
Ashes. The user's complaint: picks followed 17Lands win rate, not the deck (Saheeli's
noncreature-spell engine, the lane, the archetype). Packs and picks are decoded from
Draft.Notify / EventPlayerDraftMakePick. Card rows cover the whole set (archetype engines
derive from every card, as they do live); they and the archetype synthesis are the FRA
primer and 17lands snapshot of the day with Arena mana costs, inlined so the tests need
no local cache.
"""

from __future__ import annotations

from arenamcp.set_primer import SetCard, SetPrimer

# Draft.Notify packs, in pick order.
PACKS: dict[str, list[int]] = {
    "P1p1": [
        106433,
        106344,
        106226,
        106329,
        106263,
        106376,
        106358,
        106397,
        106287,
        106424,
        106518,
        106492,
        106525,
        106527,
    ],
    "P1p2": [
        106329,
        106285,
        106355,
        106257,
        106248,
        106303,
        106243,
        106279,
        106484,
        106449,
        106481,
        106520,
        106435,
    ],
    "P1p3": [106311, 106276, 106350, 106397, 106304, 106387, 106343, 106234, 106467, 106519, 106478, 106422],
    "P1p4": [106294, 106235, 106296, 106268, 106344, 106311, 106400, 106409, 106480, 106463, 106535],
    "P1p5": [106354, 106269, 106289, 106329, 106226, 106419, 106307, 106459, 106519, 106423],
    "P1p6": [106229, 106268, 106338, 106231, 106276, 106415, 106239, 106520, 106437],
    "P1p7": [106350, 106265, 106276, 106306, 106417, 106297, 106453, 106486],
    "P1p8": [106235, 106268, 106399, 106425, 106522, 106448, 106532],
    "P1p9": [106226, 106358, 106424, 106518, 106525, 106527],
    "P1p10": [106355, 106243, 106484, 106481, 106435],
    "P1p11": [106311, 106397, 106234, 106519],
    "P1p12": [106294, 106311, 106535],
    "P1p13": [106226, 106519],
    "P1p14": [106415],
    "P2p1": [
        106229,
        106259,
        106407,
        106354,
        106369,
        106306,
        106297,
        106379,
        106492,
        106367,
        106515,
        106502,
        106496,
        106420,
    ],
    "P2p2": [
        106248,
        106394,
        106237,
        106303,
        106331,
        106296,
        106411,
        106391,
        106430,
        106470,
        106452,
        106484,
        106428,
    ],
    "P2p3": [106294, 106371, 106241, 106335, 106273, 106364, 106307, 106432, 106491, 106506, 106444, 106439],
    "P2p4": [106294, 106369, 106305, 106358, 106311, 106338, 106355, 106375, 106484, 106493, 106427],
    "P2p5": [106399, 106295, 106255, 106246, 106354, 106347, 106253, 106445, 106512, 106528],
    "P2p6": [106332, 106352, 106252, 106295, 106416, 106490, 106503, 106458, 106438],
    "P2p7": [106305, 106407, 106365, 106299, 106381, 106432, 106467, 106534],
    "P2p8": [106305, 106360, 106332, 106294, 106416, 106397, 106530],
    "P2p9": [106407, 106354, 106306, 106297, 106367, 106420],
    "P2p10": [106248, 106303, 106411, 106470, 106484],
    "P2p11": [106294, 106241, 106364, 106439],
    "P2p12": [106305, 106311, 106355],
    "P2p13": [106246, 106528],
    "P2p14": [106252],
    "P3p1": [
        106241,
        106325,
        106272,
        106290,
        106265,
        106371,
        106411,
        106243,
        106339,
        106284,
        106503,
        106458,
        106495,
        106531,
    ],
    "P3p2": [
        106325,
        106358,
        106296,
        106252,
        106433,
        106376,
        106250,
        106291,
        106409,
        106450,
        106524,
        106517,
        106422,
    ],
    "P3p3": [106349, 106306, 106276, 106371, 106399, 106260, 106362, 106384, 106444, 106470, 106486, 106420],
    "P3p4": [106397, 106248, 106419, 106304, 106289, 106243, 106434, 106519, 106478, 106471, 106533],
    "P3p5": [106411, 106276, 106376, 106296, 106325, 106426, 106465, 106482, 106511, 106529],
    "P3p6": [106349, 106272, 106226, 106304, 106373, 106507, 106477, 106442, 106526],
    "P3p7": [106306, 106349, 106394, 106390, 106336, 106251, 106486, 106470],
    "P3p8": [106241, 106294, 106305, 106365, 106303, 106518, 106532],
    "P3p9": [106241, 106325, 106265, 106411, 106243, 106531],
    "P3p10": [106325, 106358, 106252, 106433, 106376],
    "P3p11": [106399, 106470, 106486, 106420],
    "P3p12": [106289, 106519, 106533],
    "P3p13": [106426, 106529],
    "P3p14": [106526],
}

# EventPlayerDraftMakePick: what autoplay took.
PICKS: dict[str, int] = {
    "P1p1": 106492,
    "P1p2": 106520,
    "P1p3": 106304,
    "P1p4": 106463,
    "P1p5": 106269,
    "P1p6": 106520,
    "P1p7": 106265,
    "P1p8": 106399,
    "P1p9": 106424,
    "P1p10": 106243,
    "P1p11": 106397,
    "P1p12": 106311,
    "P1p13": 106519,
    "P1p14": 106415,
    "P2p1": 106259,
    "P2p2": 106394,
    "P2p3": 106273,
    "P2p4": 106427,
    "P2p5": 106253,
    "P2p6": 106458,
    "P2p7": 106381,
    "P2p8": 106416,
    "P2p9": 106407,
    "P2p10": 106411,
    "P2p11": 106364,
    "P2p12": 106355,
    "P2p13": 106246,
    "P2p14": 106252,
    "P3p1": 106458,
    "P3p2": 106250,
    "P3p3": 106384,
    "P3p4": 106304,
    "P3p5": 106376,
    "P3p6": 106304,
    "P3p7": 106394,
    "P3p8": 106303,
    "P3p9": 106265,
    "P3p10": 106433,
    "P3p11": 106399,
    "P3p12": 106519,
    "P3p13": 106426,
    "P3p14": 106526,
}

BASICS: dict[int, str] = {
    106526: "Plains",
    106527: "Plains",
    106528: "Island",
    106529: "Island",
    106530: "Swamp",
    106531: "Swamp",
    106532: "Mountain",
    106533: "Mountain",
    106534: "Forest",
    106535: "Forest",
}

# grp_id|name|colors|rarity|types|cmc|arena cost|GIH|baseline z|ATA|ALSA|games|play rate|game WR|rules
CARD_TABLE = r"""
106225|Emrakul, the Exigent Doom||M|Legendary Creature - Eldrazi|10|{10}||||2.4262|146|||When you cast this spell, untap all lands you control.\nFlying\nTrample\nWard—Sacrifice three permanents.\n{o3}, Exile this card from your hand: Target land gains "{oT}: Add {oCoC}" until this card is cast from exile. You may cast this card for as long as it remains exiled.
106226|Academic Ascent|W|C|Instant|2|{1}{W}|||11.6703|7.8959|348|0.1081|0.5336|Target creature gets +2/+2 and gains flying until end of turn.\nEmpower Jace 2.
106227|Blossom-Blessed Angel|W|C|Creature - Angel Cleric|4|{3}{W}|0.5668|0.354|6.8475|4.9204|2768|0.7596|0.5458|Flying\nVigilance\nThis creature enters prepared.
106229|Campus Crier|W|C|Creature - Human Advisor|2|{1}{W}|0.5565|0.063|7.5228|5.4116|3258|0.7533|0.5537|{o1}, Exile this card from your graveyard: Empower Jace 2.
106230|Enlightened Confidant|W|M|Creature - Kor Cleric|2|{1}{W}||||2.1579|277|0.8817|0.5654|Lifelink\nAt the beginning of your end step, if you gained life this turn, surveil 1. If you put a card with mana value less than or equal to the amount of life you gained this turn into your graveyard this way, put that card into your hand.
106231|Fateshaper Aspirant|W|C|Creature - Rhino Cleric|5|{4}{W}|0.5132|-1.156|10.5324|7.1308|1025|0.2999|0.5216|When this creature enters, choose one —\n•Return target legendary card from your graveyard to your hand.\n•Put a +1/+1 counter on target creature. It gains vigilance and indestructible until end of turn.
106232|Flickering Hound|W|R|Creature - Dog|4|{3}{W}||||4.1739|135|0.3643||Whenever you cast a creature spell, exile up to one other target creature you control, then return that card to the battlefield under its owner's control.
106233|Generous Revival|W|U|Sorcery|3|{2}{W}|0.574|0.556|5.9806|4.4407|1216|0.7094|0.5505|Return target creature card with mana value 3 or less from your graveyard to the battlefield with an additional +1/+1 counter on it.\nFlashback {o4oW}
106234|Germinate Recruits|W|R|Instant|3|{2}{W}||||5.4613|72|0.1371||Create X 2/2 colorless Wizard Soldier creature tokens named Cadet, where X is the amount of life you gained this turn.
106235|Graft Surgeon|W|C|Creature - Human Cleric|3|{2}{W}|0.505|-1.386|11.0217|7.3401|600|0.2245|0.5037|This creature enters with a +1/+1 counter on it.\nWhen this creature dies, put its counters on up to one target creature you control.
106236|Guiding Hydra|W|R|Creature - Hydra Horror|1|{X}{W}|0.5882|0.954|1.6667|1.6584|692|0.9189|0.5631|This creature enters with X +1/+1 counters on it.\nAt the beginning of combat on your turn, you may remove a +1/+1 counter from this creature. If you do, put a +1/+1 counter on each other creature you control.
106237|Hexhaven Battalion|W|C|Sorcery|6|{4}{W}{W}|0.5755|0.597|5.2366|4.0951|4466|0.8311|0.5553|Create three 2/2 colorless Wizard Soldier creature tokens named Cadet. Empower Jace 2.\nBasic landcycling {o2}
106238|Kindred Judgment|W|R|Sorcery|7|{5}{W}{W}|0.6034|1.384|2.2955|2.3794|880|0.8717|0.566|Choose a creature type. Destroy all creatures that aren't of the chosen type.
106239|Loyal Tutor|W|R|Instant|1|{W}|||10.3271|6.6181|8|0.0134||Search your library for a planeswalker card, reveal it, then shuffle and put that card on top.
106240|Memory Trap|W|C|Enchantment|3|{2}{W}|0.5528|-0.041|4.1706|3.2065|3656|0.8936|0.5394|When this enchantment enters, exile target nonland permanent an opponent controls until this enchantment leaves the battlefield.
106241|Predictive Preparations|W|C|Sorcery|2|{1}{W}|||11.5079|7.6473|375|0.1439|0.49|Put a +1/+1 counter on each of one or two target creatures.\nFlashback {o3oW}
106242|Prophesied End|W|U|Instant|2|{1}{W}|0.5306|-0.666|3.8246|3.118|1455|0.9179|0.5392|Destroy target creature. If it wasn't attacking, its controller draws a card.
106243|Refute Destiny|W|U|Sorcery|2|{1}{W}|||11.6281|7.7762|138|0.0793||Exile target creature or planeswalker that's green or blue. Surveil 1.
106244|Repurposed Enforcer|W|R|Creature - Human Soldier|2|{1}{W}|0.562|0.219|2.1193|2.0668|669|0.8844|0.5343|Whenever this creature attacks, empower Jace X, where X is the number of creatures you control.
106245|Return to the Light Realms|W|M|Sorcery|9|{7}{W}{W}||||4.6089|78|0.3075||Return all nonland permanent cards from your graveyard to the battlefield.
106246|Shatterwing Pegasus|W|C|Creature - Pegasus|3|{2}{W}|0.51|-1.245|9.9606|6.7837|1647|0.4587|0.5284|Flying\n{o4oW}: Creatures you control get +1/+1 until end of turn.
106247|Surgical Precision|W|C|Sorcery|2|{1}{W}|0.5663|0.34|5.5375|4.2204|3777|0.8644|0.5471|Choose one —\n•Destroy target creature with toughness 4 or greater. You gain 1 life.\n•You draw a card and gain 2 life.
106248|Unflinching Hortimancer|W|C|Creature - Human Cleric|2|{1}{W}|0.5193|-0.983|7.7376|5.2966|1862|0.6497|0.5262|Ward {o1}\nWhenever you gain life, put a +1/+1 counter on this creature.
106249|Your Fate Ends Here|W|U|Instant|3|{2}{W}|0.5728|0.522|3.4977|2.9893|1868|0.9255|0.5446|Destroy target creature or planeswalker with mana value 3 or greater. Surveil 1.
106250|Countersculpt|U|U|Instant|2|{U}{U}|0.6062|1.462|3.7179|3.3027|3103|0.8979|0.5654|As an additional cost to cast this spell, behold a Jace or pay {o1}.\nCounter target spell. Empower Jace 1.
106251|Cruel Calculations|U|R|Sorcery|3|{2}{U}|||9.3487|6.3116|39|0.0599||Draw X cards, where X is the number of cards that were put into target player's graveyard from their library this turn.
106252|Cryotheory Adept|U|C|Creature - Human Wizard|2|{1}{U}|0.5303|-0.675|9.889|6.8853|1454|0.3534|0.535|Prowess\n{o3oU}, Exile this card from your graveyard: Tap target creature and put a stun counter on it. Activate only as a sorcery.
106253|Diviner of Victory|U|R|Creature - Dwarf Wizard|1|{U}|0.6038|1.395|2.9672|2.7162|949|0.901|0.5767|This creature enters prepared.\nWhenever you scry or surveil, this creature gets +1/+1 until end of turn.
106255|Divining Duelist|U|C|Creature - Merfolk Wizard|3|{2}{U}|0.5261|-0.794|10.1272|6.8916|1401|0.3123|0.5182|Flash\nWhen this creature enters, choose one —\n•Tap target creature.\n•Untap target creature.\n•Draw a card, then discard a card.
106256|Icy Reception|U|C|Instant|2|{1}{U}|0.5781|0.673|6.1456|4.7627|5336|0.8423|0.562|Choose one —\n•Counter target creature or legendary spell unless its controller pays {o3}.\n•Target creature gets -5/-0 until end of turn.
106257|Infinite Coursework|U|C|Enchantment - Aura|3|{2}{U}|0.5737|0.547|5.623|4.084|4466|0.8837|0.5519|Enchant creature\nWhen this Aura enters, tap enchanted creature. It becomes unprepared.\nEnchanted creature loses all abilities and doesn't untap during its controller's untap step.
106258|Jace's Machinations|U|R|Instant|3|{2}{U}|0.5895|0.992|2.2904|2.1804|838|0.902|0.5376|Until end of turn, you may activate loyalty abilities of Jace planeswalkers you control on any player's turn any time you could cast an instant.\nEmpower Jace 8.
106259|Mindseeker Oculus|U|C|Creature - Homunculus|3|{2}{U}|0.6118|1.619|3.5796|3.0279|6550|0.9357|0.5785|When this creature enters, empower Jace 4.
106260|Perfected Theory|U|U|Instant|1|{U}|||9.9798|6.726|389|0.2259|0.5152|Choose one —\n•Target creature has base power and toughness 1/1 until end of turn.\n•Target creature has base power and toughness 4/5 until end of turn.
106261|Plan for All Outcomes|U|U|Enchantment|4|{3}{U}|0.6075|1.498|2.9713|2.6679|2978|0.9469|0.5732|When this enchantment enters, the owner of up to one other target nonland permanent puts it on their choice of the top or bottom of their library.\nWhenever you cast your first noncreature spell each turn, empower Jace 1.
106262|Precise Redaction|U|U|Instant|2|{1}{U}|||12.046|8.4473|16|0.0091||Counter target white or black spell.
106263|Protege's Awakening|U|C|Sorcery|4|{3}{U}|0.5882|0.956|5.8712|4.5934|5340|0.8326|0.5615|Empower Jace 6.\nDraw a card.
106264|Seasoned Cryomancer|U|M|Creature - Human Wizard|3|{1}{U}{U}|||||371|0.9387|0.5431|When this creature enters, draw two cards, then discard two cards. When you discard one or more nonland cards this way, tap up to that many target creatures and put a stun counter on each of them.\n{o3oUoU}, Exile this card from your graveyard: Draw two cards.
106265|Semester Foreseer|U|C|Creature - Human Wizard|4|{3}{U}|0.5228|-0.886|8.5529|5.933|2502|0.53|0.5185|This creature enters prepared.\nWhen this creature enters, surveil 1.
106267|Sphinx of False Conclusions|U|R|Creature - Sphinx Illusion|4|{2}{U}{U}|0.6353|2.281|1.5015|1.5268|1105|0.9305|0.5925|Flash\nFlying\nWhenever this creature attacks, draw a card, then discard a card.\nWhen this creature dies, if it isn't a token, create a token that's a copy of it.
106268|Sphinx's Approach|U|C|Instant|3|{1}{U}{U}|0.5334|-0.588|9.4616|6.9479|2608|0.4363|0.5306|Draw two cards. Then you may exile this spell and four cards named Sphinx's Approach from your graveyard. If you do, search your library for a Sphinx creature card, put it onto the battlefield, then shuffle.\nA deck can have any number of cards named Sphinx's Approach.
106269|Surveillance Phantasm|U|C|Creature - Bird Illusion|2|{1}{U}|0.57|0.444|5.8216|4.417|5442|0.8875|0.5593|Defender\nFlying\nVigilance\nAs long as you've scried or surveilled this turn, this creature can attack as though it didn't have defender.\n{o3oU}: Surveil 1.
106270|The Theorist, Jace Beleren|U|M|Legendary Planeswalker - Jace|4|{2}{U}{U}|0.6904|3.832|||520|0.9593|0.5957|At the beginning of each opponent's draw step, you draw a card.\nCreate a 1/1 blue Illusion creature token.\nFor each opponent, return up to one target artifact or creature that player controls to its owner's hand.\nDraw three cards. Then put X +1/+1 counters on each creature you control, where X is the number of cards in your hand.
106271|Theorist's Proxy|U|R|Creature - Illusion|2|{1}{U}|0.5795|0.712|3.1185|2.9048|811|0.8913|0.5556|Flash\nWhen this creature enters, empower Jace 3.\n{oU}, Sacrifice this creature: The next spell you cast this turn can't be countered.
106272|Undulating Witness|U|C|Creature - Serpent|5|{4}{U}|0.5564|0.061|6.6659|4.8302|5052|0.8559|0.5495|Flying\n{o2}: This creature gets +1/-1 until end of turn.\nBasic landcycling {o2}
106273|Unsummon|U|C|Instant|1|{U}|0.575|0.586|6.4997|4.9666|5010|0.8116|0.5559|Return target creature to its owner's hand.
106274|Variable Chaser|U|R|Creature - Human Wizard|3|{2}{U}|0.5622|0.223|2.9429|2.6779|788|0.8901|0.5376|Flying\nProwess\nThis creature enters prepared.
106276|Apex Witchstalker|B|C|Creature - Wolf|6|{4}{B}{B}|0.5588|0.129|6.7559|4.8923|4334|0.8327|0.5519|Menace\nWhen this creature enters or dies, you gain 2 life.\nBasic landcycling {o2}
106277|Bloodline Recollector|B|M|Creature - Vampire Warlock|2|{1}{B}||||3.2311|142|0.622||At the beginning of each end step, if three or more creatures died this turn, this creature becomes prepared.
106279|Break Under Pressure|B|U|Instant|3|{2}{B}|0.5739|0.553|3.0273|2.6971|2307|0.9236|0.5466|Target opponent sacrifices a creature or planeswalker with the greatest mana value among creatures and planeswalkers they control. You gain 2 life.
106280|Cast Away Doubt|B|C|Sorcery|3|{2}{B}|0.525|-0.822|10.2064|7.0058|1078|0.3144|0.5262|Draw two cards. Cast Away Doubt deals 2 damage to each player.
106281|Dark Matter Manipulator|B|R|Creature - Human Warlock|1|{B}|0.6076|1.502|4.6608|4.07|632|0.7107|0.5729|When this creature enters, mill three cards.\nThis creature gets +2/+0 for every seven cards in your graveyard.
106282|Darklight Phoenix|B|M|Creature - Phoenix|4|{3}{B}||||2.4601|190|0.7147||Flying\nHaste\nAt the beginning of combat on your turn, if two or more creatures died this turn, return this card from your graveyard to the battlefield.
106283|Extended Absence|B|C|Instant|4|{3}{B}|0.5977|1.224|3.4941|2.945|5392|0.9472|0.5682|Exile target creature or planeswalker. Extended Absence deals 1 damage to each opponent and you gain 1 life.
106284|Extrapolate the Impossible|B|R|Sorcery|2|{1}{B}|||9.7881|6.4647|30|0.05||You may reveal exactly two cards you own with different names from outside the game. An opponent chooses one of them. You put that card into your hand.
106285|Last Gasp|B|C|Instant|2|{1}{B}|0.5589|0.132|4.4048|3.5022|4514|0.8967|0.5495|Target creature gets -3/-3 until end of turn.
106286|Lich's Relic|B|R|Artifact - Equipment|1|{B}|0.6171|1.768|1.6551|1.6699|914|0.9427|0.5782|When this Equipment enters, you may pay {o2}. When you do, for each opponent, destroy up to one target creature or planeswalker that player controls.\nEquipped creature gets +2/+1.\nEquip {o2}
106287|Multiply by Zero|B|U|Instant|2|{1}{B}|0.5989|1.256|2.7408|2.4387|2291|0.9154|0.5612|Target creature has base power and toughness 0/0 until end of turn.
106288|Overwrite the Multiverse|B|M|Sorcery|6|{4}{B}{B}|||||423|0.8979|0.5819|Exile all creatures. Empower Jace X, where X is the number of creatures exiled this way.
106289|Rampart Hunter|B|C|Creature - Horror|4|{3}{B}|||12.0835|8.1719|259|0.0803|0.5084|Deathtouch\nWhen this creature enters, target creature gets +2/+2 and gains deathtouch until end of turn.
106290|Rank Rat|B|C|Creature - Zombie Rat|2|{1}{B}|0.5565|0.063|8.2975|5.8382|2647|0.6368|0.5447|When this creature enters, each opponent discards a card.
106291|Rewrite Regrets|B|U|Sorcery|4|{3}{B}|0.6001|1.29|3.8769|3.41|2658|0.8706|0.5646|Return target creature or planeswalker card with mana value 6 or less from your graveyard to the battlefield.\nEmpower Jace 2.
106292|Rise of the Deathbringer|B|R|Instant|5|{4}{B}|0.5789|0.695|2.2846|2.0703|741|0.8932|0.5432|Choose one —\n•Draw cards equal to the greatest power among creatures you control. You lose life equal to the number of cards drawn this way.\n•All creatures get -3/-3 until end of turn.
106293|Sanctum Lurker|B|R|Creature - Horror|3|{2}{B}|0.6341|2.248|1.7072|1.7111|962|0.9498|0.5761|When this creature enters, empower Jace 1.\nPlaneswalkers you control aren't put into their owners' graveyards for having 0 loyalty.\nPlaneswalkers you control have "[+2]: This planeswalker deals 1 damage to each opponent and you gain 1 life."
106294|Screeching Soulbreaker|B|C|Creature - Siren Bard|3|{2}{B}|0.5308|-0.66|9.3802|6.3066|1396|0.455|0.5273|Flying\nWhenever this creature attacks, it deals 1 damage to each opponent and you gain 1 life.
106295|Silence the Echo|B|C|Sorcery|2|{1}{B}|0.5221|-0.905|7.4244|5.1811|2377|0.6545|0.5108|As an additional cost to cast this spell, sacrifice a creature or planeswalker or pay {o3}.\nDestroy target creature or planeswalker.
106296|Solve for Disappointment|B|C|Sorcery|2|{1}{B}|0.5209|-0.937|10.8743|7.4637|764|0.2014|0.5117|Target opponent reveals their hand. You choose a nonland permanent card from it. That player discards that card.\nEmpower Jace 1.
106297|Terminal Criticism|B|U|Instant|2|{1}{B}|||11.9221|8.0659|78|0.0459||Destroy target creature or planeswalker that's blue or red. You gain 1 life.
106298|Theoretical Necromancer|B|C|Creature - Vampire Warlock|3|{2}{B}|0.5638|0.269|8.5246|6.1949|3535|0.6763|0.5626|{o3oB}, Exile this card from your graveyard: Return another target creature card from your graveyard to your hand.
106299|Void Extrapolator|B|C|Creature - Aetherborn Warlock|2|{1}{B}|0.5485|-0.162|9.7069|7.0007|2454|0.4927|0.5487|This creature enters prepared.\nThreshold — This creature gets +1/+1 as long as there are seven or more cards in your graveyard.
106301|Vraska's Final Mercy|B|R|Sorcery|2|{B}{B}|0.5213|-0.927|2.8983|2.4965|516|0.7921|0.5258|Choose one —\n•You lose 2 life. Destroy target creature or planeswalker.\n•You lose 2 life. Empower Jace 6.
106302|Ajani's Anguish|R|R|Enchantment|1|{X}{R}|0.5687|0.407|1.8597|1.9049|633|0.9399|0.5532|When this enchantment enters, it deals X damage to any target.\nCreatures you control have trample.
106303|Artifist Acumen|R|C|Sorcery|1|{R}|||12.1727|8.4188|265|0.0903|0.4695|Creatures you control gain first strike until end of turn.\nDraw a card.
106304|Awaken the Inferno|R|C|Sorcery|5|{4}{R}|0.5753|0.592|6.1433|4.4815|3845|0.8552|0.5539|Awaken the Inferno deals 6 damage to target creature or planeswalker an opponent controls. Put a +1/+1 counter on up to one target creature you control.\nBasic landcycling {o2}
106305|Blazing Crescendo|R|C|Instant|2|{1}{R}|||11.829|8.1735|423|0.1364|0.4936|Target creature gets +3/+1 until end of turn. Exile the top card of your library. Until the end of your next turn, you may play that card.
106306|Chandra's Emberling|R|C|Creature - Gremlin Elemental|3|{2}{R}|0.4865|-1.906|10.644|7.1473|929|0.3376|0.5175|Haste\nWhenever you cast a noncreature spell, put a +1/+1 counter on this creature.
106307|Command the Stage|R|U|Sorcery|3|{2}{R}|||8.7267|5.7656|490|0.3963|0.5174|Create a 2/2 colorless Wizard Soldier creature token named Cadet, then put a +1/+1 counter on each other Wizard token you control.\nAt the beginning of each upkeep, if an opponent was dealt noncombat damage last turn, return this card from your graveyard to your hand.
106308|Craterclaw Colossus|R|M|Artifact Creature - Beast Construct|7|{4}{R}{R}{R}||||3.0434|172|0.681||Haste\nWhen this creature enters, creatures you control gain trample and get +X/+0 until end of turn, where X is the number of artifacts you control.
106309|Curse-Marred Demon|R|R|Creature - Demon|4|{2}{R}{R}|0.5664|0.343|2.3498|2.118|572|0.874|0.5411|Flying\nTrample\nWhen this creature enters, search your library for a card, put it into your hand, shuffle, then discard a card at random.
106310|Draconic Visitor|R|R|Creature - Dragon|5|{3}{R}{R}|0.5095|-1.259|2.036|2.0221|526|0.814|0.5068|Flying\nIf one or more artifact tokens would be created under your control, that many 5/5 red Dragon creature tokens with flying are created instead.
106311|Eardrum Rattler|R|C|Creature - Human Bard|2|{1}{R}|||11.9402|8.3202|367|0.1165|0.5349|{o1}, {oT}: Another target creature you control with power 2 or less can't be blocked this turn.
106312|Essence Burn|R|U|Instant|2|{1}{R}|||12.0058|8.0333|53|0.0333||Essence Burn deals 5 damage to target black or green creature or planeswalker. If that permanent would die this turn, exile it instead.
106313|Face Yourself|R|R|Sorcery|7|{5}{R}{R}|||4.5154|3.7176|466|0.6463|0.5472|For each creature target player controls, create a token that's a copy of that creature, except it has haste and "At the beginning of the end step, if you don't control a planeswalker, sacrifice this creature."
106314|Fulminous Forte|R|U|Instant|3|{2}{R}|0.5653|0.31|3.4817|2.9442|1808|0.9075|0.5527|Choose one —\n•Fulminous Forte deals 1 damage to each creature and planeswalker your opponents control.\n•Fulminous Forte deals 5 damage to target creature or planeswalker.
106315|Hallway Heckler|R|C|Creature - Elemental Sorcerer|3|{2}{R}|0.5232|-0.875|10.4437|7.2248|1036|0.3426|0.5171|This creature enters prepared.\n{oT}, Discard a card: Draw a card.
106317|Heartstring Puller|R|C|Creature - Elf Sorcerer|4|{3}{R}|0.5643|0.283|8.6131|6.0762|2364|0.6132|0.5453|Trample\nWhen this creature enters, create a 2/2 colorless Wizard Soldier creature token named Cadet.
106318|Identity Echo|R|R|Enchantment|3|{2}{R}|||10.085|6.3934|14|0.0221||{o3oR}: Exile target creature or planeswalker you control. Reveal cards from the top of your library until you reveal a creature or planeswalker card. Put that card onto the battlefield and the rest on the bottom of your library in a random order. Activate only as a sorcery.
106319|Master of Barbs|R|R|Creature - Lizard Bard|2|{1}{R}||||3.4685|380|0.7072|0.5744|Menace\nWhenever one or more opponents are dealt noncombat damage, creatures you control get +1/+0 until end of turn.
106320|No Admittance|R|C|Sorcery|2|{1}{R}|0.5623|0.225|4.3146|3.401|4016|0.897|0.5508|No Admittance deals 3 damage to any target.\nEmpower Jace 1.
106321|Pompous Battlemage|R|R|Creature - Goblin Sorcerer|1|{R}||||3.8919|307|0.6618|0.5332|Prowess\nThis creature enters prepared.
106323|Pyre Rhymer|R|R|Creature - Elemental Sorcerer|3|{1}{R}{R}||||3.8024|256|0.7068|0.4975|Prowess\nThis creature enters prepared.
106325|Skilled Battlecarver|R|C|Creature - Human Warrior|2|{1}{R}|0.5138|-1.138|9.2072|6.5945|1989|0.5632|0.5333|During your turn, this creature has first strike.\n{o1oR}: This creature gets +1/+0 until end of turn.
106326|Stingcaster Mage|R|M|Creature - Human Wizard|2|{1}{R}||||2.1324|253|0.8512|0.5282|Haste\nWhen this creature enters, target instant or sorcery card in your graveyard gains flashback until end of turn. The flashback cost is equal to its mana cost.
106327|Tether Technician|R|C|Creature - Minotaur Artificer|5|{4}{R}|0.5|-1.527|11.1877|7.6963|682|0.2162|0.5121|Reach\nWhen this creature enters, you may discard a card. When you do, this creature deals 2 damage to any target.
106328|Violent Echoes|R|U|Instant|4|{2}{R}{R}|0.5926|1.08|3.0913|2.7409|2116|0.8958|0.5689|Violent Echoes deals 6 damage to target creature or planeswalker. If excess damage was dealt to that permanent this way, empower Jace X, where X is that excess damage.
106329|Wrath of the Bloodmane|R|C|Instant|3|{2}{R}|0.552|-0.064|5.091|3.912|3799|0.8859|0.5446|This spell costs {o1} less to cast if you control a legendary creature.\nWrath of the Bloodmane deals 4 damage to target creature or planeswalker.
106330|Arcane Amphisbaena|G|C|Creature - Snake|2|{1}{G}|0.557|0.076|5.9594|4.3501|3661|0.8643|0.5451|Deathtouch\nWhen this creature enters, empower Jace 2.
106331|Bestial Incursion|G|C|Sorcery|4|{3}{G}|0.5437|-0.297|6.1843|4.6307|3743|0.8441|0.5392|Create a 4/4 green Beast creature token with trample.\nFlashback {o5oG}
106332|Budding Insurgent|G|C|Creature - Dryad Scout|3|{2}{G}|0.4711|-2.34|10.7498|7.2066|675|0.2432|0.4848|Vigilance\nSacrifice this creature: Destroy target artifact or enchantment. If that permanent was a legendary enchantment, draw a card. Activate only as a sorcery.
106333|Carnivorous Cultivator|G|R|Creature - Elf Warlock|2|{1}{G}|0.5693|0.422|2.378|2.33|722|0.9048|0.5465|Deathtouch\nThis creature enters prepared.\nWhenever this creature deals combat damage to a player, return target land card from your graveyard to your hand.
106335|Compel Brutality|G|C|Instant|2|{1}{G}|0.5327|-0.606|5.8692|4.3871|3287|0.8552|0.5227|Choose one —\n•Target creature you control deals damage equal to its power to target creature or planeswalker an opponent controls.\n•Target planeswalker you control deals damage equal to its loyalty to target creature or planeswalker an opponent controls.
106336|Flourishing Grapple|G|U|Instant|1|{G}|||12.2065|8.4245|30|0.0183||Target creature or planeswalker an opponent controls that's red or white loses all abilities until end of turn. Target creature you control deals damage equal to its power to that permanent.
106337|Gardenize|G|R|Enchantment|3|{1}{G}{G}||||5.9804|12|0.0317||Whenever a creature you control dies, put a charge counter on this enchantment.\nAt the beginning of your first main phase, add {oG} for each charge counter on this enchantment.
106338|Greenhouse Propagator|G|C|Creature - Cat Druid|3|{2}{G}|0.5527|-0.044|6.1627|4.4961|2477|0.8124|0.5316|Whenever another creature you control enters, you gain 1 life.\n{oT}: Add {oG}.
106339|Heartwood Crafter|G|U|Creature - Elf Artificer|1|{G}|0.5818|0.776|4.3407|3.5861|1662|0.8352|0.5481|This creature enters prepared.\n{oT}: Add {oC}. This mana can't be spent to cast spells from your hand.
106341|Hexhaven Invigorator|G|M|Creature - Chimera Horror|4|{G}{G}{G}{G}||||3.0643|155|||Vigilance\nWhenever this creature is dealt damage, you may search your library for up to that many land cards, put them onto the battlefield tapped, then shuffle.
106342|Hungering Puppetbeast|G|R|Artifact Creature - Beast Construct|5|{3}{G}{G}|0.615|1.71|1.7868|1.7328|787|0.922|0.5673|When this creature enters, create a Heartwood token.\n{o1}, Sacrifice another artifact: Put a +1/+1 counter on this creature. It gains your choice of trample, hexproof, or haste until end of turn.
106343|Hunter's Axe|G|U|Artifact - Equipment|1|{G}|||10.2475|6.8983|324|0.2394|0.4893|Equipped creature gets +2/+0 and has "Whenever this creature attacks, it gains your choice of trample or deathtouch until end of turn."\nEquip {o2}
106344|Inspired Tethermage|G|C|Creature - Elf Warrior|3|{2}{G}|0.5003|-1.517|9.7093|6.5391|1475|0.4394|0.5028|Whenever you put one or more loyalty counters on a planeswalker, put a +1/+1 counter on this creature.\n{o6}: Empower Jace 2.
106345|Omnipresence|G|M|Enchantment|8|{5}{G}{G}{G}||||5.6735|0|0||You may cast spells with mana value less than or equal to the number of creatures you control from your hand without paying their mana costs.
106346|Puppet Crafting|G|R|Enchantment - Aura|2|{1}{G}||||5.2767|98|0.1976||Enchant artifact or non-Aura enchantment\nEnchanted permanent is a Construct creature with base power and toughness 5/5 in addition to its other types.\n{o4oG}: Return this card from your graveyard to your hand.
106347|Restore with Empathy|G|U|Instant|3|{2}{G}|0.5214|-0.925|7.8752|5.6872|1053|0.584|0.5188|Return target permanent card from your graveyard to your hand. You gain 4 life.
106348|Simulacrum Shaper|G|R|Creature - Elf Druid|3|{1}{G}{G}|0.5718|0.495|2.0824|1.9636|696|0.9174|0.5327|When this creature enters, you may search your library for a basic land card, put that card onto the battlefield tapped, then shuffle.\nWhen this creature dies, draw a card.
106349|Something Worth Saving|G|C|Instant|2|{1}{G}|0.5555|0.034|9.0277|6.6913|2632|0.5185|0.5429|Mill four cards. You may put a permanent card from among them into your hand. You gain 1 life.
106350|Sureshot Sower|G|C|Creature - Human Archer|2|{1}{G}|0.5156|-1.087|9.4715|6.487|2015|0.5423|0.527|Reach\n{o3oG}, Discard this card: Destroy target creature with flying.
106351|Tarmogoyf|G|M|Creature - Lhurgoyf|2|{1}{G}||||2.6866|238|0.8041|0.5478|Tarmogoyf's power is equal to the number of card types among cards in all graveyards and its toughness is equal to that number plus 1.
106352|Tethermage's Advantage|G|C|Instant|1|{G}|||12.4007|8.492|160|0.0549||Target creature gets +2/+2 and gains reach until end of turn. Untap it.
106353|Verdant Kraken|G|R|Creature - Plant Kraken|7|{4}{G}{G}{G}|0.587|0.922|1.6832|1.7304|707|0.8818|0.5407|At the beginning of each player's upkeep, you create a 3/3 green Forest Tentacle land creature token.
106354|Vinelasher Adept|G|C|Creature - Rhino Soldier|6|{4}{G}{G}|0.5309|-0.657|7.3342|5.2373|3511|0.7844|0.5306|Reach\nWhen this creature enters, put three +1/+1 counters on target creature.\nBasic landcycling {o2}
106355|Wrecking Gecko|G|C|Artifact Creature - Lizard Construct|5|{4}{G}|0.5477|-0.184|9.5004|6.6053|1802|0.4877|0.5353|Ward {o2}\n{o6oGoG}: This creature gets +4/+4 and gains trample until end of turn.
106356|Aerid Konstrari|RG|M|Legendary Creature - Elder Sphinx|4|{1}{R}{G}{G}|||||334|0.9018|0.5559|Flying\nWhen Aerid Konstrari enters or dies, create a Heartwood token.\n{o6}: Create a Heartwood token. Then Aerid Konstrari gets +X/+0 until end of turn, where X is the number of artifacts you control.
106357|Avatar of Burgeoning Echoes|UG|M|Creature - Avatar|2|{G}{U}||||2.3251|294|0.8134|0.5963|Landfall — Whenever a land you control enters, empower Jace 2.\nPlaneswalkers you control have "[-10]: Put a +1/+1 counter on target creature for each land you control."
106358|Blessed Ghoul|WB|C|Creature - Zombie Cleric|1|{W/B}|0.543|-0.316|8.9659|5.9198|1720|0.5162|0.5358|Lifelink\n{o2o(W/B)}: Return this card from your graveyard to your hand.
106359|Bloombrute|WG|U|Creature - Plant Elemental|4|{2}{G}{W}|0.5712|0.478|4.7415|4.0537|1348|0.7867|0.5465|Whenever you gain life, draw a card. This ability triggers only once each turn.\n{o4oGoW}: Target creature gains trample and lifelink until end of turn.
106360|Charge the Sanctum|WR|C|Instant|3|{2}{R/W}|||11.6885|7.8938|370|0.1349|0.5|Choose one —\n•Creatures you control get +2/+0 until end of turn.\n•Target creature gets +2/+0 and gains first strike until end of turn. Put a +1/+1 counter on it.
106361|Clash of Elements|UR|U|Instant|3|{1}{U}{R}|0.5612|0.194|6.4803|5.3187|1668|0.6459|0.5471|Choose target nonland permanent. Its owner may put it on top of their library. If they do, Clash of Elements deals 2 damage to them. If they didn't put the card on top of their library, they put it on the bottom.
106362|Craftwork Crusher|RG|U|Artifact Creature - Boar Construct|7|{3}{R}{R}{G}{G}|0.6152|1.716|3.4974|3.3013|1801|0.8227|0.5672|Trample\nWhen this creature enters, choose two —\n•This creature deals 4 damage to target creature or planeswalker.\n•Create a 2/2 colorless Wizard Soldier creature token named Cadet.\n•Draw a card.
106363|Denzilore Fatehold|WU|M|Legendary Creature - Elder Sphinx|4|{1}{W}{U}{U}||||1.605|396|0.886|0.5455|Flash\nFlying\nWhenever you scry or surveil, put a +1/+1 counter on each creature you control.
106364|Desperate Futurescribe|WU|U|Creature - Kor Scout|4|{2}{W}{U}|0.5701|0.447|4.8856|4.0917|1775|0.7727|0.5531|Flying\nAt the beginning of combat on your turn, another target creature you control gets +1/+1 until end of turn. If you've scried or surveilled this turn, put a +1/+1 counter on that creature instead.
106365|Emergency Phytomedic|WG|C|Creature - Dryad Cleric|1|{G/W}|0.5304|-0.672|8.3388|5.6011|1695|0.5499|0.5287|This creature enters prepared.
106367|Entrust the Spark|UG|R|Sorcery|5|{3}{G}{U}|||10.1652|6.6686|10|0.0101||You may sacrifice a planeswalker. If you do, search your library for a planeswalker card, put it onto the battlefield, then shuffle.
106368|Fatehold Charm|WU|U|Instant|2|{W}{U}|0.5709|0.468|6.4238|5.0173|1743|0.7127|0.5603|Choose one —\n•Draw a card. Empower Jace 2.\n•Return target spell or creature to its owner's hand.\n•Creatures you control get +1/+2 until end of turn.
106369|Fatehold Chronologist|WU|C|Creature - Bird Wizard|2|{1}{W/U}|0.5696|0.433|5.4588|4.0958|4854|0.915|0.5533|Flying\nThis creature enters prepared.
106371|Ferocity of the Hunt|BG|C|Enchantment - Aura|2|{1}{B/G}|0.5181|-1.017|9.1825|6.0622|1324|0.4243|0.5029|Flash\nEnchant creature\nEnchanted creature gets +1/+0 and has deathtouch.\nWhen enchanted creature dies, return that card to the battlefield tapped under its owner's control.
106372|Frostbite Pyromental|UR|R|Creature - Elemental|3|{U}{R}{R}||||5.2735|180|0.2768||Trample\nHaste\nWhenever this creature deals combat damage to a player, draw two cards.\nAt the beginning of the end step, sacrifice this creature.
106373|Grim Repriser|BR|U|Creature - Zombie Bard|2|{B}{R}|0.5226|-0.892|8.8524|6.2597|731|0.4688|0.5458|Prowess\n{oBoR}: Return this card from your graveyard to the battlefield with a finality counter on it. Activate only if an opponent has been dealt noncombat damage this turn.
106374|Ingris Stingerquill|BR|M|Legendary Creature - Elder Sphinx|3|{B}{R}{R}||||2.092|246|0.793|0.5299|Flying\nWhenever a creature you control attacks, that creature deals 1 damage to each opponent.\n{o4}: Create a 2/2 colorless Wizard Soldier creature token named Cadet. Then creatures you control gain haste until end of turn.
106375|Konstrari Charm|RG|U|Instant|2|{R}{G}|0.5332|-0.591|8.8163|6.2247|707|0.4517|0.5189|Choose one —\n•Konstrari Charm deals 6 damage to target creature with flying.\n•Put two +1/+1 counters on target creature. It gains trample until end of turn.\n•Add {oCoCoC}.
106376|Konstrari Improviser|RG|C|Creature - Human Artificer|2|{1}{R/G}|0.534|-0.569|6.0683|4.4149|2910|0.8284|0.5292|This creature enters prepared.
106378|Kwia Vigorbloom|WG|M|Legendary Creature - Elder Sphinx|6|{3}{G}{W}{W}||||1.5733|391|0.8705|0.5702|Flying\nVigilance\nLifelink\nWard {o2}\nWhenever you gain life, create a colorless artifact token named Lotus with "{oT}, Sacrifice this token: Add three mana of any one color." This ability triggers only once each turn.
106379|Mind Meanderer|UG|U|Creature - Bird Fish Illusion|6|{3}{G}{U}{U}|0.5716|0.49|5.702|4.5924|2038|0.7235|0.5494|Flying\nThis creature has vigilance as long as you control a Jace planeswalker.\nWhen this creature enters, it fights up to one target creature an opponent controls.
106380|Null Summoner|UB|R|Creature - Human Warlock|4|{2}{U}{B}|0.6377|2.348|2.192|2.2251|1035|0.9201|0.5807|When this creature enters, if you cast it, target opponent reveals their hand. You choose a nonland card from it. Exile that card.\nThreshold — As long as there are seven or more cards in your graveyard, you may cast the exiled card, and mana of any type can be spent to cast that spell.
106381|Paradox Shaper|UB|U|Creature - Octopus Wizard|2|{1}{U/B}|0.5498|-0.125|6.9573|5.382|2088|0.7145|0.5414|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.\n{o2}: Put target card from your graveyard on the bottom of your library.
106383|Primal Witchstalker|BG|U|Creature - Wolf|3|{1}{B}{G}|0.602|1.343|5.9076|5.0344|1829|0.6731|0.5682|Menace\nWhen this creature enters, mill four cards. When you do, return target land card from your graveyard to the battlefield tapped.
106384|Proctor of Potential|WU|R|Creature - Human Cleric|2|{W}{U}|0.6187|1.813|3.1455|3.0027|632|0.7762|0.5846|Whenever this creature or another creature you control enters, surveil 1.\n{oWoU}: Return this card from your graveyard to the battlefield with a finality counter on it. Activate only if you've scried or surveilled this turn.
106385|Prudent Fateseer|WU|U|Creature - Dwarf Wizard|3|{1}{W/U}{W/U}|0.549|-0.147|4.9721|3.9069|1969|0.8448|0.5479|This creature enters prepared.\nWhenever you scry or surveil, creatures you control get +1/+0 until end of turn. This ability triggers only once each turn.
106387|Recursive Recruitment|UB|U|Sorcery|4|{2}{U}{B}|0.5982|1.238|3.2326|3.0027|2917|0.9168|0.5639|Create two 2/2 colorless Wizard Soldier creature tokens named Cadet. If this spell was cast from a graveyard, put a +1/+1 counter on each of them for every three cards in your graveyard.\nFlashback {o6oUoB}
106388|Solarium Sentry|WG|R|Creature - Cat Soldier|2|{G}{W}||||3.6161|321|0.6992|0.5223|Whenever an opponent casts a spell with mana value 2 or less, you gain 2 life.
106389|Solitary Cell|WR|R|Artifact|2|{R}{W}||||4.6209|134|0.3212||When this artifact enters, exile target nonland permanent an opponent controls with mana value 3 or less until this artifact leaves the battlefield.\n{o1}, {oT}, Discard a legendary card: Draw a card.
106390|Stingerquill Charm|BR|U|Instant|2|{B}{R}|0.5546|0.01|7.1476|5.1688|1035|0.6566|0.5523|Choose one —\n•Stingerquill Charm deals 3 damage to any target.\n•Target creature gains first strike and deathtouch until end of turn.\n•Create a 2/2 colorless Wizard Soldier creature token named Cadet. It gains haste until end of turn.
106391|Stingerquill Voxmancer|BR|U|Creature - Goblin Sorcerer|1|{B/R}|0.525|-0.823|6.5294|4.6278|920|0.6791|0.5321|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.
106393|Stinging Vitriol|BR|R|Sorcery|2|{B}{R}|||5.7902|4.3582|297|0.5124|0.546|Stinging Vitriol deals 2 damage to target opponent. That player reveals their hand. You choose a nonland card from it. They discard that card.
106394|Tam's Resistance|UG|C|Sorcery|2|{1}{G/U}|0.557|0.078|7.7442|5.4508|3131|0.605|0.544|Put a +1/+1 counter on up to one target creature. It gains vigilance until end of turn.\nEmpower Jace 4.
106395|Tenured Tethermage|RG|R|Creature - Human Artificer|3|{1}{R}{G}|0.5763|0.621|2.4806|2.5729|616|0.8289|0.5523|When this creature enters, you may sacrifice a land. If you do, create two tapped Heartwood tokens.\nTap two untapped artifacts you control: Put two +1/+1 counters on this creature.
106396|Theorix Charm|UB|U|Instant|2|{U}{B}|0.5852|0.872|6.3867|4.929|1854|0.7107|0.5699|Choose one —\n•Counter target noncreature spell unless its controller pays {o2}.\n•Target creature gets -2/-2 until end of turn.\n•Mill three cards, then draw a card.
106397|Theorix Metamage|UB|C|Creature - Shade Wizard|3|{2}{U/B}|0.5488|-0.154|9.1675|6.5952|2768|0.506|0.5422|This creature enters prepared.\nThreshold — This creature gets +1/+0 and has flying as long as there are seven or more cards in your graveyard.
106399|Twinned Vision|UR|C|Instant|2|{1}{U/R}|0.5824|0.791|5.9772|4.539|5179|0.8671|0.5707|Draw a card. If this spell wasn't cast from your hand, draw two cards instead.\nFlashback—{o1o(U/R)o(U/R)}, Discard a card.
106400|Twisted Fates|WB|U|Sorcery|5|{2}{W}{W}{B}|0.5577|0.097|6.0696|4.8037|1413|0.666|0.5421|Destroy target nonland permanent. Put a +1/+1 counter on each creature target player controls.
106401|Uldaros Theorix|UB|M|Legendary Creature - Elder Sphinx|6|{3}{U}{B}{B}|||||469|0.9193|0.5979|Flying\nWhen Uldaros Theorix enters, if you cast him, exile up to one target nonland card of each card type from your graveyard. Copy those cards. You may cast any number of spells with total mana value 6 or less from among the copies without paying their mana costs. (Permanent spells cast this way become tokens.)
106402|Vigorbloom Charm|WG|U|Instant|2|{G}{W}|0.5726|0.518|6.6615|4.9653|1060|0.6601|0.542|Choose one —\n•Target permanent you control gains hexproof and indestructible until end of turn.\n•You draw a card and gain 3 life.\n•Put a +1/+1 counter on target creature you control. Then it fights target creature an opponent controls.
106403|Vigorbloom Vanguard|WG|U|Creature - Troll Druid|2|{1}{G/W}|0.5497|-0.128|4.8324|3.5426|1368|0.8792|0.5334|This creature enters prepared.\nEach creature you control with a +1/+1 counter on it has vigilance.
106405|Vindictive Triumph|WB|R|Instant|3|{W}{B}{B}|||5.101|4.1364|375|0.6246|0.5274|Exile target creature or planeswalker. If that permanent's mana value was 3 or less, return it to the battlefield tapped under your control. Exile it at the beginning of the next end step.
106406|Warrior's Blades|WR|U|Artifact - Equipment|4|{2}{R}{W}|0.5563|0.059|6.7626|4.8872|861|0.5608|0.5337|When this Equipment enters, it deals 3 damage to any target and you gain 3 life.\nEquipped creature gets +2/+1.\nEquip {o3}. This ability costs {o1} less to activate for each +1/+1 counter on the creature it targets.
106407|Whiplash Wordsmith|BR|C|Creature - Vampire Sorcerer|4|{3}{B/R}|0.5198|-0.969|10.7994|7.2306|606|0.2312|0.5203|This creature enters prepared.\nAs long as an opponent was dealt noncombat damage this turn, this creature has flying and haste.
106409|Woodwork Prodigy|RG|U|Creature - Cat Druid|3|{2}{R/G}|0.5194|-0.981|4.6926|3.5956|1134|0.8281|0.5067|At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.
106411|Afterthought Sentry||C|Artifact Creature - Gargoyle|2|{2}|0.5244|-0.841|11.223|7.8579|698|0.1695|0.5248|{o2}: This creature gains flying until end of turn.\nWhenever this creature attacks, exile up to one target card from a graveyard.
106412|Archive Arbiter||U|Artifact Creature - Sphinx|6|{6}|0.5597|0.153|7.4963|5.3161|1324|0.6816|0.5256|Flying\nWhen this creature enters, choose one —\n•Destroy target noncreature, nonland permanent.\n•You gain 4 life.
106413|Codie, Ravenous Codex||R|Legendary Artifact Creature - Book Construct|3|{3}|||9.237|6.1192|52|0.0805||Whenever you cast a prepare spell, copy it. You may choose new targets for the copy.\n{oWoUoBoRoG}, {oT}: Each creature you control becomes prepared. (Only creatures with prepare spells can become prepared.)
106414|The Echoverse Fulcrum||M|Legendary Artifact|2|{2}||||1.6522|396|0.9318|0.5316|When The Echoverse Fulcrum enters, draw a card, then discard a card.\n{o5}, {oT}, Exile The Echoverse Fulcrum: Destroy all creatures. Activate only as a sorcery.
106415|Eye of Jace||U|Artifact|1|{1}|||10.1688|6.7568|334|0.228|0.4893|At the beginning of your upkeep, surveil 1. Then if there are seven or more cards in your graveyard, sacrifice this artifact, it deals 2 damage to each opponent, and you gain 2 life.
106416|Keeper of the Quiet Hour||C|Artifact Creature - Chimera|3|{3}|0.5662|0.336|8.6559|6.2139|3264|0.6096|0.5581|When this creature enters, empower Jace 2.
106417|Living Library||C|Artifact Creature - Book Illusion|2|{2}|0.4892|-1.832|11.8539|8.3837|507|0.1252|0.5061|{o6}, Sacrifice this creature: Choose target creature or planeswalker an opponent controls. Its owner shuffles it into their library.
106418|Medic's Kitesail||C|Artifact - Equipment|2|{2}|0.524|-0.852|10.4433|7.106|1105|0.3192|0.5135|Equipped creature gets +1/+0 and has flying and "Whenever this creature attacks, you gain 1 life."\nEquip {o2}
106419|Murmuring Volume||C|Artifact - Book|3|{3}|0.5607|0.183|7.8625|5.6804|3663|0.7074|0.5503|{oT}: Add one mana of any color.\n{o2}, {oT}, Discard a card: Draw a card.
106420|Dedicated Commons||C|Land|||0.5248|-0.829|9.8122|6.7316|1008|0.4396|0.5294|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oR} or {oW}.
106421|Deserted Beach||R|Land|||||6.081|4.2118|487|0.6601|0.5598|This land enters tapped unless you control two or more other lands.\n{oT}: Add {oW} or {oU}.
106422|Fatehold Annex||C|Land|||0.5554|0.034|8.5559|5.9951|1921|0.7043|0.5489|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oW} or {oU}.
106423|Formidable Commons||C|Land|||0.5443|-0.28|8.7323|6.2152|2179|0.6966|0.5508|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oB} or {oG}.
106424|Hall of Echoes||R|Land|||||7.4059|4.949|302|0.5661|0.5094|{oT}: Add {oC}.\n{o5}: This land becomes a copy of target creature you control until end of turn. The "legend rule" doesn't apply to permanents you control this turn.
106425|Haunted Ridge||R|Land|||||6.7149|4.6244|386|0.5664|0.5613|This land enters tapped unless you control two or more other lands.\n{oT}: Add {oB} or {oR}.
106426|Hexhaven Dueling Arena||U|Land|||||11.6492|8.0102|159|0.1108||{oT}: Add {oC}.\n{o2}, {oT}: Target creature that attacked this turn becomes prepared. Activate only as a sorcery. (Only creatures with prepare spells can become prepared.)\n{o4}, {oT}: Target creature becomes prepared.
106427|Innovative Commons||C|Land|||0.5739|0.554|8.5902|6.0985|1880|0.6109|0.5572|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oU} or {oR}.
106428|Konstrari Annex||C|Land|||0.5302|-0.677|8.8446|6.1986|1524|0.657|0.5318|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oR} or {oG}.
106429|Meticulous Commons||C|Land|||0.5418|-0.352|9.2278|6.5614|1401|0.5417|0.5383|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oW} or {oB}.
106430|Overgrown Farmland||R|Land||||||4.5945|322|0.5799|0.5007|This land enters tapped unless you control two or more other lands.\n{oT}: Add {oG} or {oW}.
106431|Rockfall Vale||R|Land|||||6.472|4.3841|449|0.7006|0.5442|This land enters tapped unless you control two or more other lands.\n{oT}: Add {oR} or {oG}.
106432|Roiling Canopy||R|Land||||||5.5622|97|0.228||This land enters tapped.\nWhenever a Forest you control enters, if you control at least five other Forests, target creature you control gets +3/+3 until end of turn.\n{oT}: Add {oG}.
106433|Room of Refuge||C|Land|||0.5672|0.365|6.9845|4.9246|5238|0.9524|0.5575|This land enters tapped. As it enters, choose a color.\n{oT}: Add one mana of the chosen color.\n{o5}, {oT}, Sacrifice this land: Put two +1/+1 counters on target creature. Activate only as a sorcery.
106434|Shipwreck Marsh||R|Land|||0.5598|0.156|5.5268|4.1248|736|0.7782|0.5515|This land enters tapped unless you control two or more other lands.\n{oT}: Add {oU} or {oB}.
106435|Stingerquill Annex||C|Land|||0.5396|-0.413|9.0966|6.3821|1516|0.5855|0.5568|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oB} or {oR}.
106436|Theorist's Sanctum||R|Land - Island|||0.5875|0.937|2.6854|2.5266|834|0.8827|0.5582|As this land enters, you may behold a Jace. If you don't, this land enters tapped.\n{o2oU}, {oT}: Empower Jace 2.\n{oT}: Add {oU}.
106437|Theorix Annex||C|Land|||0.5724|0.512|7.9745|5.722|2706|0.7813|0.5621|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oU} or {oB}.
106438|Transformative Commons||C|Land|||0.5475|-0.19|8.851|6.2548|2011|0.6378|0.5454|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oG} or {oU}.
106439|Vigorbloom Annex||C|Land|||0.5284|-0.727|9.1437|6.4209|1302|0.5662|0.5322|This land enters tapped unless you control a planeswalker.\n{oT}: Add {oG} or {oW}.
106440|Ajani Resolute|W|M|Legendary Planeswalker - Ajani|2|{1}{W}||||2.3696|147|||Whenever you gain life, put a loyalty counter on Ajani.\nYou gain 1 life.\nCreate a 2/2 white Cat Soldier creature token named Ajani's Pridemate with "Whenever you gain life, put a +1/+1 counter on this token."\nYou get an emblem with "Creatures you control get +2/+2."
106441|Danitha, Sword of Hope|W|U|Legendary Creature - Human Knight|3|{2}{W}|||9.1542|6.2693|476|0.3436|0.5185|First strike\nWhenever you cast an Equipment spell or a spell that targets a creature you control, draw a card. This ability triggers only once each turn.
106442|Ghalta the Immovable|W|U|Legendary Creature - Elder Dinosaur|9|{8}{W}|||10.0037|6.7494|308|0.2017|0.4841|This spell costs {oX} less to cast, where X is the greatest toughness among creatures you control.\nCreatures you control can attack as though they didn't have defender.\nEach creature you control with toughness greater than its power assigns combat damage equal to its toughness rather than its power.
106443|Gideon's Memorial|W|R|Legendary Artifact|2|{1}{W}||||3.982|337|0.6297|0.4993|Creature tokens you control get +1/+0 and have vigilance.\n{oT}: Add one mana of any color. Spend this mana only to cast a planeswalker spell.\n{o1oW}, Discard this card: It deals 4 damage to target attacking or blocking creature.
106444|Koth of the Homestead|W|U|Legendary Creature - Human Citizen|3|{2}{W}|0.5276|-0.75|4.7852|3.7131|906|0.7643|0.5257|Landfall — Whenever a land you control enters, you gain 1 life.\nWhenever a Plains you control enters, put a +1/+1 counter on target creature.
106445|Liliana the Faultless|W|R|Legendary Creature - Human Cleric|1|{W}|0.5825|0.796|2.7358|2.4602|527|0.875|0.5226|Whenever another creature or planeswalker you control enters, you gain 1 life.\n{o1}, {oT}, Discard a card: Another target creature or planeswalker you control gains hexproof until end of turn.
106446|Lyra, Archangel of Dawn|W|R|Legendary Creature - Angel Knight|3|{2}{W}|0.5604|0.173|2.7902|2.3792|530|0.895|0.5413|Flying\nWhenever you gain life, put a +1/+1 counter on each Angel you control.
106447|Rescue Girl, First Responder|W|U|Legendary Creature - Human Cleric|3|{2}{W}|0.5073|-1.32|6.4127|4.7651|1157|0.6226|0.5153|Flying\n{oT}: Return another target permanent you control to its owner's hand. Activate only during your turn.
106448|Saheeli, Consul of Oversight|W|U|Legendary Creature - Human Advisor|5|{3}{W}{W}|0.5667|0.35|5.4076|4.1533|1320|0.7413|0.5561|Flying\nWhenever you scry or surveil, create a 1/1 colorless Thopter artifact creature token with flying. This ability triggers only once each turn.
106449|Teyo, Lightshield Expert|W|U|Legendary Creature - Human Cleric|2|{1}{W}|0.5727|0.521|5.8279|4.3248|1306|0.793|0.5468|Flash\nWhen Teyo enters, target permanent you control gains hexproof until end of turn. Put a +1/+1 counter on it if it's a creature. Put a loyalty counter on it if it's a planeswalker.
106450|Thalia, the Survivor|W|U|Legendary Creature - Human Soldier|4|{3}{W}|0.5328|-0.604|6.0649|4.4031|1006|0.7219|0.5277|Lifelink\nNoncreature spells your opponents cast cost {o1} more to cast.
106451|Tomik, Orzhov Lawmage|W|U|Legendary Creature - Human Advisor|2|{1}{W}|0.5531|-0.032|6.681|4.8433|1083|0.7315|0.544|Flying\nPlaneswalkers you control have "No more than one creature can attack this planeswalker each combat."\n{oT}: Target creature with a +1/+1 counter on it gains flying until end of turn.
106452|Way of the Healer|W|U|Legendary Enchantment|4|{3}{W}|0.6005|1.303|3.6336|3.2211|1890|0.8373|0.5588|When Way of the Healer enters, empower Jace 5.\nPlaneswalkers you control have "[-2]: Create a 2/2 colorless Wizard Soldier creature token named Cadet. Surveil 1."
106453|Way of the Mentor|W|U|Legendary Enchantment|3|{2}{W}|||8.3812|5.5748|389|0.3941|0.4778|When Way of the Mentor enters, empower Jace 5.\nWhenever you gain life, put a loyalty counter on each planeswalker you control.
106454|Yoshimaru, Beloved Companion|W|U|Legendary Creature - Dog|3|{2}{W}|0.4706|-2.355|7.537|5.1678|612|0.5551|0.4986|If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead.\n{o6}: Put a +1/+1 counter on target legendary creature.
106455|Yuriko, Blade of the Mighty|W|U|Legendary Creature - Human Samurai|4|{3}{W}|0.5568|0.071|7.5529|5.2773|546|0.4625|0.5082|During combat, players can't cast spells or activate abilities that aren't mana abilities.\nWhenever a creature you control attacks a player alone, it gains double strike until end of turn.
106456|Arni, Humble Scribe|U|U|Legendary Creature - Human Wizard|3|{2}{U}|0.5324|-0.615|6.691|5.0065|1544|0.6883|0.532|Whenever another nontoken creature you control enters, untap Arni.\n{oT}: Draw a card, then discard a card.
106457|Chandra, Chill of Compliance|U|M|Legendary Planeswalker - Chandra|3|{1}{U}{U}||||1.7383|486|0.9565|0.5594|Surveil 1. If you put a noncreature, nonland card into your graveyard this way, put that card into your hand.\nAdd {oU}. Spend this mana only to cast a noncreature spell.\nTap target artifact or creature. Put X stun counters on it.\nYou get an emblem with "Whenever you cast a spell, draw a card."
106458|Fblthp, Impossibly Lost|U|U|Legendary Creature - Homunculus|2|{1}{U}|0.5965|1.19|4.074|3.5917|3237|0.896|0.5536|When one or more of your opponents are dealt combat damage during your turn, draw two cards. If your library has no cards in it, you win the game. Fblthp's owner shuffles him into their library. (If you draw from an empty library this way, you still win the game.)
106459|Geist of Saint Thalia|U|U|Legendary Creature - Spirit Cleric|2|{1}{U}|0.5708|0.466|4.8658|3.9646|2020|0.8604|0.5654|Flying\nNoncreature spells you cast cost {o1} less to cast.
106460|Hapatra, the Desert Frost|U|U|Legendary Creature - Human Wizard|4|{3}{U}|0.5667|0.35|7.844|5.4287|840|0.4874|0.5308|When Hapatra enters, for each opponent, tap up to one target creature that player controls. Put a stun counter on each of those creatures.\n{o2oU}: Untap target creature.
106461|Jace, Reality Sculptor|U|R|Legendary Planeswalker - Jace|5|{3}{U}{U}|0.5841|0.839|1.8487|1.8241|803|0.9081|0.5442|Empower Jace X, where X is the number of Islands you control.\nUntil your next turn, whenever a creature attacks you or a planeswalker you control, it gets -5/-0 until end of turn.\nExile all but the bottom card of each opponent's library. Activate only if there are twenty-five or more loyalty counters among Jaces you control.
106462|Lyra, Tolarian Archangel|U|R|Legendary Creature - Angel Wizard|3|{1}{U}{U}|0.6158|1.732|1.9316|1.8671|989|0.9583|0.5678|Flying\nAt the beginning of each end step, if you've drawn three or more cards this turn, create a 3/3 blue Angel creature token with flying.\n{o3oUoU}: Until end of turn, whenever Lyra deals combat damage to a player, draw two cards.
106463|Proft, Consulting Detective|U|U|Legendary Creature - Human Detective|2|{1}{U}|0.5699|0.441|4.2078|3.531|2439|0.9003|0.5638|Whenever you scry or surveil, you may pay {o2}. If you do, put a +1/+1 counter on Proft and draw a card. (Draw after you scry or surveil.)
106464|Ruric Thar, Biomagus|U|U|Legendary Creature - Ogre Crab Wizard|6|{4}{U}{U}|0.5545|0.006|4.6143|3.7085|1643|0.8504|0.5333|Flying\nProwess\nWhenever Ruric Thar becomes the target of a spell or ability an opponent controls, draw a card.
106465|Samut, Tyrant of Naktamun|U|R|Legendary Creature - Human Wizard|2|{1}{U}||||5.451|105|0.1978||Instant and sorcery spells you control have split second.
106466|Tetsuko Umezawa, Fugitive|U|U|Legendary Creature - Human Rogue|2|{1}{U}|0.5424|-0.332|7.7843|5.7215|1296|0.5692|0.5241|Creatures you control with power or toughness 1 or less can't be blocked.
106467|Traxos, Academy Guardian|U|U|Legendary Artifact Creature - Dragon Construct|4|{3}{U}|0.5468|-0.209|5.8199|4.3122|1719|0.809|0.5453|This spell costs {o2} less to cast if you've cast a noncreature spell this turn.\nFlying\nVigilance\nProwess
106468|Way of the Cryomancer|U|U|Legendary Enchantment|3|{2}{U}|0.5222|-0.902|7.7551|5.3732|923|0.5095|0.5216|When Way of the Cryomancer enters, empower Jace 5.\nPlaneswalkers you control have "[-3]: When you next cast an instant or sorcery spell this turn, copy that spell. You may choose new targets for the copy."
106469|Way of the Mind Sculptor|U|U|Legendary Enchantment|5|{4}{U}|0.5836|0.826|3.125|2.9444|2656|0.905|0.5554|When Way of the Mind Sculptor enters, empower Jace 5.\nWhenever you activate a loyalty ability, if you removed two or more loyalty counters to activate it, draw a card.
106470|Yargle, Goliath of Otaria|U|U|Legendary Creature - Frog Spirit|5|{4}{U}|||11.5328|7.8015|62|0.0339||
106471|Yuriko, Hope from the Shadows|U|U|Legendary Creature - Human Ninja|1|{U}|0.5439|-0.29|9.2447|6.2526|717|0.3666|0.5332|Flash\nWhen Yuriko enters, choose one —\n•Target creature gets -X/-0 until end of turn, where X is the number of cards in your graveyard.\n•Surveil 2.
106472|Danitha, Spear of Agony|B|U|Legendary Creature - Human Knight|3|{2}{B}|0.4792|-2.112|7.5401|5.2438|649|0.4984|0.4923|First strike\nWhenever you cast a spell that targets an opponent or a creature an opponent controls, put a +1/+1 counter on Danitha.
106473|Gallia, Tragic Host|B|U|Legendary Creature - Zombie Satyr|2|{1}{B}|0.5493|-0.138|6.4496|4.8419|1591|0.772|0.5535|Menace\n{o4oB}, Exile another creature card from your graveyard: Return this card from your graveyard to the battlefield tapped with a +1/+1 counter on her.
106474|Garruk, Veiled Butcher|B|M|Legendary Planeswalker - Garruk|5|{3}{B}{B}||||1.4585|422|0.9613|0.5687|If a creature an opponent controls would die, exile it instead.\nUp to one target creature gets -4/-1 until your next turn.\nEach player sacrifices a creature of their choice. If you sacrificed a creature this way, create a 4/4 green Beast creature token with trample.\nEach opponent discards two cards. For each opponent who didn't discard two nonland cards this way, you draw a card.
106475|Gideon the Oathless|B|R|Legendary Creature - Human Mercenary|3|{2}{B}|0.5556|0.037|2.2996|2.2165|756|0.9086|0.5328|Ward—Discard a card.\nWhenever a creature an opponent controls enters, Gideon deals 1 damage to that player.\nWhenever an opponent activates a loyalty ability, Gideon deals 1 damage to that player.
106476|Liliana the Repentant|B|R|Legendary Creature - Human Warlock|2|{1}{B}|0.5572|0.082|3.3935|2.867|761|0.8476|0.5665|Whenever another creature or planeswalker you control enters, mill two cards.\nExhaust — {o5oB}: Return target creature or planeswalker card from your graveyard to the battlefield. Put a +1/+1 counter on Liliana. Activate only as a sorcery.
106477|Loot, the Anomaly|B|U|Legendary Creature - Beast Horror|3|{2}{B}|||11.2631|7.6046|165|0.1007||If Loot's power is negative, he assigns combat damage as though his power were positive.\nThreshold — Sacrifice another creature or planeswalker: Loot gets -2/-0 until end of turn. Activate only if there are seven or more cards in your graveyard.
106478|Mabel, Bitter Recluse|B|U|Legendary Creature - Mouse Warlock|1|{B}|0.5372|-0.48|7.8221|5.3254|1022|0.6105|0.5399|Deathtouch\nWhen Mabel enters, remove up to three counters from another target creature or planeswalker.
106479|Massacre Girl, Most Wanted|B|U|Legendary Creature - Human Assassin|5|{4}{B}|0.4782|-2.141|7.5176|5.1523|665|0.536|0.501|Whenever another creature or planeswalker you control dies, Massacre Girl deals 1 damage to target opponent and you gain 1 life.\nWhenever an opponent is dealt noncombat damage, put a +1/+1 counter on Massacre Girl.
106480|Proft, Sinister Mastermind|B|U|Legendary Creature - Human Rogue|3|{2}{B}|0.5491|-0.146|7.1851|5.212|1284|0.6702|0.5319|Threshold — You can't cast this spell unless there are seven or more cards in your graveyard.\nMenace\n{oB}, Discard this card: Target creature gets -3/-1 until end of turn.
106481|Teyo, Diamondblade Mage|B|U|Legendary Creature - Human Warlock|4|{3}{B}|||9.2981|6.2087|488|0.35|0.4991|Flash\nWhen Teyo enters, target permanent you control gains deathtouch until end of turn. Put a +1/+1 counter on it if it's a creature. Put a loyalty counter on it if it's a planeswalker.
106482|Tinybones, Pocket Nuisance|B|U|Legendary Creature - Skeleton Rogue|3|{2}{B}|0.5275|-0.753|7.005|4.8791|1109|0.6374|0.513|When Tinybones enters, each opponent discards a card.\nWhenever a player discards one or more cards, Tinybones deals 1 damage to each opponent.
106483|Way of the Deathbringer|B|U|Legendary Enchantment|3|{2}{B}|||8.6605|5.9088|424|0.311|0.4815|When Way of the Deathbringer enters, empower Jace 5.\nPlaneswalkers you control have "[-2]: You may sacrifice a creature. If you do, create a 4/4 green Beast creature token with trample."
106484|Way of the Necromancer|B|U|Legendary Enchantment|2|{1}{B}|||11.2318|7.3773|97|0.0682||When Way of the Necromancer enters, empower Jace 2.\nWhenever a creature you control dies, put a loyalty counter on each planeswalker you control.
106485|Winter, Tormented Loner|B|U|Legendary Creature - Human Warlock|3|{2}{B}|0.5173|-1.041|7.211|5.18|984|0.5887|0.5252|When Winter enters, you may sacrifice a creature or planeswalker. When you do, each opponent sacrifices a creature of their choice.\nWinter gets +1/+0 for each creature and planeswalker card in your graveyard.
106486|Yargle, Glutton of Urborg|B|U|Legendary Creature - Frog Spirit|5|{4}{B}|||12.3097|8.2985|26|0.0137||
106487|Ajani Unrelenting|R|M|Legendary Planeswalker - Ajani|6|{4}{R}{R}|||||483|0.9968|0.6598|Whenever you activate a loyalty ability, create a 2/2 colorless Wizard Soldier creature token named Cadet.\nCreatures you control get +1/+0 and gain haste until end of turn.\nDiscard your hand, then draw a card for each creature you control.\nAjani deals 4 damage to each creature except for tokens you control.
106488|Arni, Renowned Champion|R|U|Legendary Creature - Human Berserker|4|{3}{R}|||10.9319|7.1672|167|0.1527||Trample\nWhenever another creature you control enters, Arni gets +X/+0 until end of turn, where X is that creature's power.
106489|Chandra, Torch of Defiance|R|M|Legendary Planeswalker - Chandra|4|{2}{R}{R}|||||357|0.9407|0.5671|Exile the top card of your library. You may cast that card. If you don't, Chandra deals 2 damage to each opponent.\nAdd {oRoR}.\nChandra deals 4 damage to target creature.\nYou get an emblem with "Whenever you cast a spell, this emblem deals 5 damage to any target."
106490|Gallia, the Merrymaker|R|U|Legendary Creature - Satyr|2|{1}{R}|0.5081|-1.3|8.6478|5.8462|559|0.4601|0.5428|Haste\nEach other creature you control with a +1/+1 counter on it has haste.\n{o1oR}, {oT}: Put a +1/+1 counter on target creature that entered this turn.
106491|Jiang Yanggu, Alone|R|U|Legendary Creature - Human Berserker|5|{4}{R}|0.4923|-1.744|9.1553|6.157|583|0.4156|0.5022|Menace\nWhenever a creature you control attacks a player alone, discard a card, then draw a card. Then put a +1/+1 counter on that creature for each card you've discarded this turn.
106492|Kiora of Fire and Ashes|R|U|Legendary Creature - Merfolk Noble|6|{4}{R}{R}|0.6128|1.649|2.197|2.1276|2304|0.9259|0.5644|When Kiora enters, create a 5/5 red Dragon creature token with flying.\n{o8}: Create a 5/5 red Dragon creature token with flying.
106493|Koth, the Geomancer|R|U|Legendary Creature - Human Warrior|3|{2}{R}|0.5393|-0.421|7.0578|4.8782|827|0.6539|0.525|Reach\nLandfall — Whenever a land you control enters, Koth deals 1 damage to each opponent. If that land is a Mountain, add {oR}.
106494|Marwyn, the Clearcutter|R|U|Legendary Creature - Elf Warrior|1|{R}|0.5502|-0.113|8.6927|6.0285|936|0.5537|0.5645|{o2}, {oT}, Sacrifice an artifact or land: Draw a card.
106495|Pia, Determined Rebuilder|R|U|Legendary Creature - Human Artificer|3|{2}{R}|0.5542|-0.001|5.7724|4.4614|1467|0.7725|0.5495|When Pia enters, create a 1/1 colorless Thopter artifact creature token with flying.\n{o5oR}: Target creature gets +X/+0 until end of turn, where X is the number of artifacts you control.
106496|Samut, Hazoret's Champion|R|R|Legendary Creature - Human Warrior Cleric|2|{1}{R}|0.5637|0.265|3.6604|2.9706|534|0.8646|0.5442|Creatures you control have haste.
106497|Tetsuko Umezawa, Pursuer|R|U|Legendary Creature - Human Mercenary|4|{3}{R}|0.5183|-1.011|7.875|5.3851|627|0.5554|0.5335|Double strike\nProwess\nWhenever a creature an opponent controls with power or toughness 1 or less blocks, Tetsuko Umezawa deals 1 damage to that creature's controller.
106498|Tomik, Izzet Sparkmage|R|U|Legendary Creature - Human Wizard|2|{1}{R}|0.5061|-1.355|7.2624|5.1667|818|0.598|0.5215|Prowess\nIf a source you control would deal noncombat damage to an opponent or a permanent an opponent controls, it deals that much damage plus 1 instead.
106499|Way of the Pyromancer|R|U|Legendary Enchantment|2|{1}{R}|0.497|-1.612|6.0898|4.4352|831|0.6387|0.4982|When Way of the Pyromancer enters, empower Jace 2.\nPlaneswalkers you control have "[+1]: Add {oR}."
106500|Way of the Warlord|R|U|Legendary Enchantment|3|{2}{R}|0.5211|-0.934|6.4187|4.6097|950|0.6365|0.5166|When Way of the Warlord enters, empower Jace 5.\nPlaneswalkers you control have "[-4]: This planeswalker deals 2 damage to up to one target creature or planeswalker and 2 damage to target player."
106501|Winter, Team Player|R|U|Legendary Creature - Human Warrior|5|{4}{R}|||10.6142|7.2153|281|0.2225|0.5051|Convoke\nWhenever you cast a noncreature spell, creatures you control get +1/+0 until end of turn.
106502|Edgar, Moonlit Sovereign|G|U|Legendary Creature - Werewolf Noble|5|{3}{G}{G}|0.4642|-2.536|7.3597|4.9803|558|0.5013|0.4772|Flash\nAt the beginning of your end step, if you didn't cast a spell this turn, put two +1/+1 counters on Edgar.\n{o4oG}: Put a +1/+1 counter on each creature you control with a +1/+1 counter on it.
106503|Fblthp, Knows the Way|G|U|Legendary Creature - Homunculus Scout|2|{X}{G}{G}|0.5861|0.897|4.4826|3.7849|1829|0.8304|0.5626|Domain — Fblthp's power is equal to the number of basic land types among lands you control.\nWhen Fblthp enters, search your library for up to X basic land cards with different names, reveal them, put them into your hand, then shuffle.
106504|Garruk, Curse Breaker|G|M|Legendary Planeswalker - Garruk|5|{3}{G}{G}||||1.5294|425|0.9795|0.5925|Whenever a creature you control with power 4 or greater enters, draw a card.\nUntap up to two target lands.\nCreate a 4/4 green Beast creature token with trample.\nUntil your next turn, whenever one or more creatures attack one of your opponents, those creatures get +2/+2 and gain trample until end of turn.
106505|Ghalta the Unstoppable|G|U|Legendary Creature - Elder Dinosaur|9|{8}{G}|0.4984|-1.572|5.6291|4.1729|935|0.7238|0.4998|This spell costs {oX} less to cast, where X is the greatest power among creatures you control.\nTrample\nOther creatures you control have trample.
106506|Jiang Yanggu, Never Alone|G|U|Legendary Creature - Human Druid|4|{3}{G}|0.5456|-0.244|5.4681|4.2034|1393|0.7987|0.5337|When Jiang Yanggu enters, create Mowu, a legendary 3/3 green Dog creature token.\nAt the beginning of your end step, untap all tokens you control.
106507|Loot, the Nexus|G|U|Legendary Creature - Beast Noble|3|{2}{G}|0.5181|-1.018|6.3985|4.5561|996|0.7012|0.5316|{oT}: Choose a color. Add one mana of that color for each different power among creatures you control.
106508|Marwyn, the Preserver|G|U|Legendary Creature - Elf Druid|2|{1}{G}|0.5281|-0.735|7.7107|5.4831|1066|0.6322|0.5412|Lands you control have hexproof.\n{o2}: Return target land card from your graveyard to your hand.
106509|Pia, Aether Ascetic|G|U|Legendary Creature - Human Druid|3|{2}{G}|||10.3173|7.0113|301|0.1938|0.5087|When Pia enters, you may discard a card. If you do, search your library for an enchantment card, reveal it, put it into your hand, then shuffle.
106510|Ruric Thar, Magecrusher|G|U|Legendary Creature - Ogre Warrior|7|{5}{G}{G}|0.5336|-0.582|5.5451|4.1945|1117|0.7846|0.5156|This spell can't be countered.\nReach\nVigilance\nTrample\nRuric Thar has hexproof as long as they haven't dealt combat damage yet.
106511|Titanbones, Towering Heart|G|U|Legendary Creature - Skeleton Druid|4|{3}{G}|0.4919|-1.756|6.7572|4.6853|799|0.7072|0.5014|Reach\nWhenever you gain life, put two +1/+1 counters on Titanbones.\nWhen you discard this card, you gain 3 life.
106512|Way of the Paradox|G|U|Legendary Enchantment|3|{2}{G}|0.5567|0.068|6.2799|4.6427|1218|0.7029|0.5365|When Way of the Paradox enters, empower Jace 5.\nWhenever you activate a loyalty ability, you gain 1 life. You may play an additional land this turn.
106513|Way of the Wildspeaker|G|U|Legendary Enchantment|5|{4}{G}|0.5309|-0.657|4.3011|3.4526|1407|0.8359|0.5298|When Way of the Wildspeaker enters, empower Jace 7.\nPlaneswalkers you control have "[-4]: Create a 4/4 green Beast creature token with trample."
106514|Yoshimaru, Scrappy Stray|G|U|Legendary Creature - Dog|2|{1}{G}|0.5353|-0.534|5.8776|4.4035|1233|0.7898|0.5132|When Yoshimaru enters, another target creature you control fights up to one target creature an opponent controls.\n{o6}: Put a +1/+1 counter on target nonlegendary creature.
106515|Edgar, Ancient Bloodlord|WB|U|Legendary Creature - Vampire Noble|2|{W}{B}|||9.7543|6.6716|383|0.2593|0.5424|Whenever another creature or planeswalker you control dies, you gain 1 life.\n{o2}, Sacrifice another creature or planeswalker: Put a +1/+1 counter on Edgar. He gains menace until end of turn.
106516|Hapatra, the Desert Fang|BG|U|Legendary Creature - Human Cleric|5|{2}{B}{B}{G}|0.589|0.979|4.5198|4.0062|2241|0.8007|0.5601|When Hapatra enters, for each opponent, put X -1/-1 counters on up to one target creature that player controls, where X is the greatest mana value among cards in your graveyard.
106517|Karn, Gilded Guardian|WUBRG|R|Legendary Artifact Creature - Golem|5|{2/W}{2/U}{2/B}{2/R}{2/G}|||9.7796|6.3599|56|0.0713||Vigilance\nTrample\nWhen Karn enters, draw a card for each color among other artifacts you control.
106518|Kiora of Salt and Sand|UG|U|Legendary Creature - Merfolk Noble|3|{1}{G}{U}|0.5434|-0.306|9.2429|6.5237|611|0.3062|0.5133|Whenever you attack, if you've activated a loyalty ability this turn, untap target attacking creature. It can't be blocked this turn.\nPlaneswalkers you control have "[-8]: Create an 8/8 blue Leviathan creature token with hexproof."
106519|Mabel, Valley Hero|WR|U|Legendary Creature - Mouse Soldier|3|{1}{R}{W}|||8.6565|6.1045|429|0.3523|0.5112|Whenever Mabel or another creature you control enters, put a +1/+1 counter on target creature that entered this turn.
106520|Saheeli, Jewel of Avishkar|UR|U|Legendary Creature - Human Artificer|4|{2}{U}{R}|0.5726|0.517|5.5131|4.2832|1591|0.708|0.5424|Thopters you control have haste.\nWhenever you cast a noncreature spell, create a 1/1 colorless Thopter artifact creature token with flying.
106521|Tam, the Possibility|UG|R|Legendary Creature - Gorgon Wizard|3|{1}{G}{U}|||10.3067|6.6292|4|0.006||Planeswalker spells you cast cost {o1} less to cast.\n{oWoUoBoRoG}, {oT}: Proliferate X times, where X is the number of planeswalker types among planeswalkers you control.
106522|Vraska, Soul of Stone|WUR|R|Legendary Creature - Gorgon Wizard|3|{U}{R}{W}|||6.7983|4.8081|260|0.3663|0.5198|Artifact creatures you control have vigilance.\nWhenever you cast a noncreature spell, create a 1/1 colorless Sculpture Treasure artifact creature token with "{oT}, Sacrifice this token: Add one mana of any color."
106523|Vraska, the Cutting Glare|BG|R|Legendary Creature - Gorgon Assassin|3|{B}{B}{G}|0.6398|2.409|2.8989|2.7457|758|0.8282|0.5699|Deathtouch\nWhen Vraska enters, if you control six or more lands, destroy target permanent an opponent controls. They create a Treasure token.
106524|Karn, Argent Defender||R|Legendary Artifact Creature - Golem|2|{2}||||5.89|44|0.0717||Artifacts and creatures entering the battlefield don't cause abilities to trigger.
106525|Traxos, Scourge Eternal||U|Legendary Artifact Creature - Dragon Construct|4|{4}|||9.9018|6.4221|429|0.2957|0.5144|Trample\nTraxos doesn't untap during your untap step.\nWhenever you cast an artifact or creature spell, untap Traxos.
106566|Eye of Ugin||M|Legendary Land|||||||0|||Colorless Eldrazi spells you cast cost {o2} less to cast.\n{o7}, {oT}: Search your library for a colorless creature card, reveal it, put it into your hand, then shuffle.
106567|Austere Command|W|R|Sorcery|6|{4}{W}{W}|||||79|||Choose two —\n•Destroy all artifacts.\n•Destroy all enchantments.\n•Destroy all creatures with mana value 3 or less.\n•Destroy all creatures with mana value 4 or greater.
106568|Flesh Duplicate|U|R|Creature - Shapeshifter Rebel|2|{U}{U}|||||39|||You may have this creature enter as a copy of any creature on the battlefield, except it has vanishing 3 if that creature doesn't have vanishing.
106569|Consign to Memory|U|U|Instant|1|{U}|||||0|||Replicate {o1}\nCounter target triggered ability or colorless spell.
106570|Sublime Epiphany|U|R|Instant|6|{4}{U}{U}|||||122|||Choose one or more —\n•Counter target spell.\n•Counter target activated or triggered ability.\n•Return target nonland permanent to its owner's hand.\n•Create a token that's a copy of target creature you control.\n•Target player draws a card.
106571|Consider|U|C|Instant|1|{U}|||||92|||Surveil 1.\nDraw a card.
106572|Necrodominance|B|M|Legendary Enchantment|3|{B}{B}{B}|||||9|||Skip your draw step.\nAt the beginning of your end step, you may pay any amount of life. If you do, draw that many cards.\nYour maximum hand size is five.\nIf a card or token would be put into your graveyard from anywhere, exile it instead.
106573|Mind Twist|B|R|Sorcery|1|{X}{B}|||||91|||Target player discards X cards at random.
106574|Splinter Twin|R|R|Enchantment - Aura|4|{2}{R}{R}|||||61|||Enchant creature\nEnchanted creature has "{oT}: Create a token that's a copy of this creature, except it has haste. Exile that token at the beginning of the next end step."
106575|Root Maze|G|M|Enchantment|1|{G}|||||0|||Artifacts and lands enter tapped.
"""

ARCHETYPES = [
    {
        "colors": "WU",
        "name": "Azorius Scry-Surveil Flyers",
        "plan": "Chain cheap scry/surveil effects to pump Diviner of Victory and Denzilore Fatehold while empowering Jace planeswalkers, then win with evasive fliers like Sphinx of False Conclusions backed by tap/stun tempo.",
        "tier": 1,
        "payoffs": [
            "Diviner of Victory",
            "Denzilore Fatehold",
            "Proft, Consulting Detective",
            "Desperate Futurescribe",
            "Prudent Fateseer",
            "Saheeli, Consul of Oversight",
            "Surveillance Phantasm",
        ],
        "enablers": [
            "Consider",
            "Semester Foreseer",
            "Proctor of Potential",
            "Eye of Jace",
            "Your Fate Ends Here",
            "Refute Destiny",
            "Fatehold Chronologist",
        ],
        "key_commons": [
            "Mindseeker Oculus",
            "Icy Reception",
            "Infinite Coursework",
            "Unsummon",
            "Fatehold Chronologist",
            "Proctor of Potential",
            "Consider",
            "Surgical Precision",
        ],
        "key_uncommons": [
            "Sphinx of False Conclusions",
            "Diviner of Victory",
            "Countersculpt",
            "Jace's Machinations",
            "Lyra, Tolarian Archangel",
            "Fblthp, Impossibly Lost",
            "Plan for All Outcomes",
            "Theorist's Sanctum",
        ],
        "avoid": [
            "Yoshimaru, Beloved Companion",
            "Fateshaper Aspirant",
            "Rescue Girl, First Responder",
            "Shatterwing Pegasus",
            "Graft Surgeon",
            "Living Library",
        ],
    },
    {
        "colors": "WB",
        "name": "Orzhov Removal & Recursion",
        "plan": "Win by trading resources efficiently with cheap removal while recurring creatures from the graveyard via self-mill and reanimation effects like Liliana the Repentant and Rewrite Regrets.",
        "tier": 2,
        "payoffs": [
            "Liliana the Repentant",
            "Gallia, Tragic Host",
            "Rewrite Regrets",
            "Generous Revival",
            "Theoretical Necromancer",
            "Blessed Ghoul",
            "Dark Matter Manipulator",
        ],
        "enablers": [
            "Liliana the Repentant",
            "Silence the Echo",
            "Winter, Tormented Loner",
            "Edgar, Ancient Bloodlord",
            "Apex Witchstalker",
            "Last Gasp",
        ],
        "key_commons": [
            "Last Gasp",
            "Silence the Echo",
            "Surgical Precision",
            "Apex Witchstalker",
            "Theoretical Necromancer",
            "Rank Rat",
            "Memory Trap",
            "Screeching Soulbreaker",
        ],
        "key_uncommons": [
            "Rewrite Regrets",
            "Generous Revival",
            "Multiply by Zero",
            "Break Under Pressure",
            "Rise of the Deathbringer",
            "Extended Absence",
        ],
        "avoid": [
            "Danitha, Spear of Agony",
            "Massacre Girl, Most Wanted",
            "Yoshimaru, Beloved Companion",
            "Shatterwing Pegasus",
            "Living Library",
            "Rescue Girl, First Responder",
        ],
    },
    {
        "colors": "WR",
        "name": "Boros Tokens & Burn",
        "plan": "Win by flooding the board with cheap Cadet tokens and efficient creatures, then closing with combat pumps and direct damage while planeswalkers and Empower Jace effects grind value.",
        "tier": 2,
        "payoffs": [
            "Repurposed Enforcer",
            "Master of Barbs",
            "Charge the Sanctum",
            "Ajani's Anguish",
            "Gideon's Memorial",
            "Winter, Team Player",
        ],
        "enablers": [
            "Heartstring Puller",
            "Command the Stage",
            "Hexhaven Battalion",
            "Germinate Recruits",
            "Pia, Determined Rebuilder",
        ],
        "key_commons": [
            "No Admittance",
            "Surgical Precision",
            "Charge the Sanctum",
            "Hexhaven Battalion",
            "Academic Ascent",
            "Predictive Preparations",
            "Blossom-Blessed Angel",
            "Wrath of the Bloodmane",
        ],
        "key_uncommons": [
            "Guiding Hydra",
            "Repurposed Enforcer",
            "Samut, Hazoret's Champion",
            "Heartstring Puller",
            "Command the Stage",
            "Generous Revival",
            "Warrior's Blades",
            "Fulminous Forte",
        ],
        "avoid": [
            "Chandra's Emberling",
            "Gallia, the Merrymaker",
            "Jiang Yanggu, Alone",
            "Tetsuko Umezawa, Pursuer",
            "Tomik, Izzet Sparkmage",
            "Skilled Battlecarver",
            "Tether Technician",
            "Yoshimaru, Beloved Companion",
        ],
    },
    {
        "colors": "WG",
        "name": "Selesnya Lifegain Counters",
        "plan": "Gain life repeatedly through cheap lifelink/life-trigger sources and convert each life gain into +1/+1 counters, card draw (Bloombrunte-style engines), and board presence with Ajani Resolute and Lyra; the deck wins by out-valuing opponents with an ever-growing board while gaining life to stabilize.",
        "tier": 2,
        "payoffs": [
            "Bloombrute",
            "Liliana the Faultless",
            "Lyra, Archangel of Dawn",
            "Unflinching Hortimancer",
            "Titanbones, Towering Heart",
            "Ajani Resolute",
            "Kwia Vigorbloom",
            "Greenhouse Propagator",
        ],
        "enablers": [
            "Medic's Kitesail",
            "Surgical Precision",
            "Vigorbloom Charm",
            "Restore with Empathy",
            "Generous Revival",
            "Blossom-Blessed Angel",
        ],
        "key_commons": [
            "Blossom-Blessed Angel",
            "Greenhouse Propagator",
            "Surgical Precision",
            "Emergency Phytomedic",
            "Something Worth Saving",
        ],
        "key_uncommons": [
            "Bloombrute",
            "Vigorbloom Charm",
            "Generous Revival",
            "Liliana the Faultless",
            "Lyra, Archangel of Dawn",
            "Teyo, Lightshield Expert",
        ],
        "avoid": [
            "Budding Insurgent",
            "Inspired Tethermage",
            "Sureshot Sower",
            "Edgar, Moonlit Sovereign",
            "Shatterwing Pegasus",
            "Rescue Girl, First Responder",
            "Yoshimaru, Beloved Companion",
        ],
    },
    {
        "colors": "UB",
        "name": "Dimir Threshold Mill",
        "plan": "Fill your own graveyard early with surveil, milling, and discard effects to hit seven cards, unlocking threshold payoffs like recasting Null Summoner and buffed Theorix Metamage while removal and tempo spells keep the board clear.",
        "tier": 1,
        "payoffs": [
            "Null Summoner",
            "Theorix Metamage",
            "Void Extrapolator",
            "Dark Matter Manipulator",
            "Recursive Recruitment",
            "Proft, Sinister Mastermind",
        ],
        "enablers": [
            "Theorix Charm",
            "Consider",
            "Yuriko, Hope from the Shadows",
            "Liliana the Repentant",
            "Sphinx of False Conclusions",
            "Seasoned Cryomancer",
            "Murmuring Volume",
        ],
        "key_commons": [
            "Null Summoner",
            "Theorix Charm",
            "Multiply by Zero",
            "Extended Absence",
            "Break Under Pressure",
            "Last Gasp",
            "Mindseeker Oculus",
            "Theorix Metamage",
        ],
        "key_uncommons": [
            "Dark Matter Manipulator",
            "Sanctum Lurker",
            "Rewrite Regrets",
            "Countersculpt",
            "Recursive Recruitment",
            "Diviner of Victory",
            "Theorist's Proxy",
        ],
        "avoid": [
            "Massacre Girl, Most Wanted",
            "Danitha, Spear of Agony",
            "Living Library",
            "Cast Away Doubt",
            "Solve for Disappointment",
        ],
    },
    {
        "colors": "UR",
        "name": "Izzet Spells & Jace Empower",
        "plan": "Cast lots of instants/sorceries to grow prowess creatures and empower Jace planeswalkers, drawing cards and burning out blockers while big flash flyers close the game.",
        "tier": 1,
        "payoffs": [
            "The Theorist, Jace Beleren",
            "Sphinx of False Conclusions",
            "Lyra, Tolarian Archangel",
            "Variable Chaser",
            "Cryotheory Adept",
            "Tomik, Izzet Sparkmage",
            "Countersculpt",
            "Jace's Machinations",
        ],
        "enablers": [
            "Twinned Vision",
            "Consider",
            "Protege's Awakening",
            "Mindseeker Oculus",
            "Theorist's Proxy",
            "Plan for All Outcomes",
            "Theorist's Sanctum",
            "Innovative Commons",
        ],
        "key_commons": [
            "Twinned Vision",
            "Mindseeker Oculus",
            "Protege's Awakening",
            "Cryotheory Adept",
            "Infinite Coursework",
            "Icy Reception",
            "Unsummon",
            "Keeper of the Quiet Hour",
        ],
        "key_uncommons": [
            "The Theorist, Jace Beleren",
            "Sphinx of False Conclusions",
            "Lyra, Tolarian Archangel",
            "Jace's Machinations",
            "Countersculpt",
            "Variable Chaser",
            "Theorist's Proxy",
            "Diviner of Victory",
        ],
        "avoid": [
            "Chandra's Emberling",
            "Gallia, the Merrymaker",
            "Skilled Battlecarver",
            "Tether Technician",
            "Jiang Yanggu, Alone",
            "Living Library",
        ],
    },
    {
        "colors": "UG",
        "name": "Simic Ramp & Jace Empowerment",
        "plan": "Ramp early with green mana elves and land-fetch creatures, then deploy oversized threats like Verdant Kraken and Hungering Puppetbeast while blue instants and Jace-empowering creatures protect the board and generate card advantage.",
        "tier": 2,
        "payoffs": [
            "Verdant Kraken",
            "Hungering Puppetbeast",
            "Fblthp, Knows the Way",
            "Ruric Thar, Magecrusher",
            "Mindseeker Oculus",
            "Countersculpt",
        ],
        "enablers": [
            "Heartwood Crafter",
            "Simulacrum Shaper",
            "Carnivorous Cultivator",
            "Greenhouse Propagator",
            "Mindseeker Oculus",
            "Keeper of the Quiet Hour",
        ],
        "key_commons": [
            "Heartwood Crafter",
            "Greenhouse Propagator",
            "Tam's Resistance",
            "Something Worth Saving",
            "Arcane Amphisbaena",
            "Bestial Incursion",
            "Wrecking Gecko",
        ],
        "key_uncommons": [
            "Simulacrum Shaper",
            "Carnivorous Cultivator",
            "Fblthp, Knows the Way",
            "Hungering Puppetbeast",
            "Verdant Kraken",
            "Countersculpt",
            "Ruric Thar, Magecrusher",
        ],
        "avoid": [
            "Budding Insurgent",
            "Sureshot Sower",
            "Inspired Tethermage",
            "Edgar, Moonlit Sovereign",
            "Living Library",
        ],
    },
    {
        "colors": "BR",
        "name": "Rakdos Noncombat Damage & Sacrifice",
        "plan": "Win by grinding with efficient removal and pinging opponents with noncombat damage, which pumps Master of Barbs and turns on Grim Repriser and Whiplash Wordsmith; self-mill fuels Dark Matter Manipulator and graveyard recursion like Rewrite Regrets.",
        "tier": 2,
        "payoffs": [
            "Master of Barbs",
            "Grim Repriser",
            "Whiplash Wordsmith",
            "Dark Matter Manipulator",
            "Rewrite Regrets",
            "Liliana the Repentant",
        ],
        "enablers": [
            "No Admittance",
            "Fulminous Forte",
            "Screeching Soulbreaker",
            "Cast Away Doubt",
            "Silence the Echo",
            "Stinging Vitriol",
            "Last Gasp",
        ],
        "key_commons": [
            "Sanctum Lurker",
            "Last Gasp",
            "No Admittance",
            "Rank Rat",
            "Screeching Soulbreaker",
            "Cast Away Doubt",
            "Silence the Echo",
            "Apex Witchstalker",
        ],
        "key_uncommons": [
            "Lich's Relic",
            "Multiply by Zero",
            "Extended Absence",
            "Rewrite Regrets",
            "Rise of the Deathbringer",
            "Break Under Pressure",
            "Stingerquill Charm",
            "Dark Matter Manipulator",
        ],
        "avoid": [
            "Massacre Girl, Most Wanted",
            "Danitha, Spear of Agony",
            "Chandra's Emberling",
            "Skilled Battlecarver",
            "Living Library",
        ],
    },
    {
        "colors": "BG",
        "name": "Golgari Self-Mill Graveyard",
        "plan": "Mill your own deck to hit Threshold quickly and stock the graveyard, then win with huge graveyard-fueled bodies (Dark Matter Manipulator, Hapatra) and recursive threats (Rewrite Regrets, Gallia), backed by efficient black removal.",
        "tier": 1,
        "payoffs": [
            "Dark Matter Manipulator",
            "Hapatra, the Desert Fang",
            "Rewrite Regrets",
            "Gallia, Tragic Host",
            "Theoretical Necromancer",
            "Void Extrapolator",
            "Proft, Sinister Mastermind",
        ],
        "enablers": [
            "Primal Witchstalker",
            "Something Worth Saving",
            "Liliana the Repentant",
            "Arcane Amphisbaena",
            "Eye of Jace",
        ],
        "key_commons": [
            "Multiply by Zero",
            "Extended Absence",
            "Break Under Pressure",
            "Last Gasp",
            "Rank Rat",
            "Theoretical Necromancer",
            "Something Worth Saving",
            "Arcane Amphisbaena",
        ],
        "key_uncommons": [
            "Primal Witchstalker",
            "Dark Matter Manipulator",
            "Rewrite Regrets",
            "Hapatra, the Desert Fang",
            "Gallia, Tragic Host",
            "Sanctum Lurker",
            "Break Under Pressure",
        ],
        "avoid": [
            "Danitha, Spear of Agony",
            "Massacre Girl, Most Wanted",
            "Winter, Tormented Loner",
            "Budding Insurgent",
            "Marwyn, the Preserver",
            "Living Library",
            "Sureshot Sower",
            "Inspired Tethermage",
        ],
    },
    {
        "colors": "RG",
        "name": "Gruul Heartwood Ramp",
        "plan": "Ramp early with elf druids and Heartwood tokens, then deploy huge artifact Beasts like Hungering Puppetbeast and Craftwork Crusher that generate card advantage and removal on arrival while growing via artifact sacrifices.",
        "tier": 1,
        "payoffs": [
            "Hungering Puppetbeast",
            "Craftwork Crusher",
            "Verdant Kraken",
            "Craterclaw Colossus",
            "Aerid Konstrari",
            "Kiora of Fire and Ashes",
        ],
        "enablers": [
            "Tenured Tethermage",
            "Heartwood Crafter",
            "Simulacrum Shaper",
            "Konstrari Improviser",
            "Murmuring Volume",
            "Rockfall Vale",
        ],
        "key_commons": [
            "Konstrari Improviser",
            "Greenhouse Propagator",
            "Arcane Amphisbaena",
            "Awaken the Inferno",
            "Heartstring Puller",
            "Wrecking Gecko",
        ],
        "key_uncommons": [
            "Hungering Puppetbeast",
            "Craftwork Crusher",
            "Tenured Tethermage",
            "Heartwood Crafter",
            "Simulacrum Shaper",
            "Kiora of Fire and Ashes",
            "Verdant Kraken",
            "Aerid Konstrari",
        ],
        "avoid": [
            "Budding Insurgent",
            "Inspired Tethermage",
            "Sureshot Sower",
            "Skilled Battlecarver",
            "Chandra's Emberling",
            "Gallia, the Merrymaker",
        ],
    },
]

SYNERGY_NOTES = [
    {
        "cards": ["Diviner of Victory", "Consider"],
        "why": "Every surveil/scry triggers Diviner's repeated +1/+1 buff; Consider is a one-mana repeatable trigger that also filters.",
    },
    {
        "cards": ["Denzilore Fatehold", "Semester Foreseer"],
        "why": "Each scry/surveil puts a +1/+1 counter on every creature you control; Semester Foreseer's ETB surveil turns Denzilore into a team-wide growth engine.",
    },
    {
        "cards": ["Proctor of Potential", "Your Fate Ends Here"],
        "why": "Proctor surveils on each creature entry, feeding removal like Your Fate Ends Here which itself surveils 1; Proctor recurs from the graveyard after you've scried/surveilled.",
    },
    {
        "cards": ["Mindseeker Oculus", "Jace, Reality Sculptor"],
        "why": "Oculus empowers Jace 4 on entry, letting Jace's minus abilities (bounce/tap) come online immediately to clear blockers for your fliers.",
    },
    {
        "cards": ["Sphinx of False Conclusions", "Arni, Humble Scribe"],
        "why": "Sphinx loots on attack, filling the graveyard and hand; Arni untaps off creature entries to convert loot into extra draws and churn.",
    },
    {
        "cards": ["Infinite Coursework", "Tetsuko Umezawa, Fugitive"],
        "why": "Coursework permanently taps down a blocker and strips abilities; small-powered fliers and tokens slip through unblocked under Tetsuko.",
    },
    {
        "cards": ["Liliana the Repentant", "Rewrite Regrets"],
        "why": "Liliana mills two cards off every creature you play to fill your graveyard; Rewrite Regrets returns an MV 6-or-less creature or planeswalker from your graveyard to the battlefield.",
    },
    {
        "cards": ["Liliana the Repentant", "Gallia, Tragic Host"],
        "why": "Liliana's mill loads your graveyard so Gallia can pay {4}{B} and exile a creature card from it to recur herself with a +1/+1 counter.",
    },
    {
        "cards": ["Silence the Echo", "Theoretical Necromancer"],
        "why": "Sacrificing a creature as an additional cost fills the graveyard; Theoretical Necromancer later exiles itself from the graveyard to return another creature card to your hand.",
    },
    {
        "cards": ["Winter, Tormented Loner", "Edgar, Ancient Bloodlord"],
        "why": "Winter's enter trigger sacrifices a creature to force each opponent to sacrifice one; Edgar converts each dying creature or planeswalker into 1 life and can sacrifice extras for +1/+1 counters and menace.",
    },
    {
        "cards": ["Dark Matter Manipulator", "Liliana the Repentant"],
        "why": "Manipulator mills three on entry and gains +2/+0 for every seven cards in your graveyard; Liliana's repeated milling quickly reaches those thresholds to grow him.",
    },
    {
        "cards": ["Last Gasp", "Multiply by Zero"],
        "why": "Cheap instant-speed -3/-3 and 0/0 base-P/T effects kill midsize blockers without committing cards, letting you trade up while your recursion recovers bodies.",
    },
    {
        "cards": ["Heartstring Puller", "Command the Stage"],
        "why": "Both create 2/2 Cadet Wizard tokens; Command the Stage also puts a +1/+1 counter on each other Wizard token you control, so stacking Cadet makers makes each new token bigger.",
    },
    {
        "cards": ["Hexhaven Battalion", "Repurposed Enforcer"],
        "why": "Hexhaven Battalion creates three Cadets and empowers Jace 2; Repurposed Enforcer empowers Jace X equal to your creature count when it attacks, so every token directly scales its triggered value.",
    },
    {
        "cards": ["Ajani's Anguish", "Charge the Sanctum"],
        "why": "Ajani's Anguish gives your whole team trample after dealing X damage; Charge the Sanctum's +2/+0 mode turns a wide Cadet board into a lethal alpha strike through chump blockers.",
    },
    {
        "cards": ["Guiding Hydra", "Predictive Preparations"],
        "why": "Guiding Hydra enters with X +1/+1 counters and can move them in combat; Predictive Preparations adds more counters (with flashback), keeping the Hydra a huge attacker and enabling counter-matters payoffs.",
    },
    {
        "cards": ["Samut, Hazoret's Champion", "Winter, Team Player"],
        "why": "Samut grants all your creatures haste; Winter, Team Player grows the whole team whenever you cast a noncreature spell, so a burst of spells turns into an immediate hasted swarm attack.",
    },
    {
        "cards": ["Generous Revival", "Graft Surgeon"],
        "why": "Generous Revival reanimates an MV3-or-less creature with an extra +1/+1 counter; Graft Surgeon enters with a counter and passes its counters on death, feeding recurring bodies back into revival loops.",
    },
    {
        "cards": ["Medic's Kitesail", "Unflinching Hortimancer"],
        "why": "Equipping the Kitesail gives a creature flying and 'gain 1 life whenever it attacks,' triggering Hortimancer's +1/+1 counter every attack while padding your life total.",
    },
    {
        "cards": ["Surgical Precision", "Titanbones, Towering Heart"],
        "why": "Both modes of Surgical Precision gain life (1 from the destroy mode, 2 from the draw mode), each triggering two +1/+1 counters onto Titanbones.",
    },
    {
        "cards": ["Bloombrute", "Vigorbloom Charm"],
        "why": "Vigorbloom Charm's draw-and-gain-3-life mode both replaces itself and triggers Bloombrute's once-per-turn 'whenever you gain life, draw a card' engine.",
    },
    {
        "cards": ["Generous Revival", "Liliana the Faultless"],
        "why": "Returning a MV3-or-less creature with a +1/+1 counter re-triggers Liliana's 'another creature enters: gain 1 life,' chaining back into your counter payoffs.",
    },
    {
        "cards": ["Restore with Empathy", "Titanbones, Towering Heart"],
        "why": "The 4 life gained off returning a permanent adds two +1/+1 counters to Titanbones while recycling a key creature.",
    },
    {
        "cards": ["Ajani Resolute", "Greenhouse Propagator"],
        "why": "Propagator gains 1 life whenever another creature enters; each trigger puts a loyalty counter on Ajani and grows Ajani's Pridemate tokens.",
    },
    {
        "cards": ["Theorix Charm", "Null Summoner"],
        "why": "The charm's mill-three mode fills your graveyard toward seven cards; once threshold is reached Null Summoner can be cast again from the graveyard, giving repeated card advantage off one pick.",
    },
    {
        "cards": ["Consider", "Theorix Metamage"],
        "why": "Cheap surveil enablers quickly stack the graveyard so Metamage hits threshold and gains flying as a evasive attacker.",
    },
    {
        "cards": ["Sphinx of False Conclusions", "Dark Matter Manipulator"],
        "why": "The Sphinx's attack trigger loots a card into the graveyard each turn, growing the Manipulator's power (+2 per seven cards) while providing evasion itself.",
    },
    {
        "cards": ["Liliana the Repentant", "Rewrite Regrets"],
        "why": "Liliana mills two on every other creature entering, accelerating threshold; Rewrite Regrets then reuses the best MV6-or-less creature or planeswalker from the filled graveyard.",
    },
    {
        "cards": ["Yuriko, Hope from the Shadows", "Proft, Sinister Mastermind"],
        "why": "Yuriko's surveil-2 mode both fuels the graveyard and sets up Proft, who can only be cast at seven or more cards in the graveyard.",
    },
    {
        "cards": ["Mindseeker Oculus", "Jace, Reality Sculptor"],
        "why": "Oculus empowers Jace 4 on entry, letting the big Jace immediately use his minus ability as a repeatable board-control engine behind UB removal.",
    },
    {
        "cards": ["The Theorist, Jace Beleren", "Protege's Awakening"],
        "why": "Cheap noncreature spells like Protege's Awakening empower Jace and trigger his draw engine, turning cantrips into card advantage and a growing board of Illusions.",
    },
    {
        "cards": ["Sphinx of False Conclusions", "Twinned Vision"],
        "why": "Flash flyer attacks to draw a card, then Twinned Vision's flashback (cast from graveyard) draws two more, feeding Sphinx's death-copy and keeping spells flowing.",
    },
    {
        "cards": ["Lyra, Tolarian Archangel", "Twinned Vision"],
        "why": "Drawing three or more cards in a turn via Twinned Vision plus Consider/cantrips makes Lyra create a 3/3 Angel token every end step.",
    },
    {
        "cards": ["Variable Chaser", "Cryotheory Adept"],
        "why": "Prowess creatures grow with every cheap instant/sorcery; Cryotheory Adept also flashes back from the graveyard for another spell cast and a stun counter.",
    },
    {
        "cards": ["Countersculpt", "Theorist's Sanctum"],
        "why": "Beholding a Jace (from any Jace card or Theorist's Sanctum) discounts Countersculpt, and both empower Jace for value.",
    },
    {
        "cards": ["Jace's Machinations", "Jace, Reality Sculptor"],
        "why": "Machinations lets you activate Jace loyalty abilities at instant speed and empowers Jace 8, supercharging Reality Sculptor's -X and defensive triggers.",
    },
    {
        "cards": ["Heartwood Crafter", "Hungering Puppetbeast"],
        "why": "Heartwood Crafter's {oT}: Add {oC} (mana that can't cast spells from hand) conveniently pays Hungering Puppetbeast's {o1} activation to grow it and grant trample/hexproof/haste.",
    },
    {
        "cards": ["Simulacrum Shaper", "Verdant Kraken"],
        "why": "Simulacrum Shaper ramps a basic land onto the battlefield on entry and replaces itself when it dies, accelerating toward casting Verdant Kraken early so its upkeep 3/3 Tentacle tokens snowball.",
    },
    {
        "cards": ["Mindseeker Oculus", "Countersculpt"],
        "why": "Mindseeker Oculus empowers Jace 4 on entry; Countersculpt both counters a spell and empowers Jace further (only needing the {o1} alternative if you behold a Jace), stacking loyalty for big empowered payoffs.",
    },
    {
        "cards": ["Carnivorous Cultivator", "Something Worth Saving"],
        "why": "Something Worth Saving mills four and returns a permanent to hand — putting lands in your graveyard that Carnivorous Cultivator recovers whenever it deals combat damage, sustaining the ramp loop.",
    },
    {
        "cards": ["Greenhouse Propagator", "Bestial Incursion"],
        "why": "Every creature entry (including Bestial Incursion's 4/4 Beast token and its flashback token) triggers Greenhouse Propagator's lifegain, which also fuels life-gain payoffs while the Propagator taps for green mana.",
    },
    {
        "cards": ["Tam's Resistance", "Wrecking Gecko"],
        "why": "Tam's Resistance adds a +1/+1 counter and vigilance to Wrecking Gecko, whose ward {o2} and {o6oGoG}: +4/+4 and trample make it a hard-to-remove finisher once buffed.",
    },
    {
        "cards": ["Master of Barbs", "No Admittance"],
        "why": "Any noncombat damage to an opponent (burn spells, Screeching Soulbreaker pings) triggers Master of Barbs' +1/+0 pump repeatedly through a turn of burn and attack steps.",
    },
    {
        "cards": ["Grim Repriser", "Fulminous Forte"],
        "why": "Dealing noncombat damage to an opponent this turn lets Grim Repriser return from the graveyard with {B}{R}; cheap burn like Fulminous Forte or No Admittance enables the recursion every turn.",
    },
    {
        "cards": ["Whiplash Wordsmith", "Screeching Soulbreaker"],
        "why": "Screeching Soulbreaker's attack trigger deals noncombat damage to each opponent, turning on Whiplash Wordsmith's flying and haste so it swings evasively the same turn.",
    },
    {
        "cards": ["Dark Matter Manipulator", "Liliana the Repentant"],
        "why": "Liliana mills two whenever another creature enters, quickly loading the graveyard so Dark Matter Manipulator's +2/+0 per seven cards makes it a large attacker.",
    },
    {
        "cards": ["Rewrite Regrets", "Apex Witchstalker"],
        "why": "Self-mill from manipulation effects fills the yard; Rewrite Regrets reanimates any MV6-or-less creature like Apex Witchstalker for a recurring threat.",
    },
    {
        "cards": ["Lich's Relic", "Last Gasp"],
        "why": "Lich's Relic destroys an opposing creature on entry via its {2} ability, and Last Gasp/-X effects finish off survivors; removal density keeps the board clear while pingers chip in.",
    },
    {
        "cards": ["Primal Witchstalker", "Dark Matter Manipulator"],
        "why": "Primal Witchstalker mills four on entry, immediately advancing Manipulator's +2/+0-per-seven-cards-in-graveyard scaling toward a huge attacker.",
    },
    {
        "cards": ["Something Worth Saving", "Hapatra, the Desert Fang"],
        "why": "Mill four puts high-mana-value cards in the graveyard; Hapatra's ETB places -1/-1 counters equal to greatest MV among cards in your graveyard, turning milled bombs into board wipes.",
    },
    {
        "cards": ["Liliana the Repentant", "Rewrite Regrets"],
        "why": "Liliana mills two on every other creature/planeswalker entering while her exhaust returns a graveyard card; Rewrite Regrets reanimates any MV<=6 creature or planeswalker from the stocked yard.",
    },
    {
        "cards": ["Arcane Amphisbaena", "Void Extrapolator"],
        "why": "Amphisbaena empowers Jace 2 on entry and Void Extrapolator's Threshold buff needs seven cards in the graveyard—the same mill engine fills both conditions.",
    },
    {
        "cards": ["Gallia, Tragic Host", "Theoretical Necromancer"],
        "why": "Both use the graveyard as a resource: Gallia exiles a creature card from your yard to recur herself with a counter; Necrodomancer-style Necromancer exiles itself from the yard to return another creature to hand.",
    },
    {
        "cards": ["Multiply by Zero", "Last Gasp"],
        "why": "Cheap instant-speed -x/-x effects trade up against fat ground stalls while your recursive threats grind out the late game.",
    },
    {
        "cards": ["Tenured Tethermage", "Hungering Puppetbeast"],
        "why": "Tethermage sacrifices a land to make two tapped Heartwood artifact tokens; those artifacts fuel Puppetbeast's sac-activated +1/+1 counters and give it bodies to sacrifice.",
    },
    {
        "cards": ["Heartwood Crafter", "Craftwork Crusher"],
        "why": "Crafter ramps with ritual mana that can cast the expensive Crusher ahead of curve, and tapping untapped artifacts pumps Tethermage-style payoffs.",
    },
    {
        "cards": ["Simulacrum Shaper", "Verdant Kraken"],
        "why": "Shaper fetches a basic land onto the battlefield to fix for the 7-mana Kraken and replaces itself when it dies, keeping the ramp chain going.",
    },
    {
        "cards": ["Aerid Konstrari", "Hungering Puppetbeast"],
        "why": "Konstrari makes extra Heartwood artifact tokens that feed Puppetbeast's sacrifice activations and pump Konstrari's own power.",
    },
    {
        "cards": ["Craterclaw Colossus", "Murmuring Volume"],
        "why": "Colossus's ETB grants trample and a power boost scaled by artifact count; cheap artifacts like Murmuring Volume inflate the buff while fixing mana.",
    },
    {
        "cards": ["Kiora of Fire and Ashes", "Craftwork Crusher"],
        "why": "Kiora's 5/5 Dragon token is a huge body immediately, and her {8} ability keeps making Dragons to close games after Crusher stabilizes the board.",
    },
]

PAIR_STATS = {
    "WU": {"win_rate": 0.5723, "games": 31580, "share": 0.1521},
    "WB": {"win_rate": 0.5364, "games": 10739, "share": 0.0517},
    "WR": {"win_rate": 0.5405, "games": 13227, "share": 0.0637},
    "WG": {"win_rate": 0.5462, "games": 24782, "share": 0.1194},
    "UB": {"win_rate": 0.5602, "games": 34908, "share": 0.1682},
    "UR": {"win_rate": 0.5595, "games": 21641, "share": 0.1043},
    "UG": {"win_rate": 0.5203, "games": 7025, "share": 0.0338},
    "BR": {"win_rate": 0.5416, "games": 22418, "share": 0.108},
    "BG": {"win_rate": 0.5455, "games": 12981, "share": 0.0625},
    "RG": {"win_rate": 0.5606, "games": 28266, "share": 0.1362},
}

TRAPS = [
    {
        "card": "Danitha, Spear of Agony",
        "why": "Looks like a build-around but posts 47.9 GIH and -2.1 IWD; her targeting-trigger payoff rarely comes together in limited board states.",
    },
    {
        "card": "Massacre Girl, Most Wanted",
        "why": "Five-drop legend with 47.8 GIH and -3.9 IWD — slow death-trigger engine that does little the turn it arrives.",
    },
    {
        "card": "Gallia, the Merrymaker",
        "why": "Haste anthem looks aggressive but -6.0 IWD means she underperforms when drawn; needs a critical mass of counter-bearing creatures you won't reliably have.",
    },
    {
        "card": "Chandra's Emberling",
        "why": "Noncreature-spell payoff with -5.4 IWD; requires too many instants/sorceries to grow fast enough in a creature-heavy format.",
    },
    {
        "card": "Yoshimaru, Beloved Companion",
        "why": "Counter-doubling sounds powerful but 47.1-level performance (-4.9 IWD); doubling modest counters rarely changes races.",
    },
    {
        "card": "Budding Insurgent",
        "why": "Artifact/enchantment hate with a legendary-enchantment bonus is narrow sideboard material masquerading as a maindeck body (47.1 GIH).",
    },
    {
        "card": "Grim Repriser",
        "why": "Prowess recursion line demands opponent damage first plus mana investment; -3.8 IWD shows the hoops aren't worth it.",
    },
    {
        "card": "Living Library",
        "why": "Expensive shuffle-out effect on a 2/2 body with -3.2 IWD; it answers almost nothing relevant in practice.",
    },
]

SPEED = "medium"


def _num(value: str, kind=float):
    return kind(value) if value else None


def primer() -> SetPrimer:
    cards = {}
    for line in CARD_TABLE.strip().splitlines():
        grp, name, colors, rarity, types, cmc, _cost, gih, base, ata, alsa, games, play, game_wr, rules = (
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
            alsa=_num(alsa),
            games=int(games),
            play_rate=_num(play),
            game_wr=_num(game_wr),
        )
    return SetPrimer(
        set_code="FRA",
        source="model",
        speed=SPEED,
        archetypes=[dict(entry) for entry in ARCHETYPES],
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
ORDER = list(PACKS)


def pool_before(key: str) -> list[int]:
    """What autoplay had drafted before this pick."""
    return [PICKS[k] for k in ORDER[: ORDER.index(key)]]
