"""Every query in Purroxy's real corpus must compile, or raise Unsupported with a reason.

tests/fixtures/purroxy_queries.txt: 39 real queries Purroxy sends (docs/PLAN-2026-10-03.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.parsing import parse_scryfall_query
from oracle_searcher.sqlite_compiler import Unsupported, compile_query
from tests.helpers import make_db

CORPUS_PATH = Path(__file__).parent / "fixtures" / "purroxy_queries.txt"
CORPUS_QUERIES = [line for line in CORPUS_PATH.read_text().splitlines() if line.strip()]


def test_corpus_file_has_39_queries() -> None:
    assert len(CORPUS_QUERIES) == 39


@pytest.mark.parametrize("query_text", CORPUS_QUERIES)
def test_every_corpus_query_compiles_and_runs(query_text: str) -> None:
    """Every real Purroxy query compiles to SQL that SQLite accepts against an empty db."""
    query = parse_scryfall_query(query_text)
    try:
        sql, params = compile_query(query)
    except Unsupported as exc:  # pragma: no cover - failure path, parametrize id shows the query
        pytest.fail(f"{query_text!r} raised Unsupported: {exc}")
    conn = make_db()
    try:
        conn.execute(f"SELECT card.id FROM cards AS card WHERE {sql}", params)
    finally:
        conn.close()
