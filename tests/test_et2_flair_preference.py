"""eT2 FLAIR preference is a default, never an override of human decisions."""

import copy

import pytest
from test_qc_candidate_limit import make_count_index
from test_qc_exclusions import make_missing
from test_qc_identify import payload_for, publish

from gb_dicom2bids.classify import CLASSIFICATION_VERSION, named_t2_flair_variant
from gb_dicom2bids.qc_identify import require_quality
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, read_decision, record_digest, save_decision


@pytest.mark.parametrize(
    "name,variant",
    [
        ("eT2 FLAIR", "et2"),
        ("ET2__FLAIR", "et2"),
        ("eT2W-FLAIR-HST", "et2"),
        ("eT2FLAIR", "et2"),
        ("OAx_eT2_extra_FLAIR", "et2"),
        ("ＦＬＡＩＲ ｅＴ２", "et2"),
        ("T2 FLAIR", "t2"),
        ("T2__FLAIR", "t2"),
        ("FLAIR-T2W", "t2"),
        ("T2FLAIR", "t2"),
        ("eFLAIR", None),
        ("preT2 FLAIR", None),
        ("T20 FLAIR", None),
        ("T2star FLAIR", None),
        ("T1 eT2 FLAIR", None),
        ("FLAIR--PosDisp-eT2", None),
    ],
)
def test_variant_name_matching(record_factory, name, variant):
    record = record_factory(series_description=name, protocol_name="", sequence_name="")
    assert named_t2_flair_variant(record) == variant


def test_variants_do_not_join_names_or_use_subject_identifiers(record_factory):
    record = record_factory(
        series_description="eT2",
        protocol_name="FLAIR",
        sequence_name="",
        subject_id="eT2FLAIR",
        center="eT2FLAIR",
    )
    assert named_t2_flair_variant(record) is None
    record.series_description = "eT2 FLAIR"
    record.protocol_name = "T2 FLAIR"
    assert named_t2_flair_variant(record) == "et2"


@pytest.mark.parametrize(
    "enhanced,plain",
    [
        ("eT2 FLAIR", "T2 FLAIR"),
        ("eT2W-FLAIR-HST", "T2__FLAIR"),
        ("202001011200__MR__0001__eT2 FLAIR AX", "202001011200__MR__0002__T2 FLAIR AX"),
        ("eT2 FLAIR", "T2 FLAIR AX"),
    ],
)
def test_pair_selects_et2_without_quality_or_source_changes(tmp_path, enhanced, plain):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1", enhanced, plain]})
    before = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    records = {u: record_digest(r) for u, r in index.records.items()}
    state = copy.deepcopy(identify.state)
    choice = index.choices("phantom01")["flair"]
    assert index.records[choice["choice"]].series_description == enhanced
    assert choice["count"] == 2 and choice["top_count"] == 1
    assert choice["selection_reason"] == "flair_et2_over_t2"
    group = next(g for g in identify.catalogue()["groups"] if g["modality"] == "flair")
    assert group["pending_count"] == 0
    assert group["default_selection_reason"] == "flair_et2_over_t2"
    assert identify.summary()["counts"]["flair"]["automatic_unique"] == 1
    service = ReviewService(index.config)
    try:
        assert service.subject("phantom01")["sequence_choices"]["flair"] == choice["choice"]
    finally:
        service.close()
    assert identify.state == state
    assert {u: record_digest(r) for u, r in index.records.items()} == records
    assert all(digest(p) == value for p, value in before.items())
    assert not read_decision(index.root.parent, "phantom01")["candidates"]
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))


@pytest.mark.parametrize(
    "protection",
    [
        "priority",
        "same_priority",
        "image",
        "choice",
        "none",
        "absent",
        "recheck",
        "defer",
        "negative",
        "completed",
    ],
)
def test_new_preference_never_overrides_human_intent(tmp_path, protection):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1", "eT2 FLAIR", "T2 FLAIR"]})
    by_name = {r.series_description: u for u, r in index.records.items()}
    enhanced, plain = by_name["eT2 FLAIR"], by_name["T2 FLAIR"]
    if protection in {"priority", "same_priority", "defer", "negative"}:
        payload = payload_for(identify, "flair")
        if protection in {"priority", "same_priority"}:
            payload["compare_in_quality"] = protection == "same_priority"
            for family, entry in payload["templates"].items():
                entry["priority"] = (
                    100
                    if protection == "same_priority"
                    else (0 if family == identify.families[plain] else 100)
                )
        else:
            payload["templates"] = {}
            payload.update(
                {"deferred_candidates": [enhanced]}
                if protection == "defer"
                else {"negative_templates": [identify.families[enhanced]]}
            )
        publish(identify, payload)
    elif protection in {"image", "choice", "none"}:
        decision = index.decisions["phantom01"]
        if protection == "image":
            decision["candidates"][plain] = {"modality": "flair", "quality": "pass"}
        else:
            decision["groups"]["flair"] = (
                {"choice": plain} if protection == "choice" else {"none": True}
            )
    else:
        state = copy.deepcopy(identify.state)
        field = "manual_completed" if protection == "completed" else protection
        state.setdefault(field, {})["phantom01:flair"] = {
            "stamp": identify.subject_stamp("phantom01")
        }
        identify._save(state, "synthetic_manual_override")
    before = copy.deepcopy(identify.state), copy.deepcopy(index.decisions)
    values = {u: identify.assignment(u) for u in index.subjects["phantom01"]}
    assert not identify.preferred_et2_flair("phantom01", identify.state, values)
    choice = index.choices("phantom01")["flair"]
    assert choice.get("selection_reason") != "flair_et2_over_t2"
    if protection in {"priority", "negative"}:
        assert choice["choice"] == plain
    assert before == (identify.state, index.decisions)


@pytest.mark.parametrize(
    "name,count,expected",
    [
        ("eT2 FLAIR", 2, "image1.nii.gz"),
        ("eT2 FLAIR", 3, "image2.nii.gz"),
        ("eT2 FLAIR AX", 3, "image2.nii.gz"),
        ("eT2 FLAIR AX", 4, None),
        ("eT2 FLAIR SAG", 1, "plain"),
        ("eT2 FLAIR COR", 1, "plain"),
        ("eT2 FLAIR CT", 1, "plain"),
    ],
)
def test_existing_exclusions_limits_and_repeat_order(tmp_path, name, count, expected):
    index, identify = make_count_index(
        tmp_path, {"phantom01": {"T1": 1, name: count, "T2 FLAIR": 1}}
    )
    choice = index.choices("phantom01")["flair"]
    if expected is None:
        assert choice["choice"] is None
        assert identify.summary()["counts"]["flair"]["pending_subjects"] == int(count < 4)
    else:
        record = index.records[choice["choice"]]
        if expected == "plain":
            assert record.series_description == "T2 FLAIR"
        else:
            assert record.source_relpaths[0].endswith(expected)
    assert index.choices("phantom01")["t1"]["choice"]


def test_rejected_plain_field_cannot_be_bypassed_or_cross_subject_joined(tmp_path):
    index, identify = make_count_index(
        tmp_path,
        {
            "phantom01": {"T1": 1, "eT2 FLAIR": 1, "T2 FLAIR": 4},
            "phantom02": {"T1": 1, "eT2 FLAIR": 1, "FLAIR other": 1},
            "phantom03": {"T1": 1, "T2 FLAIR": 1},
        },
    )
    assert "flair" in identify.candidate_limits("phantom01")
    assert index.choices("phantom01")["flair"]["choice"] is None
    assert index.choices("phantom02")["flair"]["choice"] is None
    assert index.choices("phantom03")["flair"].get("selection_reason") != "flair_et2_over_t2"


def test_v7_upgrade_preserves_saved_ranking_and_quality(tmp_path):
    index, identify = make_missing(
        tmp_path, {"phantom01": ["T1", "eT2 FLAIR", "T2 FLAIR", "FLAIR SAG"]}
    )
    state = copy.deepcopy(identify.state)
    state["defaults_version"] = "sequence-defaults-7"
    identify._save(state, "synthetic_old_defaults")
    assert index.choices("phantom01")["flair"]["choice"] is None
    assert identify.summary()["default_excluded_series"] == 1
    plain = next(u for u, r in index.records.items() if r.series_description == "T2 FLAIR")
    payload = payload_for(identify, "flair")
    for family, value in payload["templates"].items():
        value["priority"] = 0 if family == identify.families[plain] else 100
    publish(identify, payload)
    decision = read_decision(index.root.parent, "phantom01")
    decision["candidates"][plain] = {"quality": "pass", "modality": "flair"}
    decision["groups"]["flair"] = {"choice": plain}
    save_decision(index.root.parent, "phantom01", decision)
    rules_before = copy.deepcopy(identify.state["templates"])
    quality_before = read_decision(index.root.parent, "phantom01")
    identify._save(dict(identify.state, phase="quality"), "synthetic_old_quality")
    with pytest.raises(ValueError, match="catalog"):
        require_quality(index.root)
    fresh = ProtocolIndex(index.config)
    fresh.identification.enable()
    assert fresh.identification.state["defaults_version"] == CLASSIFICATION_VERSION
    assert fresh.identification.state["templates"] == rules_before
    assert fresh.choices("phantom01")["flair"]["choice"] == plain
    assert fresh.identification.summary()["counts"]["flair"]["manual_completed"] == 1
    assert read_decision(index.root.parent, "phantom01") == quality_before
    assert list((index.root / "identification_backups").glob("*.json"))


def test_new_rule_is_enabled_only_after_catalog_and_retained_on_restart(tmp_path):
    index, identify = make_missing(tmp_path, {"phantom01": ["T1", "eT2 FLAIR", "T2 FLAIR"]})
    identify._save(dict(identify.state, defaults_version="sequence-defaults-7"), "synthetic_old")
    assert index.choices("phantom01")["flair"]["choice"] is None
    identify.enable()
    choice = index.choices("phantom01")["flair"]
    assert choice["selection_reason"] == "flair_et2_over_t2"
    assert ProtocolIndex(index.config).choices("phantom01")["flair"] == choice
