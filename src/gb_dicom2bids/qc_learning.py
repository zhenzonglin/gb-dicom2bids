"""Frozen, patient-separated quality models and independent acceptance audits."""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict

import numpy as np

from .qc_features import FEATURE_NAMES, FEATURE_VERSION
from .qc_protocols import ProtocolIndex, fingerprint, source_path
from .qc_state import record_digest
from .runtime import atomic_write_json, read_json, utc_now

MODEL_VERSION = "quality-logistic-1"
QUALITY_REASONS = {
    "motion_blur",
    "ghosting",
    "signal_loss",
    "distortion",
    "noise",
    "coverage",
    "other_quality",
}


def partition(subject: str) -> str:
    bucket = int(fingerprint(["patient-split-1", subject])[:8], 16) % 100
    return "train" if bucket < 60 else "calibration" if bucket < 80 else "audit"


def feature_rows(index: ProtocolIndex, subjects: set[str] | None = None) -> list[dict]:
    result = []
    for uid, record in sorted(index.records.items()):
        if subjects is not None and record.subject_id not in subjects:
            continue
        assignment = index.assignment(uid)
        if assignment["modality"] not in {"t1", "flair"}:
            continue
        feature = read_json(index.root / "features" / f"{uid}.json")
        try:
            stat = source_path(index.config, record).stat()
            valid = (
                feature.get("state") == "completed"
                and feature.get("version") == FEATURE_VERSION
                and feature.get("record_digest") == record_digest(record)
                and feature.get("source_stats") == [stat.st_size, stat.st_mtime_ns]
                and all(
                    np.isfinite(float(feature.get("features", {}).get(f, np.nan)))
                    for f in FEATURE_NAMES
                )
            )
        except (OSError, ValueError, TypeError):
            valid = False
        result.append(
            {
                "id": uid,
                "subject": record.subject_id,
                "center": record.center,
                "modality": assignment["modality"],
                "domain": assignment["template_id"],
                "split": partition(record.subject_id),
                "feature": feature,
                "valid": valid,
            }
        )
    return result


def human_label(index: ProtocolIndex, row: dict) -> int | None:
    rating = index.decisions[row["subject"]].get("candidates", {}).get(row["id"], {})
    if rating.get("decision_source", "manual") != "manual":
        return None
    if not row["valid"] or rating.get("record_digest") != row["feature"].get("record_digest"):
        return None
    if rating.get("image_sha256") and rating["image_sha256"] != row["feature"].get("image_sha256"):
        return None
    if rating.get("quality") == "pass" and rating.get("image_sha256"):
        return 1
    if rating.get("quality") != "fail":
        return None
    category = rating.get("failure_category", "")
    reason = str(rating.get("reason", "")).lower()
    if category in QUALITY_REASONS or (
        not category
        and re.search(r"头动|模糊|重影|信号缺失|畸变|噪声|\b(?:motion|blur|ghosting)\b", reason)
        and not re.search(r"误分类|未选|分类错误|not selected|wrong modality", reason)
    ):
        # Legacy failures have no checksum: require unchanged inventory file statistics.
        if not rating.get("image_sha256"):
            note = index.records[row["id"]].inventory_note
            size, mtime = row["feature"]["source_stats"]
            if f"source_size={size};source_mtime_ns={mtime};" not in note:
                return None
        return 0
    return None


def upper_error_bound(errors: int, n: int) -> float:
    if n <= 0 or errors >= n:
        return 1.0
    if not 0 <= errors <= n:
        raise ValueError("invalid audit counts")
    if errors == 0:
        return float(1 - 0.05 ** (1 / n))
    from scipy.stats import beta

    return float(beta.ppf(0.95, errors + 1, n - errors))


def load_model(root, modality: str) -> dict:
    active = read_json(root / "models" / f"{modality}.json")
    version = active.get("version", "")
    if not re.fullmatch(r"[a-f0-9]{24}", version):
        return {}
    model = read_json(root / "models" / version / "model.json")
    body = {k: v for k, v in model.items() if k != "version"}
    valid = (
        fingerprint(body)[:24] == version
        and model.get("algorithm") == MODEL_VERSION
        and model.get("feature_version") == FEATURE_VERSION
    )
    return model if valid else {}


def score(model: dict, rows: list[dict]) -> np.ndarray:
    if not rows:
        return np.array([])
    x = np.array([[row["feature"]["features"][f] for f in model["features"]] for row in rows])
    x = (x - np.asarray(model["mean"])) / np.asarray(model["scale"])
    z = np.clip(x @ np.asarray(model["coefficients"]) + model["intercept"], -60, 60)
    return 1 / (1 + np.exp(-z))


def calibrate(index: ProtocolIndex, *, new_model: bool = False, audit_size: int = 59) -> dict:
    from .qc_identify import require_quality

    require_quality(index.root)
    if not 59 <= audit_size <= 10000:
        raise ValueError("audit size must be between 59 and 10000 independent patients")
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from threadpoolctl import threadpool_limits
    except ImportError as exc:
        raise ValueError(
            "install the optional qc-assist dependencies: pip install -e '.[qc-assist]'"
        ) from exc
    rows = feature_rows(index)
    report = {}
    for modality in ("t1", "flair"):
        current = load_model(index.root, modality)
        if current and not new_model:
            report[modality] = {
                "state": "frozen",
                "version": current["version"],
                "note": "use --new-model for changed rules or new training labels",
            }
            continue
        if new_model and current:
            atomic_write_json(index.root / "models" / f"{modality}.json", {})
        labelled = [dict(r, label=human_label(index, r)) for r in rows if r["modality"] == modality]
        labelled = [r for r in labelled if r["label"] is not None]
        train = [r for r in labelled if r["split"] == "train"]
        calibration = [r for r in labelled if r["split"] == "calibration"]
        summary = {
            "labels": len(labelled),
            "training": len(train),
            "calibration": len(calibration),
            "training_classes": dict(Counter(r["label"] for r in train)),
            "label_distribution": {
                domain: {
                    "center": index.templates[group[0]["id"]]["center"],
                    "template": index.templates[group[0]["id"]],
                    "counts": dict(Counter(f"{r['split']}:{r['label']}" for r in group)),
                }
                for domain in sorted({r["domain"] for r in labelled})
                if (group := [r for r in labelled if r["domain"] == domain])
            },
        }
        if len(train) < 20 or len({r["label"] for r in train}) != 2 or len(calibration) < 10:
            report[modality] = dict(
                summary,
                state="needs_labels",
                note="need >=20 train (both classes) and >=10 calibration candidates",
            )
            continue
        features = FEATURE_NAMES
        x = np.array([[r["feature"]["features"][f] for f in features] for r in train])
        y = [r["label"] for r in train]
        with threadpool_limits(limits=1):
            scaler = StandardScaler().fit(x)
            classifier = LogisticRegression(
                C=1, class_weight="balanced", max_iter=2000, random_state=17
            )
            classifier.fit(scaler.transform(x), y)
        model = {
            "generation": uuid.uuid4().hex,
            "algorithm": MODEL_VERSION,
            "feature_version": FEATURE_VERSION,
            "modality": modality,
            "audit_size": audit_size,
            "features": features,
            "mean": scaler.mean_.tolist(),
            "scale": scaler.scale_.tolist(),
            "coefficients": classifier.coef_[0].tolist(),
            "intercept": float(classifier.intercept_[0]),
            "created_at": utc_now(),
            "rules_revision": index.rules["revision"],
            "inventory_digest": index.inventory_digest,
            "training_subjects": sorted({r["subject"] for r in train}),
            "calibration_subjects": sorted({r["subject"] for r in calibration}),
            "domains": sorted({r["domain"] for r in train} & {r["domain"] for r in calibration}),
        }
        scores = score(model, calibration)
        threshold = None
        for cut in sorted(set(map(float, scores))):
            selected = [r for r, s in zip(calibration, scores, strict=True) if s >= cut]
            # Threshold selection is NOT an independent accuracy claim.
            if (
                len(selected) >= 5
                and sum(r["label"] == 0 for r in selected) / len(selected) <= 0.05
            ):
                threshold = cut
                break
        if threshold is None or not model["domains"]:
            report[modality] = dict(
                summary, state="needs_labels", note="no supported acceptance threshold/domain"
            )
            continue
        model["threshold"] = threshold
        # Freeze every label used for model fitting/threshold selection, never test labels.
        snapshot = [
            {
                "id": r["id"],
                "subject": r["subject"],
                "split": r["split"],
                "label": r["label"],
                "image_sha256": r["feature"]["image_sha256"],
                "revision": index.decisions[r["subject"]]["revision"],
            }
            for r in train + calibration
        ]
        model["labels_digest"] = fingerprint(snapshot)
        model["version"] = fingerprint(model)[:24]
        folder = index.root / "models" / model["version"]
        atomic_write_json(folder / "labels.json", {"labels": snapshot})
        atomic_write_json(folder / "model.json", model)
        atomic_write_json(index.root / "models" / f"{modality}.json", {"version": model["version"]})
        report[modality] = dict(
            summary, state="awaiting_independent_audit", version=model["version"]
        )
    atomic_write_json(index.root / "calibration_summary.json", report)
    return report


def audit_model(index: ProtocolIndex, model: dict, scored: list[dict]) -> dict:
    folder = index.root / "models" / model["version"]
    manifest = read_json(folder / "audit.json")
    if not manifest:
        # The audit is selected from all eligible held-out patients, NOT just those already rated.
        candidates = [
            r
            for r in scored
            if r["split"] == "audit"
            and r["selected"]
            and r["quality_score"] >= model["threshold"]
            and r["domain"] in model["domains"]
        ]
        used = set()
        for path in (index.root / "models").glob("*/audit.json"):
            if path.parent != folder:
                used.update(r["subject"] for r in read_json(path).get("entries", []))
        candidates = [r for r in candidates if r["subject"] not in used]
        candidates.sort(key=lambda r: fingerprint([model["version"], r["subject"]]))
        unique = {}
        for r in candidates:
            unique.setdefault(r["subject"], r)
        random_sample = list(unique.values())[: model.get("audit_size", 59)]
        covered = {r["domain"] for r in random_sample}
        extra = []
        for row in candidates:
            if row["domain"] not in covered:
                extra.append(row)
                covered.add(row["domain"])
        entries = [
            {
                "id": r["id"],
                "subject": r["subject"],
                "domain": r["domain"],
                "center": r["center"],
                "image_sha256": r["feature"]["image_sha256"],
                "role": "random" if r in random_sample else "scope",
            }
            for r in random_sample + extra
        ]
        manifest = {
            "entries": entries,
            "random_required": model.get("audit_size", 59),
            "pool_size": len(unique),
            "model": model["version"],
            "frozen_at": utc_now(),
        }
        atomic_write_json(folder / "audit.json", manifest)
    by_uid = {r["id"]: r for r in scored}
    errors = 0
    known = 0
    pending = []
    domains: dict[str, dict] = defaultdict(lambda: {"n": 0, "errors": 0, "pending": 0})
    receipts = []
    for entry in manifest["entries"]:
        row = by_uid.get(entry["id"])
        label = human_label(index, row) if row else None
        if row and row["feature"].get("image_sha256") != entry["image_sha256"]:
            label = None
        domain = domains[entry["domain"]]
        if label is None:
            pending.append(entry)
            domain["pending"] += 1
        else:
            domain["n"] += 1
            domain["errors"] += label == 0
            receipts.append([entry["subject"], index.decisions[entry["subject"]]["revision"]])
            if entry["role"] == "random":
                known += 1
                errors += label == 0
    random_total = sum(e["role"] == "random" for e in manifest["entries"])
    upper = upper_error_bound(errors, known)
    valid = (
        random_total >= manifest["random_required"]
        and known == random_total
        and upper <= 0.05
        and model["rules_revision"] == index.rules["revision"]
        and model["inventory_digest"] == index.inventory_digest
    )
    report = {
        "valid": bool(valid),
        "model": model["version"],
        "n": known,
        "errors": errors,
        "upper_95": upper,
        "target": 0.05,
        "random_total": random_total,
        "pending": pending,
        "domains": dict(domains),
        "domain_templates": {
            d: next(index.templates[r["id"]] for r in scored if r["domain"] == d)
            for d in domains
            if any(r["domain"] == d for r in scored)
        },
        "receipts": receipts,
        "supported_domains": sorted(
            d for d, v in domains.items() if v["n"] > 0 and not v["errors"] and not v["pending"]
        ),
        "note": "frozen independent audit; 59 all-pass patients required at minimum",
    }
    report["evidence_id"] = fingerprint(report)
    atomic_write_json(folder / "validation.json", report)
    return report
