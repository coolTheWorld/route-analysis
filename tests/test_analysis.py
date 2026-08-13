import pytest
from shapely.geometry import box

from route_analysis.analysis import analyze_path
from route_analysis.models import (
    AnalysisSettings,
    ClearanceStatus,
    PosePoint,
    VehicleDimensions,
)

DIMENSIONS = VehicleDimensions(width=2, center_front=1, center_rear=1)


def test_safe_warning_and_outside_classification() -> None:
    settings = AnalysisSettings(position_step=0.5, yaw_step=0.1, clearance_threshold=0.5)

    safe = analyze_path([PosePoint(0, 0, 0)], DIMENSIONS, box(-3, -3, 3, 3), settings)
    warning = analyze_path([PosePoint(0, 0, 0)], DIMENSIONS, box(-1.2, -1.2, 1.2, 1.2), settings)
    outside = analyze_path([PosePoint(1, 0, 0)], DIMENSIONS, box(-1.2, -1.2, 1.2, 1.2), settings)

    assert safe.status is ClearanceStatus.SAFE
    assert safe.minimum_clearance == 2
    assert warning.status is ClearanceStatus.WARNING
    assert warning.minimum_clearance == pytest.approx(0.2)
    assert outside.status is ClearanceStatus.OUTSIDE
    assert outside.outside_samples == 1
    assert outside.first_outside is not None


def test_touching_boundary_is_warning_not_outside() -> None:
    result = analyze_path(
        [PosePoint(0, 0, 0)],
        DIMENSIONS,
        box(-1, -1, 1, 1),
        AnalysisSettings(clearance_threshold=0.05),
    )

    assert result.status is ClearanceStatus.WARNING
    assert result.minimum_clearance == 0


def test_continuous_sampling_finds_turning_vehicle_violation() -> None:
    result = analyze_path(
        [PosePoint(-1, 0, 0), PosePoint(1, 0, 1.57)],
        DIMENSIONS,
        box(-2.1, -1.1, 2.1, 1.1),
        AnalysisSettings(position_step=0.1, yaw_step=0.05),
    )

    assert result.status is ClearanceStatus.OUTSIDE
    assert result.analyzed_samples > 2


def test_missing_yaw_skips_point_and_adjacent_segments_but_analyzes_other_points() -> None:
    result = analyze_path(
        [PosePoint(0, 0, 0), PosePoint(1, 0, None), PosePoint(2, 0, 0), PosePoint(3, 0, 0)],
        DIMENSIONS,
        box(-10, -10, 10, 10),
        AnalysisSettings(position_step=0.5),
    )

    assert result.incomplete is True
    assert result.missing_yaw_indices == (1,)
    assert result.skipped_segments == 2
    assert result.analyzed_samples == 4


def test_empty_lane_area_is_unavailable() -> None:
    result = analyze_path(
        [PosePoint(0, 0, 0)], DIMENSIONS, box(0, 0, 0, 0), AnalysisSettings()
    )

    assert result.status is ClearanceStatus.UNAVAILABLE
    assert result.analyzed_samples == 0
