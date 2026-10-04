"""Tests for oracle_searcher.importer, built from the real 2026-10-03 fixture bulk files.

Every assertion here is checked against tests/fixtures/oracle_cards_sample.jsonl and
tests/fixtures/oracle_tags_sample.jsonl (real cards cut from Scryfall's bulk data, see
docs/PLAN-2026-10-03.md), never invented card shapes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from oracle_searcher.importer import _mask, build, check
from oracle_searcher.schema import COLOR_BITS
from tests.conftest import CARDS_FIXTURE, TAGS_FIXTURE, card_row, face_rows, load_fixture_cards


def test_jace_vryns_prodigy_transform_faces(conn: sqlite3.Connection) -> None:
    """A transform card's card_types is the union over both faces, and each face's own loyalty/text/colors live in card_faces."""
    row = card_row(conn, "Jace, Vryn's Prodigy // Jace, Telepath Unbound")
    assert set(json.loads(row["card_types"])) == {"Legendary", "Creature", "Planeswalker"}
    oracle_text = row["oracle_text"]
    assert "Draw a card, then discard a card" in oracle_text
    assert "Up to one target creature gets -2/-0" in oracle_text
    # The card-level column is NULL for a multi-face card -- the per-face value lives in card_faces.
    assert row["planeswalker_loyalty"] is None

    faces = face_rows(conn, row["id"])
    assert len(faces) == 2
    assert faces[0]["face_name"] == "Jace, Vryn's Prodigy"
    assert faces[0]["creature_power"] == 0.0
    assert faces[0]["creature_toughness"] == 2.0
    assert faces[1]["face_name"] == "Jace, Telepath Unbound"
    assert faces[1]["planeswalker_loyalty"] == 5.0
    assert faces[1]["face_colors"] == COLOR_BITS["U"]


def test_valki_modal_dfc_maps_colors_from_face_union(conn: sqlite3.Connection) -> None:
    """Valki (modal_dfc) has no top-level 'colors'; card_colors is the union over its two faces (B, B+R)."""
    row = card_row(conn, "Valki, God of Lies // Tibalt, Cosmic Impostor")
    assert row["card_colors"] == COLOR_BITS["B"] | COLOR_BITS["R"]
    assert row["card_color_identity"] == COLOR_BITS["B"] | COLOR_BITS["R"]
    assert set(json.loads(row["card_types"])) == {"Legendary", "Creature", "God", "Planeswalker"} or {
        "Legendary",
        "Creature",
        "Planeswalker",
    } <= set(json.loads(row["card_types"]))
    faces = face_rows(conn, row["id"])
    assert [f["face_name"] for f in faces] == ["Valki, God of Lies", "Tibalt, Cosmic Impostor"]


def test_heaven_earth_split_maps_combined_mana_cost(conn: sqlite3.Connection) -> None:
    """A split card keeps Scryfall's own combined top-level mana_cost ('{X}{G} // {X}{R}{R}')."""
    row = card_row(conn, "Heaven // Earth")
    assert row["mana_cost_text"] == "{X}{G} // {X}{R}{R}"
    assert row["card_colors"] == COLOR_BITS["G"] | COLOR_BITS["R"]
    faces = face_rows(conn, row["id"])
    assert [f["face_mana_cost"] for f in faces] == ["{X}{G}", "{X}{R}{R}"]


def test_gollum_adventure_maps_front_face_as_creature(conn: sqlite3.Connection) -> None:
    """Gollum (adventure) is a Creature with the Adventure's instant/sorcery type unioned in, and keeps its own power/toughness."""
    row = card_row(conn, "Gollum, Silent Slinker // Meager Meal")
    types = set(json.loads(row["card_types"]))
    assert "Creature" in types
    faces = face_rows(conn, row["id"])
    assert faces[0]["creature_power"] == 4.0
    assert faces[0]["creature_toughness"] == 3.0
    # The adventure half (Meager Meal) is a Sorcery, unioned into card_types too.
    assert "Sorcery" in types


def test_color_identity_mask_five_colors(conn: sqlite3.Connection) -> None:
    """Esika // The Prismatic Bridge's color_identity is all five colors -> the full WUBRG mask."""
    row = card_row(conn, "Esika, God of the Tree // The Prismatic Bridge")
    assert row["card_color_identity"] == sum(COLOR_BITS[c] for c in "WUBRG")


def test_produced_mana_mask_includes_colorless_bit(conn: sqlite3.Connection) -> None:
    """Mana Crypt produces colourless mana only -> produced_mana is just the C=32 bit, not 0."""
    row = card_row(conn, "Mana Crypt")
    assert row["produced_mana"] == COLOR_BITS["C"] == 32
    assert row["card_colors"] == 0  # Mana Crypt itself is colourless


def test_mask_ignores_a_symbol_outside_wubrgc() -> None:
    """_mask() skips a produced_mana symbol with no bit, instead of crashing the whole build.

    Found in the real 2026-10-03 oracle_cards.jsonl.gz (not in the small fixture file): the Un-set
    card "Sole Performer" has produced_mana == ["T"], Scryfall's only non-WUBRGC value in the
    whole file -- a full build raised KeyError('T') on it before this test.
    """
    assert _mask(["T"]) == 0
    assert _mask(["C", "T"]) == COLOR_BITS["C"]


def test_legalities_stored_as_given(conn: sqlite3.Connection) -> None:
    """card_legalities round-trips Scryfall's own per-format legal/not_legal object."""
    row = card_row(conn, "Grist, the Hunger Tide")
    legalities = json.loads(row["card_legalities"])
    assert legalities["modern"] == "legal"
    assert legalities["standard"] == "not_legal"
    assert legalities["commander"] == "legal"


def test_oracle_text_search_strips_reminder_text(conn: sqlite3.Connection) -> None:
    """oracle_text_search drops reminder text that oracle_text (full) keeps (Purple item 1).

    Barren Moor's only mention of drawing is inside its cycling reminder -- Scryfall's `o:`
    excludes reminder text (scryfall.com/docs/syntax), so the column `o:` compiles against must
    not have it, while the full oracle_text column (kept for anything needing the real printed
    text) still does.
    """
    row = card_row(conn, "Barren Moor")
    assert "Draw a card" in row["oracle_text"]
    assert "Draw a card" not in row["oracle_text_search"]
    assert "Cycling" in row["oracle_text_search"]
    assert "enters tapped" in row["oracle_text_search"]


def test_preview_card_is_present(conn: sqlite3.Connection) -> None:
    """A card whose chosen printing is a future preview (released_at 2026-11-13) is still imported."""
    row = card_row(conn, "Island")
    assert row["released_at"] == "2026-11-13"


def test_game_changer_flag(conn: sqlite3.Connection) -> None:
    """game_changer reflects Scryfall's own flag: Rhystic Study is one, Lightning Bolt is not."""
    assert card_row(conn, "Rhystic Study")["game_changer"] == 1
    assert card_row(conn, "Smothering Tithe")["game_changer"] == 1
    assert card_row(conn, "Lightning Bolt")["game_changer"] == 0


def test_oracle_tag_ancestors_are_included(conn: sqlite3.Connection) -> None:
    """card_oracle_tags holds the tagged slug plus every ancestor slug, transitively.

    Rhystic Study is tagged 'draw-engine' (parent 'repeatable-draw', whose own parents are
    'draw' and 'repeatable-card-advantage') in tests/fixtures/oracle_tags_sample.jsonl.
    """
    row = card_row(conn, "Rhystic Study")
    tags = set(json.loads(row["card_oracle_tags"]))
    assert {"draw-engine", "repeatable-draw", "draw", "repeatable-card-advantage"} <= tags


@pytest.mark.parametrize(
    ("name", "expected_is_extra"),
    [
        # Evidence for which layouts/set_types Scryfall's own default search hides, gathered live
        # 2026-10-03 and saved under tests/fixtures/scryfall/ (max 1 request/second, never from a
        # test): every sample card in each of these categories carries legalities that are ALL
        # "not_legal" (tests/fixtures/scryfall/layout_{token,emblem,vanguard,scheme,planar,
        # art_series}_default.json, tests/fixtures/scryfall/layout_front_card_default.json), and a
        # broad "date>=1993" search returns 33,649 of the file's 38,705 cards by default
        # (tests/fixtures/scryfall/broad_default.json vs broad_include_extras.json) -- far more
        # than vanguard+scheme+planar alone, confirming tokens/emblems are hidden too, matching
        # Scryfall's own docs ("Vanguard, plane, scheme, and phenomenon cards are hidden by
        # default, as are cards from memorabilia sets. You must ... search for their type").
        # reversible_card is the control: tests/fixtures/scryfall/layout_reversible_card_default.json's
        # sample has legalities {'not_legal', 'legal'} -- it's a real playable card, not an extra.
        ("Tyranid", True),  # layout: token
        ("Koth of the Hammer Emblem", True),  # layout: emblem
        ("Brightglass Gearhulk // Brightglass Gearhulk", True),  # layout: art_series (set_type memorabilia)
        ("Surprise!", True),  # layout: front_card (set_type memorabilia)
        # set_type funny (Un-sets, holiday cards): Scryfall's own docs (scryfall.com/docs/syntax,
        # checked 2026-10-03), "Extra Cards and Funny Cards": "Un-cards, holiday cards, and other
        # funny cards are findable with is:funny or mentioning their set" -- i.e. hidden otherwise,
        # the same "search for their type/set to reveal it" pattern as vanguard/scheme/memorabilia
        # above (docs/PLAN-2026-10-03.md, Purple item 4).
        ("Richard Garfield, Ph.D.", False),  # set_type: funny (Unsanctioned), legal nowhere but Scryfall shows it (live 2026-10-03)
        # Regression: black-border Unfinity cards legal in commander ARE in Scryfall's default
        # `legal:commander` (ours hid 174 of them as "funny": 32116 vs 31942). Funny is hidden
        # only when legal in no format.
        ("Atomwheel Acrobats", False),  # set_type: funny (Unfinity), legal in commander
        ("Celebr-8000", False),  # set_type: funny (Unfinity), legal in commander
        ("Lightning Bolt", False),
        ("Jace, Vryn's Prodigy // Jace, Telepath Unbound", False),
        # Funny sets legal nowhere: only the Un-sets show by default (live probe saved in
        # tests/fixtures/scryfall/funny_set_default_visibility.json). Regression: we hid them all,
        # so `t:creature cmc<=2` was 5000 vs Scryfall's 5071 (and `t:zombie t:creature` 613 vs 625).
        ("Wall of Fortune", False),  # und
        ("Rarity", False),  # ptg
        ("Cardboard Carapace", False),  # ugl
        ("Stop That", False),  # unh
        ("Plot Armor", False),  # unf
        ("Spike, Tournament Grinder", False),  # ust
        ("Patient Turtle", True),  # cmb2 (Mystery Booster playtest)
        ("Phyrexian Broodstar", True),  # unk (Unknown Event playtest)
        ("Bog Humbugs", True),  # hho (Happy Holidays)
        ("Collectigull // Only the Best", True),  # ph19 (Heroes of the Realm)
        ("The Windy City", True),  # punk (Black Lotus Unknown Planechase)
        # Playtest cards in non-funny sets (live probe, same fixture file) are hidden too, except
        # und's; and every content_warning card is hidden. Regression: `t:creature cmc<=2` showed
        # 18 mb2 playtest creatures and Stone-Throwing Devils that Scryfall does not.
        ("Luxior, Ignited", True),  # mb2 masters, promo_types playtest
        ("Convention Maro", True),  # pf24 promo, promo_types playtest
        ("Look at Me, I'm R&D", False),  # und playtest: Un-set, visible
        ("Stone-Throwing Devils", True),  # content_warning
        ("Pradesh Gypsies", True),  # content_warning
    ],
)
def test_is_extra(conn: sqlite3.Connection, name: str, *, expected_is_extra: bool) -> None:
    """is_extra matches what Scryfall's own default search hides (see the evidence cited above)."""
    assert card_row(conn, name)["is_extra"] == (1 if expected_is_extra else 0)


@pytest.mark.parametrize(
    ("name", "expected_commander"),
    [
        # Front-face structural eligibility, including Scryfall's `*` toughness cases (Purple
        # item 3; live-verified 2026-10-03 against api.scryfall.com's is:commander results).
        ("Ashaya, Soul of the Wild", True),  # Legendary Creature, toughness "*"
        ("Daxos, Blessed by the Sun", True),  # Legendary Enchantment Creature, toughness "*"
        ("Lumra, Bellow of the Woods", True),  # Legendary Creature, toughness "*"
        # Front face only: the legendary-creature half is the BACK face, so neither qualifies,
        # even though the combined/union type_line (what the old rewrite-time expansion read)
        # includes "Legendary Creature".
        ("Westvale Abbey // Ormendahl, Profane Prince", False),  # front face: plain Land
        ("Invasion of Ikoria // Zilortha, Apex of Ikoria", False),  # front face: Battle, no toughness
        # Structurally eligible (Legendary Creature with printed toughness) but banned as commander.
        ("Griselbrand", False),
        ("Lightning Bolt", False),  # not legendary at all
    ],
)
def test_commander_is_tag(conn: sqlite3.Connection, name: str, *, expected_commander: bool) -> None:
    """card_is_tags includes "commander" exactly where Scryfall's is:commander does."""
    tags = json.loads(card_row(conn, name)["card_is_tags"])
    assert ("commander" in tags) == expected_commander


def test_every_card_in_fixture_is_imported(conn: sqlite3.Connection) -> None:
    """Every card in the fixture file ends up in the database -- extras are flagged, not dropped."""
    (count,) = conn.execute("SELECT COUNT(*) FROM cards").fetchone()
    assert count == len(load_fixture_cards())


def test_build_is_atomic_on_failure(tmp_path: Path) -> None:
    """A build that raises partway through leaves no file at out_path (or its .tmp)."""
    bad_cards_path = tmp_path / "bad_cards.jsonl"
    # Second line is truncated/invalid JSON, so build() raises after writing the first card.
    bad_cards_path.write_text(
        Path(CARDS_FIXTURE).read_text(encoding="utf-8").splitlines()[0] + "\n{not valid json\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "out.sqlite"

    with pytest.raises(json.JSONDecodeError):
        build(bad_cards_path, TAGS_FIXTURE, out_path)

    assert not out_path.exists()
    assert not out_path.with_name(out_path.name + ".tmp").exists()


def test_check_reports_card_count_problem(built_db_path: Path) -> None:
    """check() flags a database with far fewer than a full file's ~38,700 cards."""
    problems = check(built_db_path)
    assert any("cards" in p and "30000" in p for p in problems)


def test_check_passes_known_cards_present(built_db_path: Path) -> None:
    """check() does not complain about missing well-known cards when they are present."""
    problems = check(built_db_path)
    assert not any("missing" in p for p in problems)


def test_check_detects_missing_face_rows(built_db_path: Path, tmp_path: Path) -> None:
    """check() flags a multi-face card whose card_faces rows were lost."""
    broken_path = tmp_path / "broken.sqlite"
    broken_path.write_bytes(built_db_path.read_bytes())
    conn = sqlite3.connect(broken_path)
    conn.execute("DELETE FROM card_faces")
    conn.commit()
    conn.close()

    problems = check(broken_path)
    assert any("card_faces" in p for p in problems)


def test_check_ignores_unspaced_slash_in_a_single_face_card_name(tmp_path: Path) -> None:
    """check() doesn't mistake a single-face card's own "//" for a multi-face separator.

    Real card, not invented: "SP//dr, Piloted by Peni" (oracle_cards.jsonl.gz, 2026-10-03) is a
    single-face (layout "normal") card whose own name contains "//" with no surrounding spaces,
    unlike every real multi-face name ("Front // Back", confirmed against all 3,214 multi-face
    cards in that same file). An earlier `card_name LIKE '%//%'` check flagged it as a multi-face
    card missing its card_faces rows, which it never had and never needed.
    """
    minimal_card = {
        "oracle_id": "11111111-1111-1111-1111-111111111111",
        "name": "SP//dr, Piloted by Peni",
        "layout": "normal",
        "type_line": "Legendary Creature — Human Spider",
        "legalities": {"standard": "not_legal"},
        "colors": ["U", "R"],
        "color_identity": ["U", "R"],
        "keywords": [],
        "cmc": 3.0,
        "rarity": "mythic",
        "set": "mar",
        "collector_number": "1",
        "prices": {},
        "released_at": "2025-01-01",
    }
    cards_path = tmp_path / "cards.jsonl"
    cards_path.write_text(json.dumps(minimal_card) + "\n", encoding="utf-8")
    empty_tags_path = tmp_path / "tags.jsonl"
    empty_tags_path.write_text("", encoding="utf-8")
    out_path = tmp_path / "out.sqlite"

    build(cards_path, empty_tags_path, out_path)

    assert not any("card_faces" in p for p in check(out_path))


def test_check_reports_unreadable_database(tmp_path: Path) -> None:
    """check() returns a problem (not an exception) for a missing or corrupt database file."""
    problems = check(tmp_path / "does_not_exist.sqlite")
    assert problems
    assert all(isinstance(p, str) for p in problems)
