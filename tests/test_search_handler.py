"""The search Lambda handler: cold-start download from S3 (moto), reuse, and its three answer shapes."""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

from oracle_searcher.handlers import search_handler
from oracle_searcher.importer import build
from tests.conftest import CARDS_FIXTURE, PRINTINGS_FIXTURE, TAGS_FIXTURE

if TYPE_CHECKING:
    from pathlib import Path

BUCKET = "cards-bucket"
BUILD_KEY = "cards/builds/20261003.sqlite.gz"


@pytest.fixture
def s3_cards(built_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A moto bucket holding the fixture database (gzipped) and a `latest` pointer to it; a fresh handler each test."""
    monkeypatch.setenv("CARDS_BUCKET", BUCKET)
    monkeypatch.setenv("CARDS_TMP_DIR", str(tmp_path / "lambda-tmp"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    search_handler.reset()
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key=BUILD_KEY, Body=gzip.compress(built_db_path.read_bytes()))
        s3.put_object(Bucket=BUCKET, Key="cards/latest.json", Body=json.dumps({"key": BUILD_KEY}))
        yield s3
    search_handler.reset()


PRINTINGS_KEY = "cards/builds/20261003.printings.sqlite.gz"


@pytest.fixture
def s3_printings(s3_cards, tmp_path: Path):
    """The same bucket plus a printings file built from the fixtures, `latest` naming both."""
    printings_db = tmp_path / "printings.sqlite"
    build(CARDS_FIXTURE, TAGS_FIXTURE, tmp_path / "unused.sqlite", PRINTINGS_FIXTURE, printings_db)
    s3_cards.put_object(Bucket=BUCKET, Key=PRINTINGS_KEY, Body=gzip.compress(printings_db.read_bytes()))
    s3_cards.put_object(Bucket=BUCKET, Key="cards/latest.json", Body=json.dumps({"key": BUILD_KEY, "printings_key": PRINTINGS_KEY}))
    return s3_cards


def test_search_never_downloads_the_printings_file(s3_printings) -> None:
    """Searches keep the cards file's cold start: the printings file is fetched on first need only."""
    s3_printings.delete_object(Bucket=BUCKET, Key=PRINTINGS_KEY)
    assert search_handler.handler({"q": "name:sol"}, None)["total_cards"] == 1


def test_printings_connection_attaches_the_second_file_once(s3_printings) -> None:
    conn = search_handler.printings_connection()
    (count,) = conn.execute("SELECT COUNT(*) FROM pr.printings").fetchone()
    assert count > 600
    s3_printings.delete_object(Bucket=BUCKET, Key=PRINTINGS_KEY)
    assert search_handler.printings_connection() is conn  # no second download or attach


def test_printings_file_is_read_only(s3_printings) -> None:
    with pytest.raises(Exception, match="readonly"):
        search_handler.printings_connection().execute("DELETE FROM pr.printings")


def test_build_without_a_printings_file_says_so(s3_cards) -> None:
    """A `latest` from before the printings file names only the cards file; cards-only ops keep working."""
    with pytest.raises(search_handler.PrintingsUnavailable):
        search_handler.printings_connection()
    assert search_handler.handler({"q": "name:sol"}, None)["total_cards"] == 1


def test_answers_a_query_from_the_published_file(s3_cards) -> None:
    result = search_handler.handler({"q": "name:sol"}, None)
    assert [card["name"] for card in result["data"]] == ["Sol Ring"]
    assert result["has_more"] is False
    assert result["total_cards"] == 1


def test_paging_and_order_are_passed_through(s3_cards) -> None:
    first = search_handler.handler({"q": "t:creature", "order": "name", "dir": "asc", "page": 1}, None)
    beyond = search_handler.handler({"q": "t:creature", "order": "name", "page": 99}, None)
    names = [card["name"] for card in first["data"]]
    assert names == sorted(names, key=str.lower)
    assert first["total_cards"] > 1
    assert beyond["data"] == []


def test_opens_the_file_read_only(s3_cards) -> None:
    search_handler.handler({"q": "name:sol"}, None)
    conn = search_handler.connection()
    with pytest.raises(Exception, match="readonly"):
        conn.execute("DELETE FROM cards")


def test_downloads_once_and_reuses_the_connection(s3_cards) -> None:
    """Cold start downloads; later invocations must not touch S3 (deleting the file proves it)."""
    search_handler.handler({"q": "name:sol"}, None)
    s3_cards.delete_object(Bucket=BUCKET, Key=BUILD_KEY)
    s3_cards.delete_object(Bucket=BUCKET, Key="cards/latest.json")
    assert search_handler.handler({"q": "name:sol"}, None)["total_cards"] == 1


def test_unsupported_query_answers_unsupported_with_a_reason(s3_cards) -> None:
    result = search_handler.handler({"q": "name:sol", "order": "power"}, None)
    assert set(result) == {"unsupported"}
    assert result["unsupported"]


def test_bad_query_answers_error_and_never_raises(s3_cards) -> None:
    result = search_handler.handler({"q": "(("}, None)
    assert set(result) == {"error"}


def test_missing_q_answers_error(s3_cards) -> None:
    assert "error" in search_handler.handler({}, None)


def test_failed_cold_start_answers_error_then_recovers(s3_cards) -> None:
    """A download failure must not leave the Lambda stuck: the next invocation tries again."""
    s3_cards.delete_object(Bucket=BUCKET, Key="cards/latest.json")
    assert "error" in search_handler.handler({"q": "name:sol"}, None)
    s3_cards.put_object(Bucket=BUCKET, Key="cards/latest.json", Body=json.dumps({"key": BUILD_KEY}))
    assert search_handler.handler({"q": "name:sol"}, None)["total_cards"] == 1


# The lookup ops: named, collection, prints, autocomplete.


def test_no_op_means_search_and_unknown_op_is_unsupported(s3_cards) -> None:
    assert "data" in search_handler.handler({"q": "name:sol"}, None)
    result = search_handler.handler({"op": "teleport"}, None)
    assert set(result) == {"unsupported"}
    assert "teleport" in result["unsupported"]


def test_named_op_answers_from_the_cards_file_alone(s3_cards) -> None:
    """A released card needs no printings file: it was never uploaded here (`latest` names only the cards file)."""
    assert search_handler.handler({"op": "named", "name": "Sol Ring"}, None)["card"]["name"] == "Sol Ring"
    assert search_handler.handler({"op": "named", "name": "No Such Card"}, None) == {"card": None}


def test_autocomplete_op(s3_cards) -> None:
    assert search_handler.handler({"op": "autocomplete", "prefix": "sol r"}, None) == {"names": ["Sol Ring"]}


def test_ops_that_need_printings_attach_the_second_file(s3_printings) -> None:
    result = search_handler.handler({"op": "prints", "q": '!"Sol Ring"'}, None)
    assert len(result["data"]) > 20
    ident = {"set": "trk", "collector_number": "158"}
    assert search_handler.handler({"op": "collection", "identifiers": [ident]}, None)["data"][0]["name"] == "Munitions Enthusiast"


def test_op_needing_printings_on_an_old_build_is_unsupported(s3_cards) -> None:
    """Back-compat: the build `latest` names has no printings file, so Purroxy must fall back to Scryfall."""
    result = search_handler.handler({"op": "prints", "q": '!"Sol Ring"'}, None)
    assert set(result) == {"unsupported"}


def test_op_unsupported_query_and_bad_input_follow_the_contract(s3_printings) -> None:
    assert set(search_handler.handler({"op": "prints", "q": "mana:{1}{G}"}, None)) == {"unsupported"}
    assert set(search_handler.handler({"op": "prints", "q": "(("}, None)) == {"error"}
    assert set(search_handler.handler({"op": "named"}, None)) == {"error"}
    assert set(search_handler.handler({"op": "collection", "identifiers": [{"name": "x"}] * 76}, None)) == {"unsupported"}
