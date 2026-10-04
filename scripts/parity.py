"""Parity harness: run Purroxy's real queries against a full build and against Scryfall.

Compares our `search()` to Scryfall's `/cards/search` (paced to at most one request a second,
every response cached under `.scratch/scryfall-cache/`, never called from a test -- see this
repo's docs/PLAN-2026-10-03.md). For each base query in `--queries`, plus that query with each
composite suffix from `--suffixes` appended (Purroxy's real identity/legality filter forms), it
reports our total, Scryfall's total, and the overlap of the two sides' top 20 (by the shared
`--order`), then a summary line: how many queries are within 1% on totals and at least 0.9 on the
top 20 (docs/PLAN-2026-10-03.md's Purple item 7 acceptance bar).

Usage: .venv/bin/python scripts/parity.py [--suffixes N] [--db PATH] [--queries PATH] ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from oracle_searcher.search import Unsupported, register_regexp, search

DEFAULT_DB = Path(".scratch/cards.sqlite")
DEFAULT_CACHE = Path(".scratch/scryfall-cache")
DEFAULT_QUERIES = Path("tests/fixtures/purroxy_queries.txt")
DEFAULT_SUFFIXES = Path("tests/fixtures/purroxy_composite_suffixes.txt")
SCRYFALL_SEARCH_URL = "https://api.scryfall.com/cards/search"
SCRYFALL_REQUEST_INTERVAL_SECONDS = 1.0
SCRYFALL_HTTP_NOT_FOUND = 404
TOTAL_TOLERANCE = 0.01
TOP20_OVERLAP_MINIMUM = 0.9
TOP_N = 20


def load_lines(path: Path) -> list[str]:
    """Return every non-blank, stripped line in `path`."""
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def scryfall_result(query: str, order: str, cache_dir: Path) -> dict[str, Any]:
    """The cached (or freshly fetched and cached) `{names, total}` for one Scryfall search.

    At most one request a second, with the required User-Agent/Accept headers
    (docs/PLAN-2026-10-03.md's "Scryfall calls" section); a 404 (no matches) is a valid empty
    result, not an error.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha1(f"{query}|{order}".encode(), usedforsecurity=False).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    url = SCRYFALL_SEARCH_URL + "?" + urllib.parse.urlencode({"q": query, "order": order})
    request = urllib.request.Request(  # noqa: S310 - fixed https Scryfall host, not user input
        url,
        headers={"User-Agent": "tcg-oracle-searcher/0.1", "Accept": "application/json"},
    )
    time.sleep(SCRYFALL_REQUEST_INTERVAL_SECONDS)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == SCRYFALL_HTTP_NOT_FOUND:
            body = {"data": [], "total_cards": 0}
        else:
            raise

    result = {"names": [card["name"] for card in body.get("data", [])], "total": body.get("total_cards", 0)}
    cache_path.write_text(json.dumps(result), encoding="utf-8")
    return result


def top_n_overlap(ours: list[str], theirs: list[str], n: int = TOP_N) -> float:
    """Fraction of `theirs`'s top `n` names also in `ours`'s top `n` (1.0 when `theirs` is empty)."""
    if not theirs:
        return 1.0 if not ours else 0.0
    their_top = theirs[:n]
    their_unique = set(their_top)
    return len(set(ours[:n]) & their_unique) / len(their_unique)


def totals_within_tolerance(ours: int, theirs: int, tolerance: float = TOTAL_TOLERANCE) -> bool:
    """True when `ours` is within `tolerance` (relative) of `theirs`, exact when `theirs` is 0."""
    if theirs == 0:
        return ours == 0
    return abs(ours - theirs) / theirs <= tolerance


def run(
    conn: sqlite3.Connection,
    queries: list[str],
    order: str,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    """Run every query against `conn` and against Scryfall, returning one result row each."""
    rows: list[dict[str, Any]] = []
    for query in queries:
        try:
            ours = search(conn, query, order=order)
        except Unsupported as exc:
            rows.append({"query": query, "unsupported": str(exc)})
            continue
        our_names = [card["name"] for card in ours.data]
        theirs = scryfall_result(query, order, cache_dir)
        rows.append(
            {
                "query": query,
                "our_total": ours.total_cards,
                "scryfall_total": theirs["total"],
                "top20_overlap": top_n_overlap(our_names, theirs["names"]),
                "only_ours": sorted(set(our_names) - set(theirs["names"]))[:3],
                "only_scryfall": sorted(set(theirs["names"]) - set(our_names))[:3],
            }
        )
    return rows


def expand_with_suffixes(base_queries: list[str], suffixes: list[str]) -> list[str]:
    """Each base query alone, plus that query (parenthesised) with each suffix appended.

    The base query is wrapped in parens before a suffix is appended, matching how Purroxy's own
    UI composes a user's search with an added identity/legality filter: `(user query) suffix`,
    never a bare string join. Without the parens, a base query with a top-level `or` (several of
    the corpus's `(o:"x" or o:"y")` entries, and any `keyword:a or keyword:b` query) silently
    regroups, since Scryfall-syntax precedence binds an implicit AND tighter than `or` -- the
    suffix would then apply only to the last `or`-ed term, not the whole query, and the two sides
    would legitimately disagree on a query neither side was actually asked to run.
    """
    expanded = list(base_queries)
    for suffix in suffixes:
        expanded.extend(f"({query}) {suffix}".strip() for query in base_queries)
    return expanded


def print_row(row: dict[str, Any]) -> None:
    """Print one query's result line."""
    if "unsupported" in row:
        print(f"{row['query']} | UNSUPPORTED | {row['unsupported']}")
        return
    print(
        f"{row['query']} | ours={row['our_total']} scryfall={row['scryfall_total']} "
        f"top20={row['top20_overlap']:.2f} | only_ours={row['only_ours']} only_sf={row['only_scryfall']}"
    )


def print_summary(rows: list[dict[str, Any]]) -> None:
    """Print the acceptance-bar summary line (docs/PLAN-2026-10-03.md's Purple item 7)."""
    scored = [row for row in rows if "unsupported" not in row]
    within_total = sum(1 for row in scored if totals_within_tolerance(row["our_total"], row["scryfall_total"]))
    within_top20 = sum(1 for row in scored if row["top20_overlap"] >= TOP20_OVERLAP_MINIMUM)
    unsupported = len(rows) - len(scored)
    print(
        f"\nSummary: {len(rows)} queries ({unsupported} unsupported) -- "
        f"{within_total}/{len(scored)} within {TOTAL_TOLERANCE:.0%} on total, "
        f"{within_top20}/{len(scored)} at or above {TOP20_OVERLAP_MINIMUM:.0%} top-{TOP_N} overlap"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse this script's command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite card file to query")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES, help="base queries, one per line")
    parser.add_argument(
        "--suffixes",
        type=Path,
        default=DEFAULT_SUFFIXES,
        help="composite suffixes, one per line, appended to every base query",
    )
    parser.add_argument("--max-suffixes", type=int, default=None, help="use only the first N suffixes")
    parser.add_argument("--order", default="edhrec", help="Scryfall/our shared sort order (default: edhrec)")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="Scryfall response cache directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point: run the full corpus (base queries x suffixes) and print results + summary."""
    args = parse_args(argv)
    base_queries = load_lines(args.queries)
    suffixes = load_lines(args.suffixes)
    if args.max_suffixes is not None:
        suffixes = suffixes[: args.max_suffixes]
    queries = expand_with_suffixes(base_queries, suffixes)

    conn = sqlite3.connect(args.db)
    register_regexp(conn)
    rows = run(conn, queries, args.order, args.cache)

    for row in rows:
        print_row(row)
    print_summary(rows)


if __name__ == "__main__":
    main()
