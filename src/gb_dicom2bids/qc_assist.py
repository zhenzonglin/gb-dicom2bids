"""Protocol assistance, quality triage and independently evidenced automatic decisions."""

from __future__ import annotations

import argparse
import copy
import json
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from pathlib import Path

from .config import load_config
from .qc_features import FEATURE_VERSION, run_features
from .qc_learning import audit_model, calibrate, feature_rows, human_label, load_model, score
from .qc_protocols import ProtocolIndex, explicit, fingerprint
from .qc_state import (
    assert_pipeline_idle,
    file_lock,
    read_decision,
    writer_lock,
)
from .runtime import atomic_write_json, read_json, utc_now


def _stats(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def evidence_valid(root: Path, proof: dict) -> bool:
    """Cheap conservative gate; installation additionally verifies full NIfTI/JSON hashes."""
    try:
        identification = read_json(root / "assist/identification.json")
        if identification:
            from .qc_identify import require_quality

            require_quality(root / "assist")
        if identification and (
            identification.get("phase") != "quality"
            or proof.get("identification_revision") != identification.get("revision")
        ):
            return False
        rules = read_json(root / "assist/rules.json")
        if proof["rules_revision"] != rules.get("revision", 0):
            return False
        if _stats(root.parent / "series_sources.json") != proof["inventory_stats"]:
            return False
        model = load_model(root / "assist", proof["modality"])
        if not model or model["version"] != proof["model_version"]:
            return False
        if model["rules_revision"] != proof["rules_revision"]:
            return False
        report = read_json(root / "assist/models" / model["version"] / "validation.json")
        body = {k: v for k, v in report.items() if k != "evidence_id"}
        if (
            not report.get("valid")
            or report.get("evidence_id") != proof["evidence_id"]
            or fingerprint(body) != report.get("evidence_id")
            or report.get("n", 0) < 59
            or report.get("upper_95", 1) > 0.05
            or proof["domain"] not in report.get("supported_domains", [])
        ):
            return False
        suspended = read_json(root / "assist/suspensions.json")
        if f"{model['version']}:{proof['domain']}" in suspended:
            return False
        if proof["feature_version"] != FEATURE_VERSION:
            return False
        artifact = read_json(root / "artifacts" / f"{proof['candidate_id']}.json")
        if (
            artifact.get("image_sha256") != proof["image_sha256"]
            or artifact.get("sidecar_sha256") != proof["sidecar_sha256"]
            or artifact.get("record_digest") != proof["record_digest"]
        ):
            return False
        return (
            _stats(Path(artifact["image"])) == proof["source_stats"]
            and _stats(Path(artifact["sidecar"])) == proof["sidecar_stats"]
        )
    except (KeyError, TypeError, OSError, ValueError):
        return False


def effective_decision(root: Path, subject: str) -> dict:
    """Merge valid machine proposals on read. The human decision/history files stay human-only."""
    manual = read_decision(root, subject)
    saved = read_json(root / "assist/proposals" / f"{subject}.json")
    if not saved or saved.get("manual_revision") != manual["revision"]:
        return manual
    merged = copy.deepcopy(manual)
    accepted = []
    for modality, proof in saved.get("groups", {}).items():
        uid = proof.get("candidate_id")
        group = manual.get("groups", {}).get(modality, {})
        if (
            group.get("choice")
            or group.get("none")
            or explicit(manual.get("candidates", {}).get(uid))
            or not evidence_valid(root, proof)
        ):
            continue
        merged["candidates"][uid] = {
            "quality": "pass",
            "modality": modality,
            "reason": "validated_quality_screening",
            "record_digest": proof["record_digest"],
            "image_sha256": proof["image_sha256"],
            "sidecar_sha256": proof["sidecar_sha256"],
            "decision_source": "automatic",
            "model_version": proof["model_version"],
            "rule_revision": proof["rules_revision"],
        }
        merged["groups"][modality] = {
            "choice": uid,
            "none": False,
            "reason": "validated_quality_screening",
            "reviewer": "automatic",
        }
        accepted.append(proof)
    if accepted:
        # Distinct machine transaction IDs; human revision numbers remain unchanged.
        merged["revision"] = 10**15 + int(fingerprint([manual["revision"], accepted])[:12], 16)
    return merged


def propose(index: ProtocolIndex) -> dict:
    from .qc_identify import require_quality

    require_quality(index.root)
    rows = feature_rows(index)
    # Uncalibrated rankings stay within modality/template; they never authorize images.
    strata = defaultdict(list)
    risks = {}
    for row in rows:
        if row["valid"]:
            strata[(row["modality"], row["domain"])].append(row)
    for group in strata.values():
        measures = {
            name: sorted(r["feature"]["features"][name] for r in group)
            for name in (
                "sharpness_p10",
                "background_ratio",
                "slice_ncc_p10",
                "local_blur_fraction",
            )
        }
        for row in group:
            ranks = []
            for name, values in measures.items():
                value = row["feature"]["features"][name]
                rank = (bisect_left(values, value) + bisect_right(values, value)) / (2 * len(group))
                ranks.append(1 - rank if name in {"sharpness_p10", "slice_ncc_p10"} else rank)
            risks[row["id"]] = sum(ranks) / len(ranks)
    choices = {s: index.choices(s) for s in index.subjects}
    conflicted = {
        g for g, subjects in index.groups.items() if index.conflicts(g, index.rule(subjects[0]))
    }
    identification_pending = (
        {
            (s, g["modality"])
            for g in index.identification.catalogue()["groups"]
            for s in g["pending_subjects"]
        }
        if index.identification
        else set()
    )

    def protocol_known(row):
        record = index.records[row["id"]]
        subject, modality = row["subject"], row["modality"]
        if index.identification:
            return (subject, modality) not in identification_pending
        return index.subject_group[subject] not in conflicted and (
            bool(index.rule(subject))
            or (
                record.classification_confidence == "high"
                and choices[subject][modality]["count"] == 1
            )
        )

    scores = {}
    reports = {}
    audits = set()
    for modality in ("t1", "flair"):
        model = load_model(index.root, modality)
        if not model:
            continue
        eligible = [r for r in rows if r["valid"] and r["modality"] == modality]
        values = score(model, eligible)
        scored = [
            dict(
                r,
                quality_score=float(s),
                selected=(
                    choices[r["subject"]][modality]["choice"] == r["id"] and protocol_known(r)
                ),
            )
            for r, s in zip(eligible, values, strict=True)
        ]
        if (
            model["rules_revision"] != index.rules["revision"]
            or model["inventory_digest"] != index.inventory_digest
        ):
            reports[modality] = {
                "valid": False,
                "note": "rules/inventory changed; calibrate --new-model",
            }
        else:
            reports[modality] = audit_model(index, model, scored)
            audits.update(e["id"] for e in reports[modality]["pending"])
        for r in scored:
            scores[r["id"]] = (r["quality_score"], model)
    suspended = read_json(index.root / "suspensions.json")
    proposals = {
        s: {"manual_revision": index.decisions[s]["revision"], "groups": {}} for s in index.subjects
    }
    queue = []
    # Reuse the same source identity/sidecar preparation and validation as the viewer.
    from .qc_review import ReviewService

    if not (index.root.parent / "enabled.json").exists():
        atomic_write_json(
            index.root.parent / "enabled.json", {"version": 1, "enabled_at": utc_now()}
        )
    service = ReviewService(index.config, workers=1, activate=False)
    try:
        for row in rows:
            uid, subject, modality = row["id"], row["subject"], row["modality"]
            decision = index.decisions[subject]
            group = decision.get("groups", {}).get(modality, {})
            rating = decision.get("candidates", {}).get(uid)
            selected = choices[subject][modality]["choice"] == uid
            prediction, model = scores.get(uid, (None, {}))
            report = reports.get(modality, {})
            record = index.records[uid]
            known_protocol = protocol_known(row)
            state, reason = "quality_review", "needs_quality_labels"
            if uid in audits:
                state, reason = "audit", "independent_validation"
            elif group.get("choice") or group.get("none"):
                state, reason = "manual_resolved", "existing_manual_decision"
            elif not selected:
                if choices[subject][modality]["top_count"] > 1:
                    reason = "true_multiple_candidates"
                else:
                    state, reason = "retained_alternative", "not_preferred_protocol"
            elif not known_protocol:
                reason = "protocol_unconfirmed"
            elif explicit(rating):
                reason = "manual_pending_selection"
            elif not row["valid"]:
                reason = row["feature"].get("error", "features_missing_or_stale")
            elif prediction is not None:
                if prediction < model["threshold"]:
                    reason = "blur_or_artifact_risk"
                elif f"{model['version']}:{row['domain']}" in suspended:
                    reason = "domain_suspended"
                elif not report.get("valid"):
                    reason = "independent_validation_not_passed"
                elif row["domain"] not in report["supported_domains"]:
                    reason = "domain_not_validated"
                elif (
                    int(fingerprint([model["version"], subject, modality, "monitor"])[0:8], 16)
                    % 100
                    < 5
                ):
                    state, reason = "audit", "ongoing_random_check"
                else:
                    artifact = service.artifact(uid, deep=True)
                    if not artifact:
                        service._prepare_job(uid)
                        artifact = service.artifact(uid, deep=True)
                    if not artifact or artifact["metadata"]["errors"]:
                        reason = "artifact_validation_failed"
                    else:
                        from dataclasses import replace

                        from .convert import _validate_converted_pair

                        try:
                            _validate_converted_pair(
                                Path(artifact["image"]),
                                Path(artifact["sidecar"]),
                                replace(record, candidate_type=modality),
                            )
                            proof = {
                                "identification_revision": index.identification.state["revision"]
                                if index.identification
                                else None,
                                "candidate_id": uid,
                                "modality": modality,
                                "domain": row["domain"],
                                "model_version": model["version"],
                                "evidence_id": report["evidence_id"],
                                "rules_revision": index.rules["revision"],
                                "feature_version": FEATURE_VERSION,
                                "inventory_stats": _stats(
                                    index.config.paths.audit_root / "series_sources.json"
                                ),
                                "source_stats": _stats(Path(artifact["image"])),
                                "sidecar_stats": _stats(Path(artifact["sidecar"])),
                                **{
                                    k: artifact[k]
                                    for k in ("image_sha256", "sidecar_sha256", "record_digest")
                                },
                            }
                            if proof["image_sha256"] != row["feature"]["image_sha256"]:
                                raise ValueError("source changed since quality scoring")
                            proposals[subject]["groups"][modality] = proof
                            state, reason = "auto_pass", "validated_quality_screening"
                        except (OSError, ValueError, RuntimeError) as exc:
                            reason = str(exc)
            queue.append(
                {
                    "id": uid,
                    "subject": subject,
                    "modality": modality,
                    "domain": row["domain"],
                    "queue": state,
                    "reason": reason,
                    "quality_score": prediction,
                    "risk_rank": risks.get(uid),
                    "model_version": model.get("version"),
                    "threshold": model.get("threshold"),
                    "suspect_slices": row["feature"].get("suspect_slices", []),
                }
            )
    finally:
        service.close()
    for subject, proposal in proposals.items():
        atomic_write_json(index.root / "proposals" / f"{subject}.json", proposal)
    queue.sort(
        key=lambda r: (
            r["queue"],
            r["quality_score"] if r["quality_score"] is not None else -(r["risk_rank"] or 0),
            r["subject"],
            r["id"],
        )
    )
    result = {
        "at": utc_now(),
        "counts": dict(Counter(r["queue"] for r in queue)),
        "rows": queue,
        "validation": reports,
    }
    atomic_write_json(index.root / "triage.json", result)
    return {"counts": result["counts"], "validation": reports}


def feedback(index: ProtocolIndex, subject: str) -> None:
    """Any audit edit invalidates its receipt; an observed false acceptance suspends its domain."""
    triage = {r["id"]: r for r in read_json(index.root / "triage.json").get("rows", [])}
    suspended = read_json(index.root / "suspensions.json")
    rows = feature_rows(index, subjects={subject})
    for modality in ("t1", "flair"):
        model = load_model(index.root, modality)
        if not model:
            continue
        folder = index.root / "models" / model["version"]
        manifest = read_json(folder / "audit.json")
        if any(e["subject"] == subject for e in manifest.get("entries", [])):
            report = read_json(folder / "validation.json")
            atomic_write_json(
                folder / "validation.json",
                dict(report, valid=False, note="audit_updated; run propose"),
            )
        for row in rows:
            old = triage.get(row["id"], {})
            if (
                old.get("queue") in {"auto_pass", "audit"}
                and human_label(index, row) == 0
                and old.get("model_version") == model["version"]
            ):
                suspended[f"{model['version']}:{row['domain']}"] = {
                    "candidate": row["id"],
                    "subject": subject,
                    "at": utc_now(),
                    "reason": "manual_false_acceptance",
                    "model_version": model["version"],
                }
    if suspended:
        atomic_write_json(index.root / "suspensions.json", suspended)
        affected = [
            r for r in triage.values() if f"{r.get('model_version')}:{r['domain']}" in suspended
        ]
        atomic_write_json(index.root / "recheck.json", {"rows": affected, "at": utc_now()})
    changed = False
    for row in triage.values():
        if f"{row.get('model_version')}:{row['domain']}" in suspended:
            row.update(queue="quality_review", reason="domain_suspended")
            changed = True
        if row["subject"] == subject:
            group = index.decisions[subject].get("groups", {}).get(row["modality"], {})
            if group.get("choice") or group.get("none"):
                row.update(queue="manual_resolved", reason="existing_manual_decision")
                changed = True
    if changed:
        saved = read_json(index.root / "triage.json")
        saved.update(
            at=utc_now(),
            rows=list(triage.values()),
            counts=dict(Counter(r["queue"] for r in triage.values())),
        )
        atomic_write_json(index.root / "triage.json", saved)


def status(root: Path) -> dict:
    features = read_json(root / "status.json")
    triage = read_json(root / "triage.json")
    catalogue = read_json(root / "catalogue.json")
    identification = read_json(root / "identification_catalogue.json")
    validation = {}
    for modality in ("t1", "flair"):
        model = load_model(root, modality)
        report = read_json(root / "models" / model.get("version", "missing") / "validation.json")
        validation[modality] = {
            k: report.get(k) for k in ("valid", "n", "errors", "upper_95", "note")
        }
    return {
        "features": features,
        "queues": triage.get("counts", {}),
        "queues_at": triage.get("at"),
        "protocol_groups": len((identification or catalogue).get("groups", [])),
        "identification": {k: v for k, v in identification.items() if k != "groups"},
        "validation": validation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "catalog",
            "features",
            "calibrate",
            "propose",
            "status",
            "finish-identification",
            "reopen-identification",
        ],
    )
    parser.add_argument("--config", type=Path, default=Path("config/config.nifti.local.yaml"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--new-model", action="store_true")
    parser.add_argument("--audit-size", type=int, default=59)
    parser.add_argument("--watch", type=float, default=0)
    args = parser.parse_args(argv)
    if args.watch < 0 or (args.watch and args.command != "status"):
        parser.error("--watch must be nonnegative and is only available for status")
    try:
        config = load_config(args.config)
        root = config.paths.audit_root / "visual_qc/assist"
        if args.command in {"features", "calibrate", "propose"}:
            from .qc_identify import require_quality

            require_quality(root)
        if args.command == "status":
            while True:
                print(json.dumps(status(root), ensure_ascii=False, indent=2), flush=True)
                if not args.watch:
                    return 0
                time.sleep(max(1, args.watch))
        if args.command == "features":
            result = run_features(
                ProtocolIndex(config), args.workers, args.resume, args.retry_failed
            )
        else:
            with (
                writer_lock(config),
                file_lock(root.parent / ".decisions.lock"),
                file_lock(root / ".assist.lock"),
                file_lock(root / ".features.lock"),
            ):
                assert_pipeline_idle(config)
                index = ProtocolIndex(config)
                if args.command == "catalog":
                    from .qc_identify import Identification

                    identification = index.identification or Identification(index)
                    index.identification = identification
                    catalogue = identification.enable()
                    review = identification.review_rules({})
                    atomic_write_json(root / "identification_catalogue.json", catalogue)
                    labels = Counter()
                    for s in index.subjects:
                        labels.update(
                            r.get("quality", "unreviewed")
                            for r in index.decisions[s].get("candidates", {}).values()
                        )
                    result = {
                        "subjects": len(index.subjects),
                        "groups": len(catalogue["groups"]),
                        "needs_protocol": sum(g["needs_protocol"] for g in catalogue["groups"]),
                        "human_ratings": dict(labels),
                        "active_identification_rules": review["total"],
                        "identification": identification.summary(),
                    }
                elif args.command in {"finish-identification", "reopen-identification"}:
                    if not index.identification:
                        raise ValueError("run catalog first")
                    result = index.identification.transition(
                        {
                            "revision": index.identification.state["revision"],
                            "phase": "quality"
                            if args.command == "finish-identification"
                            else "identification",
                        }
                    )
                elif args.command == "calibrate":
                    result = calibrate(index, new_model=args.new_model, audit_size=args.audit_size)
                else:
                    result = propose(index)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except KeyboardInterrupt:
        print("Interrupted; cached results and saved decisions are retained.", flush=True)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
