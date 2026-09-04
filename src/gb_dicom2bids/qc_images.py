"""Bounded previews of source voxel slices. Source NIfTI is never resampled or changed."""

from __future__ import annotations

import io
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
            value = {
                "data": np.nan_to_num(data),
                "shape": list(data.shape),
                "zooms": list(map(float, image.header.get_zooms()[:3])),
                "slice_count": int(data.shape[2]),
                "initial_slice": int(data.shape[2] // 2),
                "display_mode": "source_voxel_slices",
                "window": [float(low), float(max(high, low + 1))],
                "errors": errors,
            }
            self._values[key] = value
            while len(self._values) > self.capacity:
                self._values.popitem(last=False)
            return value

    def metadata(self, path: Path) -> dict[str, Any]:
        return {key: value for key, value in self.get(path).items() if key != "data"}

    def slice_png(self, path: Path, index: int, low: float, high: float) -> bytes:
        """Render data[:, :, index] without interpolation, cropping or affine reslicing."""

        volume = self.get(path)
        if isinstance(index, bool) or not isinstance(index, (int, np.integer)):
            raise ValueError("slice index must be an integer")
        if not 0 <= int(index) < volume["slice_count"]:
            raise ValueError("slice index outside source volume")
        if not np.isfinite([low, high]).all() or high <= low:
            raise ValueError("invalid slice/window parameters")
        # Transpose maps voxel i to screen x; flip places increasing j upward. These are
        # lossless display operations: every source voxel appears exactly once.
        values = np.flipud(volume["data"][:, :, int(index)].T)
        pixels = np.round(np.clip((values - low) / (high - low), 0, 1) * 255).astype(np.uint8)
        output = io.BytesIO()
        Image.fromarray(pixels).save(output, format="PNG")
        return output.getvalue()
