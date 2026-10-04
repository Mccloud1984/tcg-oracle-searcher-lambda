"""Import Lambda (nightly): Scryfall's bulk files -> SQLite card file -> S3, then the `latest` pointer.

Downloads `oracle_cards`, `oracle_tags` and `default_cards` (every printing; JSONL, from Scryfall's bulk catalog; data.scryfall.io has no rate
limit), builds and checks the database in the temp dir (env `CARDS_TMP_DIR`, default /tmp), gzips it, uploads it
under a new key and writes `latest` last. A build that fails its check is never uploaded: the old `latest` stays
and the invocation fails loudly (`ImportRejected`), so the Lambda's error metric shows it.

Between build and check it applies the latest `sweeps/is_tags.json` (written weekly by the sweep Lambda,
`sweep_handler`) with `apply_sweep`; with no sweep file yet it carries on without. The import itself makes no
Scryfall search calls: the sweep is the only part that does.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

from oracle_searcher.handlers import cards_store
from oracle_searcher.importer import build, check, check_printings
from oracle_searcher.is_tag_sweep import apply_sweep

if TYPE_CHECKING:
    from botocore.client import BaseClient

logger = logging.getLogger(__name__)

BULK_CATALOG_URL = "https://api.scryfall.com/bulk-data"
USER_AGENT = "tcg-oracle-searcher/0.1"
_REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


class ImportRejected(Exception):  # noqa: N818 - reads better as a noun at the raise site
    """The built database failed `check`; nothing was published."""


def get_json(url: str) -> dict[str, Any]:
    """GET `url` and parse the JSON answer."""
    request = urllib.request.Request(url, headers=_REQUEST_HEADERS)  # noqa: S310 - fixed https URL
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` (the bulk files are gzipped JSONL and stay gzipped; the importer reads them as is)."""
    request = urllib.request.Request(url, headers=_REQUEST_HEADERS)  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out, length=1 << 20)


def _bulk_entry(catalog: dict[str, Any], bulk_type: str) -> dict[str, Any]:
    for entry in catalog["data"]:
        if entry["type"] == bulk_type:
            return entry
    raise KeyError(bulk_type)


def _build_keys(cards_entry: dict[str, Any]) -> tuple[str, str]:
    """`cards/builds/<digits of the oracle_cards updated_at>[.printings].sqlite.gz`: one Scryfall export, one key pair."""
    stamp = re.sub(r"\D", "", cards_entry["updated_at"])[:14]
    return f"{cards_store.BUILDS_PREFIX}{stamp}.sqlite.gz", f"{cards_store.BUILDS_PREFIX}{stamp}.printings.sqlite.gz"


def _apply_stored_sweep(s3: BaseClient, bucket: str, database: Path) -> None:
    """Fold the latest `is:` tag sweep into `database`; no sweep file yet is fine."""
    sweep_file = cards_store.read_sweep(s3, bucket)
    if sweep_file is None:
        logger.info("no sweep file at %s yet; building without swept is: tags", cards_store.SWEEP_KEY)
        return
    sweep_path = database.with_name("is_tags.json")
    sweep_path.write_text(json.dumps(sweep_file))
    with contextlib.closing(sqlite3.connect(database)) as conn:
        counts = apply_sweep(conn, sweep_path)
    logger.info("applied the sweep from %s: %s", sweep_file.get("swept_at"), counts)


def _build_checked_database(work: Path, s3: BaseClient, bucket: str) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = get_json(BULK_CATALOG_URL)
    cards_entry = _bulk_entry(catalog, "oracle_cards")
    tags_entry = _bulk_entry(catalog, "oracle_tags")
    printings_entry = _bulk_entry(catalog, "default_cards")
    download(cards_entry["jsonl_download_uri"], work / "oracle_cards.jsonl.gz")
    download(tags_entry["jsonl_download_uri"], work / "oracle_tags.jsonl.gz")
    download(printings_entry["jsonl_download_uri"], work / "default_cards.jsonl.gz")
    stats = build(
        work / "oracle_cards.jsonl.gz",
        work / "oracle_tags.jsonl.gz",
        work / "cards.sqlite",
        work / "default_cards.jsonl.gz",
        work / "printings.sqlite",
    )
    _apply_stored_sweep(s3, bucket, work / "cards.sqlite")
    problems = check(work / "cards.sqlite")
    if not problems:
        problems = check_printings(work / "cards.sqlite", work / "printings.sqlite")
    if problems:
        raise ImportRejected("; ".join(problems))
    return cards_entry, stats


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Lambda entry point. Returns `{key, card_count, ...build stats}` once `latest` points at the new build."""
    bucket = os.environ["CARDS_BUCKET"]
    tmp_root = Path(os.environ.get("CARDS_TMP_DIR", "/tmp"))  # noqa: S108
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root) as work_name:
        work = Path(work_name)
        s3 = boto3.client("s3")
        cards_entry, stats = _build_checked_database(work, s3, bucket)
        key, printings_key = _build_keys(cards_entry)
        for name, upload_key in (("cards", key), ("printings", printings_key)):
            cards_store.gzip_file(work / f"{name}.sqlite", work / f"{name}.sqlite.gz")
            s3.upload_file(str(work / f"{name}.sqlite.gz"), bucket, upload_key)
        cards_store.write_latest_key(s3, bucket, key, printings_key)  # last: the old pair serves until the new one is whole
    logger.info("published %s and %s: %s", key, printings_key, stats)
    return {"key": key, "printings_key": printings_key, **stats}
