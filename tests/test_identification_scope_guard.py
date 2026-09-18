"""Background saves of independent modalities must not invalidate each other."""

import uuid

import pytest
from test_identification_jobs import wait_done
from test_qc_exclusions import make_missing

from gb_dicom2bids.qc_jobs import IdentificationJobs
from gb_dicom2bids.qc_review import ReviewService
from gb_dicom2bids.qc_state import digest, read_decision


def scoped_request(service, modality, subject="phantom01"):
    identify = service.assistance().identification
    group = next(g for g in identify.subject_groups(subject) if g["modality"] == modality)
    payload = {
        "subject": subject,
        "group": group["id"],
        "target_modality": modality,
        "revision": identify.state["revision"],
        "basis": identify.edit_basis(group["id"]),
        "edit_context": identify.edit_context(group["id"]),
        "reviewer": "zhenzong",
        "templates": {
            e["id"]: {"modality": modality, "priority": i} for i, e in enumerate(group["templates"])
        },
    }
    return {"request_id": str(uuid.uuid4()), "payload": payload}


def test_same_patient_independent_modality_saves_both_complete(tmp_path):
    index, _ = make_missing(tmp_path, {"phantom01": ["T1 A", "T1 B", "FLAIR A", "FLAIR B"]})
    service = ReviewService(index.config)
    jobs = service.identification_jobs()
    jobs.close()
    resumed = None
    try:
        first, second = scoped_request(service, "t1"), scoped_request(service, "flair")
        jobs.submit(first)
        jobs.submit(second)
        resumed = IdentificationJobs(service)
        results = wait_done(resumed, 2)
        assert all(j["state"] == "completed" for j in results), results
        assert service.assistance().identification.summary()["pending_groups"] == 0
        assert not read_decision(service.root, "phantom01")["candidates"]
        assert not list(service.config.paths.staging_bids_root.rglob("*.nii*"))
    finally:
        if resumed:
            resumed.close()
        service.close()


@pytest.mark.parametrize("conflicting", [False, True])
def test_shared_template_can_converge_but_cannot_overwrite_new_priority(tmp_path, conflicting):
    index, _ = make_missing(
        tmp_path,
        {
            "phantom01": ["T1 shared", "T1 first", "FLAIR"],
            "phantom02": ["T1 shared", "T1 second", "FLAIR"],
        },
    )
    service = ReviewService(index.config)
    jobs = service.identification_jobs()
    jobs.close()
    resumed = None
    try:
        first = scoped_request(service, "t1")
        second = scoped_request(service, "t1", "phantom02")
        identify = service.assistance().identification
        shared = next(f for f, name in identify.names.items() if name == "t1-shared")
        for raw in (first, second):
            for f, entry in raw["payload"]["templates"].items():
                entry["priority"] = 0 if f == shared else 10
        if conflicting:
            second["payload"]["templates"][shared]["priority"] = 1
        one, two = jobs.submit(first), jobs.submit(second)
        resumed = IdentificationJobs(service)
        results = {j["id"]: j for j in wait_done(resumed, 2)}
        assert results[one["id"]]["state"] == "completed"
        assert results[two["id"]]["state"] == ("failed" if conflicting else "completed")
        assert service.assistance().identification.state["templates"][shared]["priority"] == 0
    finally:
        if resumed:
            resumed.close()
        service.close()


def test_independent_negative_scopes_do_not_conflict_or_touch_images(tmp_path):
    index, _ = make_missing(tmp_path, {"phantom01": ["unknown A", "unknown B"]})
    before = {p: digest(p) for p in index.config.nifti_import.source_root.rglob("*.nii.gz")}
    service = ReviewService(index.config)
    jobs = service.identification_jobs()
    jobs.close()
    resumed = None
    try:
        for modality in ("t1", "flair"):
            raw = scoped_request(service, modality)
            raw["payload"].update(
                templates={},
                negative_templates=sorted(index.identification.members),
                negative_source_policy="identity_only",
                deferred_candidates=[],
            )
            jobs.submit(raw)
        resumed = IdentificationJobs(service)
        assert all(j["state"] == "completed" for j in wait_done(resumed, 2))
        assert service.assistance().identification.summary()["pending_groups"] == 0
        assert all(digest(p) == h for p, h in before.items())
        assert not read_decision(service.root, "phantom01")["candidates"]
    finally:
        if resumed:
            resumed.close()
        service.close()


def test_new_guard_still_blocks_cross_modality_template_overwrite(tmp_path):
    index, _ = make_missing(tmp_path, {"phantom01": ["T1 A", "T1 B", "FLAIR A", "FLAIR B"]})
    service = ReviewService(index.config)
    jobs = service.identification_jobs()
    jobs.close()
    resumed = None
    try:
        first, second = scoped_request(service, "t1"), scoped_request(service, "flair")
        family = next(iter(first["payload"]["templates"]))
        second["payload"]["templates"][family] = {"modality": "flair", "priority": 3}
        jobs.submit(first)
        two = jobs.submit(second)
        resumed = IdentificationJobs(service)
        results = {j["id"]: j for j in wait_done(resumed, 2)}
        assert results[two["id"]]["state"] == "failed"
        assert "已改变" in results[two["id"]]["error"]
        assert service.assistance().identification.state["templates"][family]["modality"] == "t1"
    finally:
        if resumed:
            resumed.close()
        service.close()


def test_paused_groups_are_filtered_before_pagination_not_marked_completed(tmp_path):
    _, identify = make_missing(tmp_path, {"phantom01": ["T1 A", "T1 B", "FLAIR A", "FLAIR B"]})
    groups = identify.catalogue()["groups"]
    result = identify.list_subjects({"queue": "protocol", "skip_groups": groups[0]["id"]})
    assert result["total"] == 1
    assert result["subjects"][0]["identification_group"] == groups[1]["id"]
    assert result["identification"]["pending_groups"] == 2
    assert (
        identify.list_subjects({"queue": "identified", "skip_groups": groups[0]["id"]})["total"]
        == 2
    )
