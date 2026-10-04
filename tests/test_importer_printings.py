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

from oracle_searcher.importer import (
    _get_oracle_id_from_printing,
    _trim_card_json,
    _trim_card_json_overlay,
    build,
    merge_overlay,
    printing_card,
)
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


def test_alchemy_card_legal_nowhere_is_hidden(full: sqlite3.Connection) -> None:
    """Skanos, Green Dragon Vassal (hbg, Alchemy: Baldur's Gate) is legal in no format.

    Parity 2026-10-03: t:dragon was 449 vs Scryfall's 444 and t:elf 713 vs 698; the extras were exactly the hbg cards.
    """
    assert card_row(full, "Skanos, Green Dragon Vassal")["is_extra"] == 1


def test_printings_table_exists(full: sqlite3.Connection) -> None:
    """The printings table is created by build()."""
    tables = full.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='printings'").fetchall()
    assert len(tables) == 1


def test_printings_table_has_required_columns(full: sqlite3.Connection) -> None:
    """The printings table has all required columns."""
    cols = {r["name"] for r in full.execute("PRAGMA table_info(printings)")}
    required = {"id", "oracle_id", "set_code", "collector_number", "released_at", "set_type", "games", "card_json"}
    assert required <= cols


def test_printings_table_has_indexes(full: sqlite3.Connection) -> None:
    """The printings table has the required indexes."""
    indexes = {r["name"] for r in full.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='printings'")}
    assert "printings_oracle_id" in indexes
    assert "printings_set_collector" in indexes


def test_printings_table_populated_from_fixture(full: sqlite3.Connection) -> None:
    """Every printing in the fixture is in the printings table."""
    fixture_lines = 0
    with PRINTINGS_FIXTURE.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                fixture_lines += 1

    (count,) = full.execute("SELECT COUNT(*) FROM printings").fetchone()
    assert count == fixture_lines


def test_printing_overlay_merges_to_full_trimmed_json(full: sqlite3.Connection) -> None:
    """A merged printing (oracle card + overlay) equals the full trimmed JSON of the printing.

    This is the correctness guard for the overlay approach: the overlay contains only
    the differences, and merging it back with the oracle card's card_json should give
    the exact trimmed JSON that would have been stored without the overlay.
    """
    # Pick a card with multiple printings (Forest)
    rows = full.execute(
        """
        SELECT p.id, p.card_json, c.card_json, c.oracle_id
        FROM printings p
        JOIN cards c ON p.oracle_id = c.oracle_id
        WHERE c.card_name = 'Forest'
        LIMIT 1
        """
    ).fetchall()

    assert len(rows) > 0
    printing_id, overlay_json, _oracle_card_json, oracle_id = rows[0]

    # Merge the overlay with the oracle card
    merged = printing_card(full, printing_id)

    # Verify overlay is not None/empty
    assert overlay_json, "Overlay should not be empty"
    overlay = json.loads(overlay_json)
    assert isinstance(overlay, dict), "Overlay should be a dict"

    # The merged result should be sensible
    assert merged["id"] == printing_id
    assert merged.get("oracle_id") == oracle_id


def test_printing_overlay_is_smaller_than_full_json(full: sqlite3.Connection) -> None:
    """The overlay (differences only) is significantly smaller than storing full card_json."""
    rows = full.execute(
        """
        SELECT p.card_json, c.card_json
        FROM printings p
        JOIN cards c ON p.oracle_id = c.oracle_id
        WHERE c.card_name IN ('Forest', 'Island', 'Sol Ring')
        LIMIT 10
        """
    ).fetchall()

    assert len(rows) > 0
    overlay_sizes = []
    full_sizes = []

    for overlay_json, oracle_json in rows:
        overlay_sizes.append(len(overlay_json))
        full_sizes.append(len(oracle_json))

    avg_overlay = sum(overlay_sizes) / len(overlay_sizes)
    avg_full = sum(full_sizes) / len(full_sizes)

    # Overlay should be significantly smaller on average
    assert avg_overlay < avg_full, f"Overlay {avg_overlay} should be < full {avg_full}"
    # Typically overlays should be much smaller (10-50% of full)
    assert avg_overlay < avg_full * 0.5, "Overlay should be <50% of full size"


def test_every_fixture_printing_round_trips_through_its_overlay():
    """Regression: a key the oracle card has but a printing lacks leaked into the merged printing.

    The first overlay only stored keys that differ, so a preview date, all_parts and so on came back from the card.
    Every fixture printing lacks some of its card's keys.
    """
    oracle = {}
    for line in CARDS_FIXTURE.read_text().splitlines():
        card = json.loads(line)
        oracle[card["oracle_id"]] = _trim_card_json(card)
    checked = 0
    for line in PRINTINGS_FIXTURE.read_text().splitlines():
        printing = json.loads(line)
        card = oracle.get(_get_oracle_id_from_printing(printing))
        if card is None:
            continue
        assert merge_overlay(card, _trim_card_json_overlay(printing, card)) == _trim_card_json(printing), printing["id"]
        checked += 1
    assert checked > 600
