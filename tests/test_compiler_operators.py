"""Per-family correctness tests: real fixture cards, real expected matches.

Widens past the corpus (tests/test_compiler_corpus.py) to check each operator family
actually returns the right rows, not just that it compiles.
"""

from __future__ import annotations

import sqlite3

import pytest

from api.parsing import parse_scryfall_query
from oracle_searcher.search import register_regexp
from oracle_searcher.sqlite_compiler import compile_query
from tests.helpers import insert_named_card, make_db

NICOL_BOLAS = "Nicol Bolas, Dragon-God"  # colors/identity U B R
LLANOWAR_ELVES = "Llanowar Elves"  # colors/identity G, Creature — Elf Druid
COMMAND_TOWER = "Command Tower"  # Land
MANA_CRYPT = "Mana Crypt"  # banned:commander
JACE_VRYN = "Jace, Vryn's Prodigy // Jace, Telepath Unbound"  # transform, face2 loyalty 5
TARMOGOYF_TOKEN = "Tarmogoyf"  # layout token
GRIST = "Grist, the Hunger Tide"  # Legendary Planeswalker, keywords=['Mill']
BARREN_MOOR = "Barren Moor"  # cycling land; "Draw a card" appears only in its cycling reminder
ACCURSED_MARAUDER = "Accursed Marauder"  # "each player ... creature" -- never adjacent "each creature"


def _match(conn: sqlite3.Connection, query_text: str) -> set[int]:
    query = parse_scryfall_query(query_text)
    sql, params = compile_query(query)
    rows = conn.execute(f"SELECT card.id FROM cards AS card WHERE {sql}", params)
    return {row[0] for row in rows}


def _db_with(*names: str) -> tuple[sqlite3.Connection, dict[str, int]]:
    conn = make_db()
    ids = {name: insert_named_card(conn, name) for name in names}
    return conn, ids


# ── color / color identity bitmasks ──────────────────────────────────────────────────────────


def test_color_contains_is_superset() -> None:
    conn, ids = _db_with(NICOL_BOLAS, LLANOWAR_ELVES)
    assert _match(conn, "c:u") == {ids[NICOL_BOLAS]}
    assert _match(conn, "c:g") == {ids[LLANOWAR_ELVES]}


def test_color_equals_is_exact_set() -> None:
    conn, ids = _db_with(NICOL_BOLAS, LLANOWAR_ELVES)
    assert _match(conn, "c=ubr") == {ids[NICOL_BOLAS]}
    assert _match(conn, "c=u") == set()  # Bolas has 3 colors, not just blue


def test_colorless_query_is_equality_to_empty_not_vacuous_superset() -> None:
    """`c:colorless` must not become a vacuous `mask & 0 = 0` (true for every row)."""
    conn, ids = _db_with(NICOL_BOLAS, COMMAND_TOWER)
    assert _match(conn, "c:colorless") == {ids[COMMAND_TOWER]}


def test_identity_colon_is_subset_not_superset() -> None:
    """`id:` (and `id<=`) means "at most these colors" -- the opposite default from `c:`."""
    conn, ids = _db_with(NICOL_BOLAS, LLANOWAR_ELVES)
    assert _match(conn, "id<=g") == {ids[LLANOWAR_ELVES]}
    assert _match(conn, "id<=ubr") == {ids[NICOL_BOLAS]}
    assert ids[LLANOWAR_ELVES] not in _match(conn, "id<=ubr")
    # `:` is the shorthand actually used in the wild -- exercise it directly, not just its `<=`
    # spelling, with a case that actually discriminates subset from superset: Bolas's identity
    # (UBR) is a SUPERSET of a plain "u" query, so a superset-check bug would wrongly match Bolas
    # and wrongly miss the colorless Command Tower (whose empty identity is a subset of anything).
    conn2, ids2 = _db_with(NICOL_BOLAS, COMMAND_TOWER)
    assert _match(conn2, "id:u") == {ids2[COMMAND_TOWER]}


def test_colorless_query_would_vacuously_match_everything_without_the_special_case() -> None:
    """Documents the bug `c:colorless`'s special-case guards against.

    Without it, `:`/`>=` lowers to a plain superset check `(mask & 0) = 0`, which is true for
    every row (any mask ANDed with 0 is 0) -- not just the colorless ones.
    """
    conn, ids = _db_with(NICOL_BOLAS, COMMAND_TOWER)
    naive_superset_sql = "(card.card_colors & 0) = 0"
    naive_matches = {row[0] for row in conn.execute(f"SELECT card.id FROM cards AS card WHERE {naive_superset_sql}")}
    assert naive_matches == {ids[NICOL_BOLAS], ids[COMMAND_TOWER]}  # the bug this guards against
    assert _match(conn, "c:colorless") == {ids[COMMAND_TOWER]}  # the actual compiler gets it right


# ── legality ──────────────────────────────────────────────────────────────────────────────────


def test_legal_and_banned_commander() -> None:
    conn, ids = _db_with(LLANOWAR_ELVES, MANA_CRYPT)
    assert _match(conn, "legal:commander") == {ids[LLANOWAR_ELVES]}
    assert _match(conn, "banned:commander") == {ids[MANA_CRYPT]}


# ── type / subtype resolution ────────────────────────────────────────────────────────────────


def test_type_query_routes_known_type_to_card_types() -> None:
    conn, ids = _db_with(COMMAND_TOWER, LLANOWAR_ELVES)
    assert _match(conn, "t:land") == {ids[COMMAND_TOWER]}
    assert _match(conn, "t:creature") == {ids[LLANOWAR_ELVES]}


def test_type_query_routes_unknown_value_to_card_subtypes() -> None:
    conn, ids = _db_with(LLANOWAR_ELVES)
    assert _match(conn, "t:elf") == {ids[LLANOWAR_ELVES]}


def test_type_query_routes_extra_layout_word_to_types_not_subtypes() -> None:
    """`t:token` must find a word Sylvan's CARD_TYPES set doesn't recognise as a type.

    The importer's `parse_type_line` puts every word before the em dash in `card_types`
    regardless of whether Sylvan recognises it as a real type -- a token's type line is "Token
    Creature" with no dash at all, so "Token" lands in `card_types`, never `card_subtypes`
    (docs/PLAN-2026-10-03.md, Purple item 2). Routing `t:token` to `card_subtypes` instead finds
    nothing against a real build, where Scryfall finds 821.
    """
    conn, ids = _db_with(TARMOGOYF_TOKEN, LLANOWAR_ELVES)
    assert _match(conn, "t:token") == {ids[TARMOGOYF_TOKEN]}


def test_type_planeswalker_finds_multi_face_card() -> None:
    """schema.py: card_types is the union over faces -- a transform card's back face counts."""
    conn, ids = _db_with(JACE_VRYN, LLANOWAR_ELVES)
    assert _match(conn, "t:planeswalker") == {ids[JACE_VRYN]}
    assert _match(conn, "t:creature") == {ids[JACE_VRYN], ids[LLANOWAR_ELVES]}


# ── multi-face numeric (loyalty only on card_faces for a transform card) ───────────────────────


def test_loyalty_matches_via_face_exists() -> None:
    conn, ids = _db_with(JACE_VRYN)
    assert _match(conn, "loyalty>=5") == {ids[JACE_VRYN]}
    assert _match(conn, "loyalty<=2") == set()


def test_loyalty_face_exists_guard_is_real() -> None:
    """Regression guard: without the face EXISTS union, loyalty>=5 would miss this card."""
    conn, ids = _db_with(JACE_VRYN)
    card_only_sql = "(card.planeswalker_loyalty >= ?)"
    broken = {row[0] for row in conn.execute(f"SELECT card.id FROM cards AS card WHERE {card_only_sql}", [5])}
    assert broken == set()  # the bug: card-level loyalty is NULL for a transform card
    assert _match(conn, "loyalty>=5") == {ids[JACE_VRYN]}  # the fix: face EXISTS catches it


# ── oracle text: card-level column already holds every face, no EXISTS needed ─────────────────


def test_oracle_text_finds_text_only_on_back_face() -> None:
    conn, ids = _db_with(JACE_VRYN, LLANOWAR_ELVES)
    assert _match(conn, 'o:"exile Jace"') == {ids[JACE_VRYN]}


def test_quoted_phrase_requires_adjacent_words() -> None:
    """A quoted o: phrase must match as one contiguous substring, not "words in order, anything between".

    Accursed Marauder's text is "When this creature enters, each player sacrifices a nontoken
    creature of their choice." -- it contains "each" and, much later, "creature" again, but never
    the adjacent phrase "each creature". Found via scripts/parity.py's suffix-corpus run: a
    `(o:"each creature" or ...)`-style query ran ~3x Scryfall's total even after the reminder-text
    fix (item 1), because splitting a quoted value into words and wildcarding *between* them
    (the pre-fix pattern) let any text separate them.
    """
    conn, ids = _db_with(ACCURSED_MARAUDER, LLANOWAR_ELVES)
    assert _match(conn, 'o:"each creature"') == set()
    assert _match(conn, 'o:"each player"') == {ids[ACCURSED_MARAUDER]}


# ── keywords ──────────────────────────────────────────────────────────────────────────────────


def test_keyword_membership() -> None:
    conn, ids = _db_with(GRIST, LLANOWAR_ELVES)
    assert _match(conn, "keyword:mill") == {ids[GRIST]}


# ── exact name ────────────────────────────────────────────────────────────────────────────────


def test_exact_name_case_insensitive() -> None:
    conn, ids = _db_with(LLANOWAR_ELVES)
    assert _match(conn, '!"llanowar elves"') == {ids[LLANOWAR_ELVES]}
    assert _match(conn, '!"Llanowar Elf"') == set()


# ── date / year ───────────────────────────────────────────────────────────────────────────────


def test_bare_year_date_query_uses_a_range_not_literal_equality() -> None:
    conn = make_db()
    card_id = insert_named_card(conn, LLANOWAR_ELVES, released_at="2024-06-01")
    assert _match(conn, "date:2024") == {card_id}
    assert _match(conn, "date:2023") == set()


# ── reminder text excluded from o:/oracle: (Purple item 1) ──────────────────────────────────


def test_oracle_text_search_ignores_reminder_text() -> None:
    r"""`o:` must not match a word that appears only inside a reminder-text parenthetical.

    Barren Moor's oracle text is "This land enters tapped.\n{T}: Add {B}.\nCycling {B} ({B},
    Discard this card: Draw a card.)" -- "draw a card" appears only inside the cycling reminder.
    Scryfall's docs (scryfall.com/docs/syntax): o:/oracle: search "the current Oracle text", and
    "fo:/fulloracle:" is the separate operator that "includes reminder text" -- so o:draw must not
    find it. Before this fix, our oracle_text column kept reminder text, so `o:draw` and
    `o:"draw a card"` wrongly matched (scripts/parity.py's `only_ours` list for `o:draw` against a
    real build named this exact card).
    """
    conn, ids = _db_with(BARREN_MOOR, LLANOWAR_ELVES)
    assert _match(conn, "o:draw") == set()
    assert _match(conn, 'o:"draw a card"') == set()
    # Real, non-reminder text on the card must still match.
    assert _match(conn, "o:cycling") == {ids[BARREN_MOOR]}
    assert _match(conn, "o:tapped") == {ids[BARREN_MOOR]}


# ── regex (registered REGEXP function) ──────────────────────────────────────────────────────


def test_regex_matches_oracle_text_case_insensitively() -> None:
    conn, ids = _db_with(LLANOWAR_ELVES, COMMAND_TOWER)
    register_regexp(conn)
    # The "." keeps api.parsing.rewrite.lower_literal_regexes from lowering this to a plain
    # substring search, so the test actually exercises the REGEXP operator, not LIKE.
    assert _match(conn, "o:/A.D \\{G\\}/") == {ids[LLANOWAR_ELVES]}


def test_regex_without_registered_function_raises_operational_error() -> None:
    """Documents why `search.register_regexp` must run before any query with a regex leaf."""
    conn, _ids = _db_with(LLANOWAR_ELVES)
    # A metacharacter-free pattern like "add" would be lowered to a plain substring search by
    # api.parsing.rewrite.lower_literal_regexes before it ever reaches the compiler -- "a.d" keeps
    # it a real RegexValueNode so this test exercises the REGEXP operator, not LIKE.
    query = parse_scryfall_query("o:/a.d/")
    sql, params = compile_query(query)
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        conn.execute(f"SELECT card.id FROM cards AS card WHERE {sql}", params)
