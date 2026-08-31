from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PlaneResult:
    plane: str
    nearest_plane: str
    angle_deg: float | None
    normal: tuple[float, float, float] | None
    reliable: bool


def _normal_from_iop(iop: Sequence[float]) -> np.ndarray:
    if len(iop) != 6:
        raise ValueError("ImageOrientationPatient must contain six values")
    row = np.asarray(iop[:3], dtype=float)
    column = np.asarray(iop[3:], dtype=float)
    if not np.isfinite(row).all() or not np.isfinite(column).all():
        raise ValueError("ImageOrientationPatient contains non-finite values")
    row_norm = np.linalg.norm(row)
    column_norm = np.linalg.norm(column)
    if row_norm < 1e-8 or column_norm < 1e-8:
        raise ValueError("ImageOrientationPatient contains a zero vector")
    normal = np.cross(row / row_norm, column / column_norm)
    normal_norm = np.linalg.norm(normal)
    if normal_norm < 1e-8:
        raise ValueError("ImageOrientationPatient row and column vectors are collinear")
    return normal / normal_norm


def classify_orientation(iop: Sequence[float], max_angle_deg: float = 20.0) -> PlaneResult:
    if not iop:
        return PlaneResult("unknown", "unknown", None, None, False)
    try:
        normal = _normal_from_iop(iop)
    except ValueError:
        return PlaneResult("unknown", "unknown", None, None, False)

    return classify_normal(normal, max_angle_deg)


def classify_normal(
    normal: Sequence[float], max_angle_deg: float = 20.0
) -> PlaneResult:
    """Classify an LPS or RAS direction vector; axis sign does not affect the plane."""
    vector = np.asarray(normal, dtype=float)
    if vector.shape != (3,) or not np.isfinite(vector).all():
        return PlaneResult("unknown", "unknown", None, None, False)
    norm = np.linalg.norm(vector)
    if norm < 1e-8:
        return PlaneResult("unknown", "unknown", None, None, False)
    vector = vector / norm

    absolute = np.abs(vector)
    axis = int(np.argmax(absolute))
    nearest = {0: "sagittal", 1: "coronal", 2: "axial"}[axis]
    angle = math.degrees(math.acos(float(np.clip(absolute[axis], 0.0, 1.0))))
    reliable = angle <= max_angle_deg
    return PlaneResult(
        plane=nearest if reliable else "oblique",
        nearest_plane=nearest,
        angle_deg=angle,
        normal=tuple(float(x) for x in vector),
        reliable=reliable,
    )


def maximum_orientation_deviation(orientations: Iterable[Sequence[float]]) -> float | None:
    normals: list[np.ndarray] = []
    for orientation in orientations:
        try:
            normals.append(_normal_from_iop(orientation))
        except ValueError:
            continue
    if len(normals) < 2:
        return 0.0 if normals else None
    reference = normals[0]
    maximum = 0.0
    for normal in normals[1:]:
        dot = abs(float(np.dot(reference, normal)))
        maximum = max(maximum, math.degrees(math.acos(float(np.clip(dot, 0.0, 1.0)))))
    return maximum


def coverage_from_positions(
    positions: Iterable[Sequence[float]],
    normal: Sequence[float] | None,
    slice_thickness: float | None,
) -> float | None:
    if normal is None:
        return None
    normal_array = np.asarray(normal, dtype=float)
    projections: list[float] = []
    for position in positions:
        if len(position) != 3:
            continue
        value = np.asarray(position, dtype=float)
        if np.isfinite(value).all():
            projections.append(float(np.dot(value, normal_array)))
    if not projections:
        return None
    thickness = max(float(slice_thickness or 0.0), 0.0)
    return max(projections) - min(projections) + thickness
