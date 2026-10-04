"""Sweep Scryfall for `is:`/`has:` tags that a single oracle_cards row can't answer on its own.

Most `is:` values are answered without any network call: `api/parsing/rewrite.py` expands the
gameplay-derivable ones (land cycles, `is:commander`, `is:historic`, ...) into primitive
subtrees at parse time, and `oracle_searcher/importer.py`'s `IS_TAG_CHECKS` computes the
printing-boolean ones (`is:foil`, `is:reserved`, ...) once per card at import. The tags in
`SWEEP_TAGS` below are neither: they depend on facts Scryfall tracks across a card's full print
run (`is:unique`: has it ever had more than one printing?) or a `set_type`/print-availability
distinction our one-row-per-card schema can't derive reliably from the single representative
printing `oracle_cards` gives us (verified against live counts -- see docs/is-tags.md). For
those, this module asks Scryfall directly which oracle cards match, once per tag, and
`apply_sweep` folds the answer into `card_is_tags` after `importer.build()` has run.

This is a separate step from `importer.build()` on purpose (docs/PLAN-2026-10-03.md, Teal
item 2): the import itself stays network-free and deterministic; a Lambda-side importer runs
`build()`, then `sweep()` + `apply_sweep()`, then `importer.check()`.

Scryfall's own search API limit is 2 requests/second; this module uses 1/second to leave
margin (docs/PLAN-2026-10-03.md's "Scryfall calls" section). A 429 stops the whole sweep run
immediately -- the caller gets a `SweepError` carrying whatever earlier tags *did* finish
(`.completed`), so a retry (after waiting 60s, per the plan) only has to redo the tags that
didn't land, by merging `.completed` into whatever an earlier run already wrote to disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.scryfall.com/cards/search"
_USER_AGENT = "tcg-oracle-searcher/0.1"
_DEFAULT_MIN_INTERVAL = 1.0  # seconds between requests; Scryfall allows 2/sec (plan's own margin)
_REQUEST_TIMEOUT = 30.0
_HTTP_TOO_MANY_REQUESTS = 429

# `has:` tags share card_is_tags with `is:` (api/parsing/db_info.py: both are search_aliases for
# the same FieldInfo), so they're swept the same way; only the query text differs.
_HAS_TAGS = frozenset({"watermark", "indicator"})

# Tags this schema can't answer from one oracle_cards row, each with why (docs/is-tags.md has the
# full inventory and the live counts behind every one of these calls):
#   - alchemy, funny: `set_type` of the chosen printing undercounts vs. live is:<tag> by ~10-15%
#     (711/1514 local vs. 824/1476 live, 2026-10-03) -- some alchemy/funny cards' representative
#     printing doesn't carry that set_type even though another of their printings does.
#   - digital: the representative printing's `games` field undercounts by ~3.7x (1916 vs. 7154
#     live) -- most digital-exclusive printings aren't the printing oracle_cards picked to
#     represent an otherwise-paper card.
#   - watermark: the representative printing often predates the watermark being added to a later
#     printing of the same card (2588 non-null `card_watermark` locally vs. 4691 live `has:watermark`).
#   - brawler, duelcommander, oathbreaker: commander-like eligibility for other formats, each with
#     its own banlist/rule Scryfall already evaluates; swept rather than re-derived.
#   - meldpart, meldresult: tiny (14 / 7 live), simplest to just sweep.
#   - unique: "has only been in a single set" needs full print-run data this schema doesn't keep;
#     Scryfall knows it per oracle card, so a plain sweep answers it without that data.
SWEEP_TAGS: frozenset[str] = frozenset(
    {
        "alchemy",
        "funny",
        "digital",
        "watermark",
        "brawler",
        "duelcommander",
        "oathbreaker",
        "meldpart",
        "meldresult",
        "unique",
    }
)


class SweepError(Exception):
    """A sweep run stopped early (429 or other HTTP failure) before every tag finished.

    `completed` holds every tag that finished successfully *before* the failure, in the same
    `{tag: [oracle_id, ...]}` shape `sweep()` returns -- a caller should keep those and retry
    only the tags left out of it (after waiting 60s on a 429, per docs/PLAN-2026-10-03.md).
    """

    def __init__(self, reason: str, completed: dict[str, list[str]]) -> None:
        """Initialize with a human-readable reason and whatever tags swept cleanly first."""
        self.reason = reason
        self.completed = completed
        super().__init__(reason)


class FetchPage(Protocol):
    """A function that fetches one Scryfall search results page (real HTTP or a test double)."""

    def __call__(self, url: str) -> dict[str, Any]:
        """Return the decoded JSON page at `url`."""
        ...


def _query_for(tag: str) -> str:
    prefix = "has" if tag in _HAS_TAGS else "is"
    return f"{prefix}:{tag}"


def http_fetch_page(url: str) -> dict[str, Any]:
    """Real HTTP fetch: one Scryfall search page, raising `SweepError` on any HTTP failure."""
    request = urllib.request.Request(  # noqa: S310 - fixed https://api.scryfall.com host, not user input
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:  # noqa: S310 - see above
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        reason = "rate limited (429)" if exc.code == _HTTP_TOO_MANY_REQUESTS else f"HTTP {exc.code} fetching {url}"
        raise SweepError(reason, completed={}) from exc


def sweep(
    tags: Iterable[str],
    *,
    fetch_page: FetchPage = http_fetch_page,
    min_interval: float = _DEFAULT_MIN_INTERVAL,
    sleep: Callable[[float], object] = time.sleep,
    should_continue: Callable[[], bool] = lambda: True,
) -> dict[str, list[str]]:
    """Return `{tag: [oracle_id, ...]}` for each of `tags`, paging `next_page` to exhaustion.

    Paces requests to at most one per `min_interval` seconds (real wall-clock via `sleep`,
    injectable so tests don't actually wait). On any HTTP failure -- a 429 or otherwise --
    raises `SweepError` with every tag that completed *before* that point attached as
    `.completed`; the tag that was mid-page when it failed is not included (better an absent
    tag a retry redoes in full than a silently-partial one treated as done).

    `should_continue` is asked before each tag starts (never mid-tag); when it says no, the sweep
    returns the tags finished so far without error (a Lambda uses it to stop before its timeout).
    """
    results: dict[str, list[str]] = {}
    last_request_at: float | None = None
    for tag in tags:
        if not should_continue():
            logger.warning("sweep stopped before %r: asked to stop (kept %d completed tag(s))", tag, len(results))
            break
        oracle_ids: list[str] = []
        page_url: str | None = f"{_SEARCH_URL}?q={urllib.parse.quote(_query_for(tag))}&unique=cards"
        while page_url:
            if last_request_at is not None:
                wait = min_interval - (time.monotonic() - last_request_at)
                if wait > 0:
                    sleep(wait)
            try:
                payload = fetch_page(page_url)
            except SweepError as exc:
                logger.warning("sweep stopped on %r: %s (kept %d completed tag(s))", tag, exc.reason, len(results))
                raise SweepError(exc.reason, completed=dict(results)) from exc
            last_request_at = time.monotonic()
            oracle_ids.extend(card["oracle_id"] for card in payload.get("data", []) if card.get("oracle_id"))
            page_url = payload.get("next_page") if payload.get("has_more") else None
        results[tag] = oracle_ids
    return results


def sweep_payload(tags_result: dict[str, list[str]]) -> dict[str, Any]:
    """The sweep file's content: `{"swept_at", "tags"}` (docs/PLAN-2026-10-03.md's Teal item 2)."""
    return {
        "swept_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tags": tags_result,
    }


def write_sweep_file(tags_result: dict[str, list[str]], out_path: str | Path) -> None:
    """Write the sweep payload to `out_path`."""
    payload = sweep_payload(tags_result)
    Path(out_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def apply_sweep(conn: sqlite3.Connection, sweep_path: str | Path) -> dict[str, int]:
    """Add each swept tag into `card_is_tags` for its cards. Returns `{tag: cards_updated}`.

    Additive and idempotent: a tag already present in a card's `card_is_tags` is left alone
    (no duplicate), and an oracle_id the sweep file names that isn't in this database (a card
    retired or renamed between the sweep and this build) is silently skipped rather than
    failing the whole apply.
    """
    payload = json.loads(Path(sweep_path).read_text())
    counts: dict[str, int] = {}
    for tag, oracle_ids in payload.get("tags", {}).items():
        updated = 0
        for oracle_id in oracle_ids:
            row = conn.execute("SELECT id, card_is_tags FROM cards WHERE oracle_id = ?", (oracle_id,)).fetchone()
            if row is None:
                continue
            card_id, tags_json = row
            current_tags = set(json.loads(tags_json))
            if tag not in current_tags:
                current_tags.add(tag)
                conn.execute(
                    "UPDATE cards SET card_is_tags = ? WHERE id = ?",
                    (json.dumps(sorted(current_tags)), card_id),
                )
            updated += 1
        counts[tag] = updated
    conn.commit()
    return counts


def main() -> None:
    """CLI: `python -m oracle_searcher.is_tag_sweep --out PATH [--tags t1,t2,...]`.

    Defaults to sweeping every tag in `SWEEP_TAGS`. Prints request/timing stats on success; on a
    429 or other HTTP failure, writes whatever completed before the failure and exits non-zero
    so a caller knows the file is partial.
    """
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="path to write the sweep JSON file to")
    parser.add_argument("--tags", help="comma-separated tag names (default: every SWEEP_TAGS entry)")
    args = parser.parse_args()

    tags = args.tags.split(",") if args.tags else sorted(SWEEP_TAGS)
    start = time.monotonic()
    try:
        result = sweep(tags)
    except SweepError as exc:
        logger.error("sweep stopped early: %s", exc.reason)
        write_sweep_file(exc.completed, args.out)
        raise SystemExit(1) from exc

    duration = time.monotonic() - start
    total_cards = sum(len(ids) for ids in result.values())
    logger.info(
        "swept %d tag(s), %d card(s) total, in %.1fs: %s",
        len(result),
        total_cards,
        duration,
        {tag: len(ids) for tag, ids in sorted(result.items())},
    )
    write_sweep_file(result, args.out)


if __name__ == "__main__":
    main()
