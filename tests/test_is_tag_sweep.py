"""Sweep paging, 429 stop, JSON output and apply_sweep, all against SAVED Scryfall responses.

Fixtures under tests/fixtures/scryfall/is_tags/ are real `cards/search?unique=cards` responses
fetched 2026-10-03, with each card trimmed to {object, oracle_id, name}. No test here touches
the network.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from oracle_searcher import is_tag_sweep
from oracle_searcher.importer import build
from oracle_searcher.is_tag_sweep import SweepError, apply_sweep, sweep, write_sweep_file
from oracle_searcher.search import search

FIXTURES = Path(__file__).parent / "fixtures" / "scryfall" / "is_tags"
NEVER_WAIT = 0.0


def _saved(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _ids(name: str) -> list[str]:
    return [card["oracle_id"] for card in _saved(name)["data"]]


class FakeScryfall:
    """Serves saved pages in order and records the URLs asked for."""

    def __init__(self, *pages: str | Exception) -> None:
        """Queue the saved pages (or errors) to serve, in order."""
        self.pages = list(pages)
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        self.urls.append(url)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return _saved(page)


def test_sweep_follows_next_page_to_the_end() -> None:
    """is:commander is 3731 cards over 22 pages; p1, p2 and the real last page stand for the run."""
    fake = FakeScryfall("commander_p1", "commander_p2", "commander_last")
    result = sweep(["commander"], fetch_page=fake, min_interval=NEVER_WAIT)
    assert result == {"commander": _ids("commander_p1") + _ids("commander_p2") + _ids("commander_last")}
    assert len(result["commander"]) == 175 + 175 + 56
    # The second request is exactly the next_page URL Scryfall handed back, not one rebuilt by us.
    assert fake.urls[1] == _saved("commander_p1")["next_page"]
    assert fake.urls[2] == _saved("commander_p2")["next_page"]


def test_sweep_first_url_is_a_unique_cards_is_search() -> None:
    fake = FakeScryfall("fetchland")
    sweep(["fetchland"], fetch_page=fake, min_interval=NEVER_WAIT)
    assert fake.urls == ["https://api.scryfall.com/cards/search?q=is%3Afetchland&unique=cards"]


def test_sweep_uses_has_for_watermark_and_indicator() -> None:
    """`has:` tags share card_is_tags but are asked as has:<tag>; is:watermark would match nothing."""
    fake = FakeScryfall("meldresult", "meldresult")
    sweep(["watermark", "indicator"], fetch_page=fake, min_interval=NEVER_WAIT)
    assert [url.split("q=")[1].split("&")[0] for url in fake.urls] == ["has%3Awatermark", "has%3Aindicator"]


def test_sweep_paces_requests_by_min_interval() -> None:
    waits: list[float] = []
    fake = FakeScryfall("fetchland", "shockland")
    sweep(["fetchland", "shockland"], fetch_page=fake, min_interval=5.0, sleep=waits.append)
    assert len(waits) == 1  # none before the first request
    assert 4.0 < waits[0] <= 5.0


def test_a_429_stops_the_sweep_and_keeps_the_tags_already_done() -> None:
    """Regression guard: a 429 mid-run must not lose finished tags nor carry on to the next tag."""
    fake = FakeScryfall("fetchland", SweepError("rate limited (429)", completed={}), "shockland")
    with pytest.raises(SweepError) as raised:
        sweep(["fetchland", "shockland", "meldresult"], fetch_page=fake, min_interval=NEVER_WAIT)
    assert raised.value.completed == {"fetchland": _ids("fetchland")}
    assert "429" in raised.value.reason
    assert len(fake.urls) == 2  # shockland's failing request was the last one made


def test_a_429_mid_tag_drops_the_half_finished_tag() -> None:
    fake = FakeScryfall("commander_p1", SweepError("rate limited (429)", completed={}))
    with pytest.raises(SweepError) as raised:
        sweep(["commander"], fetch_page=fake, min_interval=NEVER_WAIT)
    assert raised.value.completed == {}


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.scryfall.com/cards/search", code, "x", {}, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(("code", "expected"), [(429, "429"), (500, "HTTP 500")])
def test_real_fetch_turns_http_errors_into_sweep_errors(monkeypatch: pytest.MonkeyPatch, code: int, expected: str) -> None:
    def raise_it(*_args: object, **_kwargs: object) -> None:
        raise _http_error(code)

    monkeypatch.setattr(is_tag_sweep.urllib.request, "urlopen", raise_it)
    with pytest.raises(SweepError, match=expected):
        is_tag_sweep._fetch_page("https://api.scryfall.com/cards/search?q=is%3Afetchland")


def test_write_sweep_file_shape(tmp_path: Path) -> None:
    out = tmp_path / "is_tags.json"
    write_sweep_file({"fetchland": _ids("fetchland")}, out)
    written = json.loads(out.read_text())
    assert set(written) == {"swept_at", "tags"}
    assert written["tags"] == {"fetchland": _ids("fetchland")}
    assert written["swept_at"].endswith("Z")


# ── apply_sweep ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def land_db(tmp_path: Path) -> sqlite3.Connection:
    """A real build() of 20 real fetch/shock lands plus Island and Forest, with their real tags."""
    db_path = tmp_path / "lands.sqlite"
    build(FIXTURES / "land_cards.jsonl", FIXTURES / "land_tags.jsonl", db_path)
    return sqlite3.connect(db_path)


def _tags_of(conn: sqlite3.Connection, oracle_id: str) -> list[str]:
    row = conn.execute("SELECT card_is_tags FROM cards WHERE oracle_id = ?", (oracle_id,)).fetchone()
    return json.loads(row[0])


def test_apply_sweep_adds_the_tag_keeps_others_and_skips_unknown_ids(land_db: sqlite3.Connection, tmp_path: Path) -> None:
    fetch_ids = _ids("fetchland")
    before = _tags_of(land_db, fetch_ids[0])
    sweep_file = tmp_path / "sweep.json"
    write_sweep_file({"unique": [*fetch_ids, "00000000-0000-0000-0000-000000000000"]}, sweep_file)

    counts = apply_sweep(land_db, sweep_file)

    assert counts == {"unique": 10}  # the retired oracle_id is skipped, not counted, not an error
    assert _tags_of(land_db, fetch_ids[0]) == sorted([*before, "unique"])
    island = land_db.execute("SELECT oracle_id FROM cards WHERE card_name = 'Island'").fetchone()[0]
    assert "unique" not in _tags_of(land_db, island)


def test_apply_sweep_is_idempotent(land_db: sqlite3.Connection, tmp_path: Path) -> None:
    sweep_file = tmp_path / "sweep.json"
    write_sweep_file({"unique": _ids("fetchland")}, sweep_file)
    apply_sweep(land_db, sweep_file)
    snapshot = land_db.execute("SELECT oracle_id, card_is_tags FROM cards ORDER BY oracle_id").fetchall()
    assert apply_sweep(land_db, sweep_file) == {"unique": 10}
    assert land_db.execute("SELECT oracle_id, card_is_tags FROM cards ORDER BY oracle_id").fetchall() == snapshot


def test_apply_sweep_with_no_tags_changes_nothing(land_db: sqlite3.Connection, tmp_path: Path) -> None:
    sweep_file = tmp_path / "sweep.json"
    write_sweep_file({}, sweep_file)
    assert apply_sweep(land_db, sweep_file) == {}


# ── end to end: is:<tag> on a built database ────────────────────────────────────────────────


def _names(conn: sqlite3.Connection, query: str) -> set[str]:
    return {card["name"] for card in search(conn, query).data}


def test_swept_tag_end_to_end_sweep_apply_then_search(land_db: sqlite3.Connection, tmp_path: Path) -> None:
    """Sweep (saved fetchland page) -> JSON file -> apply_sweep -> `is:unique` finds exactly those cards.

    `unique` is a real SWEEP_TAGS entry that no rewrite or import rule answers; the saved
    fetchland response stands in for what Scryfall would return for is:unique.
    """
    land_db.row_factory = sqlite3.Row
    sweep_file = tmp_path / "sweep.json"
    write_sweep_file(sweep(["unique"], fetch_page=FakeScryfall("fetchland"), min_interval=NEVER_WAIT), sweep_file)
    apply_sweep(land_db, sweep_file)
    expected = {card["name"] for card in _saved("fetchland")["data"]}
    assert len(expected) == 10
    assert _names(land_db, "is:unique") == expected


def test_swept_tag_matches_nothing_before_the_sweep_is_applied(land_db: sqlite3.Connection) -> None:
    """Guard for the end-to-end test above: without apply_sweep, is:unique finds no card."""
    land_db.row_factory = sqlite3.Row
    assert _names(land_db, "is:unique") == set()


@pytest.mark.parametrize(("tag", "scryfall_count"), [("fetchland", 10), ("shockland", 10)])
def test_land_cycle_tags_match_scryfall_on_real_cards(land_db: sqlite3.Connection, tag: str, scryfall_count: int) -> None:
    """is:fetchland / is:shockland, answered from the real oracle tags, equal Scryfall's saved answer.

    rewrite.py turns them into otag:cycle-fetchland / otag:shockland, so no sweep is involved.
    scryfall_count is the saved response's total_cards (2026-10-03).
    """
    land_db.row_factory = sqlite3.Row
    saved = _saved(tag)
    assert saved["total_cards"] == scryfall_count
    assert _names(land_db, f"is:{tag}") == {card["name"] for card in saved["data"]}
    assert _names(land_db, f"-is:{tag} t:land") >= {"Island", "Forest"}
