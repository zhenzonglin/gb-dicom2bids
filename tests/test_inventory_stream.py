"""Legacy private inventories remain readable without a full-file JSON allocation."""

import json
import os
from pathlib import Path

import pytest

from gb_dicom2bids import json_stream
from gb_dicom2bids.manifest import load_private_records


@pytest.mark.parametrize("chunk", [1, 7, 127, 1024 * 1024])
@pytest.mark.parametrize("indent", [None, 2])
def test_stream_preserves_all_fields_unicode_and_order(
    tmp_path, record_factory, monkeypatch, chunk, indent
):
    records = [
        record_factory(
            series_uid_hash=f"synthetic{i}",
            series_description='合成 T1 \\ " [test], } \n FLAIR',
            source_relpaths=[f"synthetic/{i}/影像.nii.gz"],
        )
        for i in range(4)
    ]
    path = tmp_path / "series_sources.json"
    path.write_text(
        json.dumps([r.private_dict() for r in records], ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
    before = path.read_bytes()
    monkeypatch.setattr(json_stream, "CHUNK_BYTES", chunk)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("must not read whole file"))
    updates = []
    loaded = load_private_records(tmp_path, progress=lambda *v: updates.append(v))
    assert loaded == records
    assert path.read_bytes() == before
    assert updates[-1] == (len(before), len(before), len(records))
    assert [u[0] for u in updates] == sorted(u[0] for u in updates)
    assert [u[2] for u in updates] == sorted(u[2] for u in updates)


@pytest.mark.parametrize("value", ["[]", " \n[ \t] \r\n"])
def test_empty_inventory(tmp_path, value):
    (tmp_path / "series_sources.json").write_text(value)
    assert load_private_records(tmp_path) == []


@pytest.mark.parametrize(
    "value",
    [
        "",
        "{}",
        "[",
        "[{}",
        "[{},]",
        "[{} {}]",
        "[]x",
        "[] []",
        '[{"a": ]}',
        '[{"a": "unfinished}]',
        "[1]",
        "[null]",
        "[[{}]]",
    ],
)
def test_invalid_or_truncated_json_is_not_silently_accepted(tmp_path, monkeypatch, value):
    path = tmp_path / "series_sources.json"
    path.write_text(value)
    monkeypatch.setattr(json_stream, "CHUNK_BYTES", 3)
    with pytest.raises(ValueError):
        list(json_stream.iter_objects(path))


def test_inventory_change_while_loading_is_rejected(tmp_path):
    path = tmp_path / "series_sources.json"
    path.write_text('[{"a": 1}]')
    changed = False

    def replace_inventory(*_):
        nonlocal changed
        if not changed:
            stamp = path.stat()
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
            changed = True

    with pytest.raises(ValueError, match="changed during loading"):
        list(json_stream.iter_objects(path, replace_inventory))


def test_missing_inventory_keeps_actionable_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="run inventory first"):
        load_private_records(tmp_path)
