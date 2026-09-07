"""Read an existing JSON object array without retaining a second full inventory in memory."""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

Progress = Callable[[int, int, int], None]
CHUNK_BYTES = 1024 * 1024
WHITESPACE = re.compile(r"[ \t\r\n]*")


def iter_objects(path: Path, progress: Progress | None = None) -> Iterator[dict[str, Any]]:
    """Accept the legacy JSON array, including pretty printing and UTF-8 names.

    There is no record, byte, time or resource limit. Chunking avoids keeping the full
    text and the full list of dictionaries alongside the final SeriesRecord objects.
    Malformed/truncated or concurrently replaced inventories fail instead of yielding
    a silently incomplete list to the caller.
    """
    stamp = path.stat()
    with path.open("rb") as handle:
        buffer, pos, count, eof = "", 0, 0, False
        decoder = json.JSONDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")()

        def refill() -> bool:
            nonlocal buffer, pos, eof
            chunk = handle.read(CHUNK_BYTES)
            buffer = buffer[pos:] + utf8.decode(chunk, final=not chunk)
            pos = 0
            eof = not chunk
            if progress:
                progress(handle.tell(), stamp.st_size, count)
            return bool(chunk)

        def token() -> str:
            nonlocal pos
            while True:
                pos = WHITESPACE.match(buffer, pos).end()
                if pos < len(buffer):
                    return buffer[pos]
                if eof or not refill():
                    return ""

        if token() != "[":
            raise ValueError("private inventory must be a JSON array")
        pos += 1
        if token() != "]":
            while True:
                if token() != "{":
                    raise ValueError(f"expected inventory object after {count} records")
                while True:
                    try:
                        item, end = decoder.raw_decode(buffer, pos)
                        break
                    except json.JSONDecodeError as exc:
                        if eof or not refill():
                            raise ValueError(
                                f"invalid/truncated private inventory at record {count + 1}"
                            ) from exc
                pos = end
                count += 1
                yield item
                if token() == "]":
                    break
                if token() != ",":
                    raise ValueError(f"expected inventory separator after {count} records")
                pos += 1
        pos += 1
        if token():
            raise ValueError("unexpected content after private inventory array")
    current = path.stat()
    if (stamp.st_dev, stamp.st_ino, stamp.st_size, stamp.st_mtime_ns) != (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ):
        raise ValueError("private inventory changed during loading; restart the viewer")
    if progress:
        progress(stamp.st_size, stamp.st_size, count)
