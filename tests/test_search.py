"""End-to-end tests for `oracle_searcher.search.search`: extras, ordering, paging."""

from __future__ import annotations

import pytest

from oracle_searcher.search import SearchResult, Unsupported, search
from tests.helpers import insert_named_card, make_db

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


def test_order_released_defaults_to_newest_first() -> None:
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="released")
    assert _names(result) == [SOL_RING, LLANOWAR_ELVES, BLACK_LOTUS]


def test_order_released_desc_dir_flips_to_oldest_first() -> None:
    """dir=desc flips the *documented* arrow -- released's own arrow is newest-first."""
    conn = make_db()
    _insert_three(conn)
    result = search(conn, "", order="released", dir="desc")
    assert _names(result) == [BLACK_LOTUS, LLANOWAR_ELVES, SOL_RING]


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
