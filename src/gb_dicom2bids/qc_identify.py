"""Sequence-only rules followed by an explicit, independently gated quality stage."""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter, defaultdict

from nibabel.filebasedimages import ImageFileError

from .classify import (
    CLASSIFICATION_VERSION,
    default_classification,
    named_t2_flair_variant,
    named_target_plane,
)
from .models import SeriesRecord
from .qc_exclusions import ExclusionView
from .qc_images import check_image
from .qc_protocols import explicit, fingerprint, normalize_name
from .qc_protocols import source_path as nifti_source
from .qc_state import ConflictError, effective_modality, record_digest
from .qc_unreadable import current_failure
from .runtime import atomic_write_json, read_json, utc_now

VERSION = "identify-1"
CANDIDATE_LIMIT = 4
CANDIDATE_LIMIT_VERSION = "candidate-field-limit-1"


def state_path(root):
    return root / "identification.json"


def inventory_stamp(root):
    path = root.parent.parent / "series_sources.json"
    if not path.exists():
        return None
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def axial_candidate_order(record: SeriesRecord) -> tuple:
    """Folder first, then filename in natural order; never claim acquisition chronology."""
    source = min(record.source_relpaths) if record.source_relpaths else record.series_description
    normalized = unicodedata.normalize("NFKC", source).casefold().replace("\\", "/")
    parts = tuple(
        tuple((1, int(s)) if s.isdigit() else (0, s) for s in re.split(r"([0-9]+)", part))
        for part in normalized.split("/")
    )
    # Deterministic tie breakers, independent of inventory iteration order.
    return parts, source, record.series_uid_hash


def require_quality(root):
    from .qc_jobs import assert_no_pending

    assert_no_pending(root)
    state = read_json(state_path(root))
    if state and state.get("phase") != "quality":
        raise ValueError("请先完成序列识别，再点击进入质量检查；当前不能保存质量、计算质量或归档")
    if state and state.get("inventory_stamp") != inventory_stamp(root):
        raise ValueError("清单已变化，请重新运行 catalog 并完成序列识别")
    if state and state.get("candidate_limit_version") != CANDIDATE_LIMIT_VERSION:
        raise ValueError("候选数量规则已更新，请先运行 catalog 并完成序列识别")
    if state and state.get("defaults_version") != CLASSIFICATION_VERSION:
        raise ValueError("默认序列规则已更新，请先运行 catalog 并完成序列识别")


class Identification:
    """Names classify sequences; geometry remains in the separate quality-domain template.

    Other sequences are optional correction sources, never part of a default signature.
    Existing quality decisions are read as overrides and are never written by this class.
    """

    def __init__(self, index):
        self.index = index
        self.root = index.root
        self.state = read_json(state_path(self.root))
        self.loaded_inventory_stamp = inventory_stamp(self.root)
        self.families = {}
        self.members = defaultdict(list)
        self.names = {}
        for uid, record in index.records.items():
            name = normalize_name(record.series_description)
            generic = not name or re.fullmatch(
                r"(?:\d+|(?:image|series|sequence|scan|unknown|unnamed)(?:-\d+)?)", name
            )
            family = fingerprint([VERSION, record.center, name, uid if generic else ""])[:24]
            self.families[uid] = family
            self.members[family].append(uid)
            self.names[family] = name
        self._catalogue = None
        self._exclusions = None
        self._readable = {}
        self.defaults = {u: default_classification(r) for u, r in index.records.items()}
        self._legacy_defaults = {}
        self.name_planes = {u: named_target_plane(r, "t1") for u, r in index.records.items()}
        self.flair_planes = {u: named_target_plane(r, "flair") for u, r in index.records.items()}
        self.flair_variants = {u: named_t2_flair_variant(r) for u, r in index.records.items()}
        self._edit_bases = {}
        self._subject_stamps = {}
        self._review_cache = None
        self._unreadable_groups = {}
        self._unreadable_state = None
        self._candidate_limits = {}
        self._candidate_limit_state = None
        self.unreadable_previews = set()
        # Read only the sparse error directory, never probe every source image.
        self.failed_previews = {
            p.stem
            for p in (self.root.parent / "errors").glob("*.json")
            if p.stem in self.index.records
            and read_json(p).get("state") == "failed"
            and (
                not (self.root.parent / "artifacts" / p.name).exists()
                or (self.root.parent / "artifacts" / p.name).stat().st_mtime_ns
                < p.stat().st_mtime_ns
            )
        }
        if index.config.nifti_import.enabled:
            for uid in self.failed_previews:
                record = index.records[uid]
                value = read_json(self.root.parent / "errors" / f"{uid}.json")
                if current_failure(value, record, nifti_source(index.config, record)):
                    self.unreadable_previews.add(uid)

    def preview_outcome(self, uid: str, failed: bool, *, unreadable: bool = False):
        unreadable = failed and unreadable
        if (uid in self.failed_previews) == failed and (
            uid in self.unreadable_previews
        ) == unreadable:
            return
        if failed:
            self.failed_previews = self.failed_previews | {uid}
        else:
            self.failed_previews = self.failed_previews - {uid}
        self.unreadable_previews = (
            self.unreadable_previews | {uid} if unreadable else self.unreadable_previews - {uid}
        )
        self.invalidate()

    def unreadable_candidates(self, subject: str, modality: str, state=None) -> list[str]:
        """Skip only an entire remaining round in this patient; never reuse a failure template."""
        state = self.state if state is None else state
        if self._unreadable_state is not state:
            self._unreadable_groups = {}
            self._unreadable_state = state
        key = (subject, modality)
        if key in self._unreadable_groups:
            return self._unreadable_groups[key]
        result = []
        if self.unreadable_previews and self.defaults_enabled(state):
            exclusions = self.exclusions(state)
            remaining = exclusions.round(subject, modality, automatic=False)
            _, scope = exclusions.scope(subject, modality)
            decision = self.index.decisions[subject]
            group = decision.get("groups", {}).get(modality, {})
            held = any(
                f"{subject}:{modality}" in state.get(name, {}) for name in ("recheck", "absent")
            )
            retained = state.get("manual_completed", {}).get(f"{subject}:{modality}", {})
            protected = retained.get("stamp") == self.subject_stamp(subject) if retained else False
            if (
                remaining
                and set(remaining).issubset(self.unreadable_previews)
                and not set(remaining).intersection(scope.get("deferred", []))
                and not (group.get("choice") or group.get("none") or held or protected)
                and not any(explicit(decision.get("candidates", {}).get(u)) for u in remaining)
            ):
                result = remaining
        self._unreadable_groups[key] = result
        return result

    def exclusions(self, state=None):
        state = self.state if state is None else state
        view = self._exclusions
        if view is None or view.state is not state:
            view = ExclusionView(self, state)
            self._exclusions = view
        return view

    def candidate_limits(self, subject: str, state: dict | None = None) -> dict:
        """Count images per existing name family, locally to one patient and modality.

        This is a workload exclusion, not evidence of bad image quality. Saved human
        final decisions remain authoritative; protocol priorities do not bypass the cap.
        No source file reads or geometry-based merging are needed here.
        """
        state = self.state if state is None else state
        if self._candidate_limit_state is not state:
            self._candidate_limits = {}
            self._candidate_limit_state = state
        if subject in self._candidate_limits:
            return self._candidate_limits[subject]
        result = {}
        if state.get("candidate_limit_version") == CANDIDATE_LIMIT_VERSION:
            pools = {m: defaultdict(list) for m in ("t1", "flair")}
            exclusions = self.exclusions(state)
            for uid in sorted(set(self.index.subjects[subject])):
                assigned = self._base_assignment(uid, state)
                modality = assigned["modality"]
                if (
                    modality in pools
                    and not assigned["default_excluded"]
                    and not exclusions.rule_excluded(uid, modality)
                ):
                    pools[modality][self.families[uid]].append(uid)
            for modality, families in pools.items():
                manual = self.index.decisions[subject].get("groups", {}).get(modality, {})
                if manual.get("choice") or manual.get("none"):
                    continue
                fields = [
                    {"family_id": f, "name": self.names[f], "count": len(ids), "ids": ids}
                    for f, ids in sorted(families.items())
                    if len(ids) >= CANDIDATE_LIMIT
                ]
                if fields:
                    result[modality] = {
                        "reason": "candidate_field_limit",
                        "version": CANDIDATE_LIMIT_VERSION,
                        "threshold": CANDIDATE_LIMIT,
                        "operator": ">=",
                        "fields": fields,
                        # One excessive field skips this modality, including other fields.
                        "candidate_ids": sorted(u for ids in families.values() for u in ids),
                    }
        self._candidate_limits[subject] = result
        return result

    def reload(self):
        state = read_json(state_path(self.root))
        if state != self.state:
            self.state = state
            self.invalidate()

    def invalidate(self):
        self._catalogue = None
        self._exclusions = None
        self._review_cache = None
        self._unreadable_groups = {}
        self._unreadable_state = None
        self._candidate_limits = {}
        self._candidate_limit_state = None
        self._edit_bases = {}
        self._legacy_defaults = {}

    def defaults_enabled(self, state=None):
        return (self.state if state is None else state).get("defaults_version") in {
            "sequence-defaults-2",
            "sequence-defaults-3",
            "sequence-defaults-4",
            "sequence-defaults-5",
            "sequence-defaults-6",
            "sequence-defaults-7",
            CLASSIFICATION_VERSION,
        }

    def preferred_t1(self, subject: str, state: dict, values: dict | None = None) -> list[str]:
        return self.preferred_axial(subject, "t1", state, values)

    def axial_selection_reason(self, modality: str, state: dict) -> str:
        suffix = (
            "axial_last"
            if state.get("defaults_version")
            in {"sequence-defaults-6", "sequence-defaults-7", CLASSIFICATION_VERSION}
            else "tra_over_sag"
        )
        return f"{modality}_{suffix}"

    def preferred_axial(
        self, subject: str, modality: str, state: dict, values: dict | None = None
    ) -> list[str]:
        """Prefer named axial targets without requiring other candidates to be resolved."""
        if state.get("defaults_version") not in {
            "sequence-defaults-3",
            "sequence-defaults-4",
            "sequence-defaults-5",
            "sequence-defaults-6",
            "sequence-defaults-7",
            CLASSIFICATION_VERSION,
        } or (
            modality == "flair"
            and state.get("defaults_version")
            not in {
                "sequence-defaults-5",
                "sequence-defaults-6",
                "sequence-defaults-7",
                CLASSIFICATION_VERSION,
            }
        ):
            return []
        decision = self.index.decisions[subject]
        key = f"{subject}:{modality}"
        manual = decision.get("groups", {}).get(modality, {})
        if manual.get("choice") or manual.get("none") or key in state.get("recheck", {}):
            return []
        if key in state.get("absent", {}):
            return []
        values = (
            values
            if values is not None
            else {u: self.assignment(u, state) for u in self.index.subjects[subject]}
        )
        candidates = [
            u
            for u, a in values.items()
            if a["modality"] == modality and modality not in a.get("excluded_modalities", [])
        ]
        _, scope = self.exclusions(state).scope(subject, modality)
        planes = self.name_planes if modality == "t1" else self.flair_planes
        axial = [u for u in candidates if planes[u] == "tra"]
        if state.get("defaults_version") in {
            "sequence-defaults-6",
            "sequence-defaults-7",
            CLASSIFICATION_VERSION,
        }:
            # Keep deliberate human rankings and holds, not automatic candidate guards.
            if any(
                state.get("templates", {}).get(self.families[u], {}).get("priority", 100) != 100
                for u in candidates
            ):
                best = min(values[u]["priority"] for u in candidates)
                axial = [u for u in axial if values[u]["priority"] == best]
            if any(
                u in scope.get("deferred", []) or self.families[u] in scope.get("templates", {})
                for u in axial
            ):
                return []
            return (
                [max(axial, key=lambda u: axial_candidate_order(self.index.records[u]))]
                if axial
                else []
            )
        # Historical states retain their previous interpretation until catalog migration.
        if any(
            values[u]["classification_source"] != "default"
            or u in scope.get("deferred", [])
            or u in self.failed_previews
            or self.families[u] in scope.get("templates", {})
            for u in candidates
        ):
            return []
        if {planes[u] for u in candidates} != {"sag", "tra"}:
            return []
        return axial

    def automatic_candidates(
        self, subject: str, modality: str, state: dict, values: dict
    ) -> list[str]:
        """New tie-breaks never replace a saved human rule, decision or hold."""
        if state.get("defaults_version") != CLASSIFICATION_VERSION:
            return []
        key = f"{subject}:{modality}"
        decision = self.index.decisions[subject]
        manual = decision.get("groups", {}).get(modality, {})
        retained = state.get("manual_completed", {}).get(key, {})
        if (
            manual.get("choice")
            or manual.get("none")
            or key in state.get("recheck", {})
            or key in state.get("absent", {})
            or (retained and retained.get("stamp") == self.subject_stamp(subject))
        ):
            return []
        candidates = [
            u
            for u, a in values.items()
            if a["modality"] == modality
            and not a["default_excluded"]
            and modality not in a.get("excluded_modalities", [])
        ]
        _, scope = self.exclusions(state).scope(subject, modality)
        if any(
            values[u]["classification_source"] != "default"
            or u in scope.get("deferred", [])
            or self.families[u] in scope.get("templates", {})
            for u in candidates
        ):
            return []
        return candidates

    def last_per_family(self, candidates: list[str]) -> list[str]:
        families = defaultdict(list)
        for uid in candidates:
            families[self.families[uid]].append(uid)
        return [
            max(ids, key=lambda u: axial_candidate_order(self.index.records[u]))
            for _, ids in sorted(families.items())
        ]

    def preferred_et2_flair(self, subject: str, state: dict, values: dict) -> list[str]:
        candidates = self.automatic_candidates(subject, "flair", state, values)
        enhanced = [u for u in candidates if self.flair_variants[u] == "et2"]
        if not enhanced or not any(self.flair_variants[u] == "t2" for u in candidates):
            return []
        # Do not use this new preference to hide a known failed preview.
        if any(u in self.failed_previews for u in enhanced):
            return []
        axial = [u for u in enhanced if self.flair_planes[u] == "tra"]
        return (
            [max(axial, key=lambda u: axial_candidate_order(self.index.records[u]))]
            if axial
            else self.last_per_family(enhanced)
        )

    def preferred_candidates(
        self, subject: str, modality: str, state: dict, values: dict | None = None
    ) -> tuple[list[str], str]:
        values = (
            values
            if values is not None
            else {u: self.assignment(u, state) for u in self.index.subjects[subject]}
        )
        if modality == "flair":
            preferred = self.preferred_et2_flair(subject, state, values)
            if preferred:
                return preferred, "flair_et2_over_t2"
        preferred = self.preferred_axial(subject, modality, state, values)
        if preferred:
            return preferred, self.axial_selection_reason(modality, state)
        candidates = self.automatic_candidates(subject, modality, state, values)
        if any(u in self.failed_previews for u in candidates):
            return [], ""
        preferred = self.last_per_family(candidates)
        # Distinct protocols stay distinct. The >= 4 field cap ran before this point.
        if len(preferred) < len(candidates):
            return preferred, f"{modality}_same_name_last"
        return [], ""

    def subject_stamp(self, subject):
        if subject not in self._subject_stamps:
            self._subject_stamps[subject] = fingerprint(
                [
                    (u, record_digest(self.index.records[u]))
                    for u in sorted(self.index.subjects[subject])
                ]
            )
        return self._subject_stamps[subject]

    def retain_manual_completions(self, state, groups):
        """Freeze only genuinely human-resolved members, not an entire partly reviewed group."""
        retained = state.setdefault("manual_completed", {})
        for group in groups:
            modality = group["modality"]
            pending = set(group["pending_subjects"])
            for subject in group["subjects"]:
                if (
                    subject in pending
                    or subject in group.get("unreadable_subjects", [])
                    or subject in group.get("candidate_limit_subjects", [])
                ):
                    continue
                key = f"{subject}:{modality}"
                uids = self.index.subjects[subject]
                families = {
                    self.families[u]
                    for u in uids
                    if self.assignment(u, state)["modality"] == modality
                    and not self.exclusions(state).excluded(u, modality)
                }
                manual = self.index.decisions[subject].get("groups", {}).get(modality, {})
                _, scope = self.exclusions(state).scope(subject, modality)
                known = families and all(f in state.get("templates", {}) for f in families)
                missing = key in state.get("absent", {}) or (
                    scope.get("templates")
                    and not self.exclusions(state).round(subject, modality, automatic=False)
                )
                if known or missing or manual.get("choice") or manual.get("none"):
                    retained[key] = {"stamp": self.subject_stamp(subject), "source": "manual"}

    def default_for(self, uid, state):
        version = state.get("defaults_version", CLASSIFICATION_VERSION)
        if version == CLASSIFICATION_VERSION:
            return self.defaults[uid]
        key = uid, version
        if key not in self._legacy_defaults:
            self._legacy_defaults[key] = default_classification(
                self.index.records[uid], version=version
            )
        return self._legacy_defaults[key]

    def default_excluded(self, uid, state=None):
        state = self.state if state is None else state
        if not self.defaults_enabled(state) or not self.default_for(uid, state)["excluded"]:
            return False
        record = self.index.records[uid]
        rating = self.index.decisions[record.subject_id].get("candidates", {}).get(uid)
        saved = state.get("templates", {}).get(self.families[uid], {})
        if any(
            uid
            in state.get("recheck", {}).get(f"{record.subject_id}:{m}", {}).get("candidates", [])
            for m in ("t1", "flair")
        ):
            return False
        # A deliberate human correction always takes precedence over name defaults.
        return not (explicit(rating) or saved.get("modality") in {"t1", "flair"})

    def clear_completion(self, state, subjects, modalities=("t1", "flair")):
        for subject in subjects:
            for modality in modalities:
                state.get("manual_completed", {}).pop(f"{subject}:{modality}", None)

    def enable(self):
        self.require_current_inventory()
        if not self.state:
            self._save(
                {
                    "version": VERSION,
                    "revision": 0,
                    "phase": "identification",
                    "templates": {},
                    "absent": {},
                    "defaults_version": CLASSIFICATION_VERSION,
                    "candidate_limit_version": CANDIDATE_LIMIT_VERSION,
                },
                "enable",
            )
        else:
            if self.state.get("defaults_version") != CLASSIFICATION_VERSION:
                state = copy.deepcopy(self.state)
                backup = (
                    self.root / "identification_backups" / f"defaults-{state['revision']:09d}.json"
                )
                if not backup.exists():
                    atomic_write_json(backup, state)
                self.retain_manual_completions(state, self._groups(state))
                state.update(defaults_version=CLASSIFICATION_VERSION, phase="identification")
                self._save(state, "default_classification_upgrade")
            elif self.state.get("inventory_stamp") != inventory_stamp(self.root):
                self._save(dict(self.state, phase="identification"), "inventory_changed")
            if self.state.get("candidate_limit_version") != CANDIDATE_LIMIT_VERSION:
                backup = (
                    self.root
                    / "identification_backups"
                    / f"candidate-limit-{self.state['revision']:09d}.json"
                )
                if not backup.exists():
                    atomic_write_json(backup, self.state)
                self._save(
                    dict(
                        self.state,
                        candidate_limit_version=CANDIDATE_LIMIT_VERSION,
                        phase="identification",
                    ),
                    "candidate_field_limit_upgrade",
                )
        return self.catalogue()

    def _base_assignment(self, uid, state=None):
        state = self.state if state is None else state
        record = self.index.records[uid]
        saved = state.get("templates", {}).get(self.families[uid], {})
        rating = self.index.decisions[record.subject_id].get("candidates", {}).get(uid)
        manual = explicit(rating)
        default = (
            self.default_for(uid, state)
            if self.defaults_enabled(state)
            else {
                "modality": record.candidate_type,
                "confidence": record.classification_confidence,
                "reason": "legacy_inventory",
                "version": "legacy",
            }
        )
        return {
            "modality": effective_modality(record, rating)
            if manual
            else saved.get("modality", default["modality"]),
            "priority": saved.get("priority", 100),
            "manual": manual,
            "template_id": self.index.templates[uid]["id"],
            "family_id": self.families[uid],
            "default_classification": default,
            "default_excluded": self.default_excluded(uid, state),
            "classification_source": "manual_image"
            if manual
            else "manual_rule"
            if saved
            else "default",
        }

    def assignment(self, uid, state=None):
        state = self.state if state is None else state
        record = self.index.records[uid]
        limits = self.candidate_limits(record.subject_id, state)
        return {
            **self._base_assignment(uid, state),
            "excluded_modalities": [
                m for m in ("t1", "flair") if self.exclusions(state).excluded(uid, m)
            ],
            "unreadable_excluded_modalities": [
                m
                for m in ("t1", "flair")
                if uid in self.unreadable_candidates(record.subject_id, m, state)
            ],
            "candidate_limit_excluded_modalities": [
                m for m, detail in limits.items() if uid in detail["candidate_ids"]
            ],
        }

    def _groups(self, state):
        groups = {}
        exclusions = self.exclusions(state)
        for subject, uids in sorted(self.index.subjects.items()):
            center = self.index.records[uids[0]].center
            values = {u: self.assignment(u, state) for u in uids}
            for modality in ("t1", "flair"):
                preferred, _ = self.preferred_candidates(subject, modality, state, values)
                candidates = [
                    u
                    for u in uids
                    if values[u]["modality"] == modality
                    and not exclusions.rule_excluded(u, modality)
                ]
                families = sorted({self.families[u] for u in candidates})
                sid, scope = exclusions.scope(subject, modality)
                key = sid or fingerprint([VERSION, center, modality, families])[:24]
                group = groups.setdefault(
                    key,
                    {
                        "id": key,
                        "center": center,
                        "modality": modality,
                        "families": families,
                        "subjects": [],
                        "pending_subjects": [],
                        "reason": "",
                        "repeat_subjects": 0,
                        "auto_skipped_subjects": 0,
                        "unreadable_subjects": [],
                        "candidate_limit_subjects": [],
                        "completion_counts": Counter(),
                    },
                )
                group["subjects"].append(subject)
                group["repeat_subjects"] += int(
                    len(candidates) > len(families)
                    and not self.unreadable_candidates(subject, modality, state)
                    and modality not in self.candidate_limits(subject, state)
                )
                manual = self.index.decisions[subject].get("groups", {}).get(modality, {})
                known = all(f in state.get("templates", {}) for f in families)
                automatic = len(
                    candidates if self.defaults_enabled(state) else families
                ) == 1 and all(
                    self.assignment(u, state)["default_classification"]["confidence"] == "high"
                    for u in candidates
                )
                if preferred:
                    automatic = len(preferred) == 1
                missing_done = f"{subject}:{modality}" in state.get("absent", {})
                remaining = exclusions.round(subject, modality)
                negative_done = bool(scope) and not exclusions.round(
                    subject, modality, automatic=False
                )
                default_done = self.defaults_enabled(state) and not remaining
                target_identified = bool(families) and (known or automatic)
                # Once target protocols are settled, optional non-target images
                # cannot keep this participant in the identification queue. Preserve
                # their saved defers/errors; only target issues still block completion.
                relevant = preferred or (candidates if target_identified else remaining)
                deferred = bool(set(relevant) & set(scope.get("deferred", []))) or bool(
                    not preferred and scope and set(relevant) & self.failed_previews
                )
                recheck = f"{subject}:{modality}" in state.get("recheck", {})
                conflicted = any(
                    self.families[u] in scope.get("templates", {})
                    and exclusions.positive(u, modality)
                    for u in (preferred or uids)
                )
                if negative_done and subject not in scope.get("reviewed_subjects", []):
                    group["auto_skipped_subjects"] += 1
                retained = state.get("manual_completed", {}).get(f"{subject}:{modality}", {})
                protected = bool(retained) and retained.get("stamp") == self.subject_stamp(subject)
                if modality in self.candidate_limits(subject, state):
                    group["candidate_limit_subjects"].append(subject)
                    group["completion_counts"]["candidate_limit_skipped"] += 1
                    continue
                if manual.get("choice") or manual.get("none") or (protected and not recheck):
                    group["completion_counts"]["manual_completed"] += 1
                    continue
                if self.unreadable_candidates(subject, modality, state):
                    group["unreadable_subjects"].append(subject)
                    group["completion_counts"]["unreadable_skipped"] += 1
                    continue
                if (
                    not deferred
                    and not conflicted
                    and not recheck
                    and (
                        target_identified
                        or (not families and (missing_done or negative_done or default_done))
                    )
                ):
                    label = (
                        "manual_completed"
                        if known and families or missing_done or negative_done
                        else ("automatic_unique" if target_identified else "default_skipped")
                    )
                    group["completion_counts"][label] += 1
                    continue
                group["pending_subjects"].append(subject)
                group["reason"] = "missing" if not families else "ambiguous_protocol"
                label = "multiple_candidates" if len(candidates) > 1 else "unknown_pending"
                group["completion_counts"][label] += 1
        for group in groups.values():
            pool = group["pending_subjects"] or group["subjects"]
            actionable = [
                s
                for s in pool
                if any(
                    u not in exclusions.scope(s, group["modality"])[1].get("deferred", [])
                    and u not in self.failed_previews
                    for u in exclusions.round(s, group["modality"])
                )
            ]
            pool = actionable or pool
            rated = [s for s in pool if self.index.decisions[s].get("revision")]
            group["representative"] = (rated or pool)[0]
            group["count"] = len(group["subjects"])
            group["pending_count"] = len(group["pending_subjects"])
            group["needs_protocol"] = bool(group["pending_count"])
            # Frozen scopes can contain corrected and still-missing subjects. Render only
            # the representative's candidate families, not an unavailable union.
            if group["id"] in exclusions.scopes:
                group["families"] = sorted(
                    {
                        self.families[u]
                        for u in self.index.subjects[group["representative"]]
                        if self.assignment(u, state)["modality"] == group["modality"]
                        and not exclusions.excluded(u, group["modality"])
                    }
                )
            group.update(exclusions.annotations(group))
            preferred, reason = self.preferred_candidates(
                group["representative"], group["modality"], state
            )
            preferred_families = {self.families[u] for u in preferred}
            group["default_selection_reason"] = reason
            group["templates"] = []
            for family in group["families"]:
                sample = self.index.records[self.members[family][0]]
                value = state.get("templates", {}).get(family, {})
                group["templates"].append(
                    {
                        "id": family,
                        "name": self.names[family],
                        "example_name": sample.series_description,
                        "modality": value.get("modality", group["modality"]),
                        "priority": value.get(
                            "priority", 0 if family in preferred_families else 100
                        ),
                        "geometry_variants": len(
                            {self.index.templates[u]["id"] for u in self.members[family]}
                        ),
                    }
                )
        return sorted(
            groups.values(),
            key=lambda g: (
                not g["needs_protocol"],
                -g["pending_count"],
                g["center"],
                g["modality"],
                g["id"],
            ),
        )

    def catalogue(self):
        if self._catalogue is None:
            groups = self._groups(self.state)
            counts = {}
            for modality in ("t1", "flair"):
                subset = [g for g in groups if g["modality"] == modality]
                counts[modality] = {
                    "groups": len(subset),
                    "pending_groups": sum(g["needs_protocol"] for g in subset),
                    "pending_subjects": sum(g["pending_count"] for g in subset),
                    "repeat_subjects_for_quality": sum(g["repeat_subjects"] for g in subset),
                    "auto_skipped_subjects": sum(g["auto_skipped_subjects"] for g in subset),
                    "excluded_templates": sum(g["excluded_template_count"] for g in subset),
                    **{
                        label: sum(g["completion_counts"].get(label, 0) for g in subset)
                        for label in (
                            "manual_completed",
                            "automatic_unique",
                            "default_skipped",
                            "unreadable_skipped",
                            "candidate_limit_skipped",
                            "multiple_candidates",
                            "unknown_pending",
                        )
                    },
                }
            self._catalogue = {
                "version": VERSION,
                "revision": self.state["revision"],
                "phase": self.state["phase"],
                "defaults_version": self.state.get("defaults_version", "legacy"),
                "candidate_limit": CANDIDATE_LIMIT,
                "candidate_limit_version": self.state.get("candidate_limit_version", "legacy"),
                "default_excluded_series": sum(
                    self.default_excluded(u) for u in self.index.records
                ),
                "subjects": len(self.index.subjects),
                "groups": groups,
                "counts": counts,
                "pending_groups": sum(g["needs_protocol"] for g in groups),
            }
        return self._catalogue

    def summary(self):
        return {k: v for k, v in self.catalogue().items() if k != "groups"}

    def group(self, gid):
        group = next((g for g in self.catalogue()["groups"] if g["id"] == gid), None)
        if group is None:
            raise ValueError("序列分组已变化，请刷新列表")
        return group

    def subject_groups(self, subject):
        return [g for g in self.catalogue()["groups"] if subject in g["subjects"]]

    def edit_basis(self, gid: str) -> str:
        """Scope-specific optimistic guard for a decision viewed before another job finishes."""
        if gid not in self._edit_bases:
            group = self.group(gid)
            subjects = set(group["subjects"])
            families = {self.families[u] for s in subjects for u in self.index.subjects[s]}
            # Template values are global. Publication separately checks image-level
            # conflicts in every affected member, including other candidate combinations.
            state = self.state
            self._edit_bases[gid] = fingerprint(
                {
                    "inventory": self.loaded_inventory_stamp,
                    "members": group["subjects"],
                    "families": group["families"],
                    "phase": state["phase"],
                    "defaults": state.get("defaults_version"),
                    "limit": state.get("candidate_limit_version"),
                    "templates": {f: state.get("templates", {}).get(f) for f in sorted(families)},
                    "scopes": {
                        k: v
                        for k, v in state.get("negative_scopes", {}).items()
                        if set(v["subjects"]) & subjects
                    },
                    "decisions": {s: self.index.decisions[s] for s in sorted(subjects)},
                    "holds": {
                        name: {
                            k: v
                            for k, v in state.get(name, {}).items()
                            if k.rsplit(":", 1)[0] in subjects
                        }
                        for name in ("absent", "recheck", "manual_completed")
                    },
                }
            )
        return self._edit_bases[gid]

    def list_subjects(self, filters):
        groups = self.catalogue()["groups"]
        if filters.get("queue") in {"unreadable", "candidate_limit"}:
            limited = filters.get("queue") == "candidate_limit"
            query = filters.get("q", "").lower()
            center = filters.get("center", "").lower()
            rows = [
                {
                    "id": subject,
                    "identification_group": group["id"],
                    "center": group["center"],
                    "modality": group["modality"],
                    "count": 1,
                    "pending_count": 0,
                    "reason": "candidate_field_limit" if limited else "all_candidates_unreadable",
                    "candidate_limit": self.candidate_limits(subject).get(group["modality"])
                    if limited
                    else None,
                    "needs_protocol": False,
                }
                for group in groups
                for subject in group[
                    "candidate_limit_subjects" if limited else "unreadable_subjects"
                ]
                if (not center or center in group["center"].lower())
                and (not query or query in subject.lower())
            ]
            offset = max(0, int(filters.get("offset", 0)))
            return {
                "total": len(rows),
                "subjects": rows[offset : offset + 100],
                "unit": "subject_modalities",
                "identification": self.summary(),
            }
        pending = filters.get("queue") != "identified"
        groups = [g for g in groups if not pending or g["needs_protocol"]]
        query, center = filters.get("q", "").lower(), filters.get("center", "").lower()
        rows = []
        for g in groups:
            if center and center not in g["center"].lower():
                continue
            if (
                query
                and query
                not in " ".join(
                    [g["representative"], g["modality"], *[e["name"] for e in g["templates"]]]
                ).lower()
            ):
                continue
            rows.append(
                {
                    "id": g["representative"],
                    "identification_group": g["id"],
                    "center": g["center"],
                    "modality": g["modality"],
                    "count": g["count"],
                    "pending_count": g["pending_count"],
                    "reason": g["reason"],
                    "needs_protocol": g["needs_protocol"],
                }
            )
        offset = max(0, int(filters.get("offset", 0)))
        return {
            "total": len(rows),
            "subjects": rows[offset : offset + 100],
            "unit": "protocol_groups",
            "identification": self.summary(),
        }

    def preview(self, payload):
        self.require_current_inventory()
        if payload.get("revision") != self.state["revision"]:
            raise ConflictError("序列规则已变化，请刷新后重试")
        if self.state["phase"] != "identification":
            raise ValueError("请先返回序列识别阶段，再修改协议")
        group = self.group(payload.get("group"))
        subject = payload.get("subject")
        if subject not in group["subjects"]:
            raise ValueError("代表病例不属于当前组")
        raw = payload.get("templates", {})
        if not isinstance(raw, dict):
            raise ValueError("invalid sequence rules")
        available = {self.families[u] for u in self.index.subjects[subject]}
        negative_action = any(
            k in payload for k in ("negative_templates", "revoke_negative", "deferred_candidates")
        ) or any(isinstance(e, dict) and e.get("modality") == "other" for e in raw.values())
        if (not negative_action and not set(group["families"]).issubset(raw)) or not set(
            raw
        ).issubset(available):
            raise ValueError("保留本组模板；新增纠错序列须来自当前患者")
        state = copy.deepcopy(self.state)
        edits = {}
        for family, entry in raw.items():
            modality, priority = entry.get("modality"), entry.get("priority", 100)
            if modality not in {group["modality"], "other"}:
                raise ValueError("本组仅识别目标模态；非目标候选可移出，其他序列不要求分类")
            if type(priority) is not int or not 0 <= priority <= 999:
                raise ValueError("优先级须为 0 到 999 的整数，越小越优先")
            edits[family] = {"modality": modality, "priority": priority}
        winners = [e["priority"] for e in edits.values() if e["modality"] == group["modality"]]
        if not winners and not payload.get("absent") and not negative_action:
            raise ValueError("请加入正确序列，或明确本例未找到目标序列")
        if winners and Counter(winners)[min(winners)] > 1 and not payload.get("compare_in_quality"):
            raise ValueError("同优先级有不同协议，请设置优先级或确认留待质量阶段比较")
        if payload.get("absent"):
            if group["families"] or winners:
                raise ValueError("无目标序列只能逐例确认空候选组；不能批量判定其他患者缺失")
            state["absent"][f"{subject}:{group['modality']}"] = str(payload.get("reason", ""))
        # A target-negative rule must never relabel a real FLAIR as 'other' while
        # searching for T1 (or vice versa). Keep it separate from positive assignments.
        negatives = [f for f, e in edits.items() if e["modality"] == "other"]
        negative_payload = dict(payload)
        if negatives:
            negative_payload["negative_templates"] = sorted(
                set(payload.get("negative_templates", [])) | set(negatives)
            )
            negative_action = True
        negative_ids = negative_payload.get("negative_templates", [])
        if set(negative_ids) & {f for f, e in edits.items() if e["modality"] != "other"}:
            raise ValueError("同一模板不能同时识别为目标和排除")
        source_policy = payload.get("negative_source_policy", "require_readable")
        if source_policy not in ("require_readable", "identity_only"):
            raise ValueError("invalid negative source policy")
        image_stamps = self.check_negative_sources(subject, negative_ids, policy=source_policy)
        exclusions = ExclusionView(self, state)
        negative_affected, negative_conflicts = set(), []
        if negative_action:
            negative_affected, negative_conflicts = exclusions.edit(
                group, subject, negative_payload
            )
        for f, entry in edits.items():
            if entry["modality"] != "other":
                for scope in state.get("negative_scopes", {}).values():
                    if scope["modality"] == entry["modality"] and f in scope["templates"]:
                        raise ConflictError("该模板已有同模态排除规则；请先撤回排除再确认识别")
                state["templates"][f] = entry
                prior = self.state.get("templates", {}).get(f, {})
                if prior.get("modality") and prior["modality"] != entry["modality"]:
                    self.clear_completion(
                        state, {self.index.records[u].subject_id for u in self.members[f]}
                    )
        affected = {
            self.index.records[u].subject_id
            for f, e in edits.items()
            if e["modality"] != "other"
            for u in self.members[f]
        }
        affected |= negative_affected
        if payload.get("absent"):
            affected.add(subject)
        self.clear_completion(state, affected, (group["modality"],))
        # Explicit identification resolves a forced recheck only for the reviewed target/items.
        resolved = set(edits) | set(negative_ids)
        for owner in affected:
            key = f"{owner}:{group['modality']}"
            hold = state.get("recheck", {}).get(key)
            if not hold:
                continue
            if winners or (payload.get("absent") and owner == subject):
                state["recheck"].pop(key)
            else:
                remaining = [
                    u for u in hold.get("candidates", []) if self.families.get(u) not in resolved
                ]
                if remaining:
                    hold["candidates"] = remaining
                else:
                    state["recheck"].pop(key)
        conflicts = list(negative_conflicts)
        for f, entry in edits.items():
            if entry["modality"] == "other":
                continue
            for uid in self.members[f]:
                record = self.index.records[uid]
                rating = self.index.decisions[record.subject_id].get("candidates", {}).get(uid)
                if explicit(rating) and effective_modality(record, rating) != entry["modality"]:
                    conflicts.append(
                        {"subject": record.subject_id, "reason": "manual_modality_conflict"}
                    )
        after = self._groups(state)
        result = {
            "affected_subjects": len(affected),
            "conflicts": conflicts,
            "pending_groups_after": sum(g["needs_protocol"] for g in after),
            "changed_templates": edits,
            "negative_templates": negative_ids,
            "negative_source_policy": source_policy,
            "failed_preview_candidates": sorted(
                u
                for u in self.index.subjects[subject]
                if self.families[u] in negative_ids and u in self.failed_previews
            ),
            "source_stamps": image_stamps,
            "auto_skipped_subjects_after": sum(g["auto_skipped_subjects"] for g in after),
            "pending_subjects_after": sum(g["pending_count"] for g in after),
            "next_group": next(
                (
                    g["id"]
                    for g in after
                    if g["needs_protocol"]
                    and subject in g["subjects"]
                    and g["modality"] == group["modality"]
                ),
                None,
            ),
            "quality_copied": False,
            "state": state,
        }
        clean = {k: v for k, v in payload.items() if k != "preview_digest"}
        result["preview_digest"] = fingerprint(
            [
                clean,
                result,
                self.index.inventory_digest,
                self.index.decisions,
                sorted(self.failed_previews),
            ]
        )
        return result

    def publish(self, payload):
        result = self.preview(payload)
        if result["preview_digest"] != payload.get("preview_digest"):
            raise ConflictError("请先预览当前规则影响，再发布")
        if result["conflicts"]:
            raise ConflictError("存在人工分类冲突；原人工决定保留，请先核对冲突病例")
        if not str(payload.get("reviewer", "")).strip():
            raise ValueError("请填写审核者")
        self.retain_manual_completions(result["state"], self._groups(result["state"]))
        self._save(
            result["state"],
            "publish",
            dict(
                payload,
                negative_source_stamps=result["source_stamps"],
                negative_failed_previews=result["failed_preview_candidates"],
            ),
        )
        return {
            "affected_subjects": result["affected_subjects"],
            "next_group": result["next_group"],
            **self.summary(),
        }

    def check_negative_sources(
        self, subject: str, families: list[str], *, policy: str = "require_readable"
    ) -> dict:
        """New explicit identity decisions need no voxel read; old rules stay unchanged."""
        stamps = {}
        for uid in self.index.subjects[subject]:
            if self.families[uid] not in families:
                continue
            path = nifti_source(self.index.config, self.index.records[uid])
            if policy == "identity_only":
                # Bind preview/publish to available file metadata, without claiming
                # the image is readable or changing its failure/quality record.
                try:
                    stat = path.stat()
                    stamps[uid] = [str(path), stat.st_size, stat.st_mtime_ns]
                except OSError as exc:
                    stamps[uid] = {
                        "path": str(path),
                        "stat_error": type(exc).__name__,
                        "errno": exc.errno,
                    }
                continue
            if uid in self.failed_previews:
                raise ValueError(f"读取失败，不能排除；请先重试预览: {uid}")
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            if self._readable.get(uid) != key:
                try:
                    image, _ = check_image(path)
                    # Last voxel forces a compressed file's payload to be readable, not
                    # merely its header. No resampling or quality certification occurs.
                    image.dataobj[tuple(n - 1 for n in image.shape)]
                except (OSError, ValueError, EOFError, ImageFileError) as exc:
                    raise ValueError(f"读取失败，不能排除；请标记待定: {uid}: {exc}") from exc
                self._readable[uid] = key
            stamps[uid] = list(key)
        return stamps

    def transition(self, payload):
        self.require_current_inventory()
        if payload.get("revision") != self.state["revision"]:
            raise ConflictError("阶段或序列规则已变化，请刷新")
        target = payload.get("phase")
        if target not in {"identification", "quality"}:
            raise ValueError("unknown workflow stage")
        if target == "quality":
            from .qc_jobs import assert_no_pending

            assert_no_pending(self.root)
        self.invalidate()
        if target == "quality" and self.catalogue()["pending_groups"]:
            raise ValueError("仍有待识别组；先完成序列识别，再进入质量检查")
        state = dict(self.state, phase=target)
        self._save(state, "transition", payload)
        return self.summary()

    def review_rules(self, filters):
        from .qc_rule_review import list_rules

        return list_rules(self, filters)

    def revoke_preview(self, payload):
        from .qc_rule_review import revoke_preview

        return revoke_preview(self, payload)

    def revoke_publish(self, payload):
        from .qc_rule_review import revoke_publish

        return revoke_publish(self, payload)

    def require_current_inventory(self):
        if inventory_stamp(self.root) != self.loaded_inventory_stamp:
            raise ConflictError("清单在本次会话中发生变化，请重启阅片器或重新运行 catalog")

    def _save(self, state, action, payload=None):
        state = dict(
            state,
            revision=self.state.get("revision", 0) + 1,
            parent_revision=self.state.get("revision", 0),
            saved_at=utc_now(),
            inventory_stamp=inventory_stamp(self.root),
        )
        history = dict(state, action=action, request=payload or {})
        path = self.root / "identification_history" / f"{state['revision']:09d}.json"
        if path.exists():
            # Do not overwrite a history entry left by an interrupted atomic publish.
            state["revision"] = max(int(p.stem) for p in path.parent.glob("*.json")) + 1
            history["revision"] = state["revision"]
            path = path.with_name(f"{state['revision']:09d}.json")
        atomic_write_json(path, history)
        atomic_write_json(state_path(self.root), state)
        self.state = state
        self.invalidate()
        atomic_write_json(self.root / "identification_catalogue.json", self.catalogue())
