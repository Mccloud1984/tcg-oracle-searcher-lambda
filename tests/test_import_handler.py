"""The import Lambda handler: Scryfall downloads are served from the fixture files, S3 is moto, nothing is live."""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

from oracle_searcher.handlers import cards_store, import_handler
from tests.conftest import CARDS_FIXTURE, TAGS_FIXTURE

if TYPE_CHECKING:
    from pathlib import Path

BUCKET = "cards-bucket"
OLD_KEY = "cards/builds/old-build.sqlite.gz"
CATALOG_URL = "https://api.scryfall.com/bulk-data"
CARDS_URL = "https://data.scryfall.io/oracle-cards/oracle-cards-20261003210155.jsonl.gz"
TAGS_URL = "https://data.scryfall.io/oracle-tags/oracle-tags-20261003210032.jsonl.gz"
# Shape checked against the live catalog on 2026-10-03 (entries carry more fields; these are the ones the handler reads).
CATALOG = {
    "data": [
        {"type": "oracle_cards", "updated_at": "2026-10-03T21:01:55.394+00:00", "jsonl_download_uri": CARDS_URL},
        {"type": "default_cards", "updated_at": "2026-10-03T21:05:42.559+00:00", "jsonl_download_uri": "https://x/default.gz"},
        {"type": "oracle_tags", "updated_at": "2026-10-03T21:00:32.494+00:00", "jsonl_download_uri": TAGS_URL},
    ]
}
NEW_KEY = "cards/builds/20261003210155.sqlite.gz"


@pytest.fixture
def scryfall(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve the catalog and the two fixture files instead of the network; returns the URLs requested."""
    requested: list[str] = []

    def get_json(url: str) -> dict:
        requested.append(url)
        assert url == CATALOG_URL
        return CATALOG

    def download(url: str, dest: Path) -> None:
        requested.append(url)
        source = {CARDS_URL: CARDS_FIXTURE, TAGS_URL: TAGS_FIXTURE}[url]
        with source.open("rb") as src, gzip.open(dest, "wb") as out:
            shutil.copyfileobj(src, out)

    monkeypatch.setattr(import_handler, "get_json", get_json)
    monkeypatch.setattr(import_handler, "download", download)
    return requested


@pytest.fixture
def s3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A moto bucket whose `latest` already points at an older build."""
    monkeypatch.setenv("CARDS_BUCKET", BUCKET)
    monkeypatch.setenv("CARDS_TMP_DIR", str(tmp_path / "lambda-tmp"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=BUCKET)
        client.put_object(Bucket=BUCKET, Key=OLD_KEY, Body=b"old")
        cards_store.write_latest_key(client, BUCKET, OLD_KEY)
        yield client


def _latest(s3) -> str:
    return cards_store.read_latest_key(s3, BUCKET)


def test_publishes_a_passing_build_and_moves_latest(s3, scryfall, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_handler, "check", lambda _path: [])  # the fixtures are far under a full build's card count
    result = import_handler.handler({}, None)
    assert result["key"] == NEW_KEY
    assert result["card_count"] == 89
    assert _latest(s3) == NEW_KEY
    assert scryfall == [CATALOG_URL, CARDS_URL, TAGS_URL]  # only oracle_cards and oracle_tags are fetched


def test_published_file_is_a_gzipped_working_database(s3, scryfall, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(import_handler, "check", lambda _path: [])
    import_handler.handler({}, None)
    cards_store.download_and_gunzip(s3, BUCKET, NEW_KEY, tmp_path / "out.sqlite")
    (count,) = sqlite3.connect(tmp_path / "out.sqlite").execute("SELECT COUNT(*) FROM cards").fetchone()
    assert count == 89


def test_failing_check_keeps_the_old_latest_and_uploads_nothing(s3, scryfall) -> None:
    """The real check rejects the 89-card fixture build (a full file has over 30,000 cards)."""
    with pytest.raises(import_handler.ImportRejected, match="cards, expected at least"):
        import_handler.handler({}, None)
    assert _latest(s3) == OLD_KEY
    keys = [obj["Key"] for obj in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert sorted(keys) == sorted([OLD_KEY, "cards/latest.json"])


def test_latest_moves_only_after_the_build_is_uploaded(s3, scryfall, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_handler, "check", lambda _path: [])
    seen: list[str] = []
    real_write = cards_store.write_latest_key

    def write_latest(client, bucket, key) -> None:
        client.head_object(Bucket=bucket, Key=key)  # raises if the build isn't uploaded yet
        seen.append(key)
        real_write(client, bucket, key)

    monkeypatch.setattr(cards_store, "write_latest_key", write_latest)
    import_handler.handler({}, None)
    assert seen == [NEW_KEY]


def test_download_failure_keeps_the_old_latest(s3, scryfall, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(url: str, dest: Path) -> None:
        msg = "scryfall is down"
        raise OSError(msg)

    monkeypatch.setattr(import_handler, "download", broken)
    with pytest.raises(OSError, match="scryfall is down"):
        import_handler.handler({}, None)
    assert _latest(s3) == OLD_KEY


def test_catalog_without_oracle_tags_is_an_error(s3, scryfall, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_handler, "get_json", lambda _url: {"data": CATALOG["data"][:2]})
    with pytest.raises(KeyError, match="oracle_tags"):
        import_handler.handler({}, None)
    assert _latest(s3) == OLD_KEY


FIRST_FIXTURE_ORACLE_ID = "0013f005-2b49-48a4-8a74-9bbdadc88c9f"  # Munitions Enthusiast, first line of the sample


def _published_tags_of(s3, tmp_path: Path, oracle_id: str) -> list[str]:
    cards_store.download_and_gunzip(s3, BUCKET, NEW_KEY, tmp_path / "out.sqlite")
    row = (
        sqlite3.connect(tmp_path / "out.sqlite")
        .execute("SELECT card_is_tags FROM cards WHERE oracle_id = ?", (oracle_id,))
        .fetchone()
    )
    return json.loads(row[0])


def test_applies_the_stored_sweep_before_check(s3, scryfall, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The weekly sweep file's tags end up in the published database, and check sees them."""
    cards_store.write_sweep(s3, BUCKET, {"swept_at": "x", "tags": {"funny": [FIRST_FIXTURE_ORACLE_ID]}})
    seen_by_check: list[list[str]] = []

    def check(path: Path) -> list[str]:
        row = (
            sqlite3.connect(path)
            .execute("SELECT card_is_tags FROM cards WHERE oracle_id = ?", (FIRST_FIXTURE_ORACLE_ID,))
            .fetchone()
        )
        seen_by_check.append(json.loads(row[0]))
        return []

    monkeypatch.setattr(import_handler, "check", check)
    import_handler.handler({}, None)
    assert "funny" in seen_by_check[0]
    assert "funny" in _published_tags_of(s3, tmp_path, FIRST_FIXTURE_ORACLE_ID)


def test_a_missing_sweep_file_is_fine_and_logged(s3, scryfall, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog) -> None:
    monkeypatch.setattr(import_handler, "check", lambda _path: [])
    with caplog.at_level("INFO"):
        result = import_handler.handler({}, None)
    assert result["key"] == NEW_KEY
    assert "funny" not in _published_tags_of(s3, tmp_path, FIRST_FIXTURE_ORACLE_ID)
    assert "no sweep file" in caplog.text
