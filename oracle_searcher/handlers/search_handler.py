"""Search Lambda: answers `{"q", "order", "dir", "page"}` (and the `op` lookups, see `lookups`) from the published card file.

Cold start downloads the build `latest` points at (env `CARDS_BUCKET`) to the temp dir (env `CARDS_TMP_DIR`,
default /tmp), gunzips it and opens it read-only; later invocations reuse that connection. Right after that, a
background thread starts fetching the build's printings file, so the first `prints`/id/set lookup finds it already
local instead of paying the download on top of the cold start. Never raises: the caller (Purroxy) falls back to
Scryfall on `unsupported`, `error` or a timeout.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
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


class _PrintingsPrefetch:
    """The printings file downloading on a background thread; `wait` joins it and says whether the file is there.

    Lambda freezes the container between invocations, so the thread only progresses while an invocation runs
    (the cold start's own, then any later one); a download cut short that way fails or is simply still running,
    and `wait` covers both. It only writes the file: the ATTACH stays on the calling thread, so the sqlite
    connection is still used by one thread at a time.
    """

    def __init__(self, bucket: str, key: str, path: Path) -> None:
        self.key = key
        self.path = path
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(bucket,), name="printings-prefetch", daemon=True)
        self._thread.start()

    def _run(self, bucket: str) -> None:
        try:
            cards_store.download_and_gunzip(boto3.client("s3"), bucket, self.key, self.path)
        except BaseException as exc:  # noqa: BLE001 - reported by `wait`, never raised on a background thread
            self.error = exc

    def wait(self) -> bool:
        self._thread.join()
        return self.error is None


_prefetch: _PrintingsPrefetch | None = None


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
    _start_printings_prefetch(bucket)
    logger.info("loaded %s", key)
    return conn


def _printings_path() -> Path:
    return _tmp_dir() / "printings.sqlite"


def _start_printings_prefetch(bucket: str) -> None:
    global _prefetch  # noqa: PLW0603 - one per container, like the connection
    if _printings_key is not None:
        _prefetch = _PrintingsPrefetch(bucket, _printings_key, _printings_path())


def _fetch_printings_file(printings_key: str) -> None:
    """Make sure the printings file is on disk: the background download's result, else one inline download."""
    if _prefetch is not None and _prefetch.wait():
        return
    if _prefetch is not None:
        logger.warning("background printings download failed (%r); downloading inline", _prefetch.error)
    cards_store.download_and_gunzip(boto3.client("s3"), os.environ["CARDS_BUCKET"], printings_key, _printings_path())


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
    """The same connection with the printings file ATTACHed (as `PRINTINGS_ALIAS`), on first need only.

    Searches and card-only lookups never call this, so they never wait for the printings file (about as big again
    as the cards file); the cold start downloads it in the background and the first `prints`, id/set lookup or
    released-printing swap joins that thread (if still running) and attaches on its own thread. If the background
    download failed, it downloads inline once. Kept for the life of the container (same stale-build limitation as
    `connection`). Raises `PrintingsUnavailable` when the build `latest` pointed at has no printings file.
    """
    global _printings_attached  # noqa: PLW0603
    conn = connection()
    if not _printings_attached:
        if _printings_key is None:
            raise PrintingsUnavailable
        _fetch_printings_file(_printings_key)
        conn.execute(f"ATTACH DATABASE 'file:{_printings_path()}?mode=ro' AS {PRINTINGS_ALIAS}")
        _printings_attached = True
        logger.info("attached %s", _printings_key)
    return conn


def reset() -> None:
    """Forget the open connection (tests; a real cold start does this by being a new process)."""
    global _conn, _printings_key, _printings_attached, _prefetch  # noqa: PLW0603
    if _prefetch is not None:
        _prefetch.wait()  # a leftover thread must not write into the next test's temp dir
    _prefetch = None
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
