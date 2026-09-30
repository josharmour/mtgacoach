import json, urllib.request, urllib.parse

UA = {"User-Agent": "ArenaImportCheck/1.0", "Accept": "application/json"}

def api(path):
    req = urllib.request.Request("https://api.scryfall.com" + path, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=30))

# MDFC: Disciple of Freyalise
for name in ["Disciple of Freyalise", "Garden of Freyalise"]:
    q = urllib.parse.quote(f'!"{name}"')
    d = api(f"/cards/search?q={q}")
    if "data" in d and d["data"]:
        for c in d["data"][:2]:
            print(f"{name}: {c['name']} | {c['set']} games={c.get('games')}")
    else:
        print(name, "-> NOT FOUND", d.get("details"))

# sanity: fuzzy search for Blinkmoth/Ulamog's Crusher with unique:prints variant
for name in ["Blinkmoth Nexus", "Ulamog's Crusher"]:
    q = urllib.parse.quote(f'{name} unique:prints')
    d = api(f"/cards/search?q={q}")
    if "data" in d:
        sets = sorted({c["set"] for c in d["data"]})
        arena = any("arena" in c.get("games", []) for c in d["data"])
        print(f"{name}: sets={sets[:8]} arena={arena} total={d.get('total_cards')}")
    else:
        print(name, "-> ERROR", d.get("details"))