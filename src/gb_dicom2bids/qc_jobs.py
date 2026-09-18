"""Durable, serial sequence-rule publishing; never writes an image-quality decision."""

from __future__ import annotations

import copy
import threading
import uuid
from collections import Counter, deque
from pathlib import Path

from .qc_identify import inventory_stamp
from .qc_protocols import ProtocolIndex, fingerprint
from .qc_state import BusyError, ConflictError, assert_pipeline_idle, file_lock, writer_lock
from .runtime import atomic_write_json, read_json, utc_now


def assert_no_pending(root: Path) -> None:
    if any((root / "identification_jobs/pending").glob("*.json")):
        raise BusyError("序列识别仍有后台保存任务，请等待保存完成后再进入质量或归档")


def _queue_sequence(job: dict) -> int:
    if "queue_sequence" not in job:
        return 0  # Legacy receipts have no recoverable same-second submission order.
    sequence = job["queue_sequence"]
    if type(sequence) is not int or sequence < 1:
        raise ValueError("后台保存队列序号无效；保留队列，请检查序号记录")
    return sequence


def _queue_order(job: dict) -> tuple:
    sequence = _queue_sequence(job)
    # Drain legacy receipts first, preserving their historical timestamp/ID order.
    return (1, sequence) if sequence else (0, job["submitted_at"], job["id"])


class IdentificationJobs:
    """The publisher owns a separate index, so a slow write never locks the viewer index."""

    def __init__(self, service):
        self.service = service
        self.root = service.root / "assist/identification_jobs"
        self.pending = self.root / "pending"
        self.results = self.root / "results"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.last_error = ""
        # Completed results are cached; polling never reloads the full identification history.
        paths = sorted(self.results.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)[-50:]
        self.recent = deque((read_json(p) for p in paths), maxlen=50)
        self.thread = threading.Thread(target=self._loop, name="sequence-publisher", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        self.thread.join()  # Finish the active atomic publication; queued files survive restart.

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k not in {"payload", "fingerprint", "inventory"}}

    def _reserve_sequence(self) -> int:
        """Caller holds .submit.lock across reservation AND durable receipt creation."""
        path = self.root / "sequence.json"
        if path.exists():
            previous = read_json(path).get("last_sequence")
            if type(previous) is not int or previous < 0:
                raise ValueError("后台保存队列序号记录无效；保留队列，请检查 sequence.json")
        else:
            # Bootstrap once, including receipts retained after a missing counter.
            # Ordinary submissions never scan the completed-results directory.
            previous = max(
                (
                    _queue_sequence(read_json(p))
                    for directory in (self.pending, self.results)
                    for p in directory.glob("*.json")
                ),
                default=0,
            )
        sequence = previous + 1
        # Reserve first: a failed receipt write may leave a gap, never a reused number.
        atomic_write_json(path, {"last_sequence": sequence})
        return sequence

    def submit(self, raw: dict) -> dict:
        request_id = uuid.UUID(str(raw.get("request_id", ""))).hex
        payload = copy.deepcopy(raw.get("payload"))
        if (
            not isinstance(payload, dict)
            or payload.get("subject") not in self.service.by_subject
            or not isinstance(payload.get("group"), str)
            or not isinstance(payload.get("basis"), str)
            or len(payload["basis"]) != 64
            or type(payload.get("revision")) is not int
            or payload.get("revision", -1) < 0
            or not str(payload.get("reviewer", "")).strip()
            or payload.get("target_modality") not in {"t1", "flair"}
        ):
            raise ValueError("缺少有效的序列识别快照，请刷新后重试")
        stamp = inventory_stamp(self.service.root / "assist")
        value = {
            "id": request_id,
            "state": "queued",
            "submitted_at": utc_now(),
            "subject": payload["subject"],
            "group": payload["group"],
            "modality": payload["target_modality"],
            "payload": payload,
            "fingerprint": fingerprint(payload),
            "inventory": stamp,
        }
        with self.lock, file_lock(self.root / ".submit.lock"):
            prior = read_json(self.pending / f"{request_id}.json") or read_json(
                self.results / f"{request_id}.json"
            )
            if prior:
                if prior["fingerprint"] != value["fingerprint"]:
                    raise ConflictError("相同请求编号对应不同决定，已拒绝重复提交")
                return self.public(prior)
            value["queue_sequence"] = self._reserve_sequence()
            # Receipt is durable before the browser advances, not a claim of completed saving.
            atomic_write_json(self.pending / f"{request_id}.json", value)
        self.wake.set()
        return self.public(value)

    def status(self) -> dict:
        with self.lock:
            pending = [read_json(p) for p in sorted(self.pending.glob("*.json"))]
            recent = list(self.recent)
        counts = Counter(j["state"] for j in pending + recent)
        return {
            "pending": [self.public(j) for j in sorted(pending, key=_queue_order)],
            "recent": [self.public(j) for j in reversed(recent)],
            "counts": dict(counts),
            "recent_limit": 50,
            "error": self.last_error,
            "scope": fingerprint(str(self.service.root.resolve()))[:24],
        }

    def _finish(self, job: dict, state: str, **extra) -> None:
        result = dict(job, state=state, finished_at=utc_now(), **extra)
        with self.lock:
            atomic_write_json(self.results / f"{job['id']}.json", result)
            self._remember(result)
            (self.pending / f"{job['id']}.json").unlink(missing_ok=True)

    def _remember(self, result: dict) -> None:
        with self.lock:
            if not any(saved["id"] == result["id"] for saved in self.recent):
                self.recent.append(result)

    def _committed(self, job: dict) -> dict | None:
        """Recover the commit/receipt gap, without ever publishing the same rule twice."""
        history = self.service.root / "assist/identification_history"
        current = read_json(self.service.root / "assist/identification.json")
        revision = current.get("revision", 0)
        # History is written BEFORE the state commit. An orphan history file is not success.
        # Follow only committed ancestry; later revisions may legitimately skip an orphan.
        while revision > job["payload"]["revision"]:
            saved = read_json(history / f"{revision:09d}.json")
            if not saved:
                return None
            if saved.get("request", {}).get("background_job_id") == job["id"]:
                return {"revision": revision, "recovered": True}
            parent = saved.get("parent_revision", 0)
            if not isinstance(parent, int) or not 0 <= parent < revision:
                return None
            revision = parent
        return None

    def _run(self, job: dict) -> None:
        config, root = self.service.config, self.service.root
        with (
            writer_lock(config),
            file_lock(root / ".decisions.lock"),
            file_lock(root / "assist/.assist.lock"),
            file_lock(root / "assist/.features.lock"),
        ):
            assert_pipeline_idle(config)
            done = read_json(self.results / f"{job['id']}.json")
            if done:
                self._remember(done)
                (self.pending / f"{job['id']}.json").unlink(missing_ok=True)
                return
            if job["state"] == "running":
                committed = self._committed(job)
                if committed:
                    self._finish(job, "completed", result=committed)
                else:
                    self._finish(
                        job, "failed", error="上次保存中断，未找到提交记录；请返回该组重新确认"
                    )
                return
            job = dict(job, state="running", started_at=utc_now())
            atomic_write_json(self.pending / f"{job['id']}.json", job)
            try:
                if job["inventory"] != inventory_stamp(root / "assist"):
                    raise ConflictError("清单已变化，请重启阅片器并重新确认")
                index = ProtocolIndex(config, list(self.service.records.values()))
                identify = index.identification
                if not identify or identify.state["phase"] != "identification":
                    raise ConflictError("当前不在序列识别阶段，请返回后重新确认")
                payload = copy.deepcopy(job["payload"])
                if identify.group(payload["group"])["modality"] != payload["target_modality"]:
                    raise ConflictError("目标模态与原识别组不一致，请刷新后重新确认")
                if not identify.edit_is_current(payload):
                    raise ConflictError("本组或关联模板已改变，未覆盖已有规则；请返回该组重新确认")
                payload.update(
                    revision=identify.state["revision"],
                    background_job_id=job["id"],
                    viewed_revision=job["payload"]["revision"],
                )
                payload.pop("preview_digest", None)
                # The two validation passes remain on the server; no separate UI click is needed.
                preview = identify.preview(payload)
                if preview["conflicts"]:
                    raise ConflictError("存在人工分类冲突，请返回该组复核")
                result = identify.publish(dict(payload, preview_digest=preview["preview_digest"]))
            except Exception as exc:
                committed = self._committed(job)
                if committed:
                    self._finish(job, "completed", result=committed, warning=str(exc))
                else:
                    self._finish(job, "failed", error=str(exc))
                return
            self._finish(
                job,
                "completed",
                result={
                    "revision": identify.state["revision"],
                    "affected_subjects": result["affected_subjects"],
                    "next_group": result["next_group"],
                },
            )
            # Rule revisions invalidate old automatic evidence immediately. Refresh its report too.
            self.service.write_accepted()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                with file_lock(self.root / ".runner.lock"):
                    jobs = [read_json(p) for p in self.pending.glob("*.json")]
                    if jobs:
                        self._run(min(jobs, key=_queue_order))
                        self.last_error = ""
                        continue
            except BusyError:
                pass  # Another legitimate writer holds the lock; retain the durable queue.
            except Exception as exc:
                # Storage errors leave the pending record intact and visible, never silently lost.
                self.last_error = str(exc)
            self.wake.wait(0.5)
            self.wake.clear()
