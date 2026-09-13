"""Source-bound technical failures, never protocol or human quality labels."""

from __future__ import annotations

import gzip
import zlib
from pathlib import Path

from nibabel.filebasedimages import ImageFileError
from nibabel.spatialimages import HeaderDataError

from .qc_state import record_digest

FAILURE_KIND = "unreadable_image"


def source_stamp(path: Path) -> list | None:
    try:
        stat = path.stat()
        return [str(path), stat.st_size, stat.st_mtime_ns]
    except OSError:
        return None


def image_read_failure(exc: Exception) -> bool:
    """Only known image-format failures qualify; I/O, memory and service errors do not."""
    if isinstance(exc, (ImageFileError, HeaderDataError, EOFError, gzip.BadGzipFile, zlib.error)):
        return True
    if isinstance(exc, ValueError):
        return str(exc) in {"unsupported image dimensions", "invalid NIfTI affine"}
    # Nibabel's short uncompressed payload error has no OS errno.
    return (
        isinstance(exc, OSError)
        and exc.errno is None
        and str(exc).startswith("Expected ")
        and " bytes, got " in str(exc)
    )


def current_failure(value: dict, record, path: Path) -> bool:
    return bool(
        value.get("state") == "failed"
        and value.get("failure_kind") == FAILURE_KIND
        and value.get("record_digest") == record_digest(record)
        and value.get("source_stamp")
        and value["source_stamp"] == source_stamp(path)
    )
