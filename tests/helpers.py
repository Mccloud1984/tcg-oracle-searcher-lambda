"""Test-only helper that turns a real Scryfall card object into a row in our schema.

Not the real importer (green lane owns `oracle_searcher/importer.py`); this fills only
what a compiler/search test needs, computed from the real fixture cards in
`tests/fixtures/oracle_cards_sample.jsonl` rather than hand-typed. Grey switches these
tests to the real importer at merge (see docs/PLAN-2026-10-03.md).
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from api.parsing.card_query_nodes import calculate_devotion, fold_accents, get_rarity_number, mana_cost_str_to_dict
from api.parsing.db_info import CARD_SUPERTYPES, CARD_TYPES
from oracle_searcher.schema import COLOR_BITS, create_schema

FIXTURE_CARDS_PATH = Path(__file__).parent / "fixtures" / "oracle_cards_sample.jsonl"


@lru_cache(maxsize=1)
def load_fixture_cards() -> dict[str, dict[str, Any]]:
    """Return every fixture card, keyed by its `name` field, exactly as Scryfall gave it."""
    cards: dict[str, dict[str, Any]] = {}
    with FIXTURE_CARDS_PATH.open() as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            card = json.loads(stripped)
            cards[card["name"]] = card
    return cards


def make_db() -> sqlite3.Connection:
    """Return a fresh in-memory connection with the schema created."""
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    return conn


def _parse_type_line(type_line: str | None) -> tuple[set[str], set[str]]:
    """Split one face's (or the whole card's) type line into (types incl. supertypes, subtypes)."""
    if not type_line:
        return set(), set()
    if "—" in type_line:
        left, _, right = type_line.partition("—")
    else:
        left, right = type_line, ""
    types = {word for word in left.split() if word in CARD_SUPERTYPES or word in CARD_TYPES}
    subtypes = set(right.split())
    return types, subtypes


def _union_types_subtypes(card: dict[str, Any]) -> tuple[list[str], list[str]]:
    faces = card.get("card_faces")
    types: set[str] = set()
    subtypes: set[str] = set()
    if faces:
        for face in faces:
            face_types, face_subtypes = _parse_type_line(face.get("type_line"))
            types |= face_types
            subtypes |= face_subtypes
    else:
        types, subtypes = _parse_type_line(card.get("type_line"))
    return sorted(types), sorted(subtypes)


def _union_oracle_text(card: dict[str, Any]) -> str | None:
    if card.get("oracle_text"):
        return card["oracle_text"]
    faces = card.get("card_faces")
    if not faces:
        return None
    texts = [face["oracle_text"] for face in faces if face.get("oracle_text")]
    return "\n//\n".join(texts) if texts else None


def _color_mask(codes: list[str] | None) -> int:
    return sum(COLOR_BITS[c] for c in (codes or []))


def _union_colors_mask(card: dict[str, Any]) -> int:
    mask = _color_mask(card.get("colors"))
    for face in card.get("card_faces") or []:
        mask |= _color_mask(face.get("colors"))
    return mask


def _as_float(value: Any) -> float | None:
    """Parse a Scryfall numeric-or-"*"-or-None field the way the schema's REAL columns do."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _frame_data(card: dict[str, Any]) -> list[str]:
    entries = []
    if card.get("frame"):
        entries.append(str(card["frame"]).title())
    for effect in card.get("frame_effects") or []:
        entries.append(str(effect).title())
    return entries


# Layouts Scryfall hides from a plain search unless the query asks for them. Mirrors the
# importer's is_extra criterion closely enough for compiler/search tests; green owns the
# authoritative version in oracle_searcher/importer.py.
EXTRA_LAYOUTS = frozenset({"token", "emblem", "vanguard", "plane", "phenomenon", "scheme", "art_series"})


def _front_mana_cost(card: dict[str, Any]) -> str:
    if card.get("mana_cost"):
        return card["mana_cost"]
    faces = card.get("card_faces")
    if faces and faces[0].get("mana_cost"):
        return faces[0]["mana_cost"]
    return ""


def insert_card(conn: sqlite3.Connection, card: dict[str, Any], **overrides: Any) -> int:
    """Insert one fixture card (plus any per-test column overrides) and return its row id."""
    card_types, card_subtypes = _union_types_subtypes(card)
    row: dict[str, Any] = {
        "oracle_id": card["oracle_id"],
        "card_name": card["name"],
        "card_name_folded": fold_accents(card["name"].lower()),
        "type_line": card.get("type_line"),
        "card_types": json.dumps(card_types),
        "card_subtypes": json.dumps(card_subtypes),
        "oracle_text": _union_oracle_text(card),
        "flavor_text": card.get("flavor_text"),
        "mana_cost_text": card.get("mana_cost") or " // ".join(f.get("mana_cost", "") for f in card.get("card_faces") or []),
        "mana_cost_jsonb": json.dumps(mana_cost_str_to_dict(_front_mana_cost(card))),
        "devotion": json.dumps(calculate_devotion(_front_mana_cost(card))),
        "cmc": card.get("cmc"),
        "creature_power": _as_float(card.get("power")),
        "creature_toughness": _as_float(card.get("toughness")),
        "planeswalker_loyalty": _as_float(card.get("loyalty")),
        "card_colors": _union_colors_mask(card),
        "card_color_identity": _color_mask(card.get("color_identity")),
        "produced_mana": _color_mask(card.get("produced_mana")),
        "card_keywords": json.dumps([k.lower() for k in card.get("keywords", [])]),
        "card_oracle_tags": json.dumps([]),
        "card_art_tags": json.dumps([]),
        "card_is_tags": json.dumps([]),
        "card_legalities": json.dumps(card.get("legalities", {})),
        "card_rarity_int": get_rarity_number(card["rarity"]) if card.get("rarity") else None,
        "card_set_code": card.get("set"),
        "collector_number": card.get("collector_number"),
        "collector_number_int": _as_int(card.get("collector_number")),
        "card_layout": card.get("layout"),
        "card_border": card.get("border_color"),
        "card_watermark": card.get("watermark"),
        "card_frame_data": json.dumps(_frame_data(card)),
        "card_artist": card.get("artist"),
        "released_at": card.get("released_at"),
        "edhrec_rank": card.get("edhrec_rank"),
        "price_usd": _as_float((card.get("prices") or {}).get("usd")),
        "price_eur": _as_float((card.get("prices") or {}).get("eur")),
        "price_tix": _as_float((card.get("prices") or {}).get("tix")),
        "game_changer": int(bool(card.get("game_changer"))),
        "is_extra": int(card.get("layout") in EXTRA_LAYOUTS),
        "card_json": json.dumps(card),
    }
    row.update(overrides)
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO cards ({', '.join(columns)}) VALUES ({placeholders})"
    cursor = conn.execute(sql, [row[c] for c in columns])
    card_id = cursor.lastrowid

    for index, face in enumerate(card.get("card_faces") or []):
        conn.execute(
            """
            INSERT INTO card_faces (
                card_id, face_index, face_name, face_type_line, face_oracle_text,
                face_mana_cost, creature_power, creature_toughness, planeswalker_loyalty, face_colors
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                index,
                face.get("name"),
                face.get("type_line"),
                face.get("oracle_text"),
                face.get("mana_cost"),
                _as_float(face.get("power")),
                _as_float(face.get("toughness")),
                _as_float(face.get("loyalty")),
                _color_mask(face.get("colors")),
            ),
        )
    conn.commit()
    return card_id


def insert_named_card(conn: sqlite3.Connection, name: str, **overrides: Any) -> int:
    """Look up a fixture card by its exact Scryfall `name` and insert it."""
    return insert_card(conn, load_fixture_cards()[name], **overrides)
