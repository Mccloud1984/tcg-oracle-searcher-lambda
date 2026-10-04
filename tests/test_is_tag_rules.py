"""KNOWN_IS_TAGS and the compiler's Unsupported raise for is:/has: tags no row can answer."""

from __future__ import annotations

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
