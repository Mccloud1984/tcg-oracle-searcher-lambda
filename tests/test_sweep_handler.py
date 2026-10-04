"""The sweep Lambda handler: Scryfall pages come from saved fixtures, S3 is moto, nothing is live."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from oracle_searcher.handlers import cards_store, sweep_handler
from oracle_searcher.is_tag_sweep import SweepError

FIXTURES = Path(__file__).parent / "fixtures" / "scryfall" / "is_tags"
BUCKET = "cards-bucket"
TAGS = ["fetchland", "meldresult", "shockland"]  # sorted, the order the handler sweeps in


def _ids(name: str) -> list[str]:
    return [card["oracle_id"] for card in json.loads((FIXTURES / f"{name}.json").read_text())["data"]]


class Context:
    """Lambda context double: `remaining_ms` is a list of answers, the last one repeats."""

    def __init__(self, *remaining_ms: int) -> None:
        """Queue the remaining-time answers."""
        self.answers = list(remaining_ms)

    def get_remaining_time_in_millis(self) -> int:
        """Pop the next answer (keep the last)."""
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class FakeScryfall:
    """Answers each tag's search with its saved page, or raises for a tag in `fail_on`."""

    def __init__(self, fail_on: tuple[str, ...] = ()) -> None:
        """Remember which tags fail with a 429."""
        self.fail_on = fail_on
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        self.urls.append(url)
        tag = url.split("is%3A")[1].split("&", maxsplit=1)[0]
        if tag in self.fail_on:
            reason = "rate limited (429)"
            raise SweepError(reason, completed={})
        return json.loads((FIXTURES / f"{tag}.json").read_text())


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch):
    """An empty moto bucket, with the handler limited to three small tags and no pacing."""
    monkeypatch.setenv("CARDS_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setattr(sweep_handler, "SWEEP_TAGS", frozenset(TAGS))
    monkeypatch.setattr(sweep_handler, "MIN_INTERVAL", 0.0)
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _written(s3) -> dict[str, Any]:
    return json.loads(s3.get_object(Bucket=BUCKET, Key="sweeps/is_tags.json")["Body"].read())


def _put_previous(s3, tags: dict[str, list[str]]) -> None:
    s3.put_object(Bucket=BUCKET, Key="sweeps/is_tags.json", Body=json.dumps({"swept_at": "old", "tags": tags}))


def test_sweeps_every_tag_and_writes_the_file(s3, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_handler, "fetch_page", FakeScryfall())
    result = sweep_handler.handler({}, Context(900_000))
    written = _written(s3)
    assert written["tags"] == {tag: _ids(tag) for tag in TAGS}
    assert written["swept_at"].endswith("Z")
    assert result["swept"] == TAGS
    assert result["kept"] == []


def test_low_remaining_time_stops_new_tags_and_keeps_the_previous_entries(s3, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under 60 s left: no new tag starts. The tags not done keep last week's list, not an empty one."""
    _put_previous(s3, {"shockland": ["old-id"], "meldresult": ["old-id-2"]})
    fake = FakeScryfall()
    monkeypatch.setattr(sweep_handler, "fetch_page", fake)
    result = sweep_handler.handler({}, Context(900_000, 59_999))  # first tag starts, second does not
    written = _written(s3)
    assert written["tags"]["fetchland"] == _ids("fetchland")
    assert written["tags"]["meldresult"] == ["old-id-2"]
    assert written["tags"]["shockland"] == ["old-id"]
    assert result["swept"] == ["fetchland"]
    assert result["kept"] == ["meldresult", "shockland"]
    assert len(fake.urls) == 1


def test_a_429_keeps_finished_tags_and_previous_entries(s3, monkeypatch: pytest.MonkeyPatch) -> None:
    _put_previous(s3, {"shockland": ["old-id"]})
    monkeypatch.setattr(sweep_handler, "fetch_page", FakeScryfall(fail_on=("meldresult",)))
    result = sweep_handler.handler({}, Context(900_000))
    written = _written(s3)
    assert written["tags"] == {"fetchland": _ids("fetchland"), "shockland": ["old-id"]}
    assert "429" in result["stopped"]


def test_nothing_finished_raises_and_leaves_the_old_file_alone(s3, monkeypatch: pytest.MonkeyPatch) -> None:
    _put_previous(s3, {"shockland": ["old-id"]})
    monkeypatch.setattr(sweep_handler, "fetch_page", FakeScryfall(fail_on=("fetchland",)))
    with pytest.raises(SweepError, match="429"):
        sweep_handler.handler({}, Context(900_000))
    assert _written(s3)["swept_at"] == "old"


def test_file_lives_where_the_import_handler_looks(s3, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_handler, "fetch_page", FakeScryfall())
    sweep_handler.handler({}, Context(900_000))
    assert cards_store.read_sweep(s3, BUCKET)["tags"]["fetchland"] == _ids("fetchland")
