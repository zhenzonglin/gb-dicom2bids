from __future__ import annotations

import csv

from gb_dicom2bids.manifest import (
    load_private_records,
    load_selection,
    write_inventory,
    write_selection,
)
from gb_dicom2bids.models import SelectionRow


def test_inventory_and_selection_roundtrip(record_factory, tmp_path) -> None:
    record = record_factory(source_relpaths=["site/subject/one.dcm"])
    write_inventory(tmp_path, [record], [])
    loaded = load_private_records(tmp_path)
    assert loaded[0].source_relpaths == ["site/subject/one.dcm"]

    row = SelectionRow(
        center="site01",
        subject_id="001",
        study_uid_hash="studyhash",
        series_uid_hash="serieshash",
        candidate_type="t1",
        decision_status="selected",
        score=123.0,
        reason="best_original_axial_t1",
        source_plane="axial",
        source_kind="original",
        protocol_id="t1-test",
        output_basename="sub-001_T1w",
    )
    write_selection(tmp_path, [row], [record])
    selected = load_selection(tmp_path / "selection_manifest.tsv")
    assert selected[0].score == 123.0
    assert selected[0].output_basename == "sub-001_T1w"


def test_resolved_manual_review_remains_auditable(record_factory, tmp_path) -> None:
    record = record_factory()
    row = SelectionRow(
        center=record.center,
        subject_id=record.subject_id,
        study_uid_hash=record.study_uid_hash,
        series_uid_hash=record.series_uid_hash,
        candidate_type="t1",
        decision_status="selected",
        score=100.0,
        reason="manual_accept_original",
        source_plane="axial",
        source_kind="original",
        protocol_id=record.protocol_id,
        output_basename="sub-001_T1w",
        reviewer="zz",
        manual_decision="accept_original",
    )
    write_selection(tmp_path, [row], [record])
    with (tmp_path / "manual_review.tsv").open() as handle:
        saved = next(csv.DictReader(handle, delimiter="\t"))
    assert saved["decision"] == "accept_original"
    assert saved["reviewer"] == "zz"
