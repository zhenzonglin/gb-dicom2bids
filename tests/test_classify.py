from __future__ import annotations

import pytest

from gb_dicom2bids.classify import classify_record


def test_t1_name_classification(record_factory) -> None:
    record = record_factory(candidate_type="other", protocol_id="", source_kind="unknown")
    result = classify_record(record)
    assert result.candidate_type == "t1"
    assert result.source_kind == "original"
    assert result.protocol_id.startswith("t1-")


def test_flair_name_and_timing_classification(record_factory) -> None:
    named = record_factory(
        series_description="3D DARK FLUID",
        protocol_name="",
        candidate_type="other",
        protocol_id="",
        source_kind="unknown",
    )
    assert classify_record(named).candidate_type == "flair"

    timing = record_factory(
        series_description="IR TSE",
        protocol_name="",
        repetition_time_ms=5_000,
        echo_time_ms=120,
        inversion_time_ms=1_800,
        candidate_type="other",
        protocol_id="",
        source_kind="unknown",
    )
    result = classify_record(timing)
    assert result.candidate_type == "flair"
    assert result.classification_confidence == "medium"


@pytest.mark.parametrize(
    "description",
    [
        "202106281256__MR__0702__eFLAIR-longTR-CLEAR",
        "3D_FLAIR",
        "T2-FLAIR",
    ],
)
def test_flair_keyword_is_recognized_anywhere(record_factory, description) -> None:
    record = record_factory(
        series_description=description,
        protocol_name="",
        sequence_name="",
        candidate_type="other",
        protocol_id="",
        source_kind="unknown",
    )
    assert classify_record(record).candidate_type == "flair"


@pytest.mark.parametrize(
    "description",
    [
        "202106281256__MR__0602__eT1W-SE",
        "sT1-3D",
        "T1+C",
        "3D-T1",
    ],
)
def test_t1_keyword_is_recognized_anywhere(record_factory, description) -> None:
    record = record_factory(
        series_description=description,
        protocol_name="",
        sequence_name="",
        candidate_type="other",
        protocol_id="",
        source_kind="unknown",
    )
    assert classify_record(record).candidate_type == "t1"


def test_localizer_is_excluded(record_factory) -> None:
    record = record_factory(
        series_description="T1 localizer",
        candidate_type="t1",
        protocol_id="",
        source_kind="unknown",
    )
    assert classify_record(record).candidate_type == "other"


def test_mpr_source_kind(record_factory) -> None:
    record = record_factory(
        series_description="Axial MPR T1",
        image_type=["DERIVED", "SECONDARY"],
        candidate_type="other",
        protocol_id="",
        source_kind="unknown",
    )
    assert classify_record(record).source_kind == "derived_mpr"


@pytest.mark.parametrize(
    ("manufacturer", "description", "protocol", "tr", "te", "ti"),
    [
        ("Siemens", "t2_space_dark-fluid", "SPACE", 5000, 390, 1800),
        ("GE MEDICAL SYSTEMS", "T2 FLAIR CUBE", "CUBE", 6000, 120, 1800),
        ("Philips Medical Systems", "3D FLAIR", "VISTA", 4800, 280, 1650),
    ],
)
def test_multivendor_flair_protocols(
    record_factory, manufacturer, description, protocol, tr, te, ti
) -> None:
    record = record_factory(
        manufacturer=manufacturer,
        series_description=description,
        protocol_name=protocol,
        repetition_time_ms=tr,
        echo_time_ms=te,
        inversion_time_ms=ti,
        candidate_type="other",
        protocol_id="",
        source_kind="unknown",
    )
    result = classify_record(record)
    assert result.candidate_type == "flair"
    assert result.protocol_id.startswith("flair-")
