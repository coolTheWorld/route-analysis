"""Idealised corner geometry backing clearance, offset and double-radius analysis."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from shapely.geometry import LineString, Point

from route_analysis.models import Point2D, PosePoint, VehicleDimensions
from route_analysis.turn_radius import (
    CornerRadiusKind,
    TurnSide,
    cumulative_yaw_between,
    equivalent_rotation_center,
)

MINIMUM_RADIUS = 1e-3


def _require_positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")


def _left_normal(heading: float) -> tuple[float, float]:
    return -math.sin(heading), math.cos(heading)


def corner_radii(
    dimensions: VehicleDimensions, radius: float
) -> dict[CornerRadiusKind, float]:
    """Four-corner radii for a footprint entering a constant-radius arc at its tangent point."""

    _require_positive(radius, "转弯半径")
    inner = radius - dimensions.width / 2
    outer = radius + dimensions.width / 2
    return {
        CornerRadiusKind.FRONT_INNER: math.hypot(inner, dimensions.center_front),
        CornerRadiusKind.FRONT_OUTER: math.hypot(outer, dimensions.center_front),
        CornerRadiusKind.REAR_INNER: math.hypot(inner, dimensions.center_rear),
        CornerRadiusKind.REAR_OUTER: math.hypot(outer, dimensions.center_rear),
    }


def swept_band_width(dimensions: VehicleDimensions, radius: float) -> float:
    """Radial width of the annulus the footprint sweeps on a constant-radius arc.

    Decreasing in ``radius``: a wider turn needs a narrower channel, not a wider one.
    """

    _require_positive(radius, "转弯半径")
    outer = math.hypot(
        radius + dimensions.width / 2,
        max(dimensions.center_front, dimensions.center_rear),
    )
    return outer - abs(radius - dimensions.width / 2)


def tangent_length(radius: float, deflection: float) -> float:
    """Distance from the corner point to either tangent point of a fillet arc."""

    _require_positive(radius, "转弯半径")
    return radius * math.tan(abs(deflection) / 2)


def apex_offset(radius: float, deflection: float) -> float:
    """Distance from the corner point to the arc midpoint along the angle bisector."""

    _require_positive(radius, "转弯半径")
    return radius * (1 / math.cos(abs(deflection) / 2) - 1)


@dataclass(frozen=True, slots=True)
class OffsetSample:
    arc_length: float
    separation: float


@dataclass(frozen=True, slots=True)
class OffsetProfile:
    """Lateral gap between a lane centreline and a path centreline through one corner."""

    samples: tuple[OffsetSample, ...]
    peak: float
    peak_arc_length: float
    tangent_gap: float
    constant_approximation: float

    @property
    def overestimate(self) -> float:
        """Clearance a constant-offset model wrongly credits at the apex."""

        return self.peak - self.constant_approximation


def _fillet_polyline(
    radius: float, deflection: float, *, leg: float, steps: int
) -> list[tuple[float, float]]:
    """Line-arc-line polyline through a corner at the origin entered along +x."""

    sign = 1.0 if deflection >= 0 else -1.0
    span = abs(deflection)
    length = tangent_length(radius, deflection)
    start = (-length - leg, 0.0)
    tangent = (-length, 0.0)
    centre = (-length, sign * radius)
    points = [start, tangent]
    for index in range(1, steps + 1):
        angle = sign * span * index / steps
        radial_x = tangent[0] - centre[0]
        radial_y = tangent[1] - centre[1]
        points.append(
            (
                centre[0] + radial_x * math.cos(angle) - radial_y * math.sin(angle),
                centre[1] + radial_x * math.sin(angle) + radial_y * math.cos(angle),
            )
        )
    exit_x, exit_y = points[-1]
    points.append(
        (
            exit_x + leg * math.cos(sign * span),
            exit_y + leg * math.sin(sign * span),
        )
    )
    return points


def offset_profile(
    lane_radius: float,
    path_radius: float,
    deflection: float,
    *,
    steps: int = 96,
) -> OffsetProfile:
    """Sample how far a path arc drifts from a lane fillet sharing both straight legs."""

    _require_positive(lane_radius, "车道倒角半径")
    _require_positive(path_radius, "路径转弯半径")
    if not math.isfinite(deflection) or abs(deflection) <= 1e-9:
        raise ValueError("转角的累计 yaw 变化不能为零")
    if abs(deflection) >= math.pi:
        raise ValueError("转角的累计 yaw 变化必须小于 π")
    if steps < 4:
        raise ValueError("采样步数至少为 4")

    leg = max(tangent_length(lane_radius, deflection), tangent_length(path_radius, deflection))
    lane_line = LineString(_fillet_polyline(lane_radius, deflection, leg=leg * 2, steps=steps * 2))
    path_points = _fillet_polyline(path_radius, deflection, leg=leg, steps=steps)

    samples: list[OffsetSample] = []
    travelled = 0.0
    previous = path_points[0]
    for point in path_points:
        travelled += math.dist(previous, point)
        previous = point
        samples.append(OffsetSample(travelled, lane_line.distance(Point(point))))

    peak_sample = max(samples, key=lambda item: item.separation)
    difference = abs(lane_radius - path_radius)
    return OffsetProfile(
        samples=tuple(samples),
        peak=difference * (1 / math.cos(abs(deflection) / 2) - 1),
        peak_arc_length=peak_sample.arc_length,
        tangent_gap=difference * math.tan(abs(deflection) / 2),
        constant_approximation=samples[0].separation,
    )


@dataclass(frozen=True, slots=True)
class FittedCorner:
    """One turn section reduced to a straight-arc-straight parametrisation."""

    start_index: int
    end_index: int
    entry_point: Point2D
    exit_point: Point2D
    corner_point: Point2D | None
    centre: Point2D
    radius: float
    deflection: float
    entry_heading: float
    exit_heading: float
    residual: float

    @property
    def side(self) -> TurnSide:
        return TurnSide.LEFT if self.deflection > 0 else TurnSide.RIGHT

    @property
    def sign(self) -> float:
        return 1.0 if self.deflection > 0 else -1.0


def _ray_intersection(
    origin: Point2D, heading: float, other_origin: Point2D, other_heading: float
) -> Point2D | None:
    direction_x, direction_y = math.cos(heading), math.sin(heading)
    other_x, other_y = math.cos(other_heading), math.sin(other_heading)
    denominator = direction_x * other_y - direction_y * other_x
    if abs(denominator) <= 1e-12:
        return None
    delta_x = other_origin.x - origin.x
    delta_y = other_origin.y - origin.y
    travel = (delta_x * other_y - delta_y * other_x) / denominator
    return Point2D(origin.x + direction_x * travel, origin.y + direction_y * travel)


def fit_corner(
    points: Sequence[PosePoint], *, start_index: int, end_index: int
) -> FittedCorner | None:
    """Fit one straight-arc-straight corner to a turn section, reporting its residual."""

    deflection = cumulative_yaw_between(points, start_index, end_index)
    if abs(deflection) <= 1e-9 or abs(deflection) >= math.pi:
        return None
    start = points[start_index]
    end = points[end_index]
    if start.yaw is None or end.yaw is None:
        return None
    centre = equivalent_rotation_center(start, end, deflection)
    if centre is None:
        return None
    radius = math.hypot(start.x - centre.x, start.y - centre.y)
    if radius <= MINIMUM_RADIUS:
        return None
    residual = max(
        abs(math.hypot(point.x - centre.x, point.y - centre.y) - radius)
        for point in points[start_index : end_index + 1]
    )
    entry_point = Point2D(start.x, start.y)
    exit_point = Point2D(end.x, end.y)
    return FittedCorner(
        start_index=start_index,
        end_index=end_index,
        entry_point=entry_point,
        exit_point=exit_point,
        corner_point=_ray_intersection(entry_point, start.yaw, exit_point, end.yaw),
        centre=centre,
        radius=radius,
        deflection=deflection,
        entry_heading=start.yaw,
        exit_heading=end.yaw,
        residual=residual,
    )


def solve_offset_radius(
    corner: FittedCorner,
    *,
    entry_offset: float = 0.0,
    exit_offset: float = 0.0,
    arc_start_shift: float = 0.0,
) -> float | None:
    """Radius closing a corner whose legs are shifted and whose arc start is moved.

    The three degrees of freedom are independent only because the radius follows from
    them: with both legs fixed, choosing where the arc starts *is* choosing the radius.
    A positive ``arc_start_shift`` delays the turn and tightens the radius.
    """

    entry_normal = _left_normal(corner.entry_heading)
    exit_normal = _left_normal(corner.exit_heading)
    start_x = (
        corner.entry_point.x
        + entry_offset * entry_normal[0]
        + arc_start_shift * math.cos(corner.entry_heading)
    )
    start_y = (
        corner.entry_point.y
        + entry_offset * entry_normal[1]
        + arc_start_shift * math.sin(corner.entry_heading)
    )
    anchor_x = corner.exit_point.x + exit_offset * exit_normal[0]
    anchor_y = corner.exit_point.y + exit_offset * exit_normal[1]
    gap = (start_x - anchor_x) * exit_normal[0] + (start_y - anchor_y) * exit_normal[1]
    denominator = corner.sign * (1 - math.cos(corner.deflection))
    if abs(denominator) <= 1e-12:
        return None
    radius = gap / denominator
    if not math.isfinite(radius) or radius <= MINIMUM_RADIUS:
        return None
    return radius


def lane_centreline_through(
    corner: FittedCorner,
    radius: float | None,
    *,
    leg: float = 4.0,
    steps: int = 48,
) -> list[tuple[float, float]]:
    """World-space line-arc-line for the lane centreline through this corner.

    Drawing the lane as a sharp corner when its fillet radius is known would hide the one
    thing the plan view exists to show: where the two centrelines separate and by how much.
    """

    entry_normal = _left_normal(corner.entry_heading)
    entry_direction = (math.cos(corner.entry_heading), math.sin(corner.entry_heading))
    exit_direction = (math.cos(corner.exit_heading), math.sin(corner.exit_heading))
    vertex = corner.corner_point
    if vertex is None or radius is None or radius <= MINIMUM_RADIUS:
        points = [(corner.entry_point.x, corner.entry_point.y)]
        if vertex is not None:
            points.append((vertex.x, vertex.y))
        points.append((corner.exit_point.x, corner.exit_point.y))
        return points

    length = tangent_length(radius, corner.deflection)
    start = (vertex.x - length * entry_direction[0], vertex.y - length * entry_direction[1])
    centre = (
        start[0] + corner.sign * radius * entry_normal[0],
        start[1] + corner.sign * radius * entry_normal[1],
    )
    points = [
        (start[0] - leg * entry_direction[0], start[1] - leg * entry_direction[1]),
        start,
    ]
    radial = (start[0] - centre[0], start[1] - centre[1])
    for index in range(1, steps + 1):
        angle = corner.deflection * index / steps
        points.append(
            (
                centre[0] + radial[0] * math.cos(angle) - radial[1] * math.sin(angle),
                centre[1] + radial[0] * math.sin(angle) + radial[1] * math.cos(angle),
            )
        )
    end = points[-1]
    points.append((end[0] + leg * exit_direction[0], end[1] + leg * exit_direction[1]))
    return points


@dataclass(frozen=True, slots=True)
class CornerSolution:
    """One realisable corner geometry produced by the three degrees of freedom."""

    radius: float
    entry_offset: float
    exit_offset: float
    arc_start_shift: float
    poses: tuple[PosePoint, ...]


def build_corner_poses(
    corner: FittedCorner,
    *,
    entry_offset: float = 0.0,
    exit_offset: float = 0.0,
    arc_start_shift: float = 0.0,
    entry_length: float = 3.0,
    exit_length: float = 3.0,
    yaw_step: float = 0.02,
) -> CornerSolution | None:
    """Rebuild a corner's poses from the three degrees of freedom, or None if infeasible."""

    if entry_length < 0 or exit_length < 0:
        raise ValueError("直线段长度不能为负")
    _require_positive(yaw_step, "yaw 步长")
    radius = solve_offset_radius(
        corner,
        entry_offset=entry_offset,
        exit_offset=exit_offset,
        arc_start_shift=arc_start_shift,
    )
    if radius is None:
        return None

    entry_normal = _left_normal(corner.entry_heading)
    start = PosePoint(
        corner.entry_point.x
        + entry_offset * entry_normal[0]
        + arc_start_shift * math.cos(corner.entry_heading),
        corner.entry_point.y
        + entry_offset * entry_normal[1]
        + arc_start_shift * math.sin(corner.entry_heading),
        corner.entry_heading,
    )
    centre = Point2D(
        start.x + corner.sign * radius * entry_normal[0],
        start.y + corner.sign * radius * entry_normal[1],
    )

    poses = [
        PosePoint(
            start.x - entry_length * math.cos(corner.entry_heading),
            start.y - entry_length * math.sin(corner.entry_heading),
            corner.entry_heading,
        ),
        start,
    ]
    steps = max(2, math.ceil(abs(corner.deflection) / yaw_step))
    radial_x = start.x - centre.x
    radial_y = start.y - centre.y
    for index in range(1, steps + 1):
        angle = corner.deflection * index / steps
        poses.append(
            PosePoint(
                centre.x + radial_x * math.cos(angle) - radial_y * math.sin(angle),
                centre.y + radial_x * math.sin(angle) + radial_y * math.cos(angle),
                corner.entry_heading + angle,
            )
        )
    last = poses[-1]
    assert last.yaw is not None
    poses.append(
        PosePoint(
            last.x + exit_length * math.cos(last.yaw),
            last.y + exit_length * math.sin(last.yaw),
            last.yaw,
        )
    )
    return CornerSolution(
        radius=radius,
        entry_offset=entry_offset,
        exit_offset=exit_offset,
        arc_start_shift=arc_start_shift,
        poses=tuple(poses),
    )
