#!/usr/bin/env python3
"""Oracle-text coverage audit for rendered training/gate prompts.

The transfer medium for card generalization is TEXT: a bare card name in a
prompt teaches name->action; oracle text teaches role->action (#461 fixed
lands; this audits everything). This module measures, per rendered prompt,
which card-name mentions carry that card's oracle text in the SAME prompt,
split by context:

  menu        — numbered "Legal:"/"Candidate:" entries (production menus are
                bare names by design; reported, not "fixed")
  hand        — HAND: section
  your_board  — YOUR BOARD: section
  opp_board   — OPP BOARD: section
  attacking   — ATTACKING YOU: section (combat-gate shape)

Classification per (record, context, distinct name) mention:

  covered       oracle text resolves locally AND its normalized prefix appears
                in the prompt
  uncovered     oracle text resolves locally but does NOT appear in the prompt
  keyword_only  oracle text is only evergreen keywords — the production
                formatter deliberately renders these as flags, not text
  no_text       the card genuinely has no oracle text (vanilla creature,
                basic land reminder-only)
  unresolved    no LOCAL oracle source for this name (counted, never guessed)

FAIL CLOSED: lines that cannot be parsed are counted per context
(``unparsed_lines``), never silently skipped; headline coverage counts
``unresolved`` in the denominator.

Lookup sources (local only, no network):
  1. tools/training/magezero_card_map.json (enriched with oracle_text)
  2. optional Scryfall bulk cache (~/.arenamcp/cache/scryfall/
     default_cards.json) via ``build_lookup(use_bulk=True)`` — needed for
     gate corpora built from real Arena replays.

CLI
---
    python3 tools/training/oracle_coverage.py FILE.jsonl [...] [--bulk] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAP_PATH = REPO / "tools" / "training" / "magezero_card_map.json"
BULK_PATH = Path.home() / ".arenamcp" / "cache" / "scryfall" / "default_cards.json"

CONTEXTS = ("menu", "hand", "your_board", "opp_board", "attacking")

# Mirrors the keyword-only suppression set in coach.py _format_board_card.
_KEYWORD_WORDS = {
    "flying", "reach", "haste", "vigilance", "trample", "first", "strike",
    "double", "deathtouch", "lifelink", "menace", "ward", "hexproof",
    "indestructible", "defender",
}

_MIN_MATCH_CHARS = 12
_PREFIX_CHARS = 30


# ---------------------------------------------------------------------------
# Normalization + matching
# ---------------------------------------------------------------------------

_RE_BRACES = re.compile(r"\{[^}]*\}")
_RE_REMINDER = re.compile(r"\([^)]*\)")
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_NONALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize(text: str) -> str:
    """Lowercase, strip mana symbols / reminder text / markup, collapse to
    alphanumeric words. Same function is applied to prompts and oracle text so
    Scryfall-vs-MTGA symbol dialects ({T} vs {oT}) cannot cause a mismatch."""
    t = text.lower()
    t = _RE_TAGS.sub(" ", t)
    t = _RE_BRACES.sub(" ", t)
    t = _RE_REMINDER.sub(" ", t)
    t = _RE_NONALNUM.sub(" ", t)
    return " ".join(t.split())


def is_keyword_only(oracle_norm: str) -> bool:
    words = [w for w in oracle_norm.split() if w]
    return bool(words) and all(w in _KEYWORD_WORDS for w in words)


def classify_mention(name: str, prompt_norm: str, lookup) -> str:
    """Classify one card-name mention against the normalized prompt."""
    oracle = lookup(name)
    if oracle is None:
        return "unresolved"
    oracle_norm = normalize(oracle)
    if not oracle_norm:
        return "no_text"
    if is_keyword_only(oracle_norm):
        return "keyword_only"
    if len(oracle_norm) < _MIN_MATCH_CHARS:
        # Too short to match reliably; treat presence of the full normalized
        # text as the criterion.
        return "covered" if oracle_norm in prompt_norm else "uncovered"
    if oracle_norm[:_PREFIX_CHARS] in prompt_norm:
        return "covered"
    # DFC / multi-part oracle: any face's prefix counts.
    for part in oracle.split(" // "):
        pn = normalize(part)
        if len(pn) >= _MIN_MATCH_CHARS and pn[:_PREFIX_CHARS] in prompt_norm:
            return "covered"
    return "uncovered"


# ---------------------------------------------------------------------------
# Lookup construction
# ---------------------------------------------------------------------------


def _oracle_from_bulk_card(card: dict) -> str:
    text = card.get("oracle_text")
    if text:
        return text
    faces = card.get("card_faces") or []
    return " // ".join(f.get("oracle_text") or "" for f in faces).strip(" /")


def build_lookup(map_path: Path = MAP_PATH, use_bulk: bool = False, bulk_path: Path = BULK_PATH):
    """Return ``lookup(name) -> str | None`` (None = no local source).

    The card map is authoritative for MageZero/XMage names; the optional bulk
    index covers arbitrary Arena card names in real-replay gate corpora.
    """
    by_name: dict[str, str] = {}
    try:
        card_map = json.loads(Path(map_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        card_map = {}
    for name, entry in card_map.items():
        if "oracle_text" in entry:
            by_name[name.lower()] = entry["oracle_text"]
            canon = entry.get("scryfall") or ""
            if canon:
                by_name.setdefault(canon.lower(), entry["oracle_text"])

    bulk: dict[str, str] = {}
    if use_bulk:
        with open(bulk_path, encoding="utf-8") as f:
            cards = json.load(f)
        for c in cards:
            nm = (c.get("name") or "").lower()
            if not nm:
                continue
            layout = c.get("layout") or ""
            if "token" in layout:
                bulk.setdefault(nm + " token", _oracle_from_bulk_card(c))
                bulk.setdefault(nm, _oracle_from_bulk_card(c))
            else:
                if nm not in bulk:
                    bulk[nm] = _oracle_from_bulk_card(c)
                for face in c.get("card_faces") or []:
                    fn = (face.get("name") or "").lower()
                    if fn:
                        bulk.setdefault(fn, face.get("oracle_text") or "")

    def lookup(name: str) -> str | None:
        low = name.lower()
        if low in by_name:
            return by_name[low]
        if low in bulk:
            return bulk[low]
        return None

    return lookup


# ---------------------------------------------------------------------------
# Prompt parsing
# ---------------------------------------------------------------------------

_RE_MENU_LINE = re.compile(r"^\s+\d+\.\s+(.*)$")
# P/T tokens include live-substituted forms: 3/3, */*, */3, 1+*/2, X/X
_RE_PT_SUFFIX = re.compile(r"\s+[\dXx*+]*[\d*X]/[\dXx*+]*[\d*X]$")
_RE_HASH_SUFFIX = re.compile(r"\s+#\d+$")
_RE_XN_SUFFIX = re.compile(r"\s+x\d+$")
_RE_PAREN_PT = re.compile(r"\s*\([\dXx*+]*[\d*X]/[\dXx*+]*[\d*X]\)")

_SECTION_HEADERS = {
    "YOUR BOARD:": "your_board",
    "OPP BOARD:": "opp_board",
    "ATTACKING YOU:": "attacking",
    "HAND:": "hand",
}
# Lines that terminate a board/hand section (combat analysis, zones, plans).
_RE_SECTION_BREAK = re.compile(
    r"^(Atk:|Crackback:|Computed optimal|!!|⚠|STACK|GRAVEYARD|REVEALED|"
    r"OPP REVEALED|RECENT|EXCLUDED|Respond with|GAME PLAN|TURN PLAN|"
    r"TURN CONSISTENCY|Decision:|BOARD: Empty|=== |CANDIDATE-MENU|Opp hand:|"
    r"On the (play|draw)\.|Pending decision:|Life: |Mana: |Land: |Timing: |"
    r"T\d+ (YOUR|OPP) )"
)

_MENU_PREFIXES = (
    ("Play Land: ", "strip"),
    ("Play ", "strip"),
    ("Cast ", "strip_ok"),
    ("Attack with: ", "strip_pt"),
    ("Do not attack with ", "strip"),
    ("Attack with ", "strip"),
    ("Block with: ", "strip"),
    ("Do not block with ", "strip"),
    ("Activate Ability: ", "strip"),
    ("use ", "strip"),
    ("Activate ", "strip"),
    ("Search out ", "strip"),
)
_NO_CARD_MENU = re.compile(
    r"^(Pass$|Done\b|Action: |Keep\b|Mulligan\b|Play$|Draw$|Cancel\b|"
    r"No attacks|No blocks|Decline to block|Declare no attackers|"
    r"Declare no blockers|Fail to find)"
)


def _clean_name(name: str) -> str:
    name = name.strip()
    name = _RE_PAREN_PT.sub("", name)
    name = re.sub(r"\s*\[0 POWER[^\]]*\]", "", name)
    # Suffixes stack ("Llanowar Elves #1 1/1", "Nightmare x2 */*") — strip to
    # a fixpoint, not a single pass.
    while True:
        before = name
        for pat in (_RE_HASH_SUFFIX, _RE_PT_SUFFIX, _RE_XN_SUFFIX):
            name = pat.sub("", name)
        if name == before:
            break
    if name.startswith("*"):  # token marker
        name = name[1:]
    return name.strip()


def _menu_entry_names(entry: str, unparsed: Counter) -> list[str]:
    entry = entry.strip()
    if _NO_CARD_MENU.match(entry):
        return []
    # XMage renders activated abilities as raw ability text ("{T}: Draw a
    # card, then discard a card."). Those entries carry their own rules text
    # and no card name — they are not coverage gaps.
    if entry.startswith("{"):
        return []
    # "Block X with Y" (MZ block menus) — two names, split on last " with ".
    if entry.startswith("Block ") and not entry.startswith("Block with"):
        body = entry[len("Block "):]
        if " with " in body:
            attacker, blocker = body.rsplit(" with ", 1)
            return [_clean_name(attacker), _clean_name(blocker)]
    # "Target X with Y's ability" (production ability-target rows).
    if entry.startswith("Target ") and entry.endswith("'s ability") and " with " in entry:
        body = entry[len("Target "):-len("'s ability")]
        target, source = body.rsplit(" with ", 1)
        return [_clean_name(target), _clean_name(source)]
    for prefix, mode in _MENU_PREFIXES:
        if entry.startswith(prefix):
            rest = entry[len(prefix):]
            if mode == "strip_ok":
                rest = re.sub(r"\s*\[OK\]$", "", rest)
            if mode == "strip_pt":
                rest = _RE_PAREN_PT.sub("", rest)
                rest = re.sub(r"\s*\[0 POWER[^\]]*\]", "", rest)
            return [_clean_name(rest)]
    unparsed["menu"] += 1
    return []


_RE_HAND_CARD = re.compile(
    r"^\s{2}(\S.*?)"                      # display name (+ optional tag/cost)
    r"(?:\s+\((?:AURA|ENCHANT|EQUIP|ART|PW)\))?"
    r"(?:\s+\{[^\s\[]*\})?"               # mana cost
    r"\s+\[[SI],[^\]]*\]"                 # [timing,castability]
)


def _board_line_name(line: str) -> str | None:
    """Board-section line -> card name, or None if it is not a card line."""
    if not line.startswith("  ") or line.startswith("    ") or line.strip() == "":
        return None
    body = line[2:]
    if body.startswith((">>", "If ", "(empty)")):
        return None
    # Combat-gate shape appends " - <oracle>"; production appends flag/counter
    # suffixes.
    body = body.split(" - ")[0]
    body = re.sub(r"\s*\[[^\]]*\]", "", body)      # [T,FLY] / [tapped] / [cannot attack: ...]
    body = re.sub(r"\s*\([^)]*\)", "", body)       # (Type — Sub) / (2Coun)
    return _clean_name(body) or None


def audit_prompt(user: str, lookup, stats: dict) -> None:
    """Accumulate mention classifications for one rendered prompt."""
    prompt_norm = normalize(user)
    lines = user.splitlines()
    context: str | None = None
    in_menu = False
    seen: set[tuple[str, str]] = set()

    def _hit(ctx: str, name: str) -> None:
        if not name or (ctx, name) in seen:
            return
        seen.add((ctx, name))
        cls = classify_mention(name, prompt_norm, lookup)
        stats[ctx][cls] += 1
        if cls in ("uncovered", "unresolved"):
            stats["examples"][ctx].setdefault(cls, [])
            ex = stats["examples"][ctx][cls]
            if len(ex) < 8 and name not in ex:
                ex.append(name)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("Legal:", "Candidate:")):
            in_menu = True
            context = None
            continue
        if in_menu:
            m = _RE_MENU_LINE.match(line)
            if m:
                for name in _menu_entry_names(m.group(1), stats["unparsed_lines"]):
                    _hit("menu", name)
                continue
            in_menu = False
        header = _SECTION_HEADERS.get(stripped)
        if header:
            context = header
            continue
        if context and _RE_SECTION_BREAK.match(stripped) and not line.startswith("  "):
            context = None
            continue
        if context == "hand":
            m = _RE_HAND_CARD.match(line)
            if m:
                _hit("hand", _clean_name(m.group(1)))
            # non-matching lines are oracle continuation / analysis text
            continue
        if context in ("your_board", "opp_board", "attacking"):
            name = _board_line_name(line)
            if name:
                _hit(context, name)
            continue


def new_stats() -> dict:
    stats: dict = {ctx: Counter() for ctx in CONTEXTS}
    stats["unparsed_lines"] = Counter()
    stats["examples"] = {ctx: {} for ctx in CONTEXTS}
    return stats


def audit_records(records, lookup, stats: dict | None = None) -> dict:
    """Audit an iterable of rendered records (dicts with a ``user`` field)."""
    stats = stats if stats is not None else new_stats()
    n = 0
    for rec in records:
        user = rec.get("user") or ""
        if not user:
            stats["unparsed_lines"]["record_without_user"] += 1
            continue
        audit_prompt(user, lookup, stats)
        n += 1
    stats["records_audited"] = stats.get("records_audited", 0) + n
    return stats


def summarize(stats: dict) -> dict:
    """Per-context percentages + overall coverage (fail-closed denominator).

    coverage = covered / (covered + uncovered + unresolved).
    keyword_only and no_text are excluded: there is no text to attach.
    """
    out: dict = {"contexts": {}, "records_audited": stats.get("records_audited", 0)}
    tot_cov = tot_den = 0
    for ctx in CONTEXTS:
        c = stats[ctx]
        denom = c["covered"] + c["uncovered"] + c["unresolved"]
        out["contexts"][ctx] = {
            "covered": c["covered"],
            "uncovered": c["uncovered"],
            "unresolved": c["unresolved"],
            "keyword_only": c["keyword_only"],
            "no_text": c["no_text"],
            "coverage": round(c["covered"] / denom, 4) if denom else None,
        }
        tot_cov += c["covered"]
        tot_den += denom
    out["overall_coverage"] = round(tot_cov / tot_den, 4) if tot_den else None
    out["overall_mentions_with_text_expected"] = tot_den
    out["unparsed_lines"] = dict(stats["unparsed_lines"])
    out["examples"] = {
        ctx: {k: v[:5] for k, v in stats["examples"][ctx].items()}
        for ctx in CONTEXTS
        if stats["examples"][ctx]
    }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _iter_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Oracle-text coverage audit over rendered prompt JSONL files.")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--bulk", action="store_true",
                    help="also load the local Scryfall bulk cache (needed for real-replay gate corpora)")
    ap.add_argument("--json", type=Path, default=None, help="write summary JSON here")
    args = ap.parse_args(argv)

    lookup = build_lookup(use_bulk=args.bulk)
    results: dict[str, dict] = {}
    for path in args.files:
        stats = audit_records(_iter_jsonl(path), lookup)
        summary = summarize(stats)
        results[str(path)] = summary
        print(f"\n=== {path} ({summary['records_audited']} records) ===")
        for ctx, row in summary["contexts"].items():
            cov = row["coverage"]
            cov_s = f"{cov * 100:5.1f}%" if cov is not None else "  n/a"
            print(f"  {ctx:<11s} coverage={cov_s}  covered={row['covered']:<6d} "
                  f"uncovered={row['uncovered']:<6d} unresolved={row['unresolved']:<5d} "
                  f"keyword_only={row['keyword_only']:<5d} no_text={row['no_text']}")
        oc = summary["overall_coverage"]
        print(f"  overall     {oc * 100:.1f}%" if oc is not None else "  overall     n/a")
        if summary["unparsed_lines"]:
            print(f"  unparsed_lines: {summary['unparsed_lines']}")
        for ctx, ex in summary.get("examples", {}).items():
            for cls, names in ex.items():
                print(f"  example {ctx}/{cls}: {names}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
