"""The lookup ops (named, collection, prints, autocomplete) against a build of the fixtures plus one accented card.

`TODAY` is fixed because the real fixtures hold printings releasing after the day they were cut (Forest and Island
in trk on 2026-11-13, Jace Beleren's pspl on 2026-10-23), which is what the released-printing swap is about.
"""

from __future__ import annotations

import copy
import sqlite3

import pytest

from oracle_searcher import lookups
from oracle_searcher.importer import build
from oracle_searcher.schema import PRINTINGS_ALIAS
from oracle_searcher.search import Unsupported
from tests.conftest import CARDS_FIXTURE, FIXTURES_DIR, PRINTINGS_FIXTURE, TAGS_FIXTURE
from tests.helpers import insert_card, load_fixture_cards, make_db

TODAY = "2026-10-04"
EXTRA_CARDS = FIXTURES_DIR / "lookups_extra_cards.jsonl"  # real Jötun Grunt (an accent to fold) and its 4 real printings
EXTRA_PRINTINGS = FIXTURES_DIR / "lookups_extra_printings.jsonl"
FOREST_ID = "b34bb2dc-c1af-4d77-b0b3-a0fb342a5fc6"  # oracle id
SOL_RING = "Sol Ring"


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> sqlite3.Connection:
    """Cards and printings files built from the fixtures plus the accented card, the printings ATTACHed."""
    root = tmp_path_factory.mktemp("lookups")
    cards = root / "oracle.jsonl"
    cards.write_text(CARDS_FIXTURE.read_text() + EXTRA_CARDS.read_text())
    printings = root / "default.jsonl"
    printings.write_text(PRINTINGS_FIXTURE.read_text() + EXTRA_PRINTINGS.read_text())
    build(cards, TAGS_FIXTURE, root / "c.sqlite", printings, root / "p.sqlite")
    conn = sqlite3.connect(root / "c.sqlite")
    conn.execute(f"ATTACH DATABASE '{root / 'p.sqlite'}' AS {PRINTINGS_ALIAS}")
    return conn


class AttachSpy:
    """Stands in for the handler's `printings_connection`: counts how often the printings file was asked for."""

    def __init__(self) -> None:
        """Starts with no calls."""
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def named(conn: sqlite3.Connection, name: str, attach: AttachSpy | None = None) -> dict | None:
    return lookups.named(conn, {"name": name}, attach or AttachSpy(), today=TODAY)["card"]


# named: matching rules


def test_named_exact_name_is_case_insensitive(db) -> None:
    assert named(db, "sOl rInG")["name"] == SOL_RING


def test_named_folds_accents(db) -> None:
    """The importer folds accents in `card_name_folded`; the lookup must fold its input the same way."""
    assert named(db, "Jotun Grunt")["name"] == "Jötun Grunt"


def test_named_face_name_finds_the_double_faced_card(db) -> None:
    assert named(db, "Delver of Secrets")["name"] == "Delver of Secrets // Insectile Aberration"
    assert named(db, "Insectile Aberration")["name"] == "Delver of Secrets // Insectile Aberration"


def test_named_letters_and_digits_key_ignores_punctuation(db) -> None:
    assert named(db, "Fire/Ice")["name"] == "Fire // Ice"
    assert named(db, "Richard Garfield Ph D")["name"] == "Richard Garfield, Ph.D."


def test_named_exact_beats_a_face_name_match() -> None:
    """Regression guard for tier order: a card named exactly "Earth" must win over "Heaven // Earth"'s back face."""
    conn = make_db()
    insert_card(conn, _renamed("Heaven // Earth", "Earth", ["Earth"], "e0000000-0000-4000-8000-000000000001"))
    insert_card(conn, load_fixture_cards()["Heaven // Earth"])
    assert named(conn, "Earth")["name"] == "Earth"


def test_named_front_face_before_back_face() -> None:
    """Two cards share a face name: the one where it is the front face wins (Scryfall's /cards/named order)."""
    conn = make_db()
    insert_card(conn, load_fixture_cards()["Heaven // Earth"])  # "Earth" is its back face
    insert_card(conn, _renamed("Heaven // Earth", "Earth // Sky", ["Earth", "Sky"], "e0000000-0000-4000-8000-000000000002"))
    assert named(conn, "Earth")["name"] == "Earth // Sky"


def test_named_unknown_name_is_none(db) -> None:
    assert named(db, "No Such Card") is None


def test_named_prefers_the_visible_card_over_a_hidden_one(db) -> None:
    """Two oracle cards are called Red Herring; one is a playtest card Scryfall hides (is_extra)."""
    card = named(db, "Red Herring")
    (visible_id,) = db.execute("SELECT oracle_id FROM cards WHERE card_name = 'Red Herring' AND is_extra = 0").fetchone()
    assert card["oracle_id"] == visible_id


def test_named_finds_a_hidden_card_when_nothing_visible_matches(db) -> None:
    assert named(db, "Tyranid")["name"] == "Tyranid"


def test_named_exact_hidden_beats_nothing_but_a_visible_fuzzier_match_wins(db) -> None:
    """The plan's rule is literal: hidden extras only when NOTHING visible matches, across all three matching steps."""
    conn = make_db()
    insert_card(conn, load_fixture_cards()["Tyranid"])  # a token named "Tyranid"
    visible = _renamed("Fire // Ice", "Tyranid // Ice", ["Tyranid", "Ice"], "e0000000-0000-4000-8000-000000000003")
    insert_card(conn, visible)
    assert named(conn, "Tyranid")["name"] == "Tyranid // Ice"


def _renamed(source: str, name: str, face_names: list[str], oracle_id: str) -> dict:
    card = copy.deepcopy(load_fixture_cards()[source])
    card["name"], card["oracle_id"] = name, oracle_id
    for face, face_name in zip(card.get("card_faces") or [], face_names, strict=False):
        face["name"] = face_name
    return card


# named: the released-printing swap


def test_named_swaps_an_unreleased_printing_for_the_newest_released_one(db) -> None:
    """Forest's chosen printing is trk (2026-11-13), not out on TODAY; its newest released one is sld 2442."""
    spy = AttachSpy()
    card = named(db, "Forest", spy)
    assert (card["set"], card["collector_number"], card["released_at"]) == ("sld", "2442", "2026-08-10")
    assert card["oracle_id"] == FOREST_ID
    assert spy.calls == 1


def test_named_keeps_an_unreleased_card_with_no_released_printing(db) -> None:
    """Munitions Enthusiast exists only as trk (2026-11-13) and pw26 (2026-11-20): nothing to swap to."""
    assert named(db, "Munitions Enthusiast")["released_at"] == "2026-11-13"


def test_named_released_card_never_opens_the_printings_file(db) -> None:
    spy = AttachSpy()
    named(db, SOL_RING, spy)
    assert spy.calls == 0


def test_named_swap_waits_for_the_release_date(db) -> None:
    """On the day trk comes out, Forest is its own newest printing and nothing is swapped."""
    card = lookups.named(db, {"name": "Forest"}, AttachSpy(), today="2026-11-13")["card"]
    assert card["set"] == "trk"


# collection


def collection(conn: sqlite3.Connection, identifiers: list[dict], attach: AttachSpy | None = None) -> dict:
    return lookups.collection(conn, {"identifiers": identifiers}, attach or AttachSpy(), today=TODAY)


def test_collection_by_name_follows_named_including_the_swap(db) -> None:
    result = collection(db, [{"name": "sol ring"}, {"name": "Forest"}, {"name": "Nope"}])
    assert [c["name"] for c in result["data"]] == [SOL_RING, "Forest"]
    assert result["data"][1]["set"] == "sld"
    assert result["not_found"] == [{"name": "Nope"}]


def test_collection_by_id_returns_that_exact_printing_even_if_unreleased(db) -> None:
    """An id means this printing on purpose: no swap (Purroxy's own rule for set+number identifiers)."""
    (forest_trk,) = db.execute("SELECT id FROM printings WHERE oracle_id = ? AND set_code = 'trk'", (FOREST_ID,)).fetchone()
    (card,) = collection(db, [{"id": forest_trk}])["data"]
    assert (card["id"], card["set"], card["released_at"]) == (forest_trk, "trk", "2026-11-13")


def test_collection_by_set_and_collector_number_lowercases_the_set(db) -> None:
    (card,) = collection(db, [{"set": "MSH", "collector_number": "290"}])["data"]
    assert (card["name"], card["set"], card["collector_number"]) == ("Island", "msh", "290")


def test_collection_unknown_id_and_set_are_not_found(db) -> None:
    missing = [{"id": "00000000-0000-4000-8000-000000000000"}, {"set": "zzz", "collector_number": "1"}]
    result = collection(db, missing)
    assert result == {"data": [], "not_found": missing}


def test_collection_keeps_request_order(db) -> None:
    result = collection(db, [{"name": "Island"}, {"name": SOL_RING}, {"name": "Forest"}])
    assert [c["name"] for c in result["data"]] == ["Island", SOL_RING, "Forest"]


def test_collection_over_75_identifiers_is_unsupported(db) -> None:
    with pytest.raises(Unsupported, match="75"):
        collection(db, [{"name": SOL_RING}] * 76)


def test_collection_identifier_of_another_shape_is_unsupported(db) -> None:
    with pytest.raises(Unsupported, match="identifier"):
        collection(db, [{"name": SOL_RING, "set": "cmd"}])


def test_collection_with_only_names_of_released_cards_never_opens_printings(db) -> None:
    spy = AttachSpy()
    collection(db, [{"name": SOL_RING}, {"name": "Black Lotus"}], spy)
    assert spy.calls == 0


# prints


def prints(conn: sqlite3.Connection, q: str, **fields: object) -> list[dict]:
    return lookups.prints(conn, {"q": q, **fields}, AttachSpy())["data"]


def test_prints_returns_every_printing_newest_first(db) -> None:
    data = prints(db, f'!"{SOL_RING}"')
    (expected,) = db.execute(
        "SELECT COUNT(*) FROM printings p JOIN cards c USING (oracle_id) WHERE c.card_name = ?", (SOL_RING,)
    ).fetchone()
    assert len(data) == expected > 20
    dates = [c["released_at"] for c in data]
    assert dates == sorted(dates, reverse=True)
    assert {c["name"] for c in data} == {SOL_RING}


def test_prints_each_entry_is_that_printings_own_card(db) -> None:
    ids = {c["id"] for c in prints(db, f'!"{SOL_RING}"')}
    sql = "SELECT p.id FROM printings p JOIN cards c USING (oracle_id) WHERE c.card_name = ?"
    printing_ids = {row[0] for row in db.execute(sql, (SOL_RING,))}
    assert ids == printing_ids


def test_prints_ties_on_date_go_by_collector_number(db) -> None:
    data = prints(db, '!"Doubling Season"')
    same_day = [c["collector_number"] for c in data if c["released_at"] == "2024-11-15"]
    assert same_day == ["216", "216s", "428", "438"]  # 4 real printings on one day


def test_prints_includes_extras_so_tokens_match(db) -> None:
    """Tokens are hidden from a normal search; a printings lookup for a token name is exactly what Purroxy asks."""
    data = prints(db, '!"Tyranid" t:token')
    assert [c["name"] for c in data] == ["Tyranid"]


def test_prints_sets_exclude(db) -> None:
    data = prints(db, '!"Forest"', sets_exclude=["TRK", "sld"])
    assert data
    assert not {"trk", "sld"} & {c["set"] for c in data}


def test_prints_sets_restrict(db) -> None:
    data = prints(db, '!"Forest"', sets_restrict=["tmt", "FIC"])
    assert {c["set"] for c in data} == {"tmt", "fic"}


def test_prints_released_on_or_before_is_inclusive(db) -> None:
    data = prints(db, '!"Forest"', released_on_or_before="2026-08-10")
    assert data[0]["released_at"] == "2026-08-10"
    assert all(c["released_at"] <= "2026-08-10" for c in data)


def test_prints_paper_only_drops_digital_only_printings(db) -> None:
    """Jötun Grunt's td0 printing is MTGO-only (real `games` data); the other three are paper."""
    everything = prints(db, '!"Jötun Grunt"')
    paper = prints(db, '!"Jötun Grunt"', paper_only=True)
    assert {c["set"] for c in everything} - {c["set"] for c in paper} == {"td0"}
    assert len(paper) == len(everything) - 1


def test_prints_with_no_match_is_empty(db) -> None:
    assert prints(db, '!"No Such Card"') == []


def test_prints_unsupported_query_raises_unsupported(db) -> None:
    with pytest.raises(Unsupported, match="mana"):
        prints(db, "mana:{1}{G}")


def test_prints_needs_the_printings_file(db) -> None:
    spy = AttachSpy()
    lookups.prints(db, {"q": '!"Sol Ring"'}, spy)
    assert spy.calls == 1


# autocomplete


def autocomplete(conn: sqlite3.Connection, prefix: str) -> list[str]:
    return lookups.autocomplete(conn, {"prefix": prefix}, AttachSpy())["names"]


def test_autocomplete_under_two_characters_is_empty(db) -> None:
    assert autocomplete(db, "s") == []
    assert autocomplete(db, " s ") == []


def test_autocomplete_prefix_matches_come_before_contains_matches(db) -> None:
    """Invasion of Ikoria (EDHREC 1396) starts with "in", so it precedes Sol Ring (rank 1), which only contains it."""
    names = autocomplete(db, "in")
    assert names[0].startswith("Invasion of Ikoria")
    assert names[1] == SOL_RING
    assert all("in" in n.lower() for n in names)


def test_autocomplete_orders_each_group_by_edhrec_rank_nulls_last(db) -> None:
    names = autocomplete(db, "jace")
    ranks = [db.execute("SELECT edhrec_rank FROM cards WHERE card_name = ?", (n,)).fetchone()[0] for n in names]
    assert len(names) > 2
    keyed = [(r is None, r or 0) for r in ranks]
    assert keyed == sorted(keyed)


def test_autocomplete_hides_extras(db) -> None:
    assert "Tyranid" not in autocomplete(db, "tyranid")
    assert autocomplete(db, "tyranid") == []


def test_autocomplete_folds_accents_and_case(db) -> None:
    assert autocomplete(db, "JOT") == ["Jötun Grunt"]
    assert autocomplete(db, "jötun") == ["Jötun Grunt"]


def test_autocomplete_returns_at_most_20(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """No two-letter string matches over 20 of the 93 fixture cards, so the cap is shrunk to prove it is applied."""
    assert lookups.MAX_AUTOCOMPLETE == 20
    assert len(autocomplete(db, "in")) == 17
    monkeypatch.setattr(lookups, "MAX_AUTOCOMPLETE", 5)
    assert len(autocomplete(db, "in")) == 5


def test_autocomplete_multi_face_card_matches_by_its_front_face(db) -> None:
    assert "Delver of Secrets // Insectile Aberration" in autocomplete(db, "delver")
