"""Search Lambda: answers `{"q", "order", "dir", "page"}` from the published card file.

Cold start downloads the build `latest` points at (env `CARDS_BUCKET`) to the temp dir (env `CARDS_TMP_DIR`,
default /tmp), gunzips it and opens it read-only; later invocations reuse that connection. Never raises: the
caller (Purroxy) falls back to Scryfall on `unsupported`, `error` or a timeout.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import boto3

from oracle_searcher.handlers import cards_store
from oracle_searcher.search import Unsupported, register_regexp, search

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None


def _tmp_dir() -> Path:
    return Path(os.environ.get("CARDS_TMP_DIR", "/tmp"))  # noqa: S108 - Lambda's only writable path


def _open_published_file() -> sqlite3.Connection:
    bucket = os.environ["CARDS_BUCKET"]
    s3 = boto3.client("s3")
    key = cards_store.read_latest_key(s3, bucket)
    path = _tmp_dir() / "cards.sqlite"
    cards_store.download_and_gunzip(s3, bucket, key, path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    register_regexp(conn)
    logger.info("loaded %s", key)
    return conn


def connection() -> sqlite3.Connection:
    """The read-only connection, opened on first use and kept for the life of the Lambda container.

    Known limitation: a warm container never rechecks `latest`, so after the nightly import it keeps answering
    from the previous build until AWS recycles it (minutes when idle, a few hours under steady traffic). Accepted
    for now (an hour or so of stale cards is fine). If it ever matters: recheck `latest` every few minutes here and
    reopen when the key changes, or have the import Lambda touch this function's config to force fresh containers.
    """
    global _conn  # noqa: PLW0603 - Lambda container reuse is the point
    if _conn is None:
        _conn = _open_published_file()
    return _conn


def reset() -> None:
    """Forget the open connection (tests; a real cold start does this by being a new process)."""
    global _conn  # noqa: PLW0603
    if _conn is not None:
        _conn.close()
    _conn = None


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Lambda entry point. Answers `{data, has_more, total_cards}`, `{unsupported: reason}` or `{error: message}`."""
    try:
        result = search(
            connection(),
            event["q"],
            order=event.get("order", "edhrec"),
            dir=event.get("dir", "auto"),
            page=int(event.get("page", 1)),
        )
        return {"data": result.data, "has_more": result.has_more, "total_cards": result.total_cards}
    except Unsupported as exc:
        return {"unsupported": str(exc)}
    except Exception as exc:  # the contract: never raise to the caller
        logger.exception("search failed")
        return {"error": f"{type(exc).__name__}: {exc}"}
