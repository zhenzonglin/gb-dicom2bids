"""Scoped removal of current identification rules; never replay a whole old snapshot."""

from __future__ import annotations

import copy

from .qc_protocols import explicit, fingerprint
from .qc_state import ConflictError
from .runtime import atomic_write_json, read_json


def flatten(state: dict) -> dict:
    result = {}

    def add(kind, key, scope, value):
        uid = fingerprint(["identification-rule", kind, scope, key])[:24]
        result[uid] = {"id": uid, "kind": kind, "key": key, "scope": scope, "value": value}

    for family, value in state.get("templates", {}).items():
        add("include", family, "", value)
    for sid, scope in state.get("negative_scopes", {}).items():
        for family, value in scope.get("templates", {}).items():
            add("exclude", family, sid, dict(value, modality=scope["modality"]))
    for key, value in state.get("absent", {}).items():
        add("absent", key, "", value)
    for key, value in state.get("recheck", {}).items():
        add("recheck", key, "", value)
    return result


def review_index(identify) -> dict:
    """Incremental derived ledger: old full snapshots are read once, not on each page."""
    revision = identify.state["revision"]
    if identify._review_cache and identify._review_cache["revision"] == revision:
        return identify._review_cache
    path = identify.root / "rule_review_index.json"
    cache = read_json(path)
    if not cache or cache.get("version") != 1 or cache.get("revision", 0) > revision:
        cache = {"version": 1, "revision": -1, "flat": {}, "metadata": {}, "events": []}
    if cache["revision"] < revision:
        chain, cursor = [], revision
        while cursor > cache["revision"] and cursor > 0:
            snapshot = read_json(identify.root / "identification_history" / f"{cursor:09d}.json")
            if not snapshot:
                break
            chain.append(snapshot)
            parent = snapshot.get("parent_revision", cursor - 1)
            if not isinstance(parent, int) or not 0 <= parent < cursor:
                raise ValueError("invalid identification history chain")
            cursor = parent
        # Follow the committed parent chain, skipping journals left by interrupted writes.
        # A cache on a restored/alternate branch is disposable, unlike the source history.
        if cursor < cache["revision"]:
            cache = {"version": 1, "revision": -1, "flat": {}, "metadata": {}, "events": []}
            while cursor > 0:
                snapshot = read_json(
                    identify.root / "identification_history" / f"{cursor:09d}.json"
                )
                if not snapshot:
                    break
                chain.append(snapshot)
                parent = snapshot.get("parent_revision", cursor - 1)
                if not isinstance(parent, int) or not 0 <= parent < cursor:
                    raise ValueError("invalid identification history chain")
                cursor = parent
        for snapshot in reversed(chain):
            flat = flatten(snapshot)
            for uid in sorted(set(cache["flat"]) | set(flat)):
                before, after = cache["flat"].get(uid), flat.get(uid)
                if before == after:
                    continue
                request = snapshot.get("request") or {}
                metadata = {
                    "revision": snapshot["revision"],
                    "saved_at": snapshot.get("saved_at"),
                    "reviewer": request.get("reviewer", "未记录"),
                    "action": "remove" if after is None else "update" if before else "create",
                }
                cache["metadata"][uid] = metadata
                cache["events"].append(dict(after or before, **metadata, active=after is not None))
            cache.update(flat=flat, revision=snapshot["revision"])
        # Legacy state files without a matching journal remain readable, not invented history.
        cache.update(flat=flatten(identify.state), revision=revision)
        atomic_write_json(path, cache)
    identify._review_cache = cache
    return cache


def describe(identify, row: dict) -> dict:
    kind, key, scope_id = row["kind"], row["key"], row["scope"]
    if kind in {"include", "exclude"}:
        uids = identify.members.get(key, [])
        if kind == "exclude":
            scope = identify.state.get("negative_scopes", {}).get(scope_id, {})
            members = set(scope.get("subjects", []))
            uids = [u for u in uids if identify.index.records[u].subject_id in members]
        subjects = sorted({identify.index.records[u].subject_id for u in uids})
        modality = row["value"]["modality"]
        name = identify.names.get(key, key)
    else:
        subject, modality = key.rsplit(":", 1)
        subjects = [subject] if subject in identify.index.subjects else []
        uids = (
            identify.index.subjects.get(subject, [])
            if kind == "absent"
            else [u for u in row["value"].get("candidates", []) if u in identify.index.records]
        )
        name = "本例未找到目标" if kind == "absent" else "重新人工识别"
    center = identify.index.records[uids[0]].center if uids else ""
    return dict(
        row,
        subjects=subjects,
        count=len(subjects),
        center=center,
        modality=modality,
        name=name,
        candidate_ids=list(uids),
    )


def list_rules(identify, filters: dict) -> dict:
    cache = review_index(identify)
    current = cache["flat"]
    if filters.get("view") == "history":
        rows = []
        for row in reversed(cache["events"]):
            active = row["active"] and current.get(row["id"], {}).get("value") == row["value"]
            active = (
                active and cache["metadata"].get(row["id"], {}).get("revision") == row["revision"]
            )
            rows.append(dict(row, active=active))
    else:
        rows = [
            dict(row, **cache["metadata"].get(uid, {}), active=True) for uid, row in current.items()
        ]
    selected = []
    for row in rows:
        if filters.get("kind") and filters["kind"] != row["kind"]:
            continue
        item = describe(identify, row)
        if any(filters.get(k) and filters[k] != item[k] for k in ("modality", "center")):
            continue
        if filters.get("subject") and not any(filters["subject"] in s for s in item["subjects"]):
            continue
        if filters.get("q", "").lower() not in (item["name"] + " " + item["key"]).lower():
            continue
        selected.append(item)
    offset = max(0, int(filters.get("offset", 0)))
    return {
        "revision": identify.state["revision"],
        "phase": identify.state["phase"],
        "total": len(selected),
        "offset": offset,
        "rules": [
            {k: v for k, v in row.items() if k not in {"candidate_ids", "subjects"}}
            | {"example_subject": next(iter(row["subjects"]), None)}
            for row in selected[offset : offset + 100]
        ],
    }


def revoke_preview(identify, payload: dict) -> dict:
    identify.require_current_inventory()
    if payload.get("revision") != identify.state["revision"]:
        raise ConflictError("规则已变化，请刷新回顾列表后重新预览")
    ids = payload.get("rule_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 100
        or any(not isinstance(i, str) for i in ids)
    ):
        raise ValueError("请选择 1 到 100 条当前规则")
    if len(set(ids)) != len(ids):
        raise ValueError("不能重复选择规则")
    if type(payload.get("recheck", False)) is not bool:
        raise ValueError("invalid recheck flag")
    current = flatten(identify.state)
    if not set(ids).issubset(current):
        raise ConflictError("所选规则已失效或已撤销，请刷新")
    state = copy.deepcopy(identify.state)
    affected = set()
    changes, overrides, holds = [], set(), {}
    for uid in ids:
        row = describe(identify, current[uid])
        kind, key, scope = row["kind"], row["key"], row["scope"]
        if kind == "include":
            del state["templates"][key]
        elif kind == "exclude":
            del state["negative_scopes"][scope]["templates"][key]
        elif kind == "absent":
            del state["absent"][key]
        else:
            del state["recheck"][key]
        # A removed positive assignment can change both modality queues.
        modes = {row["modality"]}
        if kind == "include":
            modes.update(
                identify.assignment(u, state)["modality"]
                for u in row["candidate_ids"]
                if identify.assignment(u, state)["modality"] in {"t1", "flair"}
            )
        identify.clear_completion(state, row["subjects"], modes)
        for subject in row["subjects"]:
            affected.add(subject)
            decision = identify.index.decisions[subject]
            if any(explicit(decision.get("candidates", {}).get(u)) for u in row["candidate_ids"]):
                overrides.add(subject)
            if any(
                decision.get("groups", {}).get(m, {}).get("choice")
                or decision.get("groups", {}).get(m, {}).get("none")
                for m in modes
            ):
                overrides.add(subject)
            if payload.get("recheck"):
                owned = [
                    u
                    for u in row["candidate_ids"]
                    if identify.index.records[u].subject_id == subject
                ]
                for mode in modes:
                    holds.setdefault(f"{subject}:{mode}", set()).update(owned)
        changes.append({k: row[k] for k in ("id", "kind", "name", "modality", "count")})
    # Apply the optional hold after all removals, independent of checkbox order.
    for key, owned in holds.items():
        previous = state.setdefault("recheck", {}).get(key, {}).get("candidates", [])
        state["recheck"][key] = {"candidates": sorted(set(previous) | owned)}
    state["phase"] = "identification"
    before = identify._groups(identify.state)
    after = identify._groups(state)
    before_pending = {(s, g["modality"]) for g in before for s in g["pending_subjects"]}
    after_pending = {(s, g["modality"]) for g in after for s in g["pending_subjects"]}
    examples = []
    for subject in sorted(affected)[:20]:
        counts = {}
        for mode in ("t1", "flair"):
            counts[mode] = sum(
                identify.assignment(u, state)["modality"] == mode
                and not identify.exclusions(state).excluded(u, mode)
                for u in identify.index.subjects[subject]
            )
        examples.append({"subject": subject, "candidates_after": counts})
    result = {
        "state": state,
        "changes": changes,
        "affected_subjects": len(affected),
        "reopened_subject_modalities": len(after_pending - before_pending),
        "pending_groups_after": sum(g["needs_protocol"] for g in after),
        "manual_image_overrides": len(overrides),
        "examples": examples,
        "returns_to_identification": identify.state["phase"] == "quality",
        "quality_copied": False,
    }
    clean = {k: v for k, v in payload.items() if k != "preview_digest"}
    result["preview_digest"] = fingerprint(
        [
            clean,
            result,
            identify.index.inventory_digest,
            identify.index.decisions,
            sorted(identify.failed_previews),
        ]
    )
    return result


def revoke_publish(identify, payload: dict) -> dict:
    result = revoke_preview(identify, payload)
    if payload.get("preview_digest") != result["preview_digest"]:
        raise ConflictError("请先预览当前撤销影响，再确认撤销")
    if result["returns_to_identification"] and payload.get("return_to_identification") is not True:
        raise ValueError("撤销将返回序列识别并暂停质量授权，请先明确确认")
    if not str(payload.get("reviewer", "")).strip():
        raise ValueError("请填写审核者")
    identify._save(result["state"], "revoke_rules", payload)
    return {
        k: v for k, v in result.items() if k not in {"state", "preview_digest"}
    } | identify.summary()
