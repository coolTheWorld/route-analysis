"""Generate editable lane centerlines from scheduling path coordinates."""

from __future__ import annotations

import math
from collections.abc import Sequence
from copy import deepcopy
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

MAX_ARC_FIT_SAMPLES = 64


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


def arc_radius(lane: Lane, segment_index: int) -> float:
    """Return the radius of one validated circular lane segment."""

    segment = lane.segments[segment_index]
    if segment.kind is not SegmentKind.ARC or segment.arc_center is None:
        raise ValueError("selected segment is not a circular arc")
    start = lane.anchors[segment_index].point
    return math.hypot(start.x - segment.arc_center.x, start.y - segment.arc_center.y)


def _cross(first: tuple[float, float], second: tuple[float, float]) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _line_intersection(
    first_point: Point2D,
    first_direction: tuple[float, float],
    second_point: Point2D,
    second_direction: tuple[float, float],
) -> Point2D | None:
    denominator = _cross(first_direction, second_direction)
    if abs(denominator) <= 1e-10:
        return None
    offset = (second_point.x - first_point.x, second_point.y - first_point.y)
    distance = _cross(offset, second_direction) / denominator
    return Point2D(
        first_point.x + first_direction[0] * distance,
        first_point.y + first_direction[1] * distance,
    )


@dataclass(frozen=True, slots=True)
class _FilletGeometry:
    vertex: Point2D
    incoming: tuple[float, float]
    outgoing: tuple[float, float]
    tangent_factor: float
    maximum_radius: float
    turn_sign: float


def _fillet_geometry(lane: Lane, segment_index: int) -> _FilletGeometry:
    if lane.segments[segment_index].kind is not SegmentKind.ARC:
        raise ValueError("selected segment is not a circular arc")
    segment_count = len(lane.segments)
    previous_index = (segment_index - 1) % segment_count
    next_index = (segment_index + 1) % segment_count
    if not lane.closed and (segment_index == 0 or segment_index == segment_count - 1):
        raise ValueError("an editable arc requires an adjacent straight segment on both sides")
    if (
        lane.segments[previous_index].kind is not SegmentKind.LINE
        or lane.segments[next_index].kind is not SegmentKind.LINE
    ):
        raise ValueError("an editable arc requires an adjacent straight segment on both sides")

    anchor_count = len(lane.anchors)
    previous_anchor = lane.anchors[(segment_index - 1) % anchor_count].point
    arc_start = lane.anchors[segment_index].point
    arc_end = lane.anchors[(segment_index + 1) % anchor_count].point
    next_anchor = lane.anchors[(segment_index + 2) % anchor_count].point
    incoming = _unit(arc_start.x - previous_anchor.x, arc_start.y - previous_anchor.y)
    outgoing = _unit(next_anchor.x - arc_end.x, next_anchor.y - arc_end.y)
    if incoming is None or outgoing is None:
        raise ValueError("adjacent straight segments must have positive length")
    vertex = _line_intersection(arc_start, incoming, arc_end, outgoing)
    if vertex is None:
        raise ValueError("adjacent straight segments do not define a bend")

    dot = max(-1.0, min(1.0, incoming[0] * outgoing[0] + incoming[1] * outgoing[1]))
    deflection = math.acos(dot)
    tangent_factor = math.tan(deflection / 2)
    turn_sign = _cross(incoming, outgoing)
    if tangent_factor <= 1e-10 or abs(turn_sign) <= 1e-10:
        raise ValueError("adjacent straight segments do not define a bend")
    previous_capacity = math.hypot(vertex.x - previous_anchor.x, vertex.y - previous_anchor.y)
    next_capacity = math.hypot(next_anchor.x - vertex.x, next_anchor.y - vertex.y)
    maximum = min(previous_capacity, next_capacity) / tangent_factor
    if maximum <= 0:
        raise ValueError("adjacent straight segments do not have room for an arc")
    return _FilletGeometry(vertex, incoming, outgoing, tangent_factor, maximum, turn_sign)


def maximum_arc_radius(lane: Lane, segment_index: int) -> float:
    """Return the largest tangent radius allowed by adjacent straight segments."""

    return _fillet_geometry(lane, segment_index).maximum_radius


def replace_arc_radius(lane: Lane, segment_index: int, radius: float) -> Lane:
    """Return a copy with a tangent arc resized around its theoretical vertex."""

    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("arc radius must be finite and greater than zero")
    geometry = _fillet_geometry(lane, segment_index)
    if radius > geometry.maximum_radius + 1e-9:
        maximum = f"{geometry.maximum_radius:.6f}".rstrip("0").rstrip(".")
        raise ValueError(f"arc radius is too large; maximum radius is {maximum}")

    tangent_distance = radius * geometry.tangent_factor
    start = Point2D(
        geometry.vertex.x - geometry.incoming[0] * tangent_distance,
        geometry.vertex.y - geometry.incoming[1] * tangent_distance,
    )
    end = Point2D(
        geometry.vertex.x + geometry.outgoing[0] * tangent_distance,
        geometry.vertex.y + geometry.outgoing[1] * tangent_distance,
    )
    normal_sign = 1.0 if geometry.turn_sign > 0 else -1.0
    center = Point2D(
        start.x - geometry.incoming[1] * normal_sign * radius,
        start.y + geometry.incoming[0] * normal_sign * radius,
    )

    edited = deepcopy(lane)
    edited.anchors[segment_index].point = start
    edited.anchors[(segment_index + 1) % len(edited.anchors)].point = end
    segment = edited.segments[segment_index]
    segment.arc_center = center
    segment.clockwise = geometry.turn_sign < 0
    return edited


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


def _simplify_open(points: Sequence[Point2D], tolerance: float) -> list[Point2D]:
    """Simplify an open line with a bounded perpendicular error."""

    if len(points) <= 2:
        return list(points)
    coordinates = [(point.x, point.y) for point in points]
    return [Point2D(x, y) for x, y in LineString(coordinates).simplify(tolerance).coords]


def _simplify_closed(points: Sequence[Point2D], tolerance: float) -> list[Point2D]:
    coordinates = [(point.x, point.y) for point in points]
    coordinates.append(coordinates[0])
    simplified = [Point2D(x, y) for x, y in LineString(coordinates).simplify(tolerance).coords]
    if len(simplified) > 1 and _same_point(simplified[0], simplified[-1]):
        simplified.pop()
    return simplified if len(simplified) >= 3 else list(points)


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
        maximum_end = min(len(points) - 2, start + MAX_ARC_FIT_SAMPLES - 1)
        for end in range(maximum_end, start + 1, -1):
            candidate = _fit_arc_candidate(points, start, end, tolerance)
            if candidate is not None:
                candidates.append(candidate)
                break
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
    working_points = _simplify_open(points, max(tolerance / 4, 1e-6))
    fits = _find_arc_fits(working_points, tolerance)
    if not fits:
        simplified = _simplify_open(working_points, tolerance)
        lane = Lane.create(lane_id, name, width, simplified, default_join=JoinStyle.MITER)
        return lane, int(len(simplified) > 2)

    anchors = [LaneAnchor(working_points[0])]
    segments: list[LaneSegment] = []
    cursor = 0
    for fit in fits:
        line_points = _simplify_open(
            working_points[cursor : fit.start_index + 1], tolerance
        )
        for point in line_points[1:]:
            _append_line(anchors, segments, point)
        if not _same_point(anchors[-1].point, working_points[fit.start_index]):
            _append_line(anchors, segments, working_points[fit.start_index])
        anchors.append(LaneAnchor(working_points[fit.end_index]))
        segments.append(
            LaneSegment(
                SegmentKind.ARC,
                arc_center=fit.center,
                clockwise=fit.clockwise,
            )
        )
        cursor = fit.end_index
    tail = _simplify_open(working_points[cursor:], tolerance)
    for point in tail[1:]:
        _append_line(anchors, segments, point)
    return Lane(lane_id, name, width, anchors, segments, default_join=JoinStyle.MITER), 0


def _find_closed_arc_fits(points: Sequence[Point2D], tolerance: float) -> list[_ArcFit]:
    count = len(points)
    extended = list(points) * 3
    candidates: list[_ArcFit] = []
    for start in range(count, count * 2):
        maximum_end = min(
            start + count - 1,
            start + MAX_ARC_FIT_SAMPLES - 1,
            len(extended) - 2,
        )
        for end in range(maximum_end, start + 1, -1):
            candidate = _fit_arc_candidate(extended, start, end, tolerance)
            if candidate is not None:
                candidates.append(candidate)
                break
    candidates.sort(key=lambda fit: (-fit.sample_count, fit.maximum_deviation, fit.start_index))
    selected: list[_ArcFit] = []
    occupied: set[int] = set()
    for candidate in candidates:
        sample_indices = {
            index % count for index in range(candidate.start_index, candidate.end_index + 1)
        }
        if occupied.isdisjoint(sample_indices):
            selected.append(candidate)
            occupied.update(sample_indices)
    return selected


def _round_closed_lane(
    points: Sequence[Point2D], lane_id: str, name: str, width: float, tolerance: float
) -> tuple[Lane, int]:
    working_points = _simplify_closed(points, max(tolerance / 4, 1e-6))
    fits = _find_closed_arc_fits(working_points, tolerance)
    if not fits:
        return (
            Lane.create(
                lane_id,
                name,
                width,
                list(working_points),
                closed=True,
                default_join=JoinStyle.MITER,
            ),
            len(working_points),
        )

    count = len(working_points)
    cut: int | None = None
    relative_fits: list[_ArcFit] = []
    for candidate_cut in range(count):
        converted: list[_ArcFit] = []
        valid = True
        for fit in fits:
            start_modulo = fit.start_index % count
            relative_start = (start_modulo - candidate_cut) % count
            relative_end = relative_start + fit.sample_count - 1
            if relative_end > count:
                valid = False
                break
            converted.append(
                _ArcFit(
                    relative_start,
                    relative_end,
                    fit.center,
                    fit.clockwise,
                    fit.maximum_deviation,
                )
            )
        if valid:
            cut = candidate_cut
            relative_fits = sorted(converted, key=lambda fit: fit.start_index)
            break
    if cut is None:
        return (
            Lane.create(lane_id, name, width, list(working_points), closed=True),
            len(working_points),
        )

    linear = [working_points[(cut + offset) % count] for offset in range(count)]
    linear.append(linear[0])
    anchors = [LaneAnchor(linear[0])]
    segments: list[LaneSegment] = []
    cursor = 0
    for fit in relative_fits:
        line_points = _simplify_open(linear[cursor : fit.start_index + 1], tolerance)
        for point in line_points[1:]:
            _append_line(anchors, segments, point)
        if not _same_point(anchors[-1].point, linear[fit.start_index]):
            _append_line(anchors, segments, linear[fit.start_index])
        anchors.append(LaneAnchor(linear[fit.end_index]))
        segments.append(
            LaneSegment(
                SegmentKind.ARC,
                arc_center=fit.center,
                clockwise=fit.clockwise,
            )
        )
        cursor = fit.end_index
    tail = _simplify_open(linear[cursor:], tolerance)
    for point in tail[1:]:
        _append_line(anchors, segments, point)
    if not _same_point(anchors[-1].point, anchors[0].point):
        _append_line(anchors, segments, anchors[0].point)
    anchors.pop()
    return (
        Lane(
            lane_id,
            name,
            width,
            anchors,
            segments,
            closed=True,
            default_join=JoinStyle.MITER,
        ),
        0,
    )


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
            lane, arc_failures = _round_closed_lane(
                points,
                lane_id,
                name,
                width,
                maximum_deviation,
            )
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
