"""Continuous sampled swept-footprint clearance analysis."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from route_analysis.geometry import interpolate_poses, vehicle_polygon
from route_analysis.models import (
    AnalysisResult,
    AnalysisSettings,
    ClearanceStatus,
    PosePoint,
    SampleAssessment,
    VehicleDimensions,
)


def _signed_clearance(vehicle: BaseGeometry, area: BaseGeometry) -> tuple[float, bool]:
    if area.covers(vehicle):
        return vehicle.boundary.distance(area.boundary), False

    vertex_distances = [Point(x, y).distance(area) for x, y in vehicle.exterior.coords]
    penetration = max(vertex_distances, default=0.0)
    return -max(penetration, 1e-12), True


def _sample_path(
    points: Sequence[PosePoint], settings: AnalysisSettings
) -> tuple[list[tuple[PosePoint, int | None, float]], int]:
    if not points:
        return [], 0
    samples: list[tuple[PosePoint, int | None, float]] = []
    skipped_segments = 0
    if points[0].yaw is not None:
        samples.append((points[0], None, 0.0))

    for index, (start, end) in enumerate(pairwise(points)):
        if start.yaw is None or end.yaw is None:
            skipped_segments += 1
            if end.yaw is not None:
                samples.append((end, None, 0.0))
            continue
        interpolated = interpolate_poses(
            start,
            end,
            position_step=settings.position_step,
            yaw_step=settings.yaw_step,
        )
        steps = len(interpolated) - 1
        samples.extend(
            (pose, index, offset / steps) for offset, pose in enumerate(interpolated[1:], start=1)
        )
    return samples, skipped_segments


def analyze_path(
    points: Sequence[PosePoint],
    dimensions: VehicleDimensions,
    traversable_area: BaseGeometry,
    settings: AnalysisSettings,
) -> AnalysisResult:
    """Classify one path against the enabled lane union."""

    missing_yaw = tuple(index for index, point in enumerate(points) if point.yaw is None)
    if traversable_area.is_empty or traversable_area.area <= 0:
        return AnalysisResult(
            status=ClearanceStatus.UNAVAILABLE,
            missing_yaw_indices=missing_yaw,
            skipped_segments=sum(
                start.yaw is None or end.yaw is None
                for start, end in pairwise(points)
            ),
            position_step=settings.position_step,
            yaw_step=settings.yaw_step,
        )

    path_samples, skipped_segments = _sample_path(points, settings)
    assessments: list[SampleAssessment] = []
    for pose, source_segment, progress in path_samples:
        polygon = vehicle_polygon(pose, dimensions)
        clearance, outside = _signed_clearance(polygon, traversable_area)
        assessments.append(
            SampleAssessment(pose, source_segment, progress, clearance, outside)
        )

    if not assessments:
        return AnalysisResult(
            status=ClearanceStatus.UNAVAILABLE,
            skipped_segments=skipped_segments,
            missing_yaw_indices=missing_yaw,
            position_step=settings.position_step,
            yaw_step=settings.yaw_step,
        )

    minimum = min(assessments, key=lambda item: item.clearance)
    outside_assessments = [item for item in assessments if item.outside]
    if outside_assessments:
        status = ClearanceStatus.OUTSIDE
    elif minimum.clearance < settings.clearance_threshold:
        status = ClearanceStatus.WARNING
    else:
        status = ClearanceStatus.SAFE

    return AnalysisResult(
        status=status,
        minimum_clearance=minimum.clearance,
        minimum_clearance_pose=minimum.pose,
        first_outside=outside_assessments[0].pose if outside_assessments else None,
        outside_samples=len(outside_assessments),
        analyzed_samples=len(assessments),
        skipped_segments=skipped_segments,
        missing_yaw_indices=missing_yaw,
        assessments=tuple(assessments),
        position_step=settings.position_step,
        yaw_step=settings.yaw_step,
    )
