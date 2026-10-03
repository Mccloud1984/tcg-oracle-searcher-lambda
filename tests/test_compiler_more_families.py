"""Remaining operator families not already covered: rarity, frame, is-tags, grouping, mana cost."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from api.parsing import parse_scryfall_query
from oracle_searcher.sqlite_compiler import Unsupported, compile_query
from tests.helpers import insert_named_card, make_db

if TYPE_CHECKING:
    import sqlite3

SWORDS = "Swords to Plowshares"  # uncommon, frame 2015, white
COUNTERSPELL = "Counterspell"  # uncommon, blue
DEMONIC_TUTOR = "Demonic Tutor"  # mythic, black
LLANOWAR_ELVES = "Llanowar Elves"


def _match(conn: sqlite3.Connection, query_text: str) -> set[int]:
    query = parse_scryfall_query(query_text)
    sql, params = compile_query(query)
    rows = conn.execute(f"SELECT card.id FROM cards AS card WHERE {sql}", params)
    return {row[0] for row in rows}


# ── rarity (text name -> card_rarity_int, then a plain numeric comparison) ──────────────────────


def test_rarity_equality_and_ordering() -> None:
    conn = make_db()
    swords_id = insert_named_card(conn, SWORDS)
    tutor_id = insert_named_card(conn, DEMONIC_TUTOR)
    assert _match(conn, "r:uncommon") == {swords_id}
    assert _match(conn, "r>uncommon") == {tutor_id}  # mythic outranks uncommon


# ── frame (JSON array of title-cased frame/frame_effects entries) ──────────────────────────────


def test_frame_membership() -> None:
    conn = make_db()
    swords_id = insert_named_card(conn, SWORDS)
    insert_named_card(conn, LLANOWAR_ELVES, card_frame_data="[]")
    assert _match(conn, "frame:2015") == {swords_id}


# ── is: tags that are not in rewrite.py's derived-predicate table (stay a plain leaf) ──────────


def test_plain_is_tag_not_in_derived_table_compiles_and_matches() -> None:
    """Confirm the literal card_is_tags path, not just rewrite.py's derived-expansion subtrees.

    `is:gamechanger` has no entry in rewrite._DERIVED_EXPANSIONS (unlike `is:spell`, which
    oracle_searcher/is_tag_rules.py's teal lane turned into a `-t:land` rewrite -- see
    docs/is-tags.md): it's populated directly into card_is_tags at import time
    (`oracle_searcher.importer.IS_TAG_CHECKS`), so it reaches the compiler as a plain
    card_is_tags leaf and must still be in `is_tag_rules.KNOWN_IS_TAGS` or this would raise
    Unsupported instead of matching.
    """
    conn = make_db()
    tagged_id = insert_named_card(conn, SWORDS, card_is_tags='["gamechanger"]')
    insert_named_card(conn, LLANOWAR_ELVES, card_is_tags="[]")
    assert _match(conn, "is:gamechanger") == {tagged_id}


# ── AND/OR grouping (implicit AND binds tighter than explicit "or") ────────────────────────────


def test_or_of_two_oracle_leaves() -> None:
    conn = make_db()
    counter_id = insert_named_card(conn, COUNTERSPELL)
    swords_id = insert_named_card(conn, SWORDS)
    insert_named_card(conn, DEMONIC_TUTOR)
    assert _match(conn, "o:counter or o:exile") == {counter_id, swords_id}


def test_implicit_and_binds_tighter_than_or() -> None:
    """`o:land o:onto or (t:creature o:add o:mana)` -- a real corpus query's grouping.

    Must parse as `(o:land AND o:onto) OR (t:creature AND o:add AND o:mana)`, not the much
    greedier `o:land AND (o:onto OR (t:creature AND o:add AND o:mana))`. Birds of Paradise
    ("{T}: Add one mana of any color.") is a creature matching the second branch but has
    neither "land" nor "onto" in its text -- the wrong grouping would wrongly exclude it
    (its top-level AND would require "land" from every match), the right one includes it.
    """
    conn = make_db()
    birds_id = insert_named_card(conn, "Birds of Paradise")
    insert_named_card(conn, COUNTERSPELL)  # neither branch
    assert _match(conn, "o:land o:onto or (t:creature o:add o:mana)") == {birds_id}


# ── mana cost / devotion: explicitly deferred, not silently wrong ──────────────────────────────


def test_mana_cost_comparison_raises_unsupported() -> None:
    query = parse_scryfall_query("mana:{1}{G}")
    with pytest.raises(Unsupported, match="mana"):
        compile_query(query)


def test_devotion_comparison_raises_unsupported() -> None:
    query = parse_scryfall_query("devotion:{g}{g}")
    with pytest.raises(Unsupported, match="mana"):
        compile_query(query)
