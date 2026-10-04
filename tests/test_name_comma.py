"""A comma typed after a card-name word (Purroxy's autocomplete sends `name:jace, re`) is dropped, as Scryfall does.

Real cards cut from the Scryfall oracle bulk file (`tests/fixtures/jace_name_cards.jsonl`). The expected sets come
from the live API (2026-10-04, `name:jace, re` -> 6 cards; the Jace emblem is hidden by default there too).
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


@pytest.mark.parametrize("q", ["name:jace, re", "jace, re", "name:jace re", "(name:jace, re)"])
def test_comma_after_a_name_word_is_ignored(jace_db: sqlite3.Connection, q: str) -> None:
    """Regression: the lexer rejected `,`, so Purroxy's `name:jace, re` fell back to Scryfall."""
    assert _names(jace_db, q) == JACE_RE


def test_trailing_comma_at_the_end_of_the_query(jace_db: sqlite3.Connection) -> None:
    assert _names(jace_db, "name:reality,") == {"Jace, Reality Sculptor"}
    assert _names(jace_db, "reality,") == {"Jace, Reality Sculptor"}


def test_quoted_comma_stays_literal(jace_db: sqlite3.Connection) -> None:
    """Scryfall: `name:"jace," re` -> 2 cards (the comma is part of the quoted text)."""
    assert _names(jace_db, 'name:"jace," re') == {"Jace, Reality Sculptor", "Jace, Unraveler of Secrets"}


@pytest.mark.parametrize("q", ["name:jace,re", "o:draw, flying", "t:elf,", ",", "name:jace ,re"])
def test_other_commas_are_still_unsupported(jace_db: sqlite3.Connection, q: str) -> None:
    """Scryfall reads `name:jace,re` as the phrase "jace re" and `o:draw,` as a literal comma: not guessed here."""
    with pytest.raises((Unsupported, ValueError)):
        search(jace_db, q)
