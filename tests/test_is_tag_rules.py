"""KNOWN_IS_TAGS and the compiler's Unsupported raise for is:/has: tags no row can answer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from api.parsing.rewrite import _DERIVED_EXPANSIONS
from oracle_searcher.importer import IS_TAG_CHECKS
from oracle_searcher.is_tag_rules import KNOWN_IS_TAGS
from oracle_searcher.is_tag_sweep import SWEEP_TAGS
from oracle_searcher.search import Unsupported, search

if TYPE_CHECKING:
    import sqlite3


def test_known_is_tags_is_exactly_import_rules_plus_sweep_tags() -> None:
    assert frozenset(IS_TAG_CHECKS) | SWEEP_TAGS == KNOWN_IS_TAGS


def test_a_tag_is_never_both_an_import_rule_and_a_sweep_tag() -> None:
    assert not set(IS_TAG_CHECKS) & SWEEP_TAGS


def test_rewritten_tags_are_not_card_is_tags_leaves() -> None:
    rewritten = {value for (alias, value) in _DERIVED_EXPANSIONS if alias == "is"}
    assert not rewritten & KNOWN_IS_TAGS


@pytest.mark.parametrize("tag", ["atypical", "default", "nonsense"])
def test_printing_level_and_unknown_is_tags_raise_unsupported(conn: sqlite3.Connection, tag: str) -> None:
    """Without the raise these compile to 'matches nothing', an unflagged wrong answer."""
    with pytest.raises(Unsupported):
        search(conn, f"is:{tag}")


def test_unknown_has_tag_and_negation_raise_unsupported(conn: sqlite3.Connection) -> None:
    with pytest.raises(Unsupported):
        search(conn, "has:nonsense")
    with pytest.raises(Unsupported):
        search(conn, "-is:atypical")


@pytest.mark.parametrize("tag", sorted(KNOWN_IS_TAGS))
def test_every_known_tag_compiles(conn: sqlite3.Connection, tag: str) -> None:
    prefix = "has" if tag in {"watermark", "indicator"} else "is"
    search(conn, f"{prefix}:{tag}")


def _partner_cards() -> dict[str, dict]:
    path = Path(__file__).parent / "fixtures" / "scryfall" / "is_tags" / "partner_cards.jsonl"
    return {card["name"]: card for card in map(json.loads, path.read_text().splitlines())}


# Membership below is Scryfall's live is:partner answer (2026-10-03, 228 cards): every partner-like
# mechanic on a legendary card -- Partner, Partner with, Choose a background, Doctor's companion,
# the Backgrounds themselves and the Time Lord Doctors. Not the non-legendary Battlebond partners
# (Lore Weaver) and not other "Doctor" subtypes (Doctor Strange). Regression: the old rule read
# only the Partner keywords and matched 130 of 228.
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Pir, Imaginative Rascal", True),
        ("Krark, the Thumbless", True),
        ("Will Kenrith", True),
        ("Karlach, Fury of Avernus", True),
        ("Nyssa of Traken", True),
        ("Rose Tyler", True),
        ("Acolyte of Bahamut", True),
        ("The Fourteenth Doctor", True),
        ("Lore Weaver", False),
        ("Doctor Strange, Surgeon", False),
        ("Doctor Jane Foster", False),
        ("Chulane, Teller of Tales", False),
        ("Bear Umbra", False),
    ],
)
def test_partner_rule_matches_scryfall_on_real_cards(name: str, expected: bool) -> None:
    assert IS_TAG_CHECKS["partner"](_partner_cards()[name], "", "") is expected
