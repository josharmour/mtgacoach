"""Curated MTG Comprehensive Rules corpus for RAG-based coaching.

Contains ~175 rules most relevant to real-time game coaching decisions.
Rules text is verbatim from the official MTG Comprehensive Rules.
"""

RULES = [
    # =========================================================================
    # COMBAT — Attacking (~20 rules)
    # =========================================================================
    {
        "number": "508.1a",
        "section": "Declare Attackers Step",
        "category": "combat",
        "text": "The active player chooses which creatures they control will attack. The chosen creatures must be untapped, and each one must either have haste or have been continuously controlled by that player since the turn began.",
    },
    {
        "number": "508.1b",
        "section": "Declare Attackers Step",
        "category": "combat",
        "text": "If a creature has a restriction on attacking, it can't attack unless the restriction is satisfied. If a creature has a requirement for attacking, it must attack if able.",
    },
    {
        "number": "508.1c",
        "section": "Declare Attackers Step",
        "category": "combat",
        "text": "Creatures with defender can't attack.",
    },
    {
        "number": "508.3a",
        "section": "Declare Attackers Step",
        "category": "combat",
        "text": "An attacking creature with vigilance doesn't tap when declared as an attacker.",
    },
    {
        "number": "302.6",
        "section": "Creatures",
        "category": "combat",
        "text": "A creature's activated ability with the tap symbol or the untap symbol in its activation cost can't be activated unless the creature has been under its controller's control continuously since their most recent turn began. A creature can't attack unless it has been under its controller's control continuously since their most recent turn began. This rule is informally called the 'summoning sickness' rule.",
    },
    {
        "number": "506.2",
        "section": "Combat Phase",
        "category": "combat",
        "text": "During the combat phase, the active player is the attacking player; creatures that player controls may be declared as attackers. The nonactive player is the defending player; creatures that player controls may be declared as blockers.",
    },
    {
        "number": "506.3",
        "section": "Combat Phase",
        "category": "combat",
        "text": "Only a creature can attack or block. Only a player or a planeswalker can be attacked.",
    },
    {
        "number": "506.4",
        "section": "Combat Phase",
        "category": "combat",
        "text": "A permanent is removed from combat if it leaves the battlefield, if its controller changes, if it phases out, if an effect specifically removes it from combat, if it's a planeswalker that's being attacked and stops being a planeswalker, or if it's an attacking or blocking creature that regenerates or stops being a creature. A creature that's removed from combat stops being an attacking, blocking, blocked, and/or unblocked creature.",
    },
    {
        "number": "507.1",
        "section": "Beginning of Combat Step",
        "category": "combat",
        "text": "First, if the game being played is a multiplayer game in which the active player's opponents don't all automatically become defending players, the active player chooses one of their opponents. That player becomes the defending player. Then any abilities that trigger at the beginning of combat on the active player's turn trigger.",
    },
    {
        "number": "508.2",
        "section": "Declare Attackers Step",
        "category": "combat",
        "text": "Any abilities that triggered on declaring attackers are put on the stack. The active player gets priority.",
    },

    # =========================================================================
    # COMBAT — Blocking (~20 rules)
    # =========================================================================
    {
        "number": "509.1a",
        "section": "Declare Blockers Step",
        "category": "combat",
        "text": "The defending player chooses which creatures they control will block. The chosen creatures must be untapped. For each of the chosen creatures, the defending player chooses one creature for it to block that's attacking that player or a planeswalker that player controls.",
    },
    {
        "number": "509.1b",
        "section": "Declare Blockers Step",
        "category": "combat",
        "text": "An attacking creature with flying can only be blocked by creatures with flying or reach.",
    },
    {
        "number": "509.1c",
        "section": "Declare Blockers Step",
        "category": "combat",
        "text": "An attacking creature with menace can't be blocked except by two or more creatures.",
    },
    {
        "number": "509.1d",
        "section": "Declare Blockers Step",
        "category": "combat",
        "text": "If a creature has a restriction on blocking, it can't block unless the restriction is satisfied.",
    },
    {
        "number": "509.2",
        "section": "Declare Blockers Step",
        "category": "combat",
        "text": "Second, for each attacking creature that's become blocked, the active player announces that creature's damage assignment order, which includes the creatures that are blocking it in an order of that player's choice.",
    },
    {
        "number": "509.3",
        "section": "Declare Blockers Step",
        "category": "combat",
        "text": "Any abilities that triggered on declaring blockers are put on the stack. Then the active player gets priority.",
    },
    {
        "number": "509.5",
        "section": "Declare Blockers Step",
        "category": "combat",
        "text": "An attacking creature that is unblocked will deal its combat damage to the player or planeswalker it's attacking.",
    },
    {
        "number": "510.1a",
        "section": "Combat Damage Step",
        "category": "combat",
        "text": "Each attacking creature and each blocking creature assigns combat damage equal to its power. An attacking creature assigns its combat damage to the creature(s) blocking it, or to the player or planeswalker it's attacking if it's unblocked.",
    },
    {
        "number": "510.1b",
        "section": "Combat Damage Step",
        "category": "combat",
        "text": "An attacking creature with trample assigns its combat damage as follows: first, to the creature(s) blocking it. If all those blocking creatures are assigned lethal damage, any remaining damage is assigned as the attacking player chooses among the player or planeswalker it's attacking and any blocking creatures.",
    },
    {
        "number": "510.1c",
        "section": "Combat Damage Step",
        "category": "combat",
        "text": "A blocking creature assigns its combat damage to the attacking creature it blocked.",
    },
    {
        "number": "510.1d",
        "section": "Combat Damage Step",
        "category": "combat",
        "text": "If an attacking creature is blocked by multiple creatures, the attacking creature must assign at least lethal damage to the first blocking creature in damage assignment order before it can assign any damage to the next one.",
    },
    {
        "number": "510.2",
        "section": "Combat Damage Step",
        "category": "combat",
        "text": "Second, all combat damage that's been assigned is dealt simultaneously. This turn-based action doesn't use the stack.",
    },
    {
        "number": "510.4",
        "section": "Combat Damage Step",
        "category": "combat",
        "text": "If at least one attacking or blocking creature has first strike or double strike, two combat damage steps occur instead of one. The first combat damage step includes creatures with first strike or double strike. The second combat damage step includes creatures with double strike and creatures that had neither first strike nor double strike.",
    },
    {
        "number": "511.1",
        "section": "End of Combat Step",
        "category": "combat",
        "text": "First, all 'at end of combat' triggered abilities trigger and are put on the stack. Then the active player gets priority.",
    },
    {
        "number": "511.3",
        "section": "End of Combat Step",
        "category": "combat",
        "text": "As the end of combat step ends, all creatures and planeswalkers are removed from combat. After the end of combat step, the combat phase ends and the postcombat main phase begins.",
    },

    # =========================================================================
    # KEYWORDS — Evasion & Combat (~40 rules)
    # =========================================================================
    {
        "number": "702.9a",
        "section": "Flying",
        "category": "keywords",
        "text": "Flying is an evasion ability. A creature with flying can't be blocked except by creatures with flying or reach.",
    },
    {
        "number": "702.15a",
        "section": "Reach",
        "category": "keywords",
        "text": "Reach is a static ability. A creature with reach can block creatures with flying.",
    },
    {
        "number": "702.7a",
        "section": "First Strike",
        "category": "keywords",
        "text": "First strike is a static ability that modifies the rules for the combat damage step. A creature with first strike deals combat damage before creatures without first strike.",
    },
    {
        "number": "702.4a",
        "section": "Double Strike",
        "category": "keywords",
        "text": "Double strike is a static ability that modifies the rules for the combat damage step. A creature with double strike deals both first-strike and regular combat damage.",
    },
    {
        "number": "702.2a",
        "section": "Deathtouch",
        "category": "keywords",
        "text": "Deathtouch is a static ability. Any amount of damage dealt to a creature by a source with deathtouch is considered to be lethal damage.",
    },
    {
        "number": "702.2b",
        "section": "Deathtouch",
        "category": "keywords",
        "text": "A creature with toughness greater than 0 that's been dealt damage by a source with deathtouch since the last time state-based actions were checked is destroyed as a state-based action. See rule 704.",
    },
    {
        "number": "702.2c",
        "section": "Deathtouch",
        "category": "keywords",
        "text": "If an attacking creature with trample and deathtouch assigns damage, it only needs to assign 1 damage to each blocking creature to be considered lethal, and the rest can trample over to the defending player.",
    },
    {
        "number": "702.19a",
        "section": "Trample",
        "category": "keywords",
        "text": "Trample is a static ability that modifies the rules for assigning an attacking creature's combat damage. Specifically, it allows the attacking creature to assign excess damage to the defending player or planeswalker.",
    },
    {
        "number": "702.19b",
        "section": "Trample",
        "category": "keywords",
        "text": "The attacking creature with trample first assigns lethal damage to all creatures blocking it, then assigns the remainder to the defending player or planeswalker it's attacking.",
    },
    {
        "number": "702.15b",
        "section": "Lifelink",
        "category": "keywords",
        "text": "Lifelink is a static ability. Damage dealt by a source with lifelink causes that source's controller to gain that much life (in addition to any other results that damage causes).",
    },
    {
        "number": "702.20a",
        "section": "Vigilance",
        "category": "keywords",
        "text": "Vigilance is a static ability that modifies the rules for the declare attackers step. Attacking doesn't cause creatures with vigilance to tap.",
    },
    {
        "number": "702.10a",
        "section": "Haste",
        "category": "keywords",
        "text": "Haste is a static ability. A creature with haste can attack even if it hasn't been controlled continuously since its controller's most recent turn began. A creature with haste can activate abilities with the tap or untap symbol in their costs even if it hasn't been controlled continuously since its controller's most recent turn began.",
    },
    {
        "number": "702.8a",
        "section": "Flash",
        "category": "keywords",
        "text": "Flash is a static ability that functions while the spell with flash is on the stack. 'Flash' means 'You may cast this spell any time you could cast an instant.'",
    },
    {
        "number": "702.11a",
        "section": "Hexproof",
        "category": "keywords",
        "text": "Hexproof is a static ability. A permanent with hexproof can't be the target of spells or abilities your opponents control.",
    },
    {
        "number": "702.21a",
        "section": "Ward",
        "category": "keywords",
        "text": "Ward is a triggered ability. Whenever a permanent with ward becomes the target of a spell or ability an opponent controls, counter that spell or ability unless that player pays the ward cost.",
    },
    {
        "number": "702.12a",
        "section": "Indestructible",
        "category": "keywords",
        "text": "Indestructible is a static ability. A permanent with indestructible can't be destroyed. Such permanents aren't destroyed by lethal damage, and they ignore the state-based action that checks for lethal damage.",
    },
    {
        "number": "702.110a",
        "section": "Menace",
        "category": "keywords",
        "text": "Menace is an evasion ability. A creature with menace can't be blocked except by two or more creatures.",
    },
    {
        "number": "702.3a",
        "section": "Defender",
        "category": "keywords",
        "text": "Defender is a static ability. A creature with defender can't attack.",
    },
    {
        "number": "702.16a",
        "section": "Protection",
        "category": "keywords",
        "text": "Protection from [quality] is a static ability. A permanent with protection can't be blocked by creatures with the stated quality, can't be the target of spells with the stated quality, can't be the target of abilities from sources with the stated quality, can't be enchanted or equipped by permanents with the stated quality, and all damage dealt to it by sources with the stated quality is prevented.",
    },
    {
        "number": "702.14a",
        "section": "Intimidate",
        "category": "keywords",
        "text": "Intimidate is an evasion ability. A creature with intimidate can't be blocked except by artifact creatures and/or creatures that share a color with it.",
    },
    {
        "number": "702.104a",
        "section": "Prowess",
        "category": "keywords",
        "text": "Prowess is a triggered ability. Whenever you cast a noncreature spell, each creature you control with prowess gets +1/+1 until end of turn.",
    },
    {
        "number": "702.125a",
        "section": "Toxic",
        "category": "keywords",
        "text": "Toxic is a triggered ability. Whenever a creature with toxic deals combat damage to a player, that player gets a number of poison counters equal to the creature's toxic value.",
    },

    # =========================================================================
    # CASTING SPELLS (~20 rules)
    # =========================================================================
    {
        "number": "601.2",
        "section": "Casting Spells",
        "category": "casting",
        "text": "To cast a spell is to take it from where it is (usually the hand), put it on the stack, and pay its costs, so that it will eventually resolve and have its effect.",
    },
    {
        "number": "307.1",
        "section": "Sorceries",
        "category": "casting",
        "text": "A player who has priority may cast a sorcery card from their hand during a main phase of their turn when the stack is empty.",
    },
    {
        "number": "304.1",
        "section": "Instants",
        "category": "casting",
        "text": "A player who has priority may cast an instant spell from their hand.",
    },
    {
        "number": "307.5",
        "section": "Sorceries",
        "category": "casting",
        "text": "If a spell, ability, or effect states that a player can do something only 'any time they could cast a sorcery,' it means only during their main phase when the stack is empty and they have priority. The player doesn't need to have a sorcery card they could actually cast.",
    },
    {
        "number": "601.2a",
        "section": "Casting Spells",
        "category": "casting",
        "text": "To propose the casting of a spell, a player first moves that card (or that copy of a card) from where it is to the stack. It becomes the topmost object on the stack and has all the characteristics of the card.",
    },
    {
        "number": "601.2b",
        "section": "Casting Spells",
        "category": "casting",
        "text": "If the spell requires the player to choose one or more targets, the player announces their choice. A spell can't be cast unless a legal target exists for each instance of the word 'target' in the spell's text.",
    },
    {
        "number": "601.2f",
        "section": "Casting Spells",
        "category": "casting",
        "text": "The player determines the total cost of the spell. The total cost is the mana cost or alternative cost, plus any additional costs and cost increases, minus any cost reductions.",
    },
    {
        "number": "117.1a",
        "section": "Timing and Priority",
        "category": "casting",
        "text": "A player may cast an instant spell any time they have priority. A player may cast a noninstant spell during their main phase any time they have priority and the stack is empty.",
    },
    {
        "number": "117.1b",
        "section": "Timing and Priority",
        "category": "casting",
        "text": "A player may activate an activated ability any time they have priority.",
    },
    {
        "number": "602.1",
        "section": "Activated Abilities",
        "category": "casting",
        "text": "Activated abilities have a cost and an effect. They are written as '[Cost]: [Effect.]' A player may activate such an ability whenever they have priority.",
    },
    {
        "number": "601.3",
        "section": "Casting Spells",
        "category": "casting",
        "text": "A player can't begin to cast a spell unless they can make a legal choice for all costs and targets.",
    },
    {
        "number": "601.2e",
        "section": "Casting Spells",
        "category": "casting",
        "text": "If the spell is modal, the player announces the mode choice when putting the spell on the stack.",
    },
    {
        "number": "601.5",
        "section": "Casting Spells",
        "category": "casting",
        "text": "If a player is unable to pay the total cost of a spell, the casting is illegal and the game returns to the moment before the casting was proposed.",
    },

    # =========================================================================
    # STACK & PRIORITY (~15 rules)
    # =========================================================================
    {
        "number": "405.1",
        "section": "Stack",
        "category": "stack",
        "text": "When a spell is cast, the physical card is put on the stack. When an ability is activated or triggers, it goes on the stack without any card associated with it.",
    },
    {
        "number": "405.2",
        "section": "Stack",
        "category": "stack",
        "text": "The stack keeps track of the order that spells and/or abilities were added to it. Each time an object is put on the stack, it's put on top of all objects already there.",
    },
    {
        "number": "405.5",
        "section": "Stack",
        "category": "stack",
        "text": "When all players pass in succession, the top (last added) spell or ability on the stack resolves.",
    },
    {
        "number": "117.3a",
        "section": "Timing and Priority",
        "category": "stack",
        "text": "The active player receives priority at the beginning of most steps and phases, after any turn-based actions have been dealt with and abilities that trigger at the beginning of that phase or step have been put on the stack.",
    },
    {
        "number": "117.3b",
        "section": "Timing and Priority",
        "category": "stack",
        "text": "The active player receives priority after a spell or ability on the stack resolves.",
    },
    {
        "number": "117.3c",
        "section": "Timing and Priority",
        "category": "stack",
        "text": "If a player has priority, they may cast a spell, activate an ability, or take a special action. If the active player passes priority and the stack is not empty, the nonactive player receives priority.",
    },
    {
        "number": "117.3d",
        "section": "Timing and Priority",
        "category": "stack",
        "text": "If a player has priority and chooses not to take any actions, that player passes. If the stack is empty, a player passing results in both players passing consecutively, and the step or phase ends. If the stack is not empty, priority passes to the other player.",
    },
    {
        "number": "117.4",
        "section": "Timing and Priority",
        "category": "stack",
        "text": "If all players pass in succession (that is, if all players pass without any player taking an action in between the passes), the spell or ability on top of the stack resolves or, if the stack is empty, the phase or step ends.",
    },
    {
        "number": "608.1",
        "section": "Resolving Spells and Abilities",
        "category": "stack",
        "text": "Each time all players pass in succession, the spell or ability on top of the stack resolves.",
    },
    {
        "number": "608.2a",
        "section": "Resolving Spells and Abilities",
        "category": "stack",
        "text": "If a triggered ability has an intervening 'if' clause, it checks whether the clause's condition is true. If it isn't, the ability does nothing. If it is, the ability continues to resolve.",
    },
    {
        "number": "608.2b",
        "section": "Resolving Spells and Abilities",
        "category": "stack",
        "text": "If the spell or ability specifies targets, it checks whether the targets are still legal. A target is illegal if it's left the zone it was in, if an effect has made it an illegal target, or if the target is now protected from the source. If all targets are illegal, the spell or ability doesn't resolve.",
    },
    {
        "number": "117.7",
        "section": "Timing and Priority",
        "category": "stack",
        "text": "Once a player has taken an action or made a choice requested by a spell or ability that's resolving, that player can't undo that action or choice.",
    },
    {
        "number": "608.3",
        "section": "Resolving Spells and Abilities",
        "category": "stack",
        "text": "If the object that's resolving is a permanent spell, its resolution may involve several steps. The instructions are followed in order. The permanent enters the battlefield.",
    },

    # =========================================================================
    # TURN STRUCTURE (~20 rules)
    # =========================================================================
    {
        "number": "500.1",
        "section": "Turn Structure",
        "category": "turn_structure",
        "text": "A turn consists of five phases, in this order: beginning phase, precombat main phase, combat phase, postcombat main phase, and ending phase.",
    },
    {
        "number": "501.1",
        "section": "Beginning Phase",
        "category": "turn_structure",
        "text": "The beginning phase consists of three steps, in this order: untap, upkeep, and draw.",
    },
    {
        "number": "502.1",
        "section": "Untap Step",
        "category": "turn_structure",
        "text": "The active player untaps all permanents they control. This is a turn-based action that doesn't use the stack. Normally, no player receives priority during the untap step.",
    },
    {
        "number": "502.3",
        "section": "Untap Step",
        "category": "turn_structure",
        "text": "No player receives priority during the untap step, so no spells can be cast or abilities activated during this step.",
    },
    {
        "number": "503.1",
        "section": "Upkeep Step",
        "category": "turn_structure",
        "text": "First, any abilities that trigger at the beginning of the upkeep step are put on the stack. Then the active player gets priority.",
    },
    {
        "number": "504.1",
        "section": "Draw Step",
        "category": "turn_structure",
        "text": "First, the active player draws a card. This turn-based action doesn't use the stack. Then any abilities that triggered at the beginning of the draw step are put on the stack. Then the active player gets priority.",
    },
    {
        "number": "505.1",
        "section": "Main Phase",
        "category": "turn_structure",
        "text": "There are two main phases in a turn. The first, or precombat, main phase comes after the beginning phase. The second, or postcombat, main phase comes after the combat phase.",
    },
    {
        "number": "505.4",
        "section": "Main Phase",
        "category": "turn_structure",
        "text": "Second, if the active player controls a Saga with a chapter ability that has triggered but not yet been put on the stack, that player puts the triggered ability on the stack. Then the active player gets priority.",
    },
    {
        "number": "305.2",
        "section": "Lands",
        "category": "turn_structure",
        "text": "A player can normally play one land during their turn; however, continuous effects may increase this number. Playing a land is a special action that doesn't use the stack.",
    },
    {
        "number": "305.3",
        "section": "Lands",
        "category": "turn_structure",
        "text": "A player can't play a land, for any reason, if it isn't their turn. A player can play a land during their main phase when the stack is empty and they have priority.",
    },
    {
        "number": "305.9",
        "section": "Lands",
        "category": "turn_structure",
        "text": "If an object is both a land and another card type, it can be played only as a land. It can't be cast as a spell.",
    },
    {
        "number": "512.1",
        "section": "End Step",
        "category": "turn_structure",
        "text": "First, all abilities that trigger 'at the beginning of the end step' or 'at the beginning of the next end step' are put on the stack. Then the active player gets priority.",
    },
    {
        "number": "514.1",
        "section": "Cleanup Step",
        "category": "turn_structure",
        "text": "First, if the active player's hand contains more cards than their maximum hand size (normally seven), they discard enough cards to reduce their hand size to that number.",
    },
    {
        "number": "514.2",
        "section": "Cleanup Step",
        "category": "turn_structure",
        "text": "Second, the following actions happen simultaneously: all damage marked on permanents is removed and all 'until end of turn' and 'this turn' effects end.",
    },
    {
        "number": "514.3",
        "section": "Cleanup Step",
        "category": "turn_structure",
        "text": "Normally, no player receives priority during the cleanup step, so no spells can be cast and no abilities can be activated. However, if a state-based action is performed or a triggered ability is put on the stack during this step, the active player receives priority.",
    },
    {
        "number": "506.1",
        "section": "Combat Phase",
        "category": "turn_structure",
        "text": "The combat phase has five steps, which proceed in order: beginning of combat, declare attackers, declare blockers, combat damage, and end of combat.",
    },

    # =========================================================================
    # ZONES (~15 rules)
    # =========================================================================
    {
        "number": "400.1",
        "section": "Zones",
        "category": "zones",
        "text": "A zone is a place where objects can be during a game. There are normally seven zones: library, hand, battlefield, graveyard, stack, exile, and command.",
    },
    {
        "number": "401.1",
        "section": "Library",
        "category": "zones",
        "text": "A player's library is their draw pile. At the beginning of the game, each player's library contains all the cards in that player's deck. Cards are drawn from the top of the library.",
    },
    {
        "number": "401.3",
        "section": "Library",
        "category": "zones",
        "text": "If a player is required to draw a card and their library is empty, that player loses the game the next time state-based actions are checked.",
    },
    {
        "number": "402.1",
        "section": "Hand",
        "category": "zones",
        "text": "The hand is where a player holds cards that have been drawn. Cards can be put into a player's hand by other effects as well. At the beginning of the game, each player draws a number of cards equal to that player's starting hand size, normally seven.",
    },
    {
        "number": "403.1",
        "section": "Battlefield",
        "category": "zones",
        "text": "The battlefield is the zone in which permanents exist. It is shared by all players.",
    },
    {
        "number": "404.1",
        "section": "Graveyard",
        "category": "zones",
        "text": "A player's graveyard is their discard pile. Any object that's countered, discarded, destroyed, or sacrificed is put on top of its owner's graveyard. Each player's graveyard starts the game empty.",
    },
    {
        "number": "404.2",
        "section": "Graveyard",
        "category": "zones",
        "text": "The graveyard is an ordered zone. Each player's graveyard is kept in a single face-up pile. A player can examine the cards in any graveyard at any time but normally can't change their order.",
    },
    {
        "number": "406.1",
        "section": "Exile",
        "category": "zones",
        "text": "The exile zone is essentially a holding area for objects. Some spells and abilities exile an object without any way to return that object to another zone.",
    },
    {
        "number": "110.1",
        "section": "Permanents",
        "category": "zones",
        "text": "A permanent is a card or token on the battlefield. A permanent remains on the battlefield indefinitely. A card or token becomes a permanent as it enters the battlefield and it stops being a permanent as it's moved to another zone.",
    },
    {
        "number": "400.7",
        "section": "Zones",
        "category": "zones",
        "text": "An object that moves from one zone to another becomes a new object with no memory of, or relation to, its previous existence.",
    },
    {
        "number": "400.6",
        "section": "Zones",
        "category": "zones",
        "text": "If an object would move from one zone to another, a replacement effect may change the destination zone. The object moves to the modified zone instead.",
    },
    {
        "number": "110.2a",
        "section": "Permanents",
        "category": "zones",
        "text": "The oldest permanent is the one that has been on the battlefield the longest. Tokens and copies of permanent spells that entered the battlefield are just as old as any other permanent that entered at the same time.",
    },

    # =========================================================================
    # CARD TYPES (~15 rules)
    # =========================================================================
    {
        "number": "301.1",
        "section": "Artifacts",
        "category": "card_types",
        "text": "A player who has priority may cast an artifact card from their hand during a main phase of their turn when the stack is empty.",
    },
    {
        "number": "301.5",
        "section": "Artifacts",
        "category": "card_types",
        "text": "Some artifacts have the subtype 'Equipment.' An Equipment can be attached to a creature. It can't legally be attached to anything that isn't a creature.",
    },
    {
        "number": "302.1",
        "section": "Creatures",
        "category": "card_types",
        "text": "A player who has priority may cast a creature card from their hand during a main phase of their turn when the stack is empty.",
    },
    {
        "number": "302.3",
        "section": "Creatures",
        "category": "card_types",
        "text": "Creatures can attack and block.",
    },
    {
        "number": "302.4",
        "section": "Creatures",
        "category": "card_types",
        "text": "Power and toughness are characteristics only creatures have. A creature's power is the amount of damage it deals in combat. A creature's toughness is the amount of damage needed to destroy it.",
    },
    {
        "number": "303.1",
        "section": "Enchantments",
        "category": "card_types",
        "text": "A player who has priority may cast an enchantment card from their hand during a main phase of their turn when the stack is empty.",
    },
    {
        "number": "303.4a",
        "section": "Enchantments",
        "category": "card_types",
        "text": "An Aura spell requires a target, which is defined by its enchant ability. If an Aura is entering the battlefield by any other means, the player putting it on the battlefield chooses an object or player for it to be attached to.",
    },
    {
        "number": "306.1",
        "section": "Planeswalkers",
        "category": "card_types",
        "text": "A player who has priority may cast a planeswalker card from their hand during a main phase of their turn when the stack is empty.",
    },
    {
        "number": "306.7",
        "section": "Planeswalkers",
        "category": "card_types",
        "text": "If a player controls two or more legendary planeswalkers that share a subtype, that player chooses one of them and puts the rest into their owner's graveyard. This is a state-based action.",
    },
    {
        "number": "306.9",
        "section": "Planeswalkers",
        "category": "card_types",
        "text": "If noncombat damage would be dealt to a player by a source controlled by an opponent, that opponent may instead have that damage dealt to a planeswalker the first player controls.",
    },
    {
        "number": "305.1",
        "section": "Lands",
        "category": "card_types",
        "text": "A player who has priority may play a land card from their hand during a main phase of their turn when the stack is empty. Playing a land is a special action; it doesn't use the stack. It simply puts the land onto the battlefield.",
    },
    {
        "number": "704.5j",
        "section": "Legendary Rule",
        "category": "card_types",
        "text": "If a player controls two or more legendary permanents with the same name, that player chooses one of them, and the rest are put into their owners' graveyards. This is called the 'legend rule.'",
    },
    {
        "number": "205.3m",
        "section": "Subtypes",
        "category": "card_types",
        "text": "Instants and sorceries share their lists of subtypes; these subtypes are called spell types.",
    },

    # =========================================================================
    # DAMAGE & STATE-BASED ACTIONS (~10 rules)
    # =========================================================================
    {
        "number": "120.1",
        "section": "Damage",
        "category": "damage",
        "text": "Objects can deal damage to creatures, planeswalkers, and players. This is generally detrimental to the object or player that receives that damage.",
    },
    {
        "number": "120.3a",
        "section": "Damage",
        "category": "damage",
        "text": "Damage dealt to a player causes that player to lose that much life.",
    },
    {
        "number": "120.3b",
        "section": "Damage",
        "category": "damage",
        "text": "Damage dealt to a planeswalker causes that many loyalty counters to be removed from that planeswalker.",
    },
    {
        "number": "120.4",
        "section": "Damage",
        "category": "damage",
        "text": "Damage is processed in a three-part sequence. 1. Damage is dealt. 2. The results of that damage are processed. 3. The information about that damage is recorded. This process is performed simultaneously for all damage dealt at the same time.",
    },
    {
        "number": "704.1",
        "section": "State-Based Actions",
        "category": "damage",
        "text": "State-based actions are game actions that happen automatically whenever certain conditions are met. State-based actions don't use the stack.",
    },
    {
        "number": "704.5a",
        "section": "State-Based Actions",
        "category": "damage",
        "text": "If a player has 0 or less life, that player loses the game.",
    },
    {
        "number": "704.5b",
        "section": "State-Based Actions",
        "category": "damage",
        "text": "If a player attempted to draw a card from a library with no cards in it since the last time state-based actions were checked, that player loses the game.",
    },
    {
        "number": "704.5c",
        "section": "State-Based Actions",
        "category": "damage",
        "text": "If a creature has toughness 0 or less, it's put into its owner's graveyard. Regeneration can't replace this event.",
    },
    {
        "number": "704.5d",
        "section": "State-Based Actions",
        "category": "damage",
        "text": "If a creature has toughness greater than 0, it has been dealt damage, and the total damage marked on it is greater than or equal to its toughness, that creature has been dealt lethal damage and is destroyed. Regeneration can replace this event.",
    },
    {
        "number": "704.5t",
        "section": "State-Based Actions",
        "category": "damage",
        "text": "If a player has ten or more poison counters, that player loses the game.",
    },
    {
        "number": "120.6",
        "section": "Damage",
        "category": "damage",
        "text": "Damage marked on a creature remains until the cleanup step, even if that creature isn't a creature during that step.",
    },

    # =========================================================================
    # ABILITIES — Triggered, Static, Activated (~15 rules)
    # =========================================================================
    {
        "number": "603.1",
        "section": "Triggered Abilities",
        "category": "abilities",
        "text": "Triggered abilities have a trigger condition and an effect. They are written as '[When/Whenever/At] [trigger condition], [effect].'",
    },
    {
        "number": "603.2",
        "section": "Triggered Abilities",
        "category": "abilities",
        "text": "Whenever a game event or game state matches a triggered ability's trigger event, that ability automatically triggers. The ability doesn't do anything at this point; it merely goes on the stack the next time a player would receive priority.",
    },
    {
        "number": "603.3",
        "section": "Triggered Abilities",
        "category": "abilities",
        "text": "Once an ability has triggered, its controller puts it on the stack as an object the next time a player would receive priority.",
    },
    {
        "number": "603.6a",
        "section": "Triggered Abilities",
        "category": "abilities",
        "text": "Enters-the-battlefield triggered abilities trigger when a permanent enters the battlefield. These are written, 'When [this object] enters the battlefield, ...' or 'Whenever a [type] enters the battlefield, ...'",
    },
    {
        "number": "603.6d",
        "section": "Triggered Abilities",
        "category": "abilities",
        "text": "Leaves-the-battlefield triggered abilities trigger when a permanent moves from the battlefield to another zone. These are written, 'When [this object] leaves the battlefield, ...'",
    },
    {
        "number": "603.10",
        "section": "Triggered Abilities",
        "category": "abilities",
        "text": "Normally, objects that exist immediately after an event are checked to see if the event matched any trigger conditions, and continuous effects that exist at that time are used to determine what the trigger conditions are and what the objects involved in the event look like.",
    },
    {
        "number": "604.1",
        "section": "Static Abilities",
        "category": "abilities",
        "text": "Static abilities do something all the time rather than being activated or triggered. They are written as statements.",
    },
    {
        "number": "604.2",
        "section": "Static Abilities",
        "category": "abilities",
        "text": "Static abilities create continuous effects, some of which are prevention effects or replacement effects. These effects are active as long as the permanent with the ability remains on the battlefield and has the ability.",
    },
    {
        "number": "602.2",
        "section": "Activated Abilities",
        "category": "abilities",
        "text": "To activate an ability is to put it onto the stack and pay its costs, so that it will eventually resolve and have its effect.",
    },
    {
        "number": "602.3",
        "section": "Activated Abilities",
        "category": "abilities",
        "text": "Some activated abilities are mana abilities. Mana abilities follow special rules: they don't use the stack, and under certain circumstances they can be activated even though another ability or spell is on the stack.",
    },
    {
        "number": "605.1a",
        "section": "Mana Abilities",
        "category": "abilities",
        "text": "An activated ability is a mana ability if it meets all of the following criteria: it doesn't require a target, it could add mana to a player's mana pool when it resolves, and it's not a loyalty ability.",
    },
    {
        "number": "112.1",
        "section": "Abilities",
        "category": "abilities",
        "text": "An ability can be one of three things: an activated ability, a triggered ability, or a static ability.",
    },
    {
        "number": "700.2a",
        "section": "Enters the Battlefield",
        "category": "abilities",
        "text": "An object 'enters the battlefield' when it moves from another zone to the battlefield.",
    },

    # =========================================================================
    # ADDITIONAL COMBAT-RELEVANT RULES
    # =========================================================================
    {
        "number": "702.17a",
        "section": "Skulk",
        "category": "keywords",
        "text": "Skulk is an evasion ability. A creature with skulk can't be blocked by creatures with greater power.",
    },
    {
        "number": "120.3c",
        "section": "Damage to Creatures",
        "category": "damage",
        "text": "Damage dealt to a creature is marked on that creature. If the total damage marked on a creature is equal to or greater than its toughness, that creature has been dealt lethal damage and is destroyed the next time state-based actions are checked.",
    },
    {
        "number": "702.6a",
        "section": "Equip",
        "category": "keywords",
        "text": "Equip is an activated ability of Equipment cards. 'Equip [cost]' means '[Cost]: Attach this permanent to target creature you control. Activate only as a sorcery.'",
    },
    {
        "number": "702.102a",
        "section": "Exploit",
        "category": "keywords",
        "text": "Exploit is a triggered ability. When a creature with exploit enters the battlefield, you may sacrifice a creature.",
    },
    {
        "number": "702.5a",
        "section": "Enchant",
        "category": "keywords",
        "text": "Enchant is a static ability, written 'Enchant [object or player].' The enchant ability restricts what an Aura spell can target and what an Aura can be attached to.",
    },

    # =========================================================================
    # SPECIAL ACTIONS & MISCELLANEOUS
    # =========================================================================
    {
        "number": "116.2a",
        "section": "Special Actions",
        "category": "timing",
        "text": "Playing a land is a special action. To play a land, a player puts that land onto the battlefield from the zone it was in (usually that player's hand). A player can take this action any time they have priority during a main phase of their turn and the stack is empty.",
    },
    {
        "number": "116.2d",
        "section": "Special Actions",
        "category": "timing",
        "text": "Some effects allow a player to take an action at a later time, usually to end a continuous effect or to stop a delayed triggered ability from triggering. Doing so is a special action.",
    },
    {
        "number": "117.5",
        "section": "Timing and Priority",
        "category": "timing",
        "text": "Each time a player would get priority, the game first performs all applicable state-based actions as a single event, then repeats this process until no state-based actions are performed. Then triggered abilities are put on the stack. These steps repeat until no further state-based actions are performed and no abilities trigger.",
    },
    {
        "number": "117.2a",
        "section": "Timing and Priority",
        "category": "timing",
        "text": "At most one player has priority at any given time. The player with priority may cast spells, activate abilities, and take special actions.",
    },
    {
        "number": "117.9",
        "section": "Timing and Priority",
        "category": "timing",
        "text": "If a player casts a spell, activates an ability, or takes a special action, that player receives priority afterward.",
    },
    {
        "number": "118.1",
        "section": "Costs",
        "category": "timing",
        "text": "A cost is an action or payment necessary to take another action or to stop another action from being taken. To pay a cost, a player carries out the instructions specified by the spell, ability, or effect that contains the cost.",
    },
    {
        "number": "118.3",
        "section": "Costs",
        "category": "timing",
        "text": "A player can't pay a cost unless they have the necessary resources to pay it fully. For example, a player with only 1 life can't pay a cost of 2 life, and a permanent that's already tapped can't be tapped to pay a cost.",
    },
    {
        "number": "121.1",
        "section": "Drawing a Card",
        "category": "timing",
        "text": "A player draws a card by putting the top card of their library into their hand. This is done as a turn-based action during each player's draw step.",
    },
    {
        "number": "121.4",
        "section": "Drawing a Card",
        "category": "timing",
        "text": "If an effect instructs a player to draw cards, that player performs each draw as described in rule 121.1, one at a time.",
    },
    {
        "number": "701.3a",
        "section": "Destroy",
        "category": "timing",
        "text": "To destroy a permanent, move it from the battlefield to its owner's graveyard.",
    },
    {
        "number": "701.4a",
        "section": "Discard",
        "category": "timing",
        "text": "To discard a card, move it from its owner's hand to that player's graveyard.",
    },
    {
        "number": "701.7a",
        "section": "Sacrifice",
        "category": "timing",
        "text": "To sacrifice a permanent, its controller moves it from the battlefield directly to its owner's graveyard. A player can't sacrifice something that isn't a permanent, or something they don't control.",
    },
    {
        "number": "701.8a",
        "section": "Exile",
        "category": "timing",
        "text": "To exile an object, move it to the exile zone from wherever it is.",
    },
    {
        "number": "701.5a",
        "section": "Counter",
        "category": "timing",
        "text": "To counter a spell or ability means to cancel it, removing it from the stack. It doesn't resolve and none of its effects occur. A countered spell is put into its owner's graveyard.",
    },
    {
        "number": "701.19a",
        "section": "Scry",
        "category": "timing",
        "text": "To scry N, look at the top N cards of your library, then put any number of them on the bottom of your library in any order and the rest on top of your library in any order.",
    },
    {
        "number": "701.42a",
        "section": "Surveil",
        "category": "timing",
        "text": "To surveil N, look at the top N cards of your library, then put any number of them into your graveyard and the rest on top of your library in any order.",
    },
    {
        "number": "103.4",
        "section": "Mulligan",
        "category": "timing",
        "text": "Each player who wishes to take a mulligan shuffles the cards in their hand back into their library, then draws a new hand of cards equal to their starting hand size. Once a player has decided to keep their hand, that player puts a number of cards from their hand on the bottom of their library equal to the number of times that player has taken a mulligan.",
    },
]
