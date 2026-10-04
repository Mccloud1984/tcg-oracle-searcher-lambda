"""card_json keeps only what Purroxy reads, so the published file stays small.

Read set: backend/app/services/utils.py (format_card_response, card_image_uri, price_fields, purchase_fields), tokens.py
all_parts, proxy art and frame code.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from oracle_searcher.importer import _trim_card_json
from tests.conftest import card_row, load_fixture_cards

if TYPE_CHECKING:
    import sqlite3

JACE = "Jace, Vryn's Prodigy // Jace, Telepath Unbound"


def _fixture_card(name: str) -> dict:
    return next(card for card in load_fixture_cards() if card["name"] == name)


def test_drops_fields_purroxy_never_reads() -> None:
    """Artist, frame, set URIs, flags and the like are dead weight in the 255 MB file (they were ~70% of card_json)."""
    trimmed = _trim_card_json(_fixture_card("Sol Ring"))
    for gone in ("artist", "frame", "set_search_uri", "related_uris", "illustration_id", "games", "finishes", "lang", "object"):
        assert gone not in trimmed


def test_keeps_every_field_purroxy_reads() -> None:
    card = _fixture_card("Sol Ring")
    trimmed = _trim_card_json(card)
    for kept in ("id", "name", "mana_cost", "type_line", "oracle_text", "colors", "color_identity", "cmc", "keywords", "layout"):
        assert trimmed[kept] == card[kept]
    for kept in ("set", "set_name", "collector_number", "released_at", "prices", "purchase_uris", "legalities", "edhrec_rank"):
        assert trimmed[kept] == card[kept]
    assert {"grid", "large", "art_crop"} <= set(trimmed["image_uris"])


def test_double_faced_card_keeps_face_fields_purroxy_reads() -> None:
    card = _fixture_card(JACE)
    trimmed = _trim_card_json(card)
    assert len(trimmed["card_faces"]) == 2
    back, orig = trimmed["card_faces"][1], card["card_faces"][1]
    for kept in ("name", "mana_cost", "type_line", "oracle_text", "loyalty"):
        assert back[kept] == orig[kept]
    assert back["image_uris"]["grid"] == orig["image_uris"]["grid"]
    assert "artist" not in back


def test_all_parts_keep_what_token_detection_reads() -> None:
    """services/tokens.py reads component, id, name and type_line of each part."""
    card = next(c for c in load_fixture_cards() if c.get("all_parts"))
    parts = _trim_card_json(card)["all_parts"]
    assert [set(p) for p in parts] == [{"id", "component", "name", "type_line"}] * len(card["all_parts"])


def test_preview_keeps_only_previewed_at() -> None:
    card = next(c for c in load_fixture_cards() if c.get("preview"))
    assert _trim_card_json(card)["preview"] == {"previewed_at": card["preview"]["previewed_at"]}


def test_built_database_stores_trimmed_json(conn: sqlite3.Connection) -> None:
    stored = json.loads(card_row(conn, "Sol Ring")["card_json"])
    assert stored == _trim_card_json(_fixture_card("Sol Ring"))
