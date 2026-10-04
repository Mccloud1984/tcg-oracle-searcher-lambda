"""End-to-end tests for `oracle_searcher.search.search`: extras, ordering, paging."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from oracle_searcher.search import SearchResult, Unsupported, search
from tests.helpers import insert_named_card, make_db

if TYPE_CHECKING:
    import sqlite3

LLANOWAR_ELVES = "Llanowar Elves"
SOL_RING = "Sol Ring"
BLACK_LOTUS = "Black Lotus"
TARMOGOYF_TOKEN = "Tarmogoyf"  # layout token -> is_extra
KOTH_EMBLEM = "Koth of the Hammer Emblem"  # layout emblem -> is_extra


# ── is_extra visibility ──────────────────────────────────────────────────────────────────────


def test_extras_hidden_by_default() -> None:
    conn = make_db()
    insert_named_card(conn, LLANOWAR_ELVES)
    insert_named_card(conn, TARMOGOYF_TOKEN)
    result = search(conn, "")  # a query with no real constraint, just TrueNode-adjacent
    names = {card["name"] for card in result.data}
    assert TARMOGOYF_TOKEN not in names


def test_extras_revealed_by_naming_their_type() -> None:
    conn = make_db()
    insert_named_card(conn, LLANOWAR_ELVES)
    insert_named_card(conn, TARMOGOYF_TOKEN)
    result = search(conn, "t:token")
    names = {card["name"] for card in result.data}
    assert names == {TARMOGOYF_TOKEN}


def test_extras_hidden_by_default_is_a_real_guard() -> None:
    """Without the is_extra filter, a plain search would surface the token card too."""
    conn = make_db()
    insert_named_card(conn, LLANOWAR_ELVES)
    insert_named_card(conn, TARMOGOYF_TOKEN)
    unfiltered = {row[0] for row in conn.execute("SELECT card_name FROM cards")}
    assert TARMOGOYF_TOKEN in unfiltered  # the row is there; search() must filter it out itself
    assert TARMOGOYF_TOKEN not in {card["name"] for card in search(conn, "").data}


def test_negated_extra_type_does_not_reveal_it() -> None:
    """`-t:token` asks to exclude tokens, not reveal them -- it must stay excluded either way."""
    conn = make_db()
    insert_named_card(conn, LLANOWAR_ELVES)
    insert_named_card(conn, TARMOGOYF_TOKEN)
    result = search(conn, "-t:token")
    names = {card["name"] for card in result.data}
    assert names == {LLANOWAR_ELVES}


FUNNY_PLAYTEST_CARD = "Sol Ring"  # stands in for a funny-set card hidden by is_extra (name is irrelevant)


def _db_with_hidden_funny_card() -> sqlite3.Connection:
    conn = make_db()
    insert_named_card(conn, LLANOWAR_ELVES)
    insert_named_card(conn, FUNNY_PLAYTEST_CARD, is_extra=1, card_is_tags='["funny"]')
    return conn


def test_is_funny_reveals_extras() -> None:
    """`is:funny` reveals extras like `t:token` does.

    Live 2026-10-03 (tests/fixtures/scryfall/is_tag_extras_reveal.json): 1476 cards with or without
    `include:extras`, while our import hides all but 134 of them.
    """
    names = {card["name"] for card in search(_db_with_hidden_funny_card(), "is:funny").data}
    assert names == {FUNNY_PLAYTEST_CARD}


def test_negated_is_funny_does_not_reveal_extras() -> None:
    names = {card["name"] for card in search(_db_with_hidden_funny_card(), "-is:funny").data}
    assert names == {LLANOWAR_ELVES}


def test_funny_extras_stay_hidden_without_naming_funny() -> None:
    names = {card["name"] for card in search(_db_with_hidden_funny_card(), "").data}
    assert names == {LLANOWAR_ELVES}


@pytest.mark.parametrize("tag", ["digital", "alchemy", "unique"])
def test_other_swept_tags_do_not_reveal_extras(tag: str) -> None:
    """Scryfall keeps hiding extras for these, so they must not reveal.

    Live 2026-10-03: is:digital 7154 vs 7386 with include:extras, is:alchemy 824 vs 966, is:unique 16115 vs 20516.
    """
    conn = make_db()
    insert_named_card(conn, FUNNY_PLAYTEST_CARD, is_extra=1, card_is_tags=json.dumps([tag]))
    assert search(conn, f"is:{tag}").data == []


# ── ordering ──────────────────────────────────────────────────────────────────────────────────


def _insert_three(conn) -> None:
    insert_named_card(
        conn,
        LLANOWAR_ELVES,
        cmc=1.0,
        edhrec_rank=500,
        price_usd=0.25,
        released_at="1995-01-01",
    )
    insert_named_card(
        conn,
        SOL_RING,
        cmc=1.0,
        edhrec_rank=1,
        price_usd=2.50,
        released_at="2020-01-01",
    )
    insert_named_card(
        conn,
        BLACK_LOTUS,
        cmc=0.0,
        edhrec_rank=None,
        price_usd=None,
        released_at="1993-08-05",
    )


def _names(result: SearchResult) -> list[str]:
    return [card["name"] for card in result.data]


def test_order_edhrec_ascending_with_unranked_last() -> None:
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="edhrec")
    assert _names(result) == [SOL_RING, LLANOWAR_ELVES, BLACK_LOTUS]


def test_order_edhrec_desc_still_puts_unranked_last() -> None:
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="edhrec", dir="desc")
    assert _names(result) == [LLANOWAR_ELVES, SOL_RING, BLACK_LOTUS]


def test_order_edhrec_ties_break_by_name() -> None:
    """Unranked cards (every token) tie on edhrec; Scryfall lists them by name.

    Live 2026-10-03: `t:token` order=edhrec starts Adorned Pouncer, Aetherborn, Agate Instigator.
    We used insertion order, so the top-20 overlap with Scryfall was 0.05.
    """
    conn = make_db()
    insert_named_card(conn, "Tyranid")  # inserted first, sorts last
    insert_named_card(conn, TARMOGOYF_TOKEN)
    assert _names(search(conn, "t:token", order="edhrec")) == [TARMOGOYF_TOKEN, "Tyranid"]


def test_order_name_ignores_spaces_and_punctuation() -> None:
    """Scryfall sorts names on letters and digits only: `Angelo` before `Angel of Sanctions`.

    Live 2026-10-03: order=name for `t:token` and `t:creature cmc<=2` lists have zero inversions
    under an alphanumeric-only key and hundreds under a plain lower-cased one; the 0.05 -> 0.70
    top-20 overlap of `t:token` was this.
    """
    conn = make_db()
    insert_named_card(conn, "Angel of Sanctions")
    insert_named_card(conn, "Angelo")
    assert _names(search(conn, "t:token", order="name")) == ["Angelo", "Angel of Sanctions"]
    assert _names(search(conn, "t:token", order="edhrec")) == ["Angelo", "Angel of Sanctions"]


def test_order_released_defaults_to_newest_first() -> None:
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="released")
    assert _names(result) == [SOL_RING, LLANOWAR_ELVES, BLACK_LOTUS]


def test_order_released_dir_is_literal_desc_newest_asc_oldest() -> None:
    """Scryfall's API treats dir literally, whatever its docs' wording suggests.

    Live 2026-10-04, `name:Jace identity<=UW` order=released: dir=desc and dir=auto both start with Jace, Reality
    Sculptor (2026-10-02), dir=asc with Jace's Erasure (2011). We flipped desc to oldest-first, so Purroxy's "newest
    cards" pass (order=released, dir=desc), the one meant to find a set released that week, got the oldest instead
    and an upgrade never offered a Reality Fracture card (owner report, 2026-10-04).
    """
    conn = make_db()
    _insert_three(conn)
    assert _names(search(conn, "", order="released", dir="desc")) == [SOL_RING, LLANOWAR_ELVES, BLACK_LOTUS]
    assert _names(search(conn, "", order="released", dir="asc")) == [BLACK_LOTUS, LLANOWAR_ELVES, SOL_RING]


def test_order_name_ascending() -> None:
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="name")
    assert _names(result) == [BLACK_LOTUS, LLANOWAR_ELVES, SOL_RING]


def test_order_cmc_ascending() -> None:
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="cmc")
    assert _names(result)[0] == BLACK_LOTUS  # cmc 0, strictly lowest


def test_order_usd_ascending_with_unpriced_last() -> None:
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="usd")
    assert _names(result) == [LLANOWAR_ELVES, SOL_RING, BLACK_LOTUS]


# ── paging ────────────────────────────────────────────────────────────────────────────────────


def test_paging_has_more_and_total_cards() -> None:
    conn = make_db()
    _insert_three(conn)
    page1 = search(conn, "", order="name", page=1, page_size=2)
    assert _names(page1) == [BLACK_LOTUS, LLANOWAR_ELVES]
    assert page1.has_more is True
    assert page1.total_cards == 3

    page2 = search(conn, "", order="name", page=2, page_size=2)
    assert _names(page2) == [SOL_RING]
    assert page2.has_more is False
    assert page2.total_cards == 3


def test_paging_exact_fit_has_no_more_pages() -> None:
    """page_size exactly equal to the total row count: still no false has_more."""
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="name", page=1, page_size=3)
    assert len(result.data) == 3
    assert result.has_more is False


# ── unsupported order/dir ────────────────────────────────────────────────────────────────────


def test_unknown_order_raises_unsupported() -> None:
    conn = make_db()
    insert_named_card(conn, LLANOWAR_ELVES)
    with pytest.raises(Unsupported):
        search(conn, "", order="power")  # not one of the five documented orders

    with pytest.raises(Unsupported):
        search(conn, "", dir="reverse")
