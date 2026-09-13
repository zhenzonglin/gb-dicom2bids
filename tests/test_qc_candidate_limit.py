"""Synthetic per-patient, per-name-family exclusion at four candidate images."""

import copy
from dataclasses import replace

import pytest
from test_nifti_import import make_config, make_image
from test_qc_identify import payload_for, publish

from gb_dicom2bids.nifti_import import inventory_preconverted
from gb_dicom2bids.qc_features import run_features
from gb_dicom2bids.qc_identify import CANDIDATE_LIMIT_VERSION, Identification, require_quality
from gb_dicom2bids.qc_learning import feature_rows
from gb_dicom2bids.qc_protocols import ProtocolIndex
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, read_decision, save_decision
from gb_dicom2bids.runtime import atomic_write_json, read_json


def make_count_index(tmp_path, subjects):
    source = tmp_path / "source"
    for subject, fields in subjects.items():
        for name, count in fields.items():
            for i in range(count):
                make_image(source / f"site/{subject}/{name}/image{i}.nii.gz")
    config = make_config(tmp_path, source)
    inventory_preconverted(config)
    index = ProtocolIndex(config)
    index.identification = Identification(index)
    index.identification.enable()
    return index, index.identification


@pytest.mark.parametrize(
    "name,modality",
    [
        ("T1 tra", "t1"),
        ("eT1W-SE", "t1"),
        ("T1 extra SAG", "t1"),
        ("FLAIR_AX_T1", "t1"),
        ("T1__FLAIR", "t1"),
        ("MPRAGE", "t1"),
        ("T2__FLAIR", "flair"),
        ("eFLAIR-longTR-CLEAR", "flair"),
    ],
)
@pytest.mark.parametrize("count", [3, 4, 5])
def test_fixed_inclusive_threshold_all_target_fields(tmp_path, name, modality, count):
    other = "FLAIR" if modality == "t1" else "T1"
    index, identify = make_count_index(tmp_path, {"phantom01": {name: count, other: 1}})
    stats = identify.summary()["counts"]
    assert stats[modality]["candidate_limit_skipped"] == int(count >= 4)
    opposite = "flair" if modality == "t1" else "t1"
    assert index.choices("phantom01")[opposite]["count"] == 1
    assert stats[opposite]["candidate_limit_skipped"] == 0
    limits = identify.candidate_limits("phantom01")
    if count >= 4:
        assert limits[modality]["fields"][0]["count"] == count
        assert limits[modality]["threshold"] == 4
        assert limits[modality]["operator"] == ">="
        assert index.choices("phantom01")[modality]["count"] == 0
        assert identify.exclusions().round("phantom01", modality) == []
        assert stats[modality]["pending_subjects"] == 0
        assert stats[modality]["repeat_subjects_for_quality"] == 0
    else:
        assert not limits
        assert index.choices("phantom01")[modality]["count"] == 3
        assert stats[modality]["pending_subjects"] == int(name != "T1 tra")
        if name == "T1 tra":
            selected = index.choices("phantom01")[modality]["choice"]
            assert index.records[selected].source_relpaths[0].endswith("image2.nii.gz")


def test_no_cross_field_or_patient_sum_and_no_slice_count(tmp_path, monkeypatch):
    index, identify = make_count_index(
        tmp_path,
        {
            "phantom01": {"T1 tra": 3, "T1 sag": 3, "FLAIR A": 3, "FLAIR B": 3},
            "phantom02": {"T1 tra": 1, "T1 sag": 1, "FLAIR A": 1, "FLAIR B": 1},
        },
    )
    for uid, record in index.records.items():
        index.records[uid] = replace(record, instance_count=200)

    def no_read(*args):
        pytest.fail("counting must not open source images")

    monkeypatch.setattr("gb_dicom2bids.qc_identify.check_image", no_read)
    identify.invalidate()
    assert all(not identify.candidate_limits(s) for s in index.subjects)
    assert identify.summary()["counts"]["t1"]["candidate_limit_skipped"] == 0
    assert index.choices("phantom01")["t1"]["count"] == 6


def test_name_prefix_separator_and_geometry_variants_share_field(tmp_path):
    names = [
        "202001011200__MR__0001__T1 tra",
        "202001021200__MR__0002__t1__tra",
        "202001031200__MR__0003__T1--TRA",
        "202001041200__MR__0004__t1   tra",
    ]
    index, identify = make_count_index(
        tmp_path, {"phantom01": {**dict.fromkeys(names, 1), "FLAIR": 1}}
    )
    targets = [u for u in index.records if identify.assignment(u)["modality"] == "t1"]
    assert len({identify.families[u] for u in targets}) == 1
    for i, u in enumerate(targets):
        index.records[u] = replace(
            index.records[u], instance_count=i + 15, slice_thickness_mm=i + 2
        )
    identify.invalidate()
    assert identify.candidate_limits("phantom01")["t1"]["fields"][0]["count"] == 4


def test_one_excessive_field_skips_whole_modality_before_priority(tmp_path):
    index, identify = make_count_index(
        tmp_path, {"phantom01": {"T1 tra": 1, "T1 sag": 4, "FLAIR": 1}}
    )
    assert not identify.preferred_t1("phantom01", identify.state)
    payload = payload_for(identify, "t1")
    publish(identify, payload)
    assert len(identify.candidate_limits("phantom01")["t1"]["candidate_ids"]) == 5
    assert index.choices("phantom01")["t1"]["choice"] is None
    assert index.choices("phantom01")["flair"]["choice"]
    state = copy.deepcopy(identify.state)
    identify.retain_manual_completions(state, identify.catalogue()["groups"])
    assert "phantom01:t1" not in state["manual_completed"]


def test_non_targets_generic_names_and_corrected_modality(tmp_path):
    index, identify = make_count_index(
        tmp_path,
        {
            "phantom01": {
                "CT T1": 4,
                "XA FLAIR": 4,
                "TOF": 4,
                "DWI": 4,
                "b0": 4,
                "b1000": 4,
                "MRA": 4,
                "unknown": 4,
                "contrast-custom": 4,
            }
        },
    )
    assert not identify.candidate_limits("phantom01")
    uid = next(u for u, r in index.records.items() if r.series_description == "contrast-custom")
    family = identify.families[uid]
    state = copy.deepcopy(identify.state)
    state["templates"][family] = {"modality": "flair", "priority": 0}
    assert identify.candidate_limits("phantom01", state)["flair"]["fields"][0]["count"] == 4
    assert not identify.candidate_limits("phantom01")
    # Generic unknown names are never merged across independent images.
    unknown = [u for u, r in index.records.items() if r.series_description == "unknown"]
    assert len({identify.families[u] for u in unknown}) == 4


def test_saved_negative_and_individual_reclassification_recompute_count(tmp_path):
    index, identify = make_count_index(tmp_path, {"phantom01": {"T1 tra": 4, "FLAIR": 1}})
    uids = identify.candidate_limits("phantom01")["t1"]["candidate_ids"]
    index.decisions["phantom01"]["candidates"][uids[0]] = {
        "quality": "unreviewed",
        "modality": "other",
        "reason": "classification correction",
    }
    identify.invalidate()
    assert not identify.candidate_limits("phantom01")
    assert index.choices("phantom01")["t1"]["count"] == 3
    index.decisions["phantom01"]["candidates"].clear()
    identify.invalidate()
    state = copy.deepcopy(identify.state)
    family = identify.families[uids[0]]
    state["negative_scopes"] = {
        "scope": {
            "subjects": ["phantom01"],
            "modality": "t1",
            "templates": {family: {}},
            "deferred": [],
            "reviewed_subjects": ["phantom01"],
        }
    }
    assert not identify.candidate_limits("phantom01", state)
    assert identify.candidate_limits("phantom01")["t1"]


def test_preserve_saved_human_final_but_not_protocol_only_completion(tmp_path):
    index, identify = make_count_index(tmp_path, {"phantom01": {"T1 tra": 4, "FLAIR": 4}})
    t1 = identify.candidate_limits("phantom01")["t1"]["candidate_ids"][0]
    prior = read_decision(index.root.parent, "phantom01")
    prior["candidates"][t1] = {"quality": "pass", "modality": "t1"}
    prior["groups"]["t1"] = {"choice": t1}
    saved = save_decision(index.root.parent, "phantom01", prior)
    index.decisions["phantom01"] = saved
    identify.state["manual_completed"] = {
        "phantom01:flair": {"stamp": identify.subject_stamp("phantom01"), "source": "manual"},
    }
    identify.invalidate()
    assert set(identify.candidate_limits("phantom01")) == {"flair"}
    assert identify.summary()["counts"]["t1"]["manual_completed"] == 1
    assert identify.summary()["counts"]["flair"]["candidate_limit_skipped"] == 1
    assert read_decision(index.root.parent, "phantom01") == saved


def test_catalog_upgrade_backup_invalidation_and_source_identity(tmp_path):
    index, identify = make_count_index(tmp_path, {"phantom01": {"T1 tra": 4, "FLAIR": 1}})
    state = copy.deepcopy(identify.state)
    state.pop("candidate_limit_version")
    state["phase"] = "quality"
    state["templates"] = {
        identify.families[u]: {"modality": "t1", "priority": 0}
        for u, r in index.records.items()
        if r.candidate_type == "t1"
    }
    atomic_write_json(index.root / "identification.json", state)
    sources = {u: (identify.families[u], identify.index.templates[u]["id"]) for u in index.records}
    before = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    with pytest.raises(ValueError, match="catalog"):
        require_quality(index.root)
    fresh = ProtocolIndex(index.config).identification
    fresh.enable()
    assert fresh.state["phase"] == "identification"
    assert fresh.state["revision"] == state["revision"] + 1
    assert fresh.state["candidate_limit_version"] == CANDIDATE_LIMIT_VERSION
    assert fresh.state["templates"] == state["templates"]
    backup = index.root / "identification_backups" / f"candidate-limit-{state['revision']:09d}.json"
    assert read_json(backup) == state
    assert sources == {
        u: (fresh.families[u], fresh.index.templates[u]["id"]) for u in fresh.index.records
    }
    assert all(digest(p) == value for p, value in before.items())
    assert not list(index.config.paths.staging_bids_root.rglob("*.nii*"))
    assert not read_decision(index.root.parent, "phantom01")["candidates"]
    revision = fresh.state["revision"]
    fresh.enable()
    assert fresh.state["revision"] == revision


def test_skipped_modalities_not_in_quality_jobs_or_new_approvals(tmp_path):
    index, identify = make_count_index(
        tmp_path,
        {
            "phantom01": {"T1 tra": 4, "FLAIR": 4, "CT": 1},
            "phantom02": {"T1 tra": 4, "FLAIR": 1},
        },
    )
    assert identify.list_subjects({"queue": "candidate_limit"})["total"] == 3
    assert identify.list_subjects({"queue": "candidate_limit", "q": "phantom02"})["total"] == 1
    assert identify.list_subjects({"queue": "candidate_limit", "center": "absent"})["total"] == 0
    assert identify.list_subjects({"queue": "candidate_limit", "offset": "3"})["subjects"] == []
    identify.transition({"revision": identify.state["revision"], "phase": "quality"})
    rows = feature_rows(index)
    assert [(r["subject"], r["modality"]) for r in rows] == [("phantom02", "flair")]
    # Exercise a zero-job run without reading any images or spawning feature tasks.
    single = ProtocolIndex(index.config)
    single.records = {u: r for u, r in single.records.items() if r.subject_id == "phantom01"}
    assert run_features(single, workers=1)["total"] == 0
    service = ReviewService(index.config)
    try:
        assert [s["id"] for s in service.list_subjects({})["subjects"]] == ["phantom02"]
        assert service.list_subjects({"others": "1"})["total"] == 2
        subject = service.subject("phantom02")
        assert set(subject["candidate_limits"]) == {"t1"}
        assert subject["sequence_choices"]["t1"] is None
        assert subject["sequence_choices"]["flair"]
        t1 = subject["candidate_limits"]["t1"]["candidate_ids"][0]
        decision = read_decision(service.root, "phantom02")
        decision["candidates"][t1] = {"quality": "pass", "modality": "t1"}
        with pytest.raises(ValueError, match="达到 4"):
            service.save("phantom02", decision)
        assert not read_decision(service.root, "phantom02")["candidates"]
        assert service.apply(dry_run=True) == []
        row = service.list_subjects({})["subjects"][0]
        assert (row["t1"], row["flair"]) == (0, 1)
        flair = subject["sequence_choices"]["flair"]
        service._prepare_job(flair)
        good = read_decision(service.root, "phantom02")
        good["candidates"][flair] = {"quality": "pass", "modality": "flair"}
        good["groups"]["flair"] = {"choice": flair}
        service.save("phantom02", good)
        assert service.list_subjects({"pending": "1"})["total"] == 0
    finally:
        service.close()
