"""Modality-specific negative identification, never a quality decision."""

from __future__ import annotations

from collections import defaultdict

from .qc_protocols import explicit, fingerprint
from .qc_state import effective_modality


class ExclusionView:
    def __init__(self, identify, state: dict):
        self.identify = identify
        self.state = state
        self.scopes = state.get("negative_scopes", {})
        self.by_subject: dict[tuple[str, str], str] = {}
        for sid, scope in self.scopes.items():
            for subject in scope["subjects"]:
                self.by_subject[subject, scope["modality"]] = sid

    def scope(self, subject: str, modality: str) -> tuple[str | None, dict]:
        sid = self.by_subject.get((subject, modality))
        return sid, self.scopes.get(sid, {})

    def positive(self, uid: str, modality: str) -> bool:
        index = self.identify.index
        record = index.records[uid]
        decision = index.decisions[record.subject_id]
        rating = decision.get("candidates", {}).get(uid)
        return bool(
            (explicit(rating) and effective_modality(record, rating) == modality)
            or decision.get("groups", {}).get(modality, {}).get("choice") == uid
        )

    def excluded(self, uid: str, modality: str) -> bool:
        record = self.identify.index.records[uid]
        _, scope = self.scope(record.subject_id, modality)
        return (
            self.identify.families[uid] in scope.get("templates", {})
            and uid not in scope.get("deferred", [])
            and uid not in self.identify.failed_previews
            and not self.positive(uid, modality)
        )

    def round(self, subject: str, modality: str) -> list[str]:
        return sorted(
            u for u in self.identify.index.subjects[subject] if not self.excluded(u, modality)
        )

    def edit(self, group: dict, subject: str, payload: dict) -> tuple[set[str], list[dict]]:
        """Edit a proposed state only. Membership never follows later regrouping."""
        identify, index = self.identify, self.identify.index
        negative = payload.get("negative_templates", [])
        revoke = payload.get("revoke_negative", [])
        deferred = payload.get("deferred_candidates")
        for values in (negative, revoke, deferred or []):
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError("invalid negative identification list")
        available = {identify.families[u] for u in index.subjects[subject]}
        if not set(negative).issubset(available):
            raise ValueError("否定模板必须来自当前患者")
        if set(negative) & set(revoke):
            raise ValueError("不能同时排除和撤回同一模板")
        sid, existing = self.scope(subject, group["modality"])
        if not set(revoke).issubset(existing.get("templates", {})):
            raise ValueError("只能撤回本组已有的排除模板")
        if deferred is not None and not set(deferred).issubset(index.subjects[subject]):
            raise ValueError("待定影像必须来自当前患者")
        if not sid:
            sid = fingerprint(["negative-scope-1", group["id"], self.state["revision"]])[:24]
            # Only unscoped members of the current group can enter a new frozen scope.
            existing = {
                "modality": group["modality"],
                "center": group["center"],
                "subjects": sorted(group["subjects"]),
                "templates": {},
                "deferred": [],
                "reviewed_subjects": [],
            }
            self.state.setdefault("negative_scopes", {})[sid] = existing
        scope = existing
        if deferred is not None:
            scope["deferred"] = sorted(
                (set(scope.get("deferred", [])) - set(index.subjects[subject])) | set(deferred)
            )
        blocked = {
            identify.families[u]
            for u in scope.get("deferred", [])
            if u in index.records and index.records[u].subject_id == subject
        }
        if blocked & set(negative):
            raise ValueError("待定或读取失败的模板不能批量排除；请先恢复预览并取消待定")
        conflicts, affected = [], set()
        members = set(scope["subjects"])
        for family in negative:
            for uid in identify.members[family]:
                owner = index.records[uid].subject_id
                if owner not in members:
                    continue
                affected.add(owner)
                if self.positive(uid, group["modality"]):
                    conflicts.append(
                        {"subject": owner, "template": family, "reason": "manual_modality_conflict"}
                    )
            scope["templates"][family] = {"representative": subject}
        for family in revoke:
            del scope["templates"][family]
            affected.update(
                index.records[u].subject_id
                for u in identify.members.get(family, [])
                if index.records[u].subject_id in members
            )
        if negative:
            scope["reviewed_subjects"] = sorted(set(scope["reviewed_subjects"]) | {subject})
        if deferred is not None:
            affected.add(subject)
        return affected, conflicts

    def annotations(self, group: dict) -> dict:
        subject, modality = group["representative"], group["modality"]
        sid, scope = self.scope(subject, modality)
        uids = self.round(subject, modality)
        by_family = defaultdict(list)
        for uid in uids:
            by_family[self.identify.families[uid]].append(uid)
        return {
            "negative_scope": sid,
            "new_candidate_ids": uids,
            "new_template_count": len(by_family),
            "excluded_template_count": len(scope.get("templates", {})),
            "negative_templates": [
                {"id": f, "name": self.identify.names.get(f, f)}
                for f in sorted(scope.get("templates", {}))
            ],
            # Only saved user choices drive checkboxes; preview failures stay separate.
            "deferred_candidates": sorted(set(scope.get("deferred", []))),
            "failed_preview_candidates": sorted(
                {
                    u
                    for u in self.identify.failed_previews
                    if self.identify.index.records[u].subject_id in group["subjects"]
                }
            ),
        }
