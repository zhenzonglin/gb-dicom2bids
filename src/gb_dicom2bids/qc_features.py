"""Native-slice quality features, without resampling, registration or source writes."""

from __future__ import annotations

import os
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from .qc_images import check_image
from .qc_protocols import ProtocolIndex, source_path
from .qc_state import digest, file_lock, record_digest
from .runtime import atomic_write_json, read_json, resource_blockers, resource_snapshot, utc_now

FEATURE_VERSION = "native-quality-1"
FEATURE_NAMES = [
    "sharpness_p10",
    "sharpness_p50",
    "laplace_p10",
    "laplace_p50",
    "hf_p50",
    "background_ratio",
    "slice_ncc_p10",
    "local_blur_fraction",
    "foreground_fraction",
    "voxel_i",
    "voxel_j",
    "voxel_k",
]


def extract(path: Path) -> dict:
    sitk.ProcessObject.SetGlobalDefaultNumberOfThreads(1)
    image, errors = check_image(path)
    if errors:
        raise ValueError("; ".join(errors))
    data = image.get_fdata(dtype=np.float32)
    if data.ndim == 4:
        data = data[..., 0]
    if not np.isfinite(data).all() or not np.any(data):
        raise ValueError("empty or non-finite image")
    zooms = np.asarray(image.header.get_zooms()[:3], dtype=float)
    if np.any(zooms <= 0) or not np.isfinite(zooms).all():
        raise ValueError("invalid voxel sizes")
    low, high = np.percentile(data, [1, 99.5])
    if high <= low:
        raise ValueError("insufficient intensity range")
    norm = np.clip((data - low) / (high - low), 0, 1)
    foreground = sitk.OtsuThreshold(sitk.GetImageFromArray(norm), 0, 1)
    mask = sitk.GetArrayFromImage(foreground).astype(bool)
    if mask.sum() < 32 or mask.mean() > 0.95:
        raise ValueError("foreground extraction failed")
    sharp, laplace, hf, ncc, indices = [], [], [], [], []
    previous = None
    previous_mask = None
    min_area = max(16, int(mask.sum(axis=(0, 1)).max() * 0.15))
    for k in range(data.shape[2]):
        roi = mask[:, :, k]
        if roi.sum() < min_area:
            previous = None
            continue
        plane = norm[:, :, k]
        gx, gy = np.gradient(plane, *zooms[:2])
        energy = gx * gx + gy * gy
        lap = np.gradient(gx, zooms[0], axis=0) + np.gradient(gy, zooms[1], axis=1)
        sharp.append(float(np.mean(energy[roi])))
        laplace.append(float(np.var(lap[roi])))
        window = np.hanning(plane.shape[0])[:, None] * np.hanning(plane.shape[1])[None, :]
        power = np.abs(np.fft.rfft2((plane - plane.mean()) * window)) ** 2
        fx = np.fft.fftfreq(plane.shape[0])[:, None]
        fy = np.fft.rfftfreq(plane.shape[1])[None, :]
        hf.append(float(power[fx * fx + fy * fy >= 0.25**2].sum() / (power.sum() + 1e-12)))
        indices.append(k)
        corr = 1.0
        if previous is not None:
            common = roi & previous_mask
            a, b = plane[common], previous[common]
            if len(a) >= 16 and np.std(a) > 1e-8 and np.std(b) > 1e-8:
                corr = float(np.corrcoef(a, b)[0, 1])
        ncc.append(corr)
        previous, previous_mask = plane, roi
    if len(indices) < 3:
        raise ValueError("too few informative source slices")
    # Dilated foreground avoids interpreting the object boundary as background artifact.
    dilated = sitk.BinaryDilate(foreground, [2, 2, 2])
    background = ~sitk.GetArrayFromImage(dilated).astype(bool)
    if background.sum() < 16:
        raise ValueError("insufficient background for artifact screening")
    sharp_array = np.array(sharp)
    med = float(np.median(sharp_array))
    suspicious = np.argsort(sharp_array / (med + 1e-12) + np.asarray(ncc))[:5]
    values = [
        np.percentile(sharp, 10),
        med,
        np.percentile(laplace, 10),
        np.median(laplace),
        np.median(hf),
        np.mean(norm[background] ** 2) / (np.mean(norm[mask] ** 2) + 1e-12),
        np.percentile(ncc, 10),
        np.mean(sharp_array < med * 0.4),
        mask.mean(),
        *zooms,
    ]
    if not np.isfinite(values).all():
        raise ValueError("non-finite quality features")
    return {
        "features": dict(zip(FEATURE_NAMES, map(float, values), strict=True)),
        "suspect_slices": [int(indices[i]) for i in suspicious],
        "slice_sharpness": sharp,
        "slice_indices": indices,
        "shape": list(data.shape),
        "informative_slices": len(indices),
    }


def _worker(job: dict) -> dict:
    path, output = Path(job["source"]), Path(job["output"])
    started = time.monotonic()
    value = dict(job, version=FEATURE_VERSION, state="running", pid=os.getpid(), at=utc_now())
    atomic_write_json(output.with_suffix(".running.json"), value)
    try:
        before = path.stat()
        checksum = digest(path)
        value.update(image_sha256=checksum, source_stats=[before.st_size, before.st_mtime_ns])
        cached = read_json(output)
        if (
            job["resume"]
            and cached.get("version") == FEATURE_VERSION
            and cached.get("image_sha256") == checksum
            and cached.get("record_digest") == job["record_digest"]
            and (
                cached.get("state") == "completed"
                or (cached.get("state") == "failed" and not job["retry_failed"])
            )
        ):
            result = dict(cached, cached=True, source_stats=[before.st_size, before.st_mtime_ns])
            atomic_write_json(output, result)
            atomic_write_json(output.with_suffix(".running.json"), dict(result, pid=None))
            return result
        result = extract(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("source changed during feature extraction")
        value.update(
            result,
            image_sha256=checksum,
            state="completed",
            source_stats=[after.st_size, after.st_mtime_ns],
        )
    except Exception as exc:
        value.update(state="failed", error=str(exc))
    value.update(seconds=time.monotonic() - started, finished_at=utc_now())
    atomic_write_json(output, value)
    atomic_write_json(output.with_suffix(".running.json"), dict(value, pid=None))
    return value


def run_features(
    index: ProtocolIndex, workers: int = 8, resume: bool = True, retry_failed: bool = False
) -> dict:
    from .qc_identify import require_quality

    require_quality(index.root)
    if not 1 <= workers <= 64:
        raise ValueError("feature workers must be between 1 and 64")
    jobs = []
    for uid, record in sorted(index.records.items()):
        if index.assignment(uid)["modality"] not in {"t1", "flair"}:
            continue
        jobs.append(
            {
                "id": uid,
                "subject": record.subject_id,
                "source": str(source_path(index.config, record)),
                "record_digest": record_digest(record),
                "output": str(index.root / "features" / f"{uid}.json"),
                "resume": resume,
                "retry_failed": retry_failed,
            }
        )
    state = {
        "stage": "features",
        "state": "running",
        "pid": os.getpid(),
        "total": len(jobs),
        "completed": 0,
        "failed": 0,
        "cached": 0,
        "running": 0,
    }
    started = time.monotonic()
    # Spawned workers inherit these limits before importing numerical libraries.
    keys = (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS",
    )
    prior = {k: os.environ.get(k) for k in keys}
    for key in keys:
        os.environ[key] = "1"

    def report():
        elapsed = max(0.001, time.monotonic() - started)
        done = state["completed"] + state["failed"]
        state.update(
            at=utc_now(),
            seconds=elapsed,
            per_minute=done / elapsed * 60,
            eta_seconds=(len(jobs) - done) * elapsed / done if done else None,
        )
        atomic_write_json(index.root / "status.json", state)

    try:
        with (
            file_lock(index.root / ".features.lock"),
            ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool,
        ):
            require_quality(index.root)
            pending, cursor = {}, 0
            while cursor < len(jobs) or pending:
                snapshot = resource_snapshot(index.config)
                blockers = resource_blockers(index.config, snapshot)
                state.update(
                    state="paused_resources" if blockers else "running",
                    blockers=blockers,
                    resources=snapshot,
                )
                while not blockers and cursor < len(jobs) and len(pending) < workers:
                    job = jobs[cursor]
                    pending[pool.submit(_worker, job)] = job
                    cursor += 1
                state["running"] = len(pending)
                report()
                if not pending:
                    time.sleep(1)
                    continue
                done, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    job = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = dict(job, state="failed", error=str(exc), at=utc_now())
                        atomic_write_json(Path(job["output"]), result)
                    state["completed" if result["state"] == "completed" else "failed"] += 1
                    state["cached"] += bool(result.get("cached"))
            state.update(state="completed", running=0, pid=None)
            report()
    except BaseException:
        state.update(state="interrupted", pid=None)
        report()
        raise
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return state
