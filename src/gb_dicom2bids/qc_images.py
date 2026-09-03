"""Bounded, affine-driven orthogonal previews. Source NIfTI is never resampled on disk."""

from __future__ import annotations

import io
import itertools
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
from PIL import Image


def check_image(path: Path) -> tuple[nib.Nifti1Image, list[str]]:
    image = nib.load(str(path))
    errors = []
    if len(image.shape) not in {3, 4} or any(size < 2 for size in image.shape[:3]):
        raise ValueError("unsupported image dimensions")
    if len(image.shape) == 4 and image.shape[3] != 1:
        errors.append("multiple volumes: only volume 1 displayed; approval blocked")
    if np.prod(image.shape) > 256_000_000:
        raise ValueError("image exceeds preview memory limit")
    if not np.isfinite(image.affine).all() or abs(np.linalg.det(image.affine[:3, :3])) < 1e-8:
        raise ValueError("invalid NIfTI affine")
    if not int(image.header["qform_code"]) and not int(image.header["sform_code"]):
        errors.append("qform and sform are both unset")
    return image, errors


class VolumeCache:
    def __init__(self, capacity: int = 2) -> None:
        self.capacity = capacity
        self._values: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key]
            image, errors = check_image(path)
            data = image.get_fdata(dtype=np.float32)
            if data.ndim == 4:
                data = data[..., 0]
            if not np.isfinite(data).all():
                errors.append("non-finite image intensities")
            if not np.any(np.nan_to_num(data)):
                errors.append("empty image")
            samples = data.ravel()[:: max(1, data.size // 1_000_000)]
            finite = samples[np.isfinite(samples)]
            low, high = np.percentile(finite, [1, 99]) if finite.size else (0, 1)
            corners = np.array(list(itertools.product(*[(0, n - 1) for n in data.shape])))
            world = nib.affines.apply_affine(image.affine, corners)
            bounds = [world.min(axis=0).tolist(), world.max(axis=0).tolist()]
            value = {
                "data": np.nan_to_num(data),
                "inverse": np.linalg.inv(image.affine),
                "shape": list(data.shape),
                "zooms": list(map(float, image.header.get_zooms()[:3])),
                "bounds": bounds,
                "window": [float(low), float(max(high, low + 1))],
                "errors": errors,
            }
            self._values[key] = value
            while len(self._values) > self.capacity:
                self._values.popitem(last=False)
            return value

    def metadata(self, path: Path) -> dict[str, Any]:
        return {
            key: value for key, value in self.get(path).items() if key not in {"data", "inverse"}
        }

    def slice_png(self, path: Path, plane: str, position: float, low: float, high: float) -> bytes:
        volume = self.get(path)
        if plane not in {"axial", "coronal", "sagittal"}:
            raise ValueError("invalid plane")
        if not np.isfinite([position, low, high]).all() or high <= low:
            raise ValueError("invalid slice/window parameters")
        minimum, maximum = map(np.array, volume["bounds"])
        horizontal, vertical, fixed = {
            "axial": (0, 1, 2),
            "coronal": (0, 2, 1),
            "sagittal": (1, 2, 0),
        }[plane]
        if not minimum[fixed] - 1e-4 <= position <= maximum[fixed] + 1e-4:
            raise ValueError("slice outside image bounds")
        lengths = maximum - minimum
        step = max(min(volume["zooms"]), max(lengths[horizontal], lengths[vertical]) / 511)
        width = max(2, int(round(lengths[horizontal] / step)) + 1)
        height = max(2, int(round(lengths[vertical] / step)) + 1)
        x, y = np.meshgrid(
            np.linspace(minimum[horizontal], maximum[horizontal], width),
            np.linspace(maximum[vertical], minimum[vertical], height),
        )
        world = np.ones((4, width * height))
        world[horizontal] = x.ravel()
        world[vertical] = y.ravel()
        world[fixed] = position
        coordinates = (volume["inverse"] @ world)[:3]
        values = trilinear(volume["data"], coordinates).reshape(height, width)
        pixels = np.round(np.clip((values - low) / (high - low), 0, 1) * 255).astype(np.uint8)
        output = io.BytesIO()
        Image.fromarray(pixels).save(output, format="PNG")
        return output.getvalue()


def trilinear(data: np.ndarray, coords: np.ndarray) -> np.ndarray:
    base = np.floor(coords).astype(int)
    fraction = coords - base
    result = np.zeros(coords.shape[1], dtype=np.float32)
    valid = np.all((coords >= -1e-5) & (coords <= np.array(data.shape)[:, None] - 1 + 1e-5), axis=0)
    for offset in itertools.product((0, 1), repeat=3):
        index = base + np.array(offset)[:, None]
        index = np.clip(index, 0, np.array(data.shape)[:, None] - 1)
        weight = np.prod(np.where(np.array(offset)[:, None], fraction, 1 - fraction), axis=0)
        result += data[tuple(index)] * weight
    result[~valid] = 0
    return result
