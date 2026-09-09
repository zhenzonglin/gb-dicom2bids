"""Private, center-scoped protocol templates; a template never grants image quality."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .manifest import load_private_records
from .models import SeriesRecord
from .qc_images import check_image
from .qc_state import ConflictError, candidate_id, effective_modality, read_decision, record_digest
from .runtime import atomic_write_json, read_json, utc_now

VERSION = "protocol-1"
MODALITIES = {"t1", "flair", "other"}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def assist_root(config: ProjectConfig) -> Path:
    return config.paths.audit_root / "visual_qc" / "assist"


def source_path(config: ProjectConfig, record: SeriesRecord) -> Path:
    root = config.nifti_import.source_root
    if root is None or len(record.source_relpaths) != 1:
        raise ValueError("protocol assistance requires the preconverted NIfTI inventory")
    path = root / record.source_relpaths[0]
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
        raise ValueError("symlinked source is not supported")
    return resolved


def normalize_name(name: str) -> str:
    # Only documented export prefixes: never strip arbitrary digits such as 3D or T1.
    parts = []
    for part in name.replace("\\", "/").split("/"):
        prefix = re.match(r"^(\d{8,14})[_ -]+(?:MR[_ -]+)?\d{2,6}[_ -]+", part, flags=re.I)
        if prefix:
            stamp = prefix.group(1)
            formats = {8: "%Y%m%d", 10: "%Y%m%d%H", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
            try:
                datetime.strptime(stamp, formats[len(stamp)])
                part = part[prefix.end() :]
            except (KeyError, ValueError):
                pass  # An uncertain numeric prefix is retained, never merged by guesswork.
        part = re.sub(r"^MR[_ -]+\d{2,6}[_ -]+", "", part, flags=re.I)
        parts.append(re.sub(r"[\s_-]+", "-", part.strip().lower()))
    return "/".join(parts)


def template(record: SeriesRecord) -> dict[str, Any]:
    geometry = [
        record.rows,
        record.columns,
        record.instance_count,
        *[round(float(v), 3) for v in record.pixel_spacing_mm],
        round(float(record.slice_thickness_mm or 0), 3),
        record.nearest_plane,
        record.source_kind,
    ]
    value = {
        "center": record.center,
        "name": normalize_name(record.series_description),
        "geometry": geometry,
        "version": VERSION,
    }
    return dict(value, id=fingerprint(value)[:24])


def explicit(rating: dict | None) -> bool:
    return bool(
        rating
        and (
            rating.get("quality", "unreviewed") != "unreviewed"
            or str(rating.get("reason", "")).strip()
        )
    )


class ProtocolIndex:
    def __init__(
        self,
        config: ProjectConfig,
        records: list[SeriesRecord] | None = None,
        *,
        decisions: dict[str, dict[str, Any]] | None = None,
    ):
        if not config.nifti_import.enabled:
            raise ValueError("QC assistance is available only for preconverted NIfTI")
        self.config = config
        self.root = assist_root(config)
        self.records = {
            candidate_id(r): r
            for r in (
                records if records is not None else load_private_records(config.paths.audit_root)
            )
        }
        self.templates = {uid: template(r) for uid, r in self.records.items()}
        self.subjects: dict[str, list[str]] = defaultdict(list)
        for uid, record in self.records.items():
            self.subjects[record.subject_id].append(uid)
        # The viewer already loaded saved decisions. Avoid probing every unreviewed subject
        # on a network filesystem during the first /api/subjects request.
        self.decisions = {
            s: read_decision(self.root.parent, s)
            if decisions is None
            else decisions.get(
                s,
                {
                    "subject_id": s,
                    "revision": 0,
                    "reviewer": "zhenzong",
                    "candidates": {},
                    "groups": {},
                },
            )
            for s in self.subjects
        }
        self.rules = read_json(self.root / "rules.json") or {"revision": 0, "groups": {}}
        self.groups: dict[str, list[str]] = defaultdict(list)
        self.subject_group: dict[str, str] = {}
        for subject, uids in self.subjects.items():
            signature = sorted(Counter(self.templates[u]["id"] for u in uids).items())
            gid = fingerprint([VERSION, signature])[:24]
            self.groups[gid].append(subject)
            self.subject_group[subject] = gid
        self._inventory_digest: str | None = None
        self._digest_lock = threading.Lock()
        self.identification = None
        if (self.root / "identification.json").exists():
            from .qc_identify import Identification

            self.identification = Identification(self)

    @property
    def inventory_digest(self) -> str:
        # Required for publishing rules and model authorization, not for listing patients.
        with self._digest_lock:
            if self._inventory_digest is None:
                self._inventory_digest = fingerprint(
                    sorted((uid, record_digest(r)) for uid, r in self.records.items())
                )
            if self.identification:
                return fingerprint([self._inventory_digest, self.identification.state])
            return self._inventory_digest

    def rule(self, subject: str) -> dict:
        return self.rules["groups"].get(self.subject_group[subject], {})

    def assignment(self, uid: str, rule: dict | None = None) -> dict:
        if self.identification and rule is None:
            return self.identification.assignment(uid)
        record = self.records[uid]
        rule = self.rule(record.subject_id) if rule is None else rule
        value = rule.get("templates", {}).get(self.templates[uid]["id"], {})
        rating = self.decisions[record.subject_id].get("candidates", {}).get(uid)
        modality = (
            effective_modality(record, rating)
            if explicit(rating)
            else value.get("modality", record.candidate_type)
        )
        return {
            "modality": modality,
            "priority": value.get("priority", 100),
            "template_id": self.templates[uid]["id"],
            "manual": explicit(rating),
        }

    def choices(self, subject: str, rule: dict | None = None) -> dict:
        values = {uid: self.assignment(uid, rule) for uid in self.subjects[subject]}
        result = {}
        for modality in ("t1", "flair"):
            candidates = [
                u
                for u, a in values.items()
                if a["modality"] == modality and modality not in a.get("excluded_modalities", [])
            ]
            best = min((values[u]["priority"] for u in candidates), default=100)
            winners = [u for u in candidates if values[u]["priority"] == best]
            preference = []
            if self.identification and rule is None and modality == "t1":
                preference = self.identification.preferred_t1(
                    subject, self.identification.state, values
                )
                if preference:
                    winners = preference
            result[modality] = {
                "choice": winners[0] if len(winners) == 1 else None,
                "count": len(candidates),
                "top_count": len(winners),
            }
            if preference:
                result[modality]["selection_reason"] = "t1_tra_over_sag"
        return result

    def conflicts(self, gid: str, rule: dict | None = None) -> list[dict]:
        signatures: dict[str, set[str]] = defaultdict(set)
        conflicts = []
        for subject in self.groups[gid]:
            decision = self.decisions[subject]
            for uid, rating in decision.get("candidates", {}).items():
                if uid not in self.records or not explicit(rating):
                    continue
                tid = self.templates[uid]["id"]
                modality = effective_modality(self.records[uid], rating)
                signatures[tid].add(modality)
                assigned = (rule or {}).get("templates", {}).get(tid, {})
                if assigned and assigned["modality"] != modality:
                    conflicts.append(
                        {"subject": subject, "template": tid, "reason": "manual_modality_conflict"}
                    )
            if rule:
                choices = self.choices(subject, rule)
                for modality, group in decision.get("groups", {}).items():
                    uid = group.get("choice")
                    proposed = choices.get(modality, {}).get("choice")
                    if uid and proposed and uid != proposed:
                        conflicts.append({"subject": subject, "reason": "manual_choice_conflict"})
        for tid, modalities in signatures.items():
            if len(modalities) > 1:
                conflicts.append({"template": tid, "reason": "inconsistent_manual_classes"})
        return conflicts

    def catalogue(self) -> dict:
        groups = []
        for gid, subjects in sorted(self.groups.items()):
            subjects = sorted(subjects)
            rated = [s for s in subjects if self.decisions[s]["revision"]]
            representative, readable = (rated or subjects)[0], False
            for s in dict.fromkeys([*rated, *subjects]):
                try:
                    for uid in self.subjects[s]:
                        check_image(source_path(self.config, self.records[uid]))
                    representative, readable = s, True
                    break
                except (OSError, ValueError):
                    continue
            entries = {}
            for uid in self.subjects[representative]:
                a = self.assignment(uid)
                entries[a["template_id"]] = dict(
                    self.templates[uid],
                    **{
                        "modality": a["modality"],
                        "priority": a["priority"],
                        "example_name": self.records[uid].series_description,
                    },
                )
            choices = self.choices(representative)
            conflicts = self.conflicts(gid, self.rule(representative))
            groups.append(
                {
                    "id": gid,
                    "center": self.records[self.subjects[representative][0]].center,
                    "subjects": subjects,
                    "count": len(subjects),
                    "representative": representative,
                    "readable": readable,
                    "templates": list(entries.values()),
                    "conflicts": conflicts,
                    "published": bool(self.rule(representative)),
                    "needs_protocol": bool(conflicts)
                    or any(v["top_count"] != 1 for v in choices.values())
                    or any(
                        self.records[u].classification_confidence != "high"
                        for u in self.subjects[representative]
                        if self.assignment(u)["modality"] in {"t1", "flair"}
                    ),
                }
            )
        return {
            "version": VERSION,
            "inventory_digest": self.inventory_digest,
            "revision": self.rules["revision"],
            "groups": groups,
            "subjects": len(self.subjects),
            "candidates": len(self.records),
        }

    def preview(self, payload: dict) -> dict:
        gid = str(payload.get("group", ""))
        if gid not in self.groups:
            raise ValueError("unknown protocol group; rebuild the catalogue")
        if payload.get("revision") != self.rules["revision"]:
            raise ConflictError("protocol rules changed; reload the catalogue")
        if payload.get("inventory_digest") != self.inventory_digest:
            raise ConflictError("inventory changed; rebuild the catalogue")
        raw = payload.get("templates", {})
        expected = {self.templates[u]["id"] for u in self.subjects[self.groups[gid][0]]}
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("assign every template in this candidate combination")
        entries = {}
        for tid, value in raw.items():
            if not isinstance(value, dict) or value.get("modality") not in MODALITIES:
                raise ValueError("invalid template modality")
            rank = value.get("priority", 100)
            if type(rank) is not int or not 0 <= rank <= 999:
                raise ValueError("priority must be an integer between 0 and 999")
            entries[tid] = {"modality": value["modality"], "priority": rank}
        rule = {"templates": entries}
        changes = [
            {"subject": s, "before": self.choices(s), "after": self.choices(s, rule)}
            for s in sorted(self.groups[gid])
        ]
        return {
            "group": gid,
            "rule": rule,
            "changes": changes,
            "affected_subjects": len(changes),
            "conflicts": self.conflicts(gid, rule),
            "preview_digest": fingerprint([payload, changes]),
        }

    def publish(self, payload: dict) -> dict:
        preview = self.preview(payload)
        if payload.get("preview_digest"):
            clean = {k: v for k, v in payload.items() if k != "preview_digest"}
            if self.preview(clean)["preview_digest"] != payload["preview_digest"]:
                raise ConflictError("preview changed; preview the rule again")
        else:
            raise ValueError("preview the affected candidates before publishing")
        if preview["conflicts"]:
            raise ConflictError("manual decisions conflict; review conflicting cases first")
        reviewer, reason = (
            str(payload.get("reviewer", "")).strip(),
            str(payload.get("reason", "")).strip(),
        )
        if not reviewer:
            raise ValueError("reviewer is required")
        rules = dict(
            self.rules, groups=dict(self.rules["groups"]), revision=self.rules["revision"] + 1
        )
        rules["groups"][preview["group"]] = dict(
            preview["rule"], reviewer=reviewer, reason=reason, saved_at=utc_now()
        )
        self._save_rules(rules)
        return {"revision": rules["revision"], "affected_subjects": preview["affected_subjects"]}

    def revoke(self, payload: dict) -> dict:
        if payload.get("revision") != self.rules["revision"]:
            raise ConflictError("protocol rules changed; reload")
        gid = payload.get("group")
        if gid not in self.rules["groups"]:
            raise ValueError("no published rule for this group")
        rules = dict(
            self.rules, groups=dict(self.rules["groups"]), revision=self.rules["revision"] + 1
        )
        del rules["groups"][gid]
        self._save_rules(rules)
        return {"revision": rules["revision"]}

    def _save_rules(self, rules: dict) -> None:
        atomic_write_json(self.root / "rule_history" / f"{rules['revision']:09d}.json", rules)
        atomic_write_json(self.root / "rules.json", rules)
        self.rules = rules
