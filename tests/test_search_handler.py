"""The search Lambda handler: cold-start download from S3 (moto), reuse, and its three answer shapes."""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

from oracle_searcher.handlers import search_handler

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
