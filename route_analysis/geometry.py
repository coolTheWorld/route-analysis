"""Pure geometric construction for vehicles, paths, and editable lanes."""

from __future__ import annotations

import math
from collections.abc import Iterable

from shapely import BufferCapStyle, BufferJoinStyle
from shapely.geometry import GeometryCollection, LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from route_analysis.models import (
    JoinStyle,
    Lane,
    Point2D,
    PosePoint,
    SegmentKind,
    VehicleDimensions,
)


def shortest_angle_delta(start: float, end: float) -> float:
    """Return the signed shortest rotation from start to end in radians."""

    return (end - start + math.pi) % math.tau - math.pi


def interpolate_poses(
    start: PosePoint,
    end: PosePoint,
    *,
    position_step: float,
    yaw_step: float,
) -> list[PosePoint]:
    """Interpolate both endpoints using the stricter position/yaw step count."""

    if start.yaw is None or end.yaw is None:
        raise ValueError("cannot interpolate a pose without yaw")
    if position_step <= 0 or yaw_step <= 0:
        raise ValueError("interpolation steps must be greater than zero")

    distance = math.hypot(end.x - start.x, end.y - start.y)
    yaw_delta = shortest_angle_delta(start.yaw, end.yaw)
    # Subtract a tiny relative tolerance so an exact mathematical multiple is
    # not rounded up by binary floating-point noise.
    position_steps = math.ceil(distance / position_step - 1e-12)
    rotation_steps = math.ceil(abs(yaw_delta) / yaw_step - 1e-12)
    steps = max(1, position_steps, rotation_steps)
    poses: list[PosePoint] = []
    for index in range(steps + 1):
        ratio = index / steps
        poses.append(
            PosePoint(
                start.x + (end.x - start.x) * ratio,
                start.y + (end.y - start.y) * ratio,
                start.yaw + yaw_delta * ratio,
            )
        )
    return poses


def vehicle_polygon(pose: PosePoint, dimensions: VehicleDimensions) -> Polygon:
    """Build a vehicle rectangle around a front-axle-center pose."""

    if pose.yaw is None:
        raise ValueError("vehicle polygon requires yaw")
    half_width = dimensions.width / 2
    local_corners = (
        (dimensions.center_front, half_width),
        (dimensions.center_front, -half_width),
        (-dimensions.center_rear, -half_width),
        (-dimensions.center_rear, half_width),
    )
    cos_yaw = math.cos(pose.yaw)
    sin_yaw = math.sin(pose.yaw)
    world_corners = [
        (
            pose.x + x * cos_yaw - y * sin_yaw,
            pose.y + x * sin_yaw + y * cos_yaw,
        )
        for x, y in local_corners
    ]
    return Polygon(world_corners)


def _point_line_distance(point: Point2D, start: Point2D, end: Point2D) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    if dx == 0 and dy == 0:
        return math.hypot(point.x - start.x, point.y - start.y)
    return abs(dy * point.x - dx * point.y + end.x * start.y - end.y * start.x) / math.hypot(
        dx, dy
    )


def _midpoint(first: Point2D, second: Point2D) -> Point2D:
    return Point2D((first.x + second.x) / 2, (first.y + second.y) / 2)


def flatten_cubic(
    start: Point2D,
    control1: Point2D,
    control2: Point2D,
    end: Point2D,
    *,
    tolerance: float = 0.02,
) -> list[Point2D]:
    """Adaptively flatten a cubic Bézier curve while retaining both endpoints."""

    if tolerance <= 0:
        raise ValueError("Bezier tolerance must be greater than zero")
    points = [start]

    def subdivide(p0: Point2D, p1: Point2D, p2: Point2D, p3: Point2D, depth: int) -> None:
        flatness = max(_point_line_distance(p1, p0, p3), _point_line_distance(p2, p0, p3))
        if flatness <= tolerance or depth >= 18:
            points.append(p3)
            return
        p01 = _midpoint(p0, p1)
        p12 = _midpoint(p1, p2)
        p23 = _midpoint(p2, p3)
        p012 = _midpoint(p01, p12)
        p123 = _midpoint(p12, p23)
        center = _midpoint(p012, p123)
        subdivide(p0, p01, p012, center, depth + 1)
        subdivide(center, p123, p23, p3, depth + 1)

    subdivide(start, control1, control2, end, 0)
    return points


def lane_segment_points(lane: Lane, index: int, *, tolerance: float = 0.02) -> list[Point2D]:
    start = lane.anchors[index].point
    end = lane.anchors[(index + 1) % len(lane.anchors)].point
    segment = lane.segments[index]
    if segment.kind is SegmentKind.LINE:
        return [start, end]
    if segment.control1 is None or segment.control2 is None:
        raise ValueError("cubic segment control points are required")
    return flatten_cubic(start, segment.control1, segment.control2, end, tolerance=tolerance)


def _shapely_join(join: JoinStyle) -> BufferJoinStyle:
    return BufferJoinStyle.round if join is JoinStyle.ROUND else BufferJoinStyle.mitre


def build_lane_area(
    lane: Lane,
    *,
    tolerance: float = 0.02,
    miter_limit: float = 4.0,
) -> BaseGeometry:
    """Construct one total-width lane with flat open ends and per-anchor joins."""

    if miter_limit <= 0:
        raise ValueError("miter limit must be greater than zero")
    half_width = lane.width / 2
    polylines = [
        lane_segment_points(lane, index, tolerance=tolerance)
        for index in range(len(lane.segments))
    ]
    pieces: list[BaseGeometry] = []
    for points in polylines:
        coordinates = [(point.x, point.y) for point in points]
        pieces.append(
            LineString(coordinates).buffer(
                half_width,
                cap_style=BufferCapStyle.flat,
                join_style=BufferJoinStyle.round,
            )
        )

    join_indices = range(len(lane.anchors)) if lane.closed else range(1, len(lane.anchors) - 1)
    for anchor_index in join_indices:
        previous_index = (anchor_index - 1) % len(polylines)
        next_index = anchor_index % len(polylines)
        previous = polylines[previous_index]
        following = polylines[next_index]
        if len(previous) < 2 or len(following) < 2:
            continue
        local = [previous[-2], previous[-1], following[1]]
        join = lane.anchors[anchor_index].join_override or lane.default_join
        pieces.append(
            LineString([(point.x, point.y) for point in local]).buffer(
                half_width,
                cap_style=BufferCapStyle.flat,
                join_style=_shapely_join(join),
                mitre_limit=miter_limit,
            )
        )
    return unary_union(pieces) if pieces else GeometryCollection()


def build_traversable_area(
    lanes: Iterable[Lane],
    *,
    tolerance: float = 0.02,
    miter_limit: float = 4.0,
) -> BaseGeometry:
    """Union all enabled lane regions into one traversable area."""

    areas = [
        build_lane_area(lane, tolerance=tolerance, miter_limit=miter_limit)
        for lane in lanes
        if lane.enabled
    ]
    return unary_union(areas) if areas else GeometryCollection()
