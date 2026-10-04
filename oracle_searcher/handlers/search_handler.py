"""Search Lambda: answers `{"q", "order", "dir", "page"}` (and the `op` lookups, see `lookups`) from the published card file.

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

from oracle_searcher import lookups
from oracle_searcher.handlers import cards_store
from oracle_searcher.schema import PRINTINGS_ALIAS
from oracle_searcher.search import Unsupported, register_regexp, search

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_printings_key: str | None = None  # from the `latest` read at cold start; None for a build with no printings file
_printings_attached = False


class PrintingsUnavailable(Exception):  # noqa: N818 - reads better as a noun at the raise site
    """The served build has no printings file (it predates it), so ops that need printings can't be answered."""


def _tmp_dir() -> Path:
    return Path(os.environ.get("CARDS_TMP_DIR", "/tmp"))  # noqa: S108 - Lambda's only writable path


def _open_published_file() -> sqlite3.Connection:
    bucket = os.environ["CARDS_BUCKET"]
    s3 = boto3.client("s3")
    latest = cards_store.read_latest(s3, bucket)
    key = latest["key"]
    global _printings_key  # noqa: PLW0603 - set with the connection, so both belong to the same build
    _printings_key = latest.get("printings_key")
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


def printings_connection() -> sqlite3.Connection:
    """The same connection with the printings file ATTACHed (as `PRINTINGS_ALIAS`), fetched on first need only.

    Searches and card-only lookups never call this, so they keep the cards file's cold start; the printings file
    (about as big again) is downloaded by the first `prints`, id/set lookup or released-printing swap, then kept
    for the life of the container (same stale-build limitation as `connection`). Raises `PrintingsUnavailable`
    when the build `latest` pointed at has no printings file.
    """
    global _printings_attached  # noqa: PLW0603
    conn = connection()
    if not _printings_attached:
        if _printings_key is None:
            raise PrintingsUnavailable
        path = _tmp_dir() / "printings.sqlite"
        cards_store.download_and_gunzip(boto3.client("s3"), os.environ["CARDS_BUCKET"], _printings_key, path)
        conn.execute(f"ATTACH DATABASE 'file:{path}?mode=ro' AS {PRINTINGS_ALIAS}")
        _printings_attached = True
        logger.info("attached %s", _printings_key)
    return conn


def reset() -> None:
    """Forget the open connection (tests; a real cold start does this by being a new process)."""
    global _conn, _printings_key, _printings_attached  # noqa: PLW0603
    if _conn is not None:
        _conn.close()
    _conn = None
    _printings_key = None
    _printings_attached = False


def _search(event: dict[str, Any]) -> dict[str, Any]:
    result = search(
        connection(),
        event["q"],
        order=event.get("order", "edhrec"),
        dir=event.get("dir", "auto"),
        page=int(event.get("page", 1)),
    )
    return {"data": result.data, "has_more": result.has_more, "total_cards": result.total_cards}


def _answer(event: dict[str, Any]) -> dict[str, Any]:
    """The answer for the event's `op` (default `search`); lookups reach the printings file through `printings_connection`."""
    op = event.get("op", "search")
    if op == "search":
        return _search(event)
    if op in lookups.OPS:
        return lookups.OPS[op](connection(), event, printings_connection)
    return {"unsupported": f"op {op!r} is not supported"}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Lambda entry point. Answers the op's result (search: `{data, has_more, total_cards}`), `{unsupported: reason}` or `{error: message}`."""
    try:
        return _answer(event)
    except (Unsupported, PrintingsUnavailable) as exc:
        return {"unsupported": str(exc) or type(exc).__name__}
    except Exception as exc:  # the contract: never raise to the caller
        logger.exception("%s failed", event.get("op", "search") if isinstance(event, dict) else "event")
        return {"error": f"{type(exc).__name__}: {exc}"}
