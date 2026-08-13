import math

import pytest
from shapely.geometry import Point

from route_analysis.geometry import (
    build_lane_area,
    build_traversable_area,
    flatten_cubic,
    interpolate_poses,
    shortest_angle_delta,
    vehicle_polygon,
)
from route_analysis.models import (
    JoinStyle,
    Lane,
    LaneAnchor,
    LaneSegment,
    Point2D,
    PosePoint,
    SegmentKind,
    VehicleDimensions,
)


def test_vehicle_polygon_uses_front_axle_center_and_yaw() -> None:
    dimensions = VehicleDimensions(width=2.0, center_front=1.0, center_rear=3.0)

    east = vehicle_polygon(PosePoint(10.0, 20.0, 0.0), dimensions)
    north = vehicle_polygon(PosePoint(10.0, 20.0, math.pi / 2), dimensions)

    assert east.bounds == pytest.approx((7.0, 19.0, 11.0, 21.0))
    assert north.bounds == pytest.approx((9.0, 17.0, 11.0, 21.0))


def test_shortest_angle_interpolation_crosses_pi_without_full_rotation() -> None:
    start = math.radians(179)
    end = math.radians(-179)

    assert shortest_angle_delta(start, end) == pytest.approx(math.radians(2))
    poses = interpolate_poses(
        PosePoint(0, 0, start),
        PosePoint(0, 0, end),
        position_step=1,
        yaw_step=math.radians(0.5),
    )

    assert len(poses) == 5
    assert abs(abs(poses[2].yaw or 0) - math.pi) < 1e-9


def test_interpolation_obeys_stricter_of_position_and_yaw_steps() -> None:
    poses = interpolate_poses(
        PosePoint(0, 0, 0),
        PosePoint(1, 0, 0.2),
        position_step=0.3,
        yaw_step=0.04,
    )

    assert len(poses) == 6
    assert poses[-1].x == 1
    assert poses[-1].y == 0
    assert poses[-1].yaw == pytest.approx(0.2)


def test_flatten_cubic_preserves_endpoints_and_curve_side() -> None:
    points = flatten_cubic(
        Point2D(0, 0), Point2D(0, 2), Point2D(2, 2), Point2D(2, 0), tolerance=0.02
    )

    assert points[0] == Point2D(0, 0)
    assert points[-1] == Point2D(2, 0)
    assert max(point.y for point in points) == pytest.approx(1.5, abs=0.03)


def test_open_lane_has_flat_caps_and_expected_total_width() -> None:
    lane = Lane.create("lane", "Lane", 2.0, [Point2D(0, 0), Point2D(10, 0)])

    area = build_lane_area(lane)

    assert area.bounds == pytest.approx((0, -1, 10, 1))
    assert area.area == pytest.approx(20)
    assert not area.covers(Point(-0.01, 0))


def test_cubic_lane_and_closed_lane_are_supported() -> None:
    cubic = Lane(
        id="curve",
        name="Curve",
        width=1.0,
        anchors=[LaneAnchor(Point2D(0, 0)), LaneAnchor(Point2D(2, 0))],
        segments=[
            LaneSegment(
                SegmentKind.CUBIC,
                control1=Point2D(0, 2),
                control2=Point2D(2, 2),
            )
        ],
    )
    loop = Lane.create(
        "loop",
        "Loop",
        1.0,
        [Point2D(0, 0), Point2D(4, 0), Point2D(4, 4), Point2D(0, 4)],
        closed=True,
    )

    assert build_lane_area(cubic).covers(Point(1, 1.5))
    assert build_lane_area(loop).covers(Point(0, 2))


def test_round_and_miter_join_create_different_outer_corners() -> None:
    points = [Point2D(0, 0), Point2D(2, 0), Point2D(2, 2)]
    sharp = Lane.create("sharp", "Sharp", 2.0, points, default_join=JoinStyle.MITER)
    rounded = Lane.create("round", "Round", 2.0, points, default_join=JoinStyle.MITER)
    rounded.anchors[1].join_override = JoinStyle.ROUND

    sharp_area = build_lane_area(sharp)
    round_area = build_lane_area(rounded)

    assert sharp_area.covers(Point(3, -1))
    assert not round_area.covers(Point(2.95, -0.95))


def test_miter_limit_truncates_an_acute_join() -> None:
    lane = Lane.create(
        "acute",
        "Acute",
        2,
        [Point2D(0, 0), Point2D(2, 0), Point2D(0.2, 0.2)],
    )

    truncated = build_lane_area(lane, miter_limit=1)
    unbounded = build_lane_area(lane, miter_limit=10)

    assert truncated.bounds[2] < 3.1
    assert unbounded.bounds[2] > 10


def test_enabled_lanes_are_unioned() -> None:
    first = Lane.create("a", "A", 2, [Point2D(0, 0), Point2D(2, 0)])
    second = Lane.create("b", "B", 2, [Point2D(2, 0), Point2D(4, 0)])
    disabled = Lane.create("c", "C", 10, [Point2D(100, 0), Point2D(101, 0)])
    disabled.enabled = False

    union = build_traversable_area([first, second, disabled])

    assert union.covers(Point(3.5, 0.9))
    assert not union.covers(Point(100, 0))
