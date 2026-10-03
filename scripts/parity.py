"""Parity: each query against our full build and against Scryfall (1 request a second, responses cached)."""
import json, sqlite3, sys, time, hashlib, urllib.parse, urllib.request
from pathlib import Path
from oracle_searcher.search import search, register_regexp, Unsupported

CACHE = Path(".scratch/scryfall-cache"); CACHE.mkdir(exist_ok=True)
SUFFIXES = ["", " identity<=UB (legal:commander or date>2026-10-03)", " identity<=BRUW legal:commander"]

def scryfall(q, order):
    key = CACHE / (hashlib.sha1(f"{q}|{order}".encode()).hexdigest() + ".json")
    if key.exists(): return json.loads(key.read_text())
    url = "https://api.scryfall.com/cards/search?" + urllib.parse.urlencode({"q": q, "order": order})
    req = urllib.request.Request(url, headers={"User-Agent": "tcg-oracle-searcher/0.1", "Accept": "application/json"})
    time.sleep(1.0)
    try:
        with urllib.request.urlopen(req, timeout=20) as r: body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404: body = {"data": [], "total_cards": 0}
        else: raise
    out = {"names": [c["name"] for c in body.get("data", [])], "total": body.get("total_cards", 0)}
    key.write_text(json.dumps(out)); return out

conn = sqlite3.connect(".scratch/cards.sqlite"); register_regexp(conn)
rows = []
for base in [l.strip() for l in open("tests/fixtures/purroxy_queries.txt") if l.strip()]:
    for suf in SUFFIXES[: int(sys.argv[1]) if len(sys.argv) > 1 else 3]:
        q = (base + suf).strip()
        try:
            ours = search(conn, q, order="edhrec"); mine = [c["name"] for c in ours.data]; mt = ours.total_cards
        except Unsupported as e:
            rows.append((q, "UNSUPPORTED", str(e))); continue
        sf = scryfall(q, "edhrec")
        a, b = set(mine), set(sf["names"])
        top = len(set(mine[:20]) & set(sf["names"][:20])) / max(1, min(20, len(sf["names"]))) if sf["names"] else (1.0 if not mine else 0.0)
        rows.append((q, f"ours={mt} scryfall={sf['total']} top20={top:.2f}", f"only_ours={sorted(a-b)[:3]} only_sf={sorted(b-a)[:3]}"))
for r in rows: print(" | ".join(r))
