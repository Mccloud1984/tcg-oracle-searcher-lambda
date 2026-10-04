"""Per-tag parity: our `is:`/`has:` count vs Scryfall's `total_cards`, one request per tag.

Usage: python scripts/is_tag_parity.py --db .scratch/cards.sqlite --sweep .scratch/is_tags.json
Scryfall's totals are cached in --cache (default .scratch/is_tag_totals.json); delete it to refetch.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from api.parsing.rewrite import _DERIVED_EXPANSIONS
from oracle_searcher.is_tag_rules import KNOWN_IS_TAGS
from oracle_searcher.is_tag_sweep import _HAS_TAGS, SWEEP_TAGS, apply_sweep
from oracle_searcher.search import search

_HEADERS = {"User-Agent": "tcg-oracle-searcher/0.1", "Accept": "application/json"}


def _query(tag: str) -> str:
    return f"{'has' if tag in _HAS_TAGS else 'is'}:{tag}"


def _kind(tag: str) -> str:
    if tag in SWEEP_TAGS:
        return "sweep"
    return "rule" if tag in KNOWN_IS_TAGS else "otag"


def _scryfall_total(tag: str) -> int:
    url = "https://api.scryfall.com/cards/search?" + urllib.parse.urlencode({"q": _query(tag), "unique": "cards"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=30) as resp:
        return int(json.load(resp)["total_cards"])


def main() -> None:
    """Print a markdown table of tag, kind, ours, Scryfall, delta."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--sweep", required=True)
    parser.add_argument("--cache", default=".scratch/is_tag_totals.json")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "cards.sqlite"
        shutil.copy(args.db, copy)
        conn = sqlite3.connect(copy)
        apply_sweep(conn, args.sweep)
        cache_path = Path(args.cache)
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        tags = sorted(KNOWN_IS_TAGS | {v for (a, v) in _DERIVED_EXPANSIONS if a == "is"})
        print("| tag | kind | ours | scryfall | delta |\n|---|---|---|---|---|")
        for tag in tags:
            if tag not in cache:
                cache[tag] = _scryfall_total(tag)
                cache_path.write_text(json.dumps(cache, indent=1))
                time.sleep(1)
            ours = search(conn, _query(tag), page_size=1).total_cards
            print(f"| {tag} | {_kind(tag)} | {ours} | {cache[tag]} | {ours - cache[tag]:+d} |")


if __name__ == "__main__":
    main()
