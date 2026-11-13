"""A comma is an ordinary value character, so a bare value is the same search as its quoted spelling (Sylvan #1127).

Purroxy's autocomplete sends what was typed as `name:<text>`: `name:Jace, Re` asks for names with "jace," and "re",
which is what typing "Jace, Re" means. Scryfall's own search drops a bare comma instead (`name:jace, re` -> 6 cards,
live 2026-10-04); Sylvan chose the one rule over that special case (2026-11-10) and this engine follows Sylvan.
Real cards cut from the Scryfall oracle bulk file (`tests/fixtures/jace_name_cards.jsonl`). The quoted expectation is
the live API's (`name:"jace," re` -> 2 cards, 2026-11-13).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from oracle_searcher.search import Unsupported, search
from tests.helpers import insert_card, make_db

if TYPE_CHECKING:
    import sqlite3

JACE_RE = {
    "Jace Beleren",
    "Jace Reawakened",
    "Jace's Erasure",
    "Jace, Reality Sculptor",
    "Jace, Unraveler of Secrets",
    "The Theorist, Jace Beleren",
}


@pytest.fixture(scope="module")
def jace_db() -> sqlite3.Connection:
    conn = make_db()
    for line in (Path(__file__).parent / "fixtures" / "jace_name_cards.jsonl").read_text().splitlines():
        insert_card(conn, json.loads(line))
    return conn


def _names(conn: sqlite3.Connection, q: str) -> set[str]:
    return {card["name"] for card in search(conn, q).data}


COMMA_RE = {"Jace, Reality Sculptor", "Jace, Unraveler of Secrets"}


@pytest.mark.parametrize("q", ["name:jace, re", "jace, re", "(name:jace, re)", 'name:"jace," re'])
def test_a_bare_comma_searches_the_same_as_a_quoted_one(jace_db: sqlite3.Connection, q: str) -> None:
    assert _names(jace_db, q) == COMMA_RE


def test_without_a_comma_every_jace_with_re_matches(jace_db: sqlite3.Connection) -> None:
    assert _names(jace_db, "name:jace re") == JACE_RE


@pytest.mark.parametrize(("q", "expected"), [("name:reality,", set()), ("name:jace,re", set()), ("name:sculptor,", set())])
def test_a_comma_is_part_of_the_word(jace_db: sqlite3.Connection, q: str, expected: set[str]) -> None:
    """No fixture name has "reality," or "jace,re": the comma is matched, not dropped."""
    assert _names(jace_db, q) == expected


@pytest.mark.parametrize("q", ["o:draw, flying", "t:elf,", ",", "name:jace ,re"])
def test_commas_elsewhere_parse(jace_db: sqlite3.Connection, q: str) -> None:
    search(jace_db, q)


@pytest.mark.parametrize("q", ["c:r,", "cmc>3,"])
def test_a_comma_cannot_continue_a_color_or_a_number(jace_db: sqlite3.Connection, q: str) -> None:
    with pytest.raises((Unsupported, ValueError)):
        search(jace_db, q)
