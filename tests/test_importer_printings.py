"""Printing-level data in the build: `build(..., printings_path=...)` folds every printing into its oracle card's row.

tests/fixtures/default_cards_sample.jsonl holds real printings cut from Scryfall's default_cards (2026-10-03),
trimmed to the fields the importer reads: for each fixture card its representative printing plus one printing per
distinct promo/finish/visibility signature.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from oracle_searcher.importer import build
from tests.conftest import CARDS_FIXTURE, PRINTINGS_FIXTURE, TAGS_FIXTURE, card_row

if TYPE_CHECKING:
    from pathlib import Path

PRINTING_ONLY_COLUMNS = {"card_is_tags", "is_extra"}


def _build(tmp_path: Path, name: str, printings: Path | None) -> sqlite3.Connection:
    out = tmp_path / f"{name}.sqlite"
    build(CARDS_FIXTURE, TAGS_FIXTURE, out, printings_path=printings)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def tmp_module(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("printings")


@pytest.fixture(scope="module")
def plain(tmp_module: Path) -> sqlite3.Connection:
    return _build(tmp_module, "plain", None)


@pytest.fixture(scope="module")
def full(tmp_module: Path) -> sqlite3.Connection:
    return _build(tmp_module, "full", PRINTINGS_FIXTURE)


def _tags(conn: sqlite3.Connection, name: str) -> set[str]:
    return set(json.loads(card_row(conn, name)["card_is_tags"]))


def test_promo_printing_elsewhere_adds_promo_tags(plain: sqlite3.Connection, full: sqlite3.Connection) -> None:
    """Sol Ring's representative printing (frc) is no promo; its prm, g05, pfdn and mps printings are.

    Scryfall's is:promo matches a card when ANY printing is promo; the old build only read the one printing.
    """
    assert not {"promo", "judge_gift", "buyabox", "masterpiece", "universesbeyond"} & _tags(plain, "Sol Ring")
    assert {"promo", "judge_gift", "buyabox", "masterpiece", "universesbeyond"} <= _tags(full, "Sol Ring")


def test_card_level_tags_do_not_change(plain: sqlite3.Connection, full: sqlite3.Connection) -> None:
    for name in ("Atraxa, Praetors' Voice", "Lightning Bolt", "Westvale Abbey // Ormendahl, Profane Prince"):
        assert {"commander", "spell"} & _tags(full, name) == {"commander", "spell"} & _tags(plain, name)


def test_visible_through_another_printing(plain: sqlite3.Connection, full: sqlite3.Connection) -> None:
    """Call from the Grave: the representative printing is a playtest card (mb2, hidden), its `past` printing is not."""
    assert card_row(plain, "Call from the Grave")["is_extra"] == 1
    assert card_row(full, "Call from the Grave")["is_extra"] == 0


def test_hidden_when_every_printing_is_hidden(full: sqlite3.Connection) -> None:
    """Red Herring (cmb1/cmb2) exists only as playtest printings legal nowhere."""
    hidden = full.execute("SELECT is_extra FROM cards WHERE oracle_id = ?", ("0e99efaf-6402-44c5-ae8b-1f5bb1b68333",)).fetchone()
    assert hidden["is_extra"] == 1


def test_without_printings_only_representative_printing_counts(plain: sqlite3.Connection, full: sqlite3.Connection) -> None:
    """Every column but the two printing-driven ones is identical with and without printings."""
    columns = [r["name"] for r in plain.execute("PRAGMA table_info(cards)") if r["name"] not in PRINTING_ONLY_COLUMNS]
    sql = f"SELECT {', '.join(columns)} FROM cards ORDER BY oracle_id"
    assert [tuple(r) for r in plain.execute(sql)] == [tuple(r) for r in full.execute(sql)]


def test_printing_tags_only_grow(plain: sqlite3.Connection, full: sqlite3.Connection) -> None:
    for row in plain.execute("SELECT oracle_id, card_is_tags FROM cards"):
        grown = full.execute("SELECT card_is_tags FROM cards WHERE oracle_id = ?", (row["oracle_id"],)).fetchone()
        assert set(json.loads(row["card_is_tags"])) <= set(json.loads(grown["card_is_tags"]))


def test_hidden_printing_adds_no_tags_to_a_visible_card(full: sqlite3.Connection) -> None:
    """Griselbrand's only in-store promo printing is in phel, a memorabilia set Scryfall's default search hides.

    Parity 2026-10-03: unioning hidden printings too made is:instore 126 vs Scryfall's 116 (is:etched, foil, reprint too).
    """
    assert "instore" not in _tags(full, "Griselbrand")


def test_scryfall_preview_means_a_card_page_not_a_set_page(full: sqlite3.Connection) -> None:
    """Scryfall's is:scryfallpreview is 6 cards (preview.source_uri is a /card/ page); the 321 slz printings link a set page."""
    assert "scryfallpreview" in _tags(full, "Kraul Stinger")
    assert "scryfallpreview" not in _tags(full, "Lightning Bolt")
