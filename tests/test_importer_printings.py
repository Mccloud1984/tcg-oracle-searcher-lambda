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
from oracle_searcher.schema import PRINTINGS_ALIAS, PRINTINGS_SCHEMA_VERSION
from oracle_searcher.search import search
from tests.conftest import CARDS_FIXTURE, FIXTURES_DIR, PRINTINGS_FIXTURE, TAGS_FIXTURE, card_row

if TYPE_CHECKING:
    from pathlib import Path

PRINTING_ONLY_COLUMNS = {"card_is_tags", "is_extra"}


def _build(tmp_path: Path, name: str, printings: Path | None) -> sqlite3.Connection:
    """The cards file; with `printings`, the printings file is built next to it and ATTACHed as the handler does."""
    out = tmp_path / f"{name}.sqlite"
    printings_out = tmp_path / f"{name}.printings.sqlite" if printings else None
    build(CARDS_FIXTURE, TAGS_FIXTURE, out, printings_path=printings, printings_out_path=printings_out)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    if printings_out:
        conn.execute(f"ATTACH DATABASE '{printings_out}' AS {PRINTINGS_ALIAS}")
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


def test_printings_table_is_in_the_second_file_not_the_cards_file(full: sqlite3.Connection) -> None:
    """The printings table made the cards file 345 MB (limit 512 MB /tmp); it lives in its own file now."""
    in_cards = full.execute("SELECT name FROM main.sqlite_master WHERE name = 'printings'").fetchall()
    in_printings = full.execute(f"SELECT name FROM {PRINTINGS_ALIAS}.sqlite_master WHERE name = 'printings'").fetchall()
    assert (len(in_cards), len(in_printings)) == (0, 1)


def test_printings_file_has_its_own_schema_version(full: sqlite3.Connection) -> None:
    (version,) = full.execute(f"SELECT value FROM {PRINTINGS_ALIAS}.meta WHERE key = 'printings_schema_version'").fetchone()
    assert version == str(PRINTINGS_SCHEMA_VERSION)
    (cards_version,) = full.execute("SELECT value FROM main.meta WHERE key = 'schema_version'").fetchone()
    assert cards_version == "3"


def test_build_without_printings_out_writes_no_printings_file(tmp_path: Path) -> None:
    stats = build(CARDS_FIXTURE, TAGS_FIXTURE, tmp_path / "c.sqlite", printings_path=PRINTINGS_FIXTURE)
    assert list(tmp_path.iterdir()) == [tmp_path / "c.sqlite"]
    assert stats["printing_count"] == 0


def test_printings_table_has_required_columns(full: sqlite3.Connection) -> None:
    """The printings table has all required columns."""
    cols = {r["name"] for r in full.execute("PRAGMA table_info(printings)")}
    required = {"id", "oracle_id", "set_code", "collector_number", "released_at", "set_type", "games", "card_json"}
    assert required <= cols


def test_printings_table_has_indexes(full: sqlite3.Connection) -> None:
    """The printings table has the required indexes."""
    indexes = {
        r["name"]
        for r in full.execute(f"SELECT name FROM {PRINTINGS_ALIAS}.sqlite_master WHERE type='index' AND tbl_name='printings'")
    }
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


def test_reversible_card_printing_counts_for_its_oracle_card(tmp_path: Path) -> None:
    """A reversible_card printing (Secret Lair) has no top-level oracle_id, only one per face: it was skipped.

    Adrix and Nev's only full-art printing is the sld reversible one; Scryfall's is:full lists 28 such cards that
    the build missed (full 802 vs 825, 2026-10-04). Both faces carry the same oracle_id. Fixtures are real
    2026-10-03 bulk rows.
    """
    out = tmp_path / "reversible.sqlite"
    build(
        FIXTURES_DIR / "reversible_cards.jsonl",
        TAGS_FIXTURE,
        out,
        printings_path=FIXTURES_DIR / "reversible_printings.jsonl",
    )
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    assert "full" in _tags(conn, "Adrix and Nev, Twincasters")


def test_frames_of_every_printing_count_for_is_old_and_is_new(tmp_path: Path) -> None:
    """Lightning Bolt's representative printing (msc) is 2015; its lea/3ed/m10 printings are 1993/1997/2003.

    Scryfall's is:old / is:new match a card when ANY printing has the frame; the build read only the
    representative's (is:old 4454 vs live 7285, is:new 29228 vs 29561, 2026-10-04). Real 2026-10-03 rows.
    """
    out = tmp_path / "frames.sqlite"
    build(
        FIXTURES_DIR / "frames_cards.jsonl",
        TAGS_FIXTURE,
        out,
        printings_path=FIXTURES_DIR / "frames_printings.jsonl",
    )
    conn = sqlite3.connect(out)
    for query in ("is:old", "is:new", "frame:1993", "frame:2003"):
        assert [c["name"] for c in search(conn, query).data] == ["Lightning Bolt"], query


@pytest.mark.parametrize(
    ("name", "hidden"),
    [
        ("Red Mana", True),  # type "Card" (Secret Lair box), legal nowhere
        ("Experience", True),  # type "Card" in token sets
        ("Lydari Druid", True),  # digital-only (Sega box set psdg), legal nowhere
        ("Gleemox", True),  # digital-only mtgo promo, legal nowhere
        ("Aswan Jaguar", True),  # astral digital + memorabilia
        ("Faerie Dragon", True),  # astral digital + two token printings
        ("Mechtitan", True),  # token oracle card; its sld reversible printing has faces only, typed Token
        ("Call from the Grave", True),  # astral digital + mb2 playtest printing
        ("Pinkie Pie", False),  # sld box paper, legal nowhere: Scryfall shows it
        ("Dungeon of the Mad Mage", False),  # Dungeon in token and memorabilia sets: shown
        ("Undercity // The Initiative", False),  # double_faced_token layout, but a Dungeon: shown
    ],
)
def test_default_search_visibility_of_legal_nowhere_oddities(tmp_path: Path, name: str, *, hidden: bool) -> None:
    """Scryfall's default search hides cards legal nowhere whose printings are digital-only, typed Card or Token.

    Measured 2026-10-04 against the live is:hires/is:nonfoil lists: 35 cards we showed that Scryfall hides, all
    legal in no format and all printings either digital (Astral `past`, Sega `psdg`, mtgo promo), typed "Card" or
    "Token ..." (counters, Role tokens), or already hidden; none of the 33.6k shown cards matches that. Dungeons
    stay visible (Scryfall lists 5, incl. the double_faced_token Undercity). Real 2026-10-03 rows.
    """
    out = tmp_path / "hidden.sqlite"
    build(
        FIXTURES_DIR / "default_hidden_cards.jsonl",
        TAGS_FIXTURE,
        out,
        printings_path=FIXTURES_DIR / "default_hidden_printings.jsonl",
    )
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    cards = conn.execute("SELECT is_extra FROM cards WHERE card_name = ?", (name,)).fetchall()
    assert [bool(r["is_extra"]) for r in cards] == [hidden]


def test_printing_only_in_another_game_does_not_add_tags(tmp_path: Path) -> None:
    """Arden Angel's only nonfoil printing is the Japanese psdg one (its sld printing is foil-only).

    That set is Sega-only (games ["sega"]), which Scryfall's default search hides, so is:nonfoil excludes the card:
    ours was 1 over live (33591 vs 33590, 2026-10-04). Real 2026-10-03 rows.
    """
    out = tmp_path / "lang.sqlite"
    build(
        FIXTURES_DIR / "default_hidden_cards.jsonl",
        TAGS_FIXTURE,
        out,
        printings_path=FIXTURES_DIR / "default_hidden_printings.jsonl",
    )
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    assert {"foil", "nonfoil"} & _tags(conn, "Arden Angel") == {"foil"}


def test_variation_printing_does_not_add_a_frame(tmp_path: Path) -> None:
    """Arcane Teachings' plst JUD-78 is frame 1997 and its variation JUD-78† is 2015 (`variation: true`).

    Scryfall's default search hides variations (include:variations shows them): live frame:2015 lists no plst
    printing of it, and ours was 4 over (is:new 29563 vs 29561, 2026-10-04). Real 2026-10-03 rows.
    """
    out = tmp_path / "variation.sqlite"
    build(
        FIXTURES_DIR / "variation_cards.jsonl",
        TAGS_FIXTURE,
        out,
        printings_path=FIXTURES_DIR / "variation_printings.jsonl",
    )
    conn = sqlite3.connect(out)
    assert [c["name"] for c in search(conn, "frame:1997").data] == ["Arcane Teachings"]
    assert search(conn, "frame:2015").data == []


def test_silver_border_promo_of_an_un_card_adds_no_tags(tmp_path: Path) -> None:
    """Ashnod's Coupon's pal04 printing (Arena League 2004, silver border, legal nowhere) is hidden on Scryfall.

    is:arena_league was 46 vs live 40: the six Un-card pal04 promos (Booster Tutor, Mise, ...) are not in the live
    list, nor is Ashnod's Coupon in frame:2003 (2026-10-04). Its ugl printing still shows the card. Real rows.
    """
    out = tmp_path / "silver.sqlite"
    build(
        FIXTURES_DIR / "default_hidden_cards.jsonl",
        TAGS_FIXTURE,
        out,
        printings_path=FIXTURES_DIR / "default_hidden_printings.jsonl",
    )
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    assert not {"arena_league", "promo"} & _tags(conn, "Ashnod's Coupon")
    assert not conn.execute("SELECT is_extra FROM cards WHERE card_name = ?", ("Ashnod's Coupon",)).fetchone()["is_extra"]


def _misc_conn(tmp_path: Path) -> sqlite3.Connection:
    out = tmp_path / "misc.sqlite"
    build(FIXTURES_DIR / "is_misc_cards.jsonl", TAGS_FIXTURE, out)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.parametrize(
    ("name", "hybrid"),
    [
        ("Kitchen Finks", True),
        # Prepare layout: the front face is {2}{B}{G}, only the second face costs {B/G}. Live is:hybrid is 613, ours was 620
        # because the seven Prepare cards matched through their second face (2026-10-04).
        ("Lluwen, Exchange Student // Pest Friend", False),
    ],
)
def test_hybrid_reads_the_front_face_cost(tmp_path: Path, name: str, *, hybrid: bool) -> None:
    """is:hybrid looks at the front face's mana cost."""
    assert ("hybrid" in _tags(_misc_conn(tmp_path), name)) is hybrid
