"""Shared fixtures for oracle_searcher tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from oracle_searcher.importer import build

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CARDS_FIXTURE = FIXTURES_DIR / "oracle_cards_sample.jsonl"
TAGS_FIXTURE = FIXTURES_DIR / "oracle_tags_sample.jsonl"


@pytest.fixture(scope="session")
def built_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A SQLite card file built once from the fixture bulk files, shared read-only across tests."""
    out_path = tmp_path_factory.mktemp("db") / "cards.sqlite"
    build(CARDS_FIXTURE, TAGS_FIXTURE, out_path)
    return out_path


@pytest.fixture
def conn(built_db_path: Path) -> sqlite3.Connection:
    """A connection to the shared fixture-built database, with row access by column name."""
    connection = sqlite3.connect(built_db_path)
    connection.row_factory = sqlite3.Row
    return connection


def card_row(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    """Fetch a single `cards` row by exact card_name, failing loudly if it's missing or ambiguous."""
    rows = conn.execute("SELECT * FROM cards WHERE card_name = ?", (name,)).fetchall()
    assert len(rows) == 1, f"expected exactly one row named {name!r}, found {len(rows)}"
    return rows[0]


def face_rows(conn: sqlite3.Connection, card_id: int) -> list[sqlite3.Row]:
    """Fetch a card's card_faces rows, ordered by face_index."""
    return conn.execute("SELECT * FROM card_faces WHERE card_id = ? ORDER BY face_index", (card_id,)).fetchall()


def load_fixture_cards() -> list[dict[str, Any]]:
    """Every raw card dict in the fixture oracle_cards file, for tests that need the Scryfall source."""
    with CARDS_FIXTURE.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
