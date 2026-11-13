"""Deterministic false-premise concerns for a chosen cast (Fable Nov 2026-11-12).

Two recorded autoplay decisions rested on impossible premises about the card
being cast (evidence /home/joshu/fable-nov-evidence/autoplay-false-premise-
20261112/decisions.json):

- a summoning-sick creature cast now was reasoned about as attacking this turn
  (Skyward Spider T8, Spellbook Vendor T12). The existing validator
  (game_plan.decision_claim_notes) annotates that claim after commitment but
  the selected action stood;
- a creature with no enters-the-battlefield or when-you-cast ability was
  reasoned about as producing an immediate effect on entry (Optimistic
  Scavenger T16: "casting Optimistic Scavenger first adds an Eerie +1/+1
  counter to Veteran Survivor"). Its Eerie trigger waits for an enchantment
  entry or a Room unlock, not its own.

These concerns surface such claims BEFORE the pick is final so the second
look can re-evaluate the choice itself instead of rewriting rationale text.

Conservatism contract (review rounds 2–8):

- A concern is raised only when impossibility is POSITIVELY established:
  the cast card has no own entry/cast ability AND every visible battlefield
  object's oracle text is readable AND none of them supports the claim.
- Visible external support suppresses the concern outright:
  * attack/tap claims survive when a visible ability grants haste broadly
    ("Creatures you control have haste") or via an activated grant;
  * entry/cast-effect claims survive when a visible triggered ability keys
    on casting a spell of the cast card's types ("Whenever you cast a
    creature spell, draw a card") or on a permanent of matching types
    entering ("Whenever an Enchantment enters ...");
  * self-referential abilities ("When this enchantment enters ..."),
    scoped grants ("Enchanted creature ... has haste"), and triggers keyed
    to non-matching types ("Whenever a land you control enters ...") are
    recognised as NOT support but do not create concerns either.
- Only counter placement is a candidate for refutation (review round 4:
  bounded semantic restriction). A counter appearing as a result of the
  cast/entry requires an ability that fires on an event — static continuous
  effects never place counters on their own, so an unsupported counter claim
  about an ability-less cast is positively impossible. Everything else is
  left UNKNOWN rather than refuted:
  * stat modifications ("+1/+1") are real static benefits needing no trigger
    (Master of Etherium's own static ability makes "immediately gives our
    artifact creature tokens +1/+1" TRUE);
  * mentioning tokens/cards/damage does not assert creating them ("token"
    as recipient of a buff is not token creation), and unrepresented
    effects stay unknown;
  * ordinary consequences of a creature resolving — joining the battle as
    a blocker or attacker, raising the creature count — need no ability at
    all and are NEVER refuted.
  The claim must additionally be attributed to the cast/entry itself
  (explicit immediacy marker, or the card as subject of the effect verb);
  effects attributed to other permanents or to scheduled timing are left
  alone.
- Any battlefield object whose oracle text is missing/unreadable makes the
  board scan return ``None`` (unknown) and NO concern is raised: an unseen
  ability could support the claim.
- A summoning-sick note is escalated only when the reasoning itself
  positively asserts THE CAST CREATURE attacking/tapping now (review round
  5: subject/action/time gate). The pre-existing claim validator keys
  loosely on ``attack``, so defensive phrasing ("makes our board much
  harder to attack into"), other creatures attacking and conditional later
  attacks stay unknown — annotated after commitment as before, but never
  turned into a premise concern. An assertion requires ALL of:
  * subject: the action verb is anchored to the cast card's name (directly,
    over a bounded bridge, or via an immediately following pronoun) and no
    other visible permanent's name intervenes as a competing subject;
  * action: an attack/tap verb governed by the cast creature — not an
    infinitive/comparative frame ("harder to attack"), an opponent as the
    attacker ("discourages them from attacking"), or a defensive direction
    ("attacks into us");
  * time: an explicit immediacy marker beside the verb and no future or
    conditional timing in the same local window.
- A denied effect is not an assertion (review round 6): a corrected answer
  may properly NEGATE the premise ("does not immediately put a counter",
  "first puts no counters"); treating that denial as a claim would reject
  the very revision that fixed the false premise. The denial cue must sit
  in the same clause as the effect verb (statement/contrastive breaks reset
  scope), and ambiguous denial scope ("may not put") abstains like any
  other unknown — a missed detection is the safe direction.
- The same denial discipline governs the attack gate (review round 8):
  "it isn't attacking this turn" is a correction, not a self-attack
  assertion, so the summoning-sick note it accompanies is not escalated.
  Scope is clause-local exactly as above — "doesn't attack yet; it attacks
  now" still asserts the attack.
- A conditional attack is a hypothesis, not an assertion (review round 9):
  "attacks now if we grant it haste" promises the attack only under a
  granted condition, so the gate abstains via
  :func:`arenamcp.claim_language.conditional_claim` with the same
  clause-local scope — a condition after a statement break does not soften
  an unconditional assertion ("attacks now; if we grant haste later, even
  better" still asserts).
"""

import re

from arenamcp.claim_language import conditional_claim, negated_claim
from arenamcp.game_plan import _is_creature_card
from arenamcp.play_safety import find_source

_HTML_TAG = re.compile(r"<[^>]+>")
_APOSTROPHE = str.maketrans({"’": "'", "‘": "'"})
_REMINDER_TEXT = re.compile(r"\([^)]*\)")

# A counter appearing as a result of casting/entering now. This is the ONLY
# refutable effect class (review round 4: bounded semantic restriction —
# static continuous effects never place counters without an event, so an
# unsupported counter claim about an ability-less cast is positively
# impossible). Stat buffs ("+1/+1") are real static benefits needing no
# trigger, and mentioning tokens/cards/damage does not assert creating them
# ("gives our artifact creature tokens +1/+1" is Master of Etherium's static
# ability being TRUE) — those stay unknown, never refuted:
#   _COUNTER_EFFECT — a counter-placement verb landing on a counter;
#   _CAST_ATTRIBUTION — the effect tied to the cast/entry itself, via an
#     explicit immediacy marker (the card-as-subject variant is compiled
#     per call in _entry_effect_claims because it needs the escaped name).
_COUNTER_EFFECT = re.compile(r"\b(?:adds?|puts?|places?|gives?)\b[^.;]{0,40}?\bcounters?\b")
_CAST_ATTRIBUTION = re.compile(
    r"\b(?:immediately|right away|at once|as it enters|upon entering"
    r"|on entry|first)\b"
)

# Explicit future/conditional timing is not an immediate-on-cast claim:
# "at the beginning of combat it creates a Role token" is a scheduled trigger,
# not a premise that casting produces the effect now.
_FUTURE_TIMING = re.compile(
    r"\b(?:at the beginning of|beginning of (?:combat|your|the)|next turn"
    r"|the following turn|later(?: this turn)?|on your next|end step"
    r"|end of turn|upkeep|draw step|combat begins|will)\b"
)

# --- board-scan classifiers -------------------------------------------------

_EVENT_WORD = re.compile(r"\bc(?:ast|asts|asting|asted)\b|\benter(?:s|ed)?\b")

# Abilities that talk about their OWN permanent entering are self-referential;
# they neither support nor contradict someone else's entry.
_SELF_REFERENCE = re.compile(
    r"\bthis\s+(?:creature|permanent|enchantment|artifact|aura|land|spell)\b"
    r"|\b~\b"
)

_CAST_TRIGGER = re.compile(r"\b(?:when|whenever)\b[^.;]{0,60}?\bc(?:ast|asts)\b")
_CAST_NOUN = re.compile(
    r"\bc(?:ast|asts)\b\s+(?:out\s+)?(?:a|an|your|another|the)?\s*"
    r"(?:(?:second|third)\s+)?(creature|noncreature|nontoken)?\s*spells?"
)
_ENTER_TRIGGER = re.compile(
    r"\b(?:when|whenever|as)\b[^.;]{0,80}?"
    r"\b(?:a|an|another|each|one\s+or\s+more)\s+"
    r"((?:non)?(?:creature|enchantment|artifact|land)"
    r"|nontoken(?:\s+permanent)?|permanent)"
    r"[^.;]{0,40}?\benters\b"
)

_GLOBAL_HASTE = re.compile(
    r"\b(?:creatures|permanents)\b[^.;]{0,40}?"
    r"\b(?:you\s+control\s+)?[^.;]{0,30}?"
    r"\b(?:have|gain|get)s?\b[^.;]{0,20}?\bhaste\b"
)
_SCOPED_HASTE = re.compile(
    r"\b(?:enchanted|equipped|attached)\b[^.;]{0,40}?\bhaste\b"
    r"|\bhaste\b[^.;]{0,40}?\b(?:enchanted|equipped|attached)\b"
)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n", text) if part.strip()]


def _own_entry_ability(oracle: str, name: str) -> bool:
    """True when the card itself has an ability that fires on its own entry or cast.

    Unknown oracle text conservatively counts as capable (no concern).
    """
    if not oracle:
        return True
    text = _HTML_TAG.sub("", oracle).translate(_APOSTROPHE)
    esc = re.escape(name.casefold())
    for chunk in re.split(r"[.\n]", text):
        c = chunk.strip().casefold()
        if not c:
            continue
        if re.search(r"\bwhen(?:ever)? you cast\b", c):
            return True
        if re.search(
            rf"\b(?:as|when|whenever)\b[^.;]{{0,80}}?\b(?:this creature"
            rf"|this permanent|{esc}|it)\s+enters\b",
            c,
        ):
            return True
        if re.search(r"\benters (?:the battlefield )?with\b", c) and re.search(
            rf"\b(?:this creature|this permanent|{esc})\b|^as\b|^it\b", c
        ):
            return True
    return False


def _entry_effect_claims(name: str, reasoning: str) -> list[str]:
    """Sentences asserting that casting ``name`` itself produces an effect now."""
    esc = re.escape(name.casefold())
    claims: list[str] = []
    for sentence in _sentences(reasoning.translate(_APOSTROPHE)):
        low = sentence.casefold()
        if not re.search(rf"(?<![a-z0-9]){esc}(?![a-z0-9])", low):
            continue
        linked = (
            re.search(rf"\bcast(?:ing|s|ed)?\b[^.;]{{0,60}}\b{esc}\b", low)
            or re.search(
                rf"\b{esc}\b[^.;]{{0,60}}\b(?:hits? the battlefield"
                rf"|enters(?: the battlefield)?)\b",
                low,
            )
            or re.match(rf"^\s*cast(?:ing)?\s+(?:out\s+)?(?:the\s+)?{esc}\b", low)
        )
        if not linked:
            continue
        effect = _COUNTER_EFFECT.search(low)
        if not effect:
            continue
        # Generic immediacy alone is not an ability-mediated claim: the
        # effect must be attributed to the cast/entry itself, either by an
        # explicit immediacy marker or by the card acting as the subject of
        # the effect verb ("<Name> first adds ...").
        attributed = _CAST_ATTRIBUTION.search(low) or re.search(
            rf"\b{esc}\b[^.;]{{0,24}}?\b(?:adds?|puts?|places?|gives?"
            rf"|creates?|makes?|draws?|deals?|destroys?|exiles?|returns?)\b",
            low,
        )
        if not attributed:
            continue
        if _FUTURE_TIMING.search(low):
            continue
        # A conditional framing ("when an enchantment enters it puts a counter")
        # asserts a scheduled trigger, not an immediate-on-cast effect.
        if conditional_claim(low, effect.start(), effect.end()):
            continue
        # A denied effect is not an assertion (review round 6): a corrected
        # answer may properly negate the premise ("does not immediately put
        # a counter", "first puts no counters"); treating that denial as a
        # claim would reject the correction. Ambiguous denial scope also
        # abstains — a concern requires a positively asserted effect.
        if negated_claim(low, linked.start(), effect.end()):
            continue
        claims.append(sentence)
    return claims


# --- premise-1 subject/action/time gate (review round 5) ---------------------

# Attack/tap verbs as full tokens. Bare "attack" (as in "harder to attack
# into") is deliberately NOT matched: the loose validator already keys on it
# and this gate must be stricter than what it gates.
_SICK_ACTION_VERB = re.compile(r"\b(?:attacks?|attacking|taps?|tapping)\b")
_IMMEDIATE_TIME = re.compile(r"\b(?:now|immediately|right away|at once|this turn)\b")
_INFINITIVE_FRAME = re.compile(r"\b(?:to|into)\s*$")
_OPPONENT_SUBJECT = re.compile(r"\b(?:them|they|their|opponents?|enem\w+)\b[^.;]{0,16}$")
_DEFENSIVE_DIRECTION = re.compile(r"^\s*(?:into|through)\b")


def _self_attack_assertions(name: str, reasoning: str, game_state: dict) -> list[str]:
    """Sentences positively asserting ``name`` itself attacks/taps this turn.

    Subject/action/time gate for elevating a summoning-sick note into a
    premise concern (review round 5). The pre-existing claim validator keys
    loosely on ``attack``, so defensive phrasing ("makes our board much
    harder to attack into"), other creatures attacking and conditional later
    attacks must never escalate. An assertion requires ALL of:

    - subject: the action verb is anchored to the cast card's name — directly
      over a bounded bridge, or via an immediately following pronoun — and no
      other visible permanent's name intervenes as a competing subject;
    - action: an attack/tap verb governed by the cast creature, not an
      infinitive/comparative frame ("harder to attack"), an opponent as the
      attacker ("discourages them from attacking"), or a defensive direction
      ("attacks into us");
    - time: an explicit immediacy marker beside the verb and no future or
      conditional timing in the same local window;
    - polarity: the attack is not denied in its clause (review round 8 —
      "it isn't attacking this turn" is a correction, not an assertion;
      clause-scoped via :func:`arenamcp.claim_language.negated_claim`, so a
      statement break resets scope and a later positive assertion counts);
    - modality: the attack is not conditioned on something happening
      (review round 9 — "attacks now if we grant it haste" is a hypothesis
      about granted haste, clause-scoped via
      :func:`arenamcp.claim_language.conditional_claim`, so a condition in
      a later clause does not soften an unconditional assertion).
    """
    esc = re.escape(name.casefold())
    others = [
        str(obj.get("name")).casefold()
        for obj in _visible_objects(game_state)
        if obj.get("name") and str(obj.get("name")).casefold() != name.casefold()
    ]
    assertions: list[str] = []
    for sentence in _sentences(reasoning.translate(_APOSTROPHE)):
        low = sentence.casefold()
        for m in _SICK_ACTION_VERB.finditer(low):
            prefix = low[: m.start()]
            for nm in re.finditer(rf"(?<![a-z0-9]){esc}(?![a-z0-9])", prefix):
                bridge = low[nm.end() : m.start()]
                if "." in bridge:
                    continue  # sentence boundary between name and verb
                pronoun = re.search(r"\bit\b", bridge)
                if pronoun:
                    # "<Name> ... it attacks" — pronoun chain from the name.
                    if len(bridge[: pronoun.start()]) > 60 or len(bridge[pronoun.end() :]) > 24:
                        continue
                elif len(bridge) > 40:
                    continue  # direct anchor: bounded bridge only
                if any(other and other in bridge for other in others):
                    break  # another visible permanent is the competing subject
                if _INFINITIVE_FRAME.search(bridge):
                    break  # "harder to attack ..." — implicit/opponent subject
                if _OPPONENT_SUBJECT.search(bridge):
                    break  # "... discourages them from attacking ..."
                window = low[m.end() : m.end() + 40]
                if _DEFENSIVE_DIRECTION.match(window):
                    break  # "... attacks into us" is the opponent's action
                immediate = _IMMEDIATE_TIME.search(window)
                if immediate is None:
                    break  # no explicit this-turn timing beside the verb
                if _FUTURE_TIMING.search(bridge) or _FUTURE_TIMING.search(window):
                    break  # scheduled/conditional attack, not now
                # A denied attack is not an assertion (review round 8): a
                # corrected answer may properly negate the premise ("it
                # isn't attacking this turn"); treating that denial as a
                # claim would reject the very revision that fixed the false
                # premise. Clause-scoped like the entry-effect gate: a
                # statement break resets scope, so a positive assertion in
                # a later clause ("doesn't attack yet; it attacks now")
                # still counts, and ambiguous denial scope abstains.
                if negated_claim(low, nm.start(), m.end()):
                    continue
                # A conditional attack is a hypothesis, not an assertion
                # (review round 9): "attacks now if we grant it haste"
                # promises the attack only under a granted condition, so
                # the gate abstains via claim_language.conditional_claim.
                # The condition window extends to the end of the immediacy
                # marker (the claim's tail), keeping the scope clause-local:
                # a condition after a statement break does not soften an
                # unconditional assertion ("attacks now; if we grant haste
                # later, even better" still asserts).
                if conditional_claim(low, nm.start(), m.end() + immediate.end()):
                    continue
                assertions.append(sentence)
                break
            else:
                continue
            break
    return assertions


def _type_words(card) -> frozenset[str]:
    """Permanent-type words relevant to cast/entry triggers."""
    line = str(card.get("type_line") or "").casefold()
    bases = ("creature", "enchantment", "artifact", "land", "planeswalker")
    words = {base for base in bases if base in line}
    for entry in card.get("card_types") or []:
        low = str(entry).casefold()
        words.update(base for base in bases if base in low)
    return frozenset(words)


def _classify_haste_clause(c: str, owner_esc: str):
    """True=broad/activated haste grant; False=self/scoped/irrelevant; None=unknown."""
    if "haste" not in c:
        return False
    if _GLOBAL_HASTE.search(c):
        return True
    if _SCOPED_HASTE.search(c):
        return False
    if owner_esc and re.search(owner_esc + r"[^.;]{0,40}\bhaste\b", c):
        return False  # "<This permanent> has haste": about itself only.
    if re.search(r"\bhas\s+haste\b", c):
        return False  # Third-person self-style grant ("<X> has haste").
    if re.search(r"\bgains?\s+haste\b", c):
        return True  # Activated/static grant ("Target creature gains haste").
    if re.fullmatch(r"haste[.!]?", c):
        return False  # Bare keyword line.
    return None


def _classify_event_clause(c: str, type_words: frozenset[str]):
    """True=trigger supports this cast/entry; False=known irrelevant; None=unknown."""
    if not _EVENT_WORD.search(c):
        return False

    def correlates(noun: str) -> bool:
        if noun == "creature":
            return "creature" in type_words
        if noun == "noncreature":
            return "creature" not in type_words
        if noun == "enchantment":
            return "enchantment" in type_words
        if noun == "artifact":
            return "artifact" in type_words
        if noun == "land":
            return "land" in type_words
        # permanent / nontoken / unparsed-but-present noun: any card qualifies.
        return True

    verdicts: list[bool] = []
    if _CAST_TRIGGER.search(c):
        m = _CAST_NOUN.search(c)
        verdicts.append(correlates(m.group(1)) if m and m.group(1) else True)
    m = _ENTER_TRIGGER.search(c)
    if m:
        verdicts.append(correlates(m.group(1)))
    if verdicts:
        return any(verdicts)
    if _SELF_REFERENCE.search(c):
        # Self-referential ability phrased outside our trigger grammar ("As this
        # land enters ..."): about its own permanent, not external support.
        return False
    # Event mention we could not confidently classify (unusual phrasing).
    return None


def _visible_objects(game_state: dict) -> list[dict]:
    objs: list[dict] = []
    battlefield = game_state.get("battlefield")
    if isinstance(battlefield, list):
        objs.extend(o for o in battlefield if isinstance(o, dict))
    command = game_state.get("command")
    if isinstance(command, list):
        # Command-zone objects without oracle text are skipped rather than read
        # as unknown; their cast/entry triggers are out of scope here.
        objs.extend(o for o in command if isinstance(o, dict) and isinstance(o.get("oracle_text"), str))
    return objs


_CONNECTIVE_START = re.compile(r"^(?:if|otherwise|then|and|or|unless|but|except|plus|that|whose)\b")


def _clauses(text: str) -> list[str]:
    """Oracle clauses, with continuation fragments merged into their sentence.

    Splitting on sentence ends orphans fragments like "If you don't, it enters
    tapped" from "As this land enters, you may pay 2 life."; a fragment that
    starts with a connective continues the previous clause.
    """
    merged: list[str] = []
    for part in re.split(r"[.\n]", text):
        p = part.strip()
        if not p:
            continue
        if merged and _CONNECTIVE_START.match(p.casefold()):
            merged[-1] += "; " + p
        else:
            merged.append(p)
    return merged


def _scan_board(game_state: dict, classify) -> bool | None:
    """Scan every visible permanent's oracle text with ``classify``.

    True when some visible ability positively supports the claim; False when
    every battlefield object's oracle text is readable and none does; None when
    any battlefield object's oracle text is missing/unreadable (an unseen
    ability could support the claim — conservatively no concern).
    """
    for obj in _visible_objects(game_state):
        oracle = obj.get("oracle_text")
        if not isinstance(oracle, str):
            return None
        text = _REMINDER_TEXT.sub("", _HTML_TAG.sub("", oracle)).translate(_APOSTROPHE)
        owner_esc = re.escape(str(obj.get("name") or "").casefold())
        for chunk in _clauses(text):
            c = chunk.casefold()
            if not c:
                continue
            verdict = classify(c, owner_esc)
            if verdict is True:
                return True
            if verdict is None:
                return None
    return False


def cast_premise_concerns(decision, game_state: dict, chosen: list[str], reasoning) -> list[str]:
    """Impossible-premise concerns about the casts in ``chosen``.

    Returns human-readable concern sentences; empty when nothing impossible is
    asserted OR when visible external support / unreadable oracle text makes
    impossibility unverifiable. Never mutates state and never invents a
    replacement choice.
    """
    if not isinstance(game_state, dict) or not chosen:
        return []
    text = reasoning if isinstance(reasoning, str) else ""
    if not text:
        return []
    concerns: list[str] = []
    from arenamcp.game_plan import decision_claim_notes

    for oid in chosen:
        option = decision.find(oid) if decision is not None else None
        meta = (getattr(option, "meta", None) or {}) if option is not None else {}
        if meta.get("actionType") != "ActionType_Cast":
            continue
        card = find_source(game_state, meta)
        if not card or not _is_creature_card(card):
            continue
        name = str(card.get("name") or "")
        if not name:
            continue
        instance_id = card.get("instance_id")
        cast_ids = {instance_id} if instance_id is not None else set()

        # Premise 1: a summoning-sick creature cast now attacking/tapping this turn.
        sick_notes = [
            note
            for note in decision_claim_notes(text, game_state, cast_ids=cast_ids)
            if name.casefold() in note.casefold() and "summoning sick" in note.casefold()
        ]
        # Only assert impossibility when no visible ability grants haste, no
        # oracle text is unreadable, AND the reasoning itself positively
        # asserts this creature attacking/tapping now (round-5 subject/
        # action/time gate — defensive phrasing, other attackers and
        # conditional later attacks stay unknown).
        if (
            sick_notes
            and _scan_board(game_state, _classify_haste_clause) is False
            and _self_attack_assertions(name, text, game_state)
        ):
            for note in sick_notes:
                concern = (
                    f"{note}. Re-evaluate the play without counting on {name} attacking or tapping this turn."
                )
                if concern not in concerns:
                    concerns.append(concern)

        # Premise 2: casting a creature with no entry/cast ability producing an effect.
        oracle = str(card.get("oracle_text") or "")
        if _own_entry_ability(oracle, name):
            continue
        claims = _entry_effect_claims(name, text)
        type_words = _type_words(card)

        def support_scan(clause: str, _owner_esc: str, _tw=frozenset(type_words)):
            return _classify_event_clause(clause, _tw)

        # Suppress when a visible trigger supports the claim OR any oracle text
        # on the battlefield is unreadable (conservative unknown).
        if claims and _scan_board(game_state, support_scan) is False:
            for claim in claims:
                concern = (
                    f'"{claim[:180]}" — casting {name} does not produce that effect '
                    f"by itself: {name} has no enters-the-battlefield or "
                    f"when-you-cast ability and no visible permanent's ability "
                    f"triggers on casting it or on its entry. Do not count on an "
                    f"immediate effect from casting {name}."
                )
                if concern not in concerns:
                    concerns.append(concern)
    return concerns
