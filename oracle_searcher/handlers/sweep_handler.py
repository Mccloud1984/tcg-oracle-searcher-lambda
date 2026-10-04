"""Sweep Lambda (weekly): ask Scryfall which cards carry each `SWEEP_TAGS` tag, store `sweeps/is_tags.json`.

About 213 requests at 1 a second (about 10 minutes), so the Lambda runs with a 900 s timeout. It stops starting
new tags once under `STOP_BELOW_MS` of run time is left, and a 429 stops it too. Tags it did not finish keep
the entries of the file already stored, so a partial run never empties a tag. The import Lambda applies the
file (`import_handler`); the search Lambda never reads it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import boto3

from oracle_searcher.handlers import cards_store
from oracle_searcher.is_tag_sweep import SWEEP_TAGS, SweepError, http_fetch_page, sweep, sweep_payload

logger = logging.getLogger(__name__)

STOP_BELOW_MS = 60_000  # a tag's pages take up to ~20 s; leave room to write the file
MIN_INTERVAL = 1.0  # seconds between Scryfall requests (Scryfall allows 2 a second)
fetch_page = http_fetch_page  # replaced in tests


class _Context(Protocol):
    def get_remaining_time_in_millis(self) -> int: ...


def handler(event: dict[str, Any], context: _Context) -> dict[str, Any]:  # noqa: ARG001
    """Lambda entry point. Returns `{swept, kept, stopped}`; raises only when not one tag finished."""
    bucket = os.environ["CARDS_BUCKET"]
    s3 = boto3.client("s3")
    previous = (cards_store.read_sweep(s3, bucket) or {}).get("tags", {})
    tags = sorted(SWEEP_TAGS)
    stopped = None
    try:
        done = sweep(
            tags,
            fetch_page=fetch_page,
            min_interval=MIN_INTERVAL,
            should_continue=lambda: context.get_remaining_time_in_millis() >= STOP_BELOW_MS,
        )
    except SweepError as exc:
        if not exc.completed:
            raise
        done, stopped = exc.completed, exc.reason
    if len(done) < len(tags) and stopped is None:
        stopped = "out of time"
    if done:
        cards_store.write_sweep(s3, bucket, sweep_payload({**previous, **done}))
    kept = [tag for tag in tags if tag not in done]
    logger.info("swept %s; kept the previous entries of %s; stopped: %s", sorted(done), kept, stopped)
    return {"swept": list(done), "kept": kept, "stopped": stopped}
