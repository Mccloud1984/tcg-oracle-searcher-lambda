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
from oracle_searcher.search import search
from tests.conftest import CARDS_FIXTURE, FIXTURES_DIR, PRINTINGS_FIXTURE, TAGS_FIXTURE, card_row

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
