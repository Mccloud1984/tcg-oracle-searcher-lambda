"""`game:` filters printings and `prefer:` picks which printing of each card a search returns.

Fixtures are real Scryfall data fetched 2026-11-13: Fellwar Stone (oracle pick mbc 74, $2.71; 45 printings, the
cheapest paper one frc 57 at $0.86) and Badlands (oracle pick vma 291, MTGO-only and unpriced; 15 printings, the
cheapest visible paper one 3ed 282 at $584.77, the 30th-anniversary and Collectors' Edition ones hidden). Every
expected printing below is what api.scryfall.com/cards/search answered for the same query that day.
"""

from __future__ import annotations

import sqlite3

import pytest

from oracle_searcher import lookups
from oracle_searcher.importer import build
from oracle_searcher.schema import PRINTINGS_ALIAS
from oracle_searcher.search import Unsupported, search
from tests.conftest import FIXTURES_DIR, TAGS_FIXTURE

TODAY = "2026-11-13"
FELLWAR = "Fellwar Stone"
BADLANDS = "Badlands"
BOTH = '(!"Fellwar Stone" or !Badlands)'


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> sqlite3.Connection:
    """Cards and printings files built from the two cards' real data, the printings ATTACHed."""
    root = tmp_path_factory.mktemp("prefer")
    build(
        FIXTURES_DIR / "prefer_cards.jsonl",
        TAGS_FIXTURE,
        root / "c.sqlite",
        FIXTURES_DIR / "prefer_printings.jsonl",
        root / "p.sqlite",
    )
    conn = sqlite3.connect(root / "c.sqlite")
    conn.execute(f"ATTACH DATABASE '{root / 'p.sqlite'}' AS {PRINTINGS_ALIAS}")
    return conn


class AttachSpy:
    """Stands in for the handler's `printings_connection`: counts how often the printings file was asked for."""

    def __init__(self) -> None:
        """Starts with no calls."""
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def printings(conn: sqlite3.Connection, q: str, today: str = TODAY, **kwargs: object) -> dict[str, tuple[str, str]]:
    result = search(conn, q, attach=AttachSpy(), today=today, **kwargs)
    return {card["name"]: (card["set"], card["collector_number"]) for card in result.data}


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        (f"{BOTH} game:paper prefer:usd-low", {FELLWAR: ("frc", "57"), BADLANDS: ("3ed", "282")}),
        (f"prefer:usd-low {BOTH}", {FELLWAR: ("frc", "57"), BADLANDS: ("3ed", "282")}),
        # usd-high falls back to the foil price: 9ed 297★ is foil-only at $206.75
        (f"{BOTH} prefer:usd-high game:paper", {FELLWAR: ("9ed", "297★"), BADLANDS: ("leb", "278")}),
        (f"{BOTH} prefer:oldest", {FELLWAR: ("drk", "102"), BADLANDS: ("lea", "277")}),
        (f"!{BADLANDS} prefer:newest game:paper", {BADLANDS: ("sum", "282")}),
        (f"!{BADLANDS} game:mtgo prefer:oldest", {BADLANDS: ("me2", "225")}),
        # without prefer: the oracle pick when it passes (mbc is paper), else the newest printing that does
        (f"{BOTH} game:paper", {FELLWAR: ("mbc", "74"), BADLANDS: ("sum", "282")}),
        (f'!"{FELLWAR}" -game:paper', {FELLWAR: ("me3", "195")}),
    ],
)
def test_each_card_comes_back_in_the_preferred_printing(db: sqlite3.Connection, q: str, expected: dict) -> None:
    assert printings(db, q) == expected


def test_the_chosen_printing_carries_its_own_prices(db: sqlite3.Connection) -> None:
    result = search(db, f'!"{FELLWAR}" game:paper prefer:usd-low', attach=AttachSpy(), today=TODAY)
    assert result.data[0]["prices"]["usd"] == "0.86"
    assert result.data[0]["set_name"] == "Reality Fracture Commander"
    assert result.data[0]["oracle_id"]


@pytest.mark.parametrize("order", ["edhrec", "name", "usd"])
def test_prefer_never_changes_the_card_order(db: sqlite3.Connection, order: str) -> None:
    plain = [card["name"] for card in search(db, BOTH, order=order, today=TODAY).data]
    preferred = [card["name"] for card in search(db, f"{BOTH} prefer:usd-low", order=order, attach=AttachSpy(), today=TODAY).data]
    assert preferred == plain
    assert len(plain) == 2


def test_a_card_with_no_passing_printing_is_dropped(db: sqlite3.Connection) -> None:
    # on 1993-09-01 only Alpha (lea, 1993-08-05) was out: Badlands had a paper printing, Fellwar Stone none
    result = search(db, f"{BOTH} game:paper prefer:usd-low", attach=AttachSpy(), today="1993-09-01")
    assert [(card["name"], card["set"]) for card in result.data] == [(BADLANDS, "lea")]
    assert result.total_cards == 1


def test_no_card_is_on_arena(db: sqlite3.Connection) -> None:
    result = search(db, f"{BOTH} game:arena", attach=AttachSpy(), today=TODAY)
    assert (result.data, result.total_cards) == ([], 0)


def test_searches_without_printing_keywords_never_open_the_printings_file(db: sqlite3.Connection) -> None:
    spy = AttachSpy()
    plain = search(db, BOTH, attach=spy, today=TODAY)
    default = search(db, f"{BOTH} prefer:default", attach=spy, today=TODAY)
    assert spy.calls == 0
    assert default.data == plain.data


def test_a_printing_search_opens_the_printings_file(db: sqlite3.Connection) -> None:
    spy = AttachSpy()
    search(db, f"{BOTH} prefer:usd-low", attach=spy, today=TODAY)
    assert spy.calls == 1


@pytest.mark.parametrize("q", [f"{BOTH} (game:paper or t:land)", f"{BOTH} -(game:paper t:land)", f"{BOTH} game:sega"])
def test_game_it_cannot_answer_exactly_is_unsupported(db: sqlite3.Connection, q: str) -> None:
    with pytest.raises(Unsupported):
        search(db, q, attach=AttachSpy(), today=TODAY)


def test_prints_keeps_only_the_printings_in_the_game(db: sqlite3.Connection) -> None:
    data = lookups.prints(db, {"q": f"!{BADLANDS} game:mtgo"}, AttachSpy(), today=TODAY)["data"]
    assert {(card["set"], card["collector_number"]) for card in data} == {
        ("vma", "291"),
        ("me4", "241"),
        ("me2", "225"),
        ("prm", "43600"),
    }
