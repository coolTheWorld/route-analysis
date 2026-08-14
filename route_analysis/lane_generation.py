"""Generate editable lane centerlines from scheduling path coordinates."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from shapely.geometry import LineString, Point

from route_analysis.geometry import lane_segment_points, shortest_angle_delta
from route_analysis.models import (
    JoinStyle,
    Lane,
    LaneAnchor,
    LaneSegment,
    Point2D,
    PosePoint,
    SegmentKind,
)


class BendMode(StrEnum):
    """Centerline construction used at bends in an automatically generated lane."""

    SHARP = "sharp"
    ROUND = "round"
    BEZIER = "bezier"


@dataclass(frozen=True, slots=True)
class LaneGenerationMetrics:
    source_points: int
    unique_points: int
    anchors: int
    segments: int
    maximum_deviation: float
    arc_failures: int = 0


@dataclass(frozen=True, slots=True)
class LaneGenerationResult:
    lane: Lane
    metrics: LaneGenerationMetrics


@dataclass(frozen=True, slots=True)
class _ArcFit:
    start_index: int
    end_index: int
    center: Point2D
    clockwise: bool
    maximum_deviation: float

    @property
    def sample_count(self) -> int:
        return self.end_index - self.start_index + 1


def _as_point(point: Point2D | PosePoint) -> Point2D:
    return Point2D(point.x, point.y)


def _same_point(first: Point2D, second: Point2D, *, tolerance: float = 1e-9) -> bool:
    return math.hypot(first.x - second.x, first.y - second.y) <= tolerance


def _deduplicate(points: Sequence[Point2D | PosePoint]) -> list[Point2D]:
    unique: list[Point2D] = []
    for source in points:
        point = _as_point(source)
        if not unique or not _same_point(unique[-1], point):
            unique.append(point)
    return unique


def _point_line_distance(point: Point2D, start: Point2D, end: Point2D) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    denominator = math.hypot(dx, dy)
    if denominator <= 1e-15:
        return math.hypot(point.x - start.x, point.y - start.y)
    return abs(dy * point.x - dx * point.y + end.x * start.y - end.y * start.x) / denominator


def _simplify_open(points: Sequence[Point2D], tolerance: float) -> list[Point2D]:
    """Ramer-Douglas-Peucker simplification with a bounded perpendicular error."""

    if len(points) <= 2:
        return list(points)
    maximum = -1.0
    split_index = 0
    for index in range(1, len(points) - 1):
        distance = _point_line_distance(points[index], points[0], points[-1])
        if distance > maximum:
            maximum = distance
            split_index = index
    if maximum <= tolerance:
        return [points[0], points[-1]]
    left = _simplify_open(points[: split_index + 1], tolerance)
    right = _simplify_open(points[split_index:], tolerance)
    return left[:-1] + right


def _circle_from_three(first: Point2D, middle: Point2D, last: Point2D) -> Point2D | None:
    determinant = 2 * (
        first.x * (middle.y - last.y)
        + middle.x * (last.y - first.y)
        + last.x * (first.y - middle.y)
    )
    if abs(determinant) <= 1e-10:
        return None
    first_square = first.x * first.x + first.y * first.y
    middle_square = middle.x * middle.x + middle.y * middle.y
    last_square = last.x * last.x + last.y * last.y
    center_x = (
        first_square * (middle.y - last.y)
        + middle_square * (last.y - first.y)
        + last_square * (first.y - middle.y)
    ) / determinant
    center_y = (
        first_square * (last.x - middle.x)
        + middle_square * (first.x - last.x)
        + last_square * (middle.x - first.x)
    ) / determinant
    return Point2D(center_x, center_y)


def _unit(vector_x: float, vector_y: float) -> tuple[float, float] | None:
    length = math.hypot(vector_x, vector_y)
    if length <= 1e-12:
        return None
    return vector_x / length, vector_y / length


def _is_tangent(direction: tuple[float, float], radial: tuple[float, float]) -> bool:
    # Accept modest sampling noise while rejecting a hard vertex represented by
    # only its two incident straight legs.
    return abs(direction[0] * radial[0] + direction[1] * radial[1]) <= 0.35


def _fit_arc_candidate(
    points: Sequence[Point2D], start: int, end: int, tolerance: float
) -> _ArcFit | None:
    if start <= 0 or end >= len(points) - 1 or end - start + 1 < 3:
        return None
    middle = start + (end - start) // 2
    center = _circle_from_three(points[start], points[middle], points[end])
    if center is None:
        return None
    radius = math.hypot(points[start].x - center.x, points[start].y - center.y)
    if radius <= tolerance:
        return None
    radial_errors = [
        abs(math.hypot(point.x - center.x, point.y - center.y) - radius)
        for point in points[start : end + 1]
    ]
    maximum_error = max(radial_errors, default=math.inf)
    if maximum_error > tolerance:
        return None

    signed_deltas: list[float] = []
    for first, second in zip(points[start:end], points[start + 1 : end + 1], strict=True):
        first_angle = math.atan2(first.y - center.y, first.x - center.x)
        second_angle = math.atan2(second.y - center.y, second.x - center.x)
        delta = shortest_angle_delta(first_angle, second_angle)
        if abs(delta) <= 1e-8:
            return None
        signed_deltas.append(delta)
    if not signed_deltas or any(delta * signed_deltas[0] <= 0 for delta in signed_deltas[1:]):
        return None
    if abs(sum(signed_deltas)) < math.radians(8):
        return None

    incoming = _unit(
        points[start].x - points[start - 1].x,
        points[start].y - points[start - 1].y,
    )
    outgoing = _unit(
        points[end + 1].x - points[end].x,
        points[end + 1].y - points[end].y,
    )
    start_radial = _unit(points[start].x - center.x, points[start].y - center.y)
    end_radial = _unit(points[end].x - center.x, points[end].y - center.y)
    if (
        incoming is None
        or outgoing is None
        or start_radial is None
        or end_radial is None
        or not _is_tangent(incoming, start_radial)
        or not _is_tangent(outgoing, end_radial)
    ):
        return None
    return _ArcFit(start, end, center, signed_deltas[0] < 0, maximum_error)


def _find_arc_fits(points: Sequence[Point2D], tolerance: float) -> list[_ArcFit]:
    candidates: list[_ArcFit] = []
    for start in range(1, len(points) - 3):
        for end in range(start + 2, len(points) - 1):
            candidate = _fit_arc_candidate(points, start, end, tolerance)
            if candidate is not None:
                candidates.append(candidate)
    candidates.sort(key=lambda fit: (-fit.sample_count, fit.maximum_deviation, fit.start_index))
    selected: list[_ArcFit] = []
    occupied: set[int] = set()
    for candidate in candidates:
        interior = set(range(candidate.start_index + 1, candidate.end_index))
        if occupied.isdisjoint(interior):
            selected.append(candidate)
            occupied.update(interior)
    return sorted(selected, key=lambda fit: fit.start_index)


def _append_line(
    anchors: list[LaneAnchor], segments: list[LaneSegment], point: Point2D
) -> None:
    if _same_point(anchors[-1].point, point):
        return
    anchors.append(LaneAnchor(point))
    segments.append(LaneSegment())


def _round_lane(
    points: Sequence[Point2D], lane_id: str, name: str, width: float, tolerance: float
) -> tuple[Lane, int]:
    fits = _find_arc_fits(points, tolerance)
    if not fits:
        simplified = _simplify_open(points, tolerance)
        lane = Lane.create(lane_id, name, width, simplified, default_join=JoinStyle.MITER)
        return lane, int(len(simplified) > 2)

    anchors = [LaneAnchor(points[0])]
    segments: list[LaneSegment] = []
    cursor = 0
    for fit in fits:
        line_points = _simplify_open(points[cursor : fit.start_index + 1], tolerance)
        for point in line_points[1:]:
            _append_line(anchors, segments, point)
        if not _same_point(anchors[-1].point, points[fit.start_index]):
            _append_line(anchors, segments, points[fit.start_index])
        anchors.append(LaneAnchor(points[fit.end_index]))
        segments.append(
            LaneSegment(
                SegmentKind.ARC,
                arc_center=fit.center,
                clockwise=fit.clockwise,
            )
        )
        cursor = fit.end_index
    tail = _simplify_open(points[cursor:], tolerance)
    for point in tail[1:]:
        _append_line(anchors, segments, point)
    return Lane(lane_id, name, width, anchors, segments, default_join=JoinStyle.MITER), 0


def _tangent(points: Sequence[Point2D], index: int, closed: bool) -> tuple[float, float]:
    if closed:
        previous = points[(index - 1) % len(points)]
        following = points[(index + 1) % len(points)]
    elif index == 0:
        previous, following = points[0], points[1]
    elif index == len(points) - 1:
        previous, following = points[-2], points[-1]
    else:
        previous, following = points[index - 1], points[index + 1]
    tangent = _unit(following.x - previous.x, following.y - previous.y)
    if tangent is None:
        raise ValueError("cannot construct a tangent from duplicate points")
    return tangent


def _bezier_lane(
    points: Sequence[Point2D], lane_id: str, name: str, width: float, closed: bool
) -> Lane:
    anchors = [LaneAnchor(point) for point in points]
    segments: list[LaneSegment] = []
    segment_count = len(points) if closed else len(points) - 1
    tangents = [_tangent(points, index, closed) for index in range(len(points))]
    for index in range(segment_count):
        next_index = (index + 1) % len(points)
        start = points[index]
        end = points[next_index]
        chord = math.hypot(end.x - start.x, end.y - start.y) / 3
        start_tangent = tangents[index]
        end_tangent = tangents[next_index]
        segments.append(
            LaneSegment(
                SegmentKind.CUBIC,
                control1=Point2D(
                    start.x + start_tangent[0] * chord,
                    start.y + start_tangent[1] * chord,
                ),
                control2=Point2D(
                    end.x - end_tangent[0] * chord,
                    end.y - end_tangent[1] * chord,
                ),
            )
        )
    return Lane(lane_id, name, width, anchors, segments, closed=closed)


def _maximum_deviation(lane: Lane, source: Sequence[Point2D], tolerance: float) -> float:
    coordinates: list[tuple[float, float]] = []
    flattening_tolerance = max(min(tolerance / 8, 0.002), 1e-5)
    for index in range(len(lane.segments)):
        flattened = lane_segment_points(lane, index, tolerance=flattening_tolerance)
        current = [(point.x, point.y) for point in flattened]
        coordinates.extend(current if not coordinates else current[1:])
    if lane.closed and coordinates and coordinates[-1] != coordinates[0]:
        coordinates.append(coordinates[0])
    generated = LineString(coordinates)
    return max((generated.distance(Point(point.x, point.y)) for point in source), default=0.0)


def generate_lane(
    source: Sequence[Point2D | PosePoint],
    *,
    lane_id: str,
    name: str,
    width: float,
    mode: BendMode,
    maximum_deviation: float,
    closed: bool = False,
) -> LaneGenerationResult:
    """Convert path coordinates into one independent, editable lane."""

    if not math.isfinite(maximum_deviation) or maximum_deviation <= 0:
        raise ValueError("maximum deviation must be finite and greater than zero")
    points = _deduplicate(source)
    if closed and len(points) > 1 and _same_point(points[0], points[-1]):
        points.pop()
    minimum_points = 3 if closed else 2
    if len(points) < minimum_points:
        requirement = "three" if closed else "two"
        raise ValueError(f"at least {requirement} unique points are required")

    arc_failures = 0
    if mode is BendMode.SHARP:
        lane_points = list(points) if closed else _simplify_open(points, maximum_deviation)
        lane = Lane.create(
            lane_id,
            name,
            width,
            lane_points,
            closed=closed,
            default_join=JoinStyle.MITER,
        )
    elif mode is BendMode.ROUND:
        if closed:
            # Closed circular fitting needs cyclic source windows. Until a bend
            # has enough samples on both sides, retain its exact sharp geometry.
            lane = Lane.create(
                lane_id,
                name,
                width,
                list(points),
                closed=True,
                default_join=JoinStyle.MITER,
            )
            arc_failures = len(points)
        else:
            lane, arc_failures = _round_lane(
                points, lane_id, name, width, maximum_deviation
            )
    elif mode is BendMode.BEZIER:
        lane = _bezier_lane(points, lane_id, name, width, closed)
    else:
        raise ValueError(f"unsupported bend mode: {mode}")

    deviation = _maximum_deviation(lane, points, maximum_deviation)
    if deviation > maximum_deviation + 1e-8:
        raise ValueError(
            f"generated lane deviation {deviation:.6f} exceeds limit {maximum_deviation:.6f}"
        )
    return LaneGenerationResult(
        lane,
        LaneGenerationMetrics(
            source_points=len(source),
            unique_points=len(points),
            anchors=len(lane.anchors),
            segments=len(lane.segments),
            maximum_deviation=deviation,
            arc_failures=arc_failures,
        ),
    )
