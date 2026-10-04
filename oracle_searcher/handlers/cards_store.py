"""Where the published card file lives in S3, shared by both handlers.

Layout (one bucket, `CARDS_BUCKET`): `cards/builds/<build>.sqlite.gz` holds a build (a lifecycle rule can expire the
whole `cards/builds/` prefix), next to `cards/builds/<build>.printings.sqlite.gz` (the printings, a second file the
search Lambda only fetches when an op needs them). `cards/latest.json`
(`{"key": "<cards key>", "printings_key": "<printings key>"}`) points at the pair to serve; an older build's
`latest` names only `key`. The import writes `latest` last.
`sweeps/is_tags.json` (`{"swept_at", "tags": {tag: [oracle_id]}}`) is the weekly `is:` tag sweep; the sweep Lambda writes it, the import applies it.
"""

from __future__ import annotations

import gzip
import json
import shutil
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from botocore.client import BaseClient

BUILDS_PREFIX = "cards/builds/"
LATEST_KEY = "cards/latest.json"
SWEEP_KEY = "sweeps/is_tags.json"


def read_latest(s3: BaseClient, bucket: str) -> dict[str, Any]:
    """The parsed `latest` pointer: `key` (the cards file) and, for builds that have one, `printings_key`."""
    return json.loads(s3.get_object(Bucket=bucket, Key=LATEST_KEY)["Body"].read())


def read_latest_key(s3: BaseClient, bucket: str) -> str:
    """The S3 key of the cards file `latest` points at (also what a `latest` from before the printings file names)."""
    return read_latest(s3, bucket)["key"]


def write_latest_key(s3: BaseClient, bucket: str, key: str, printings_key: str | None = None) -> None:
    """Point `latest` at build `key` (and its printings file). The import does this last, after the check passed."""
    pointer = {"key": key} if printings_key is None else {"key": key, "printings_key": printings_key}
    s3.put_object(Bucket=bucket, Key=LATEST_KEY, Body=json.dumps(pointer), ContentType="application/json")


def read_sweep(s3: BaseClient, bucket: str) -> dict[str, Any] | None:
    """The stored `is:` tag sweep, or None when no sweep has been written yet."""
    try:
        body = s3.get_object(Bucket=bucket, Key=SWEEP_KEY)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None
    return json.loads(body)


def write_sweep(s3: BaseClient, bucket: str, payload: dict[str, Any]) -> None:
    """Store the sweep file (`{"swept_at", "tags"}`)."""
    s3.put_object(Bucket=bucket, Key=SWEEP_KEY, Body=json.dumps(payload), ContentType="application/json")


def download_and_gunzip(s3: BaseClient, bucket: str, key: str, dest: Path) -> None:
    """Stream `key` from S3 and gunzip it to `dest` (never holds the file in memory)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    with gzip.GzipFile(fileobj=body) as source, dest.open("wb") as out:
        shutil.copyfileobj(source, out, length=1 << 20)


def gzip_file(source: Path, dest: Path) -> None:
    """Gzip `source` to `dest`."""
    with source.open("rb") as src, gzip.open(dest, "wb", compresslevel=6) as out:
        shutil.copyfileobj(src, out, length=1 << 20)
