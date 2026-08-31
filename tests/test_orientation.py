from __future__ import annotations

import math

from gb_dicom2bids.orientation import (
    classify_orientation,
    coverage_from_positions,
    maximum_orientation_deviation,
)


def test_cardinal_planes() -> None:
    assert classify_orientation([1, 0, 0, 0, 1, 0]).plane == "axial"
    assert classify_orientation([0, 1, 0, 0, 0, 1]).plane == "sagittal"
    assert classify_orientation([1, 0, 0, 0, 0, 1]).plane == "coronal"


def test_oblique_threshold() -> None:
    angle = math.radians(10)
    result = classify_orientation([1, 0, 0, 0, math.cos(angle), math.sin(angle)])
    assert result.plane == "axial"
    assert result.angle_deg is not None and abs(result.angle_deg - 10) < 1e-6

    angle = math.radians(25)
    result = classify_orientation([1, 0, 0, 0, math.cos(angle), math.sin(angle)])
    assert result.plane == "oblique"
    assert result.nearest_plane == "axial"

    boundary = math.radians(20)
    at_boundary = classify_orientation(
        [1, 0, 0, 0, math.cos(boundary), math.sin(boundary)]
    )
    assert at_boundary.plane == "axial"


def test_missing_and_invalid_orientation() -> None:
    assert classify_orientation([]).plane == "unknown"
    assert classify_orientation([0, 0, 0, 0, 0, 0]).plane == "unknown"


def test_orientation_consistency_ignores_normal_sign() -> None:
    axial = [1, 0, 0, 0, 1, 0]
    reversed_axial = [-1, 0, 0, 0, 1, 0]
    assert maximum_orientation_deviation([axial, reversed_axial]) == 0.0


def test_layer_orientation_inconsistency_is_detected() -> None:
    axial = [1, 0, 0, 0, 1, 0]
    angle = math.radians(8)
    tilted = [1, 0, 0, 0, math.cos(angle), math.sin(angle)]
    deviation = maximum_orientation_deviation([axial, tilted])
    assert deviation is not None and deviation > 3.0


def test_coverage_uses_slice_projection() -> None:
    coverage = coverage_from_positions([[0, 0, 0], [0, 0, 10]], [0, 0, 1], 2.0)
    assert coverage == 12.0
