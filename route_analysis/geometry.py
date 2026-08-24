"""Pure geometric construction for vehicles, paths, and editable lanes."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Iterable

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
    VehicleSection,
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


def vehicle_polygon(
    pose: PosePoint,
    dimensions: VehicleDimensions,
    section: VehicleSection = VehicleSection.FULL,
) -> Polygon:
    """Build a vehicle rectangle around a front-axle-center pose.

    ``section`` cuts the rectangle at the reference point so one end can be read on its
    own. It is a drawing choice: analysis never passes anything but the whole envelope.
    """

    if pose.yaw is None:
        raise ValueError("vehicle polygon requires yaw")
    half_width = dimensions.width / 2
    front = dimensions.center_front if section is not VehicleSection.REAR else 0.0
    rear = dimensions.center_rear if section is not VehicleSection.FRONT else 0.0
    local_corners = (
        (front, half_width),
        (front, -half_width),
        (-rear, -half_width),
        (-rear, half_width),
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


def envelope_overhang(
    pose: PosePoint, dimensions: VehicleDimensions, heading: float
) -> float:
    """How far the vehicle envelope reaches past its pose along ``heading``.

    Equals the centre-front distance when ``heading`` matches the pose yaw and the
    centre-rear distance when it opposes it, but stays correct when the two differ —
    which is exactly the case a straight line drawn between two unaligned poses creates.
    """

    if pose.yaw is None:
        raise ValueError("车辆包络外伸量需要位姿的 yaw")
    if not math.isfinite(heading):
        raise ValueError("方向必须是有限值")
    cos_yaw = math.cos(pose.yaw)
    sin_yaw = math.sin(pose.yaw)
    axis_x = math.cos(heading)
    axis_y = math.sin(heading)
    half_width = dimensions.width / 2
    corners = (
        (dimensions.center_front, half_width),
        (dimensions.center_front, -half_width),
        (-dimensions.center_rear, -half_width),
        (-dimensions.center_rear, half_width),
    )
    return max(
        (longitudinal * cos_yaw - lateral * sin_yaw) * axis_x
        + (longitudinal * sin_yaw + lateral * cos_yaw) * axis_y
        for longitudinal, lateral in corners
    )


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


def flatten_arc(
    start: Point2D,
    center: Point2D,
    end: Point2D,
    *,
    clockwise: bool,
    tolerance: float = 0.02,
) -> list[Point2D]:
    """Flatten a circular arc with a maximum radial chord error."""

    if tolerance <= 0:
        raise ValueError("arc tolerance must be greater than zero")
    start_radius, start_angle, sweep = _arc_parameters(
        start, center, end, clockwise=clockwise
    )

    ratio = min(1.0, tolerance / start_radius)
    maximum_step = 2 * math.acos(max(-1.0, 1.0 - ratio))
    steps = max(1, math.ceil(abs(sweep) / maximum_step))
    points = [start]
    for index in range(1, steps):
        angle = start_angle + sweep * index / steps
        points.append(
            Point2D(
                center.x + start_radius * math.cos(angle),
                center.y + start_radius * math.sin(angle),
            )
        )
    points.append(end)
    return points


def _arc_parameters(
    start: Point2D,
    center: Point2D,
    end: Point2D,
    *,
    clockwise: bool,
) -> tuple[float, float, float]:
    radius = math.hypot(start.x - center.x, start.y - center.y)
    end_radius = math.hypot(end.x - center.x, end.y - center.y)
    if radius <= 0 or not math.isclose(radius, end_radius, rel_tol=1e-7, abs_tol=1e-7):
        raise ValueError("arc endpoints must share one positive radius")
    start_angle = math.atan2(start.y - center.y, start.x - center.x)
    end_angle = math.atan2(end.y - center.y, end.x - center.x)
    sweep = (
        -((start_angle - end_angle) % math.tau)
        if clockwise
        else (end_angle - start_angle) % math.tau
    )
    if math.isclose(sweep, 0.0, abs_tol=1e-12):
        raise ValueError("arc endpoints must not define a zero sweep")
    return radius, start_angle, sweep


def _cubic_value(first: float, control1: float, control2: float, last: float, t: float) -> float:
    inverse = 1 - t
    return (
        inverse**3 * first
        + 3 * inverse**2 * t * control1
        + 3 * inverse * t**2 * control2
        + t**3 * last
    )


def _cubic_derivative_roots(
    first: float, control1: float, control2: float, last: float
) -> tuple[float, ...]:
    quadratic = -first + 3 * control1 - 3 * control2 + last
    linear = 2 * (first - 2 * control1 + control2)
    constant = control1 - first
    if abs(quadratic) <= 1e-15:
        if abs(linear) <= 1e-15:
            return ()
        root = -constant / linear
        return (root,) if 0 < root < 1 else ()
    discriminant = linear * linear - 4 * quadratic * constant
    if discriminant < 0:
        return ()
    square_root = math.sqrt(max(0.0, discriminant))
    roots = (
        (-linear - square_root) / (2 * quadratic),
        (-linear + square_root) / (2 * quadratic),
    )
    return tuple(root for root in roots if 0 < root < 1)


def _cubic_length(
    start: Point2D,
    control1: Point2D,
    control2: Point2D,
    end: Point2D,
) -> float:
    def speed(t: float) -> float:
        inverse = 1 - t
        dx = 3 * (
            inverse**2 * (control1.x - start.x)
            + 2 * inverse * t * (control2.x - control1.x)
            + t**2 * (end.x - control2.x)
        )
        dy = 3 * (
            inverse**2 * (control1.y - start.y)
            + 2 * inverse * t * (control2.y - control1.y)
            + t**2 * (end.y - control2.y)
        )
        return math.hypot(dx, dy)

    def simpson(a: float, b: float, fa: float, fm: float, fb: float) -> float:
        return (b - a) * (fa + 4 * fm + fb) / 6

    def integrate(
        a: float,
        b: float,
        fa: float,
        fm: float,
        fb: float,
        whole: float,
        tolerance: float,
        depth: int,
    ) -> float:
        midpoint = (a + b) / 2
        left_midpoint = (a + midpoint) / 2
        right_midpoint = (midpoint + b) / 2
        left_middle = speed(left_midpoint)
        right_middle = speed(right_midpoint)
        left = simpson(a, midpoint, fa, left_middle, fm)
        right = simpson(midpoint, b, fm, right_middle, fb)
        delta = left + right - whole
        if depth <= 0 or abs(delta) <= 15 * tolerance:
            return left + right + delta / 15
        return integrate(
            a,
            midpoint,
            fa,
            left_middle,
            fm,
            left,
            tolerance / 2,
            depth - 1,
        ) + integrate(
            midpoint,
            b,
            fm,
            right_middle,
            fb,
            right,
            tolerance / 2,
            depth - 1,
        )

    start_speed = speed(0)
    middle_speed = speed(0.5)
    end_speed = speed(1)
    initial = simpson(0, 1, start_speed, middle_speed, end_speed)
    return integrate(0, 1, start_speed, middle_speed, end_speed, initial, 1e-10, 18)


def lane_centerline_length(lane: Lane) -> float:
    """Return the total metric arc length of every lane centerline segment."""

    total = 0.0
    for index, segment in enumerate(lane.segments):
        start = lane.anchors[index].point
        end = lane.anchors[(index + 1) % len(lane.anchors)].point
        if segment.kind is SegmentKind.LINE:
            total += math.hypot(end.x - start.x, end.y - start.y)
        elif segment.kind is SegmentKind.ARC:
            if segment.arc_center is None or segment.clockwise is None:
                raise ValueError("arc segment center and direction are required")
            radius, _start_angle, sweep = _arc_parameters(
                start,
                segment.arc_center,
                end,
                clockwise=segment.clockwise,
            )
            total += radius * abs(sweep)
        else:
            if segment.control1 is None or segment.control2 is None:
                raise ValueError("cubic segment control points are required")
            total += _cubic_length(start, segment.control1, segment.control2, end)
    return total


def lane_centerline_bounds(lane: Lane) -> tuple[float, float, float, float]:
    """Return exact bounds for lines/arcs and analytic extrema for cubic segments."""

    candidates: list[Point2D] = []
    for index, segment in enumerate(lane.segments):
        start = lane.anchors[index].point
        end = lane.anchors[(index + 1) % len(lane.anchors)].point
        candidates.extend((start, end))
        if segment.kind is SegmentKind.ARC:
            if segment.arc_center is None or segment.clockwise is None:
                raise ValueError("arc segment center and direction are required")
            radius, start_angle, sweep = _arc_parameters(
                start,
                segment.arc_center,
                end,
                clockwise=segment.clockwise,
            )
            for angle in (0.0, math.pi / 2, math.pi, 1.5 * math.pi):
                relative = (
                    -((start_angle - angle) % math.tau)
                    if segment.clockwise
                    else (angle - start_angle) % math.tau
                )
                if min(0.0, sweep) - 1e-12 <= relative <= max(0.0, sweep) + 1e-12:
                    candidates.append(
                        Point2D(
                            segment.arc_center.x + radius * math.cos(angle),
                            segment.arc_center.y + radius * math.sin(angle),
                        )
                    )
        elif segment.kind is SegmentKind.CUBIC:
            if segment.control1 is None or segment.control2 is None:
                raise ValueError("cubic segment control points are required")
            roots = set(
                _cubic_derivative_roots(
                    start.x, segment.control1.x, segment.control2.x, end.x
                )
            )
            roots.update(
                _cubic_derivative_roots(
                    start.y, segment.control1.y, segment.control2.y, end.y
                )
            )
            for t in roots:
                candidates.append(
                    Point2D(
                        _cubic_value(start.x, segment.control1.x, segment.control2.x, end.x, t),
                        _cubic_value(start.y, segment.control1.y, segment.control2.y, end.y, t),
                    )
                )
    if not candidates:
        point = lane.anchors[0].point
        return point.x, point.y, point.x, point.y
    xs = [point.x for point in candidates]
    ys = [point.y for point in candidates]
    return min(xs), min(ys), max(xs), max(ys)


def _transform_lane(lane: Lane, transform: Callable[[Point2D], Point2D]) -> Lane:
    transformed = copy.deepcopy(lane)
    for anchor in transformed.anchors:
        anchor.point = transform(anchor.point)
    for segment in transformed.segments:
        if segment.control1 is not None:
            segment.control1 = transform(segment.control1)
        if segment.control2 is not None:
            segment.control2 = transform(segment.control2)
        if segment.arc_center is not None:
            segment.arc_center = transform(segment.arc_center)
    return transformed


def scale_lane_to_length(lane: Lane, target_length: float) -> Lane:
    """Uniformly scale all centerline geometry around its actual bounds center."""

    if not math.isfinite(target_length) or target_length <= 0:
        raise ValueError("车道长度必须大于零")
    current_length = lane_centerline_length(lane)
    if current_length <= 1e-12:
        raise ValueError("零长度车道无法按比例缩放")
    minimum_x, minimum_y, maximum_x, maximum_y = lane_centerline_bounds(lane)
    center_x = (minimum_x + maximum_x) / 2
    center_y = (minimum_y + maximum_y) / 2
    factor = target_length / current_length
    return _transform_lane(
        lane,
        lambda point: Point2D(
            center_x + (point.x - center_x) * factor,
            center_y + (point.y - center_y) * factor,
        ),
    )


def translate_lane(lane: Lane, delta_x: float, delta_y: float) -> Lane:
    """Move all lane geometry by one raw-coordinate delta."""

    if not math.isfinite(delta_x) or not math.isfinite(delta_y):
        raise ValueError("车道平移量必须是有限数")
    return _transform_lane(
        lane,
        lambda point: Point2D(point.x + delta_x, point.y + delta_y),
    )


def lane_segment_points(lane: Lane, index: int, *, tolerance: float = 0.02) -> list[Point2D]:
    start = lane.anchors[index].point
    end = lane.anchors[(index + 1) % len(lane.anchors)].point
    segment = lane.segments[index]
    if segment.kind is SegmentKind.LINE:
        return [start, end]
    if segment.kind is SegmentKind.ARC:
        if segment.arc_center is None or segment.clockwise is None:
            raise ValueError("arc segment center and direction are required")
        return flatten_arc(
            start,
            segment.arc_center,
            end,
            clockwise=segment.clockwise,
            tolerance=tolerance,
        )
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
