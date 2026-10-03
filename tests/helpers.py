"""Test-only helper that inserts a real Scryfall card object as a row in our schema.

Builds the row with the real importer's own row-building functions (`oracle_searcher.importer`
internals) rather than a separate reimplementation, so a compiler/search test exercises exactly
what the real importer produces (docs/PLAN-2026-10-03.md, Purple item 5). A hand-rolled parse
here previously disagreed with the importer's own type/subtype split -- it put unrecognised
leading type words (Token, Emblem, ...) in `card_subtypes`, while the real importer's
`parse_type_line` puts every word before the em dash in `card_types` regardless of whether
Sylvan recognises it -- and that disagreement hid the `t:token` bug (item 2) from this suite.

Cards are keyed by `name` from `tests/fixtures/oracle_cards_sample.jsonl`; no fixture card has
an oracle tag in `oracle_tags_sample.jsonl` that these per-card tests need, so tags are passed as
an empty map (matching the previous helper's always-empty `card_oracle_tags`).
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from oracle_searcher.importer import (
    _CARD_COLUMNS,
    _CARD_INSERT_SQL,
    _FACE_COLUMNS,
    _FACE_INSERT_SQL,
    _build_card_row,
    _build_face_rows,
)
from oracle_searcher.schema import create_schema

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


def insert_card(conn: sqlite3.Connection, card: dict[str, Any], **overrides: Any) -> int:
    """Insert one fixture card (plus any per-test column overrides) and return its row id."""
    row = _build_card_row(card, oracle_id_to_tag_slugs={})
    row.update(overrides)
    cursor = conn.execute(_CARD_INSERT_SQL, [row[c] for c in _CARD_COLUMNS])
    card_id = cursor.lastrowid

    face_rows = _build_face_rows(card_id, card)
    conn.executemany(_FACE_INSERT_SQL, [[face[c] for c in _FACE_COLUMNS] for face in face_rows])
    conn.commit()
    return card_id


def insert_named_card(conn: sqlite3.Connection, name: str, **overrides: Any) -> int:
    """Look up a fixture card by its exact Scryfall `name` and insert it."""
    return insert_card(conn, load_fixture_cards()[name], **overrides)
