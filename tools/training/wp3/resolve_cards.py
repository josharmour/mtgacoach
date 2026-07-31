#!/usr/bin/env python3
"""Resolve XMage deck card names against Scryfall.
Builds tools/training/magezero_card_map.json with canonical names.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DECK_DIR = "/home/joshu/repos/magezero/xmage/decks"
DECK_FILES = [
    "UWTempo.dck",
    "Standard-MonoR.dck",
    "Standard-MonoG.dck",
    "Standard-MonoB.dck",
    "Standard-MonoW.dck",
    "Standard-MonoU.dck",
]
CACHE_PATH = "tools/training/wp3/scryfall_cache.json"
OUTPUT_PATH = "tools/training/magezero_card_map.json"


def extract_card_names():
    all_names = set()
    ref_map = {}  # (set_code, set_num) -> True

    for fname in DECK_FILES:
        path = os.path.join(DECK_DIR, fname)
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip().rstrip("\r")
                if not line or line.startswith("LAYOUT"):
                    continue
                m = re.match(r"^(\d+)\s+\[(\w+):(\w+)\]\s+(.+)$", line)
                if m:
                    name = m.group(4).strip()
                    all_names.add(name)
                    ref_map[(m.group(2), m.group(3))] = True

    # Layout-only refs (not in card lines)
    layout_only = set()
    for fname in DECK_FILES:
        path = os.path.join(DECK_DIR, fname)
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip().rstrip("\r")
                if line.startswith("LAYOUT"):
                    for m in re.finditer(r"\[(\w+):(\w+)\]", line):
                        key = (m.group(1), m.group(2))
                        if key not in ref_map:
                            layout_only.add(key)

    return sorted(all_names), layout_only


def _scryfall_request(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "ArenaMCP-WP3/1.0")
    req.add_header("Accept", "application/json")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 2))
                time.sleep(retry_after * (attempt + 1))
                continue
            if e.code == 404:
                return None
            raise
    return None


def scryfall_exact(name):
    url = f"https://api.scryfall.com/cards/named?exact={urllib.parse.quote(name)}"
    return _scryfall_request(url)


def scryfall_fuzzy(name):
    url = f"https://api.scryfall.com/cards/named?fuzzy={urllib.parse.quote(name)}"
    return _scryfall_request(url)


def scryfall_setnum(set_code, set_num):
    url = f"https://api.scryfall.com/cards/{set_code.lower()}/{set_num}"
    return _scryfall_request(url)


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def resolve_card(name, cache):
    if name in cache:
        return cache[name]

    result = scryfall_exact(name)
    time.sleep(0.2)

    exact = True
    if result is None:
        result = scryfall_fuzzy(name)
        time.sleep(0.2)
        exact = False

    entry = {
        "scryfall": result["name"] if result else name,
        "type_line": result.get("type_line", "?") if result else "?",
        "mana_cost": result.get("mana_cost", "") if result else "",
        "exact": exact,
        "found": result is not None,
    }
    cache[name] = entry
    if len(cache) % 10 == 0:
        save_cache(cache)
    return entry


def main():
    names, layout_only = extract_card_names()
    print(f"Card names from lines: {len(names)}")
    print(f"Layout-only refs: {len(layout_only)} items: {[f'{s}:{n}' for s, n in sorted(layout_only)]}")

    cache = load_cache()
    card_map = {}

    for name in names:
        entry = resolve_card(name, cache)
        card_map[name] = entry
        status = "EXACT" if entry["exact"] else ("FUZZY" if entry["found"] else "NOT FOUND")
        print(f"  {status:10s} {name:45s} -> {entry['scryfall']}")
        time.sleep(0.05)

    # Resolve layout-only refs
    for set_code, set_num in sorted(layout_only):
        _key = f"__layout__{set_code}:{set_num}"
        if _key in cache:
            entry = cache[_key]
        else:
            result = scryfall_setnum(set_code, set_num)
            time.sleep(0.1)
            if result:
                scryfall_name = result["name"]
                entry = {
                    "scryfall": scryfall_name,
                    "type_line": result.get("type_line", "?"),
                    "mana_cost": result.get("mana_cost", ""),
                    "exact": scryfall_name in names,
                    "found": True,
                }
            else:
                entry = {
                    "scryfall": f"?{set_code}:{set_num}?",
                    "type_line": "?",
                    "mana_cost": "",
                    "exact": False,
                    "found": False,
                }
            cache[_key] = entry
            save_cache(cache)

        status = "LAYOUT" if entry["found"] else "LAYOUT-NF"
        print(f"  {status:10s} [{set_code}:{set_num}] -> {entry['scryfall']}")
        if entry["found"] and entry["scryfall"] not in card_map:
            card_map[entry["scryfall"]] = dict(entry)
            print(f"    -> added '{entry['scryfall']}' to card_map")

    # Write output
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(card_map, f, indent=2, sort_keys=True)

    exact = sum(1 for v in card_map.values() if v.get("exact"))
    fuzzy = sum(1 for v in card_map.values() if v.get("found") and not v.get("exact"))
    nfound = sum(1 for v in card_map.values() if not v.get("found"))

    print(f"\nWrote {len(card_map)} entries to {OUTPUT_PATH}")
    print(f"  Exact:   {exact}")
    print(f"  Fuzzy:   {fuzzy}")
    print(f"  Missing: {nfound}")
    return 0 if nfound == 0 else 1


if __name__ == "__main__":
    exit(main())
