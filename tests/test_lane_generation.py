import math
import time

import pytest
from shapely.geometry import LineString, Point

from route_analysis.geometry import (
    build_lane_area,
    envelope_overhang,
    lane_centerline_length,
    lane_segment_points,
    vehicle_polygon,
)
from route_analysis.lane_generation import (
    BendMode,
    ConnectionMode,
    LaneGenerationResult,
    arc_radius,
    extend_to_cover,
    generate_lane,
    generate_lane_between,
    maximum_arc_radius,
    replace_arc_radius,
)
from route_analysis.models import Point2D, PosePoint, SegmentKind, VehicleDimensions


def _generated_line(result: LaneGenerationResult) -> LineString:
    lane = result.lane
    coordinates: list[tuple[float, float]] = []
    for index in range(len(lane.segments)):
        flattened = lane_segment_points(lane, index, tolerance=0.002)
        segment_coordinates = [(point.x, point.y) for point in flattened]
        coordinates.extend(segment_coordinates if not coordinates else segment_coordinates[1:])
    if lane.closed:
        coordinates.append(coordinates[0])
    return LineString(coordinates)


def test_sharp_generation_deduplicates_and_simplifies_within_tolerance() -> None:
    source = [
        PosePoint(0, 0, None),
        PosePoint(0, 0, 0),
        PosePoint(1, 0.01, 0),
        PosePoint(2, 0, 0),
    ]

    result = generate_lane(
        source,
        lane_id="generated",
        name="Generated",
        width=2,
        mode=BendMode.SHARP,
        maximum_deviation=0.05,
    )

    assert len(result.lane.anchors) == 2
    assert [segment.kind for segment in result.lane.segments] == [SegmentKind.LINE]
    assert result.metrics.maximum_deviation <= 0.05
    assert result.metrics.source_points == 4
    assert result.metrics.unique_points == 3


def test_round_generation_fits_true_arcs_and_hard_corner_falls_back() -> None:
    rounded = [Point2D(-2, 0), Point2D(-1, 0)]
    rounded.extend(
        Point2D(-1 + math.cos(angle), 1 + math.sin(angle))
        for angle in [
            -math.pi / 2,
            -3 * math.pi / 8,
            -math.pi / 4,
            -math.pi / 8,
            0,
        ]
    )
    rounded.append(Point2D(0, 2))

    result = generate_lane(
        rounded,
        lane_id="round",
        name="Round",
        width=2,
        mode=BendMode.ROUND,
        maximum_deviation=0.08,
    )

    arcs = [segment for segment in result.lane.segments if segment.kind is SegmentKind.ARC]
    assert arcs
    assert result.metrics.maximum_deviation <= 0.08
    assert result.metrics.arc_failures == 0
    radii = [
        math.hypot(
            result.lane.anchors[index].point.x - segment.arc_center.x,
            result.lane.anchors[index].point.y - segment.arc_center.y,
        )
        for index, segment in enumerate(result.lane.segments)
        if segment.kind is SegmentKind.ARC and segment.arc_center is not None
    ]
    assert max(radii) == pytest.approx(1, abs=0.15)

    hard_corner = generate_lane(
        [Point2D(-2, 0), Point2D(0, 0), Point2D(0, 2)],
        lane_id="hard",
        name="Hard",
        width=2,
        mode=BendMode.ROUND,
        maximum_deviation=0.05,
    )

    assert all(segment.kind is SegmentKind.LINE for segment in hard_corner.lane.segments)
    assert hard_corner.metrics.arc_failures == 1


def test_bezier_generation_splits_to_meet_error_and_keeps_tangent_continuity() -> None:
    source = [Point2D(index * 0.25, math.sin(index * 0.25)) for index in range(17)]

    result = generate_lane(
        source,
        lane_id="bezier",
        name="Bezier",
        width=2,
        mode=BendMode.BEZIER,
        maximum_deviation=0.02,
    )

    assert result.lane.segments
    assert all(segment.kind is SegmentKind.CUBIC for segment in result.lane.segments)
    assert result.metrics.maximum_deviation <= 0.02
    for index in range(1, len(result.lane.segments)):
        previous = result.lane.segments[index - 1]
        following = result.lane.segments[index]
        anchor = result.lane.anchors[index].point
        assert previous.control2 is not None
        assert following.control1 is not None
        incoming = (anchor.x - previous.control2.x, anchor.y - previous.control2.y)
        outgoing = (following.control1.x - anchor.x, following.control1.y - anchor.y)
        cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
        assert cross == pytest.approx(0, abs=1e-6)
        assert dot > 0


def test_closed_generation_removes_repeated_endpoint_and_closes_lane() -> None:
    source = [
        Point2D(0, 0),
        Point2D(2, 0),
        Point2D(2, 2),
        Point2D(0, 2),
        Point2D(0, 0),
    ]

    result = generate_lane(
        source,
        lane_id="loop",
        name="Loop",
        width=1,
        mode=BendMode.SHARP,
        maximum_deviation=0.01,
        closed=True,
    )

    assert result.lane.closed is True
    assert len(result.lane.anchors) == 4
    assert len(result.lane.segments) == 4
    generated = _generated_line(result)
    assert all(generated.distance(Point(point.x, point.y)) <= 0.01 for point in source)


def test_closed_round_generation_fits_cyclic_arc_windows() -> None:
    source = [
        Point2D(1, 0),
        Point2D(3, 0),
        Point2D(3 + math.sqrt(0.5), 1 - math.sqrt(0.5)),
        Point2D(4, 1),
        Point2D(4, 3),
        Point2D(3 + math.sqrt(0.5), 3 + math.sqrt(0.5)),
        Point2D(3, 4),
        Point2D(1, 4),
        Point2D(1 - math.sqrt(0.5), 3 + math.sqrt(0.5)),
        Point2D(0, 3),
        Point2D(0, 1),
        Point2D(1 - math.sqrt(0.5), 1 - math.sqrt(0.5)),
        Point2D(1, 0),
    ]

    result = generate_lane(
        source,
        lane_id="rounded-loop",
        name="Rounded loop",
        width=1,
        mode=BendMode.ROUND,
        maximum_deviation=0.05,
        closed=True,
    )

    assert result.lane.closed is True
    assert sum(segment.kind is SegmentKind.ARC for segment in result.lane.segments) == 4
    assert result.metrics.arc_failures == 0
    assert result.metrics.maximum_deviation <= 0.05


def test_generation_rejects_fewer_than_two_unique_points() -> None:
    with pytest.raises(ValueError, match="at least two unique"):
        generate_lane(
            [Point2D(1, 1), Point2D(1, 1)],
            lane_id="bad",
            name="Bad",
            width=1,
            mode=BendMode.SHARP,
            maximum_deviation=0.05,
        )


def test_arc_radius_edit_preserves_tangent_lines_and_rejects_oversize() -> None:
    source = [Point2D(-2, 0), Point2D(-1, 0)]
    source.extend(
        Point2D(-1 + math.cos(angle), 1 + math.sin(angle))
        for angle in [-math.pi / 2, -math.pi / 4, 0]
    )
    source.append(Point2D(0, 2))
    generated = generate_lane(
        source,
        lane_id="editable",
        name="Editable",
        width=2,
        mode=BendMode.ROUND,
        maximum_deviation=0.08,
    )
    arc_index = next(
        index
        for index, segment in enumerate(generated.lane.segments)
        if segment.kind is SegmentKind.ARC
    )

    assert arc_radius(generated.lane, arc_index) == pytest.approx(1)
    assert maximum_arc_radius(generated.lane, arc_index) == pytest.approx(2)

    edited = replace_arc_radius(generated.lane, arc_index, 0.5)

    assert arc_radius(edited, arc_index) == pytest.approx(0.5)
    assert edited.anchors[arc_index].point.x == pytest.approx(-0.5)
    assert edited.anchors[arc_index].point.y == pytest.approx(0)
    assert edited.anchors[arc_index + 1].point.x == pytest.approx(0)
    assert edited.anchors[arc_index + 1].point.y == pytest.approx(0.5)
    center = edited.segments[arc_index].arc_center
    assert center is not None
    assert center.x == pytest.approx(-0.5)
    assert center.y == pytest.approx(0.5)
    assert arc_radius(generated.lane, arc_index) == pytest.approx(1)

    with pytest.raises(ValueError, match="maximum radius is 2"):
        replace_arc_radius(generated.lane, arc_index, 2.01)


def test_dense_round_path_is_simplified_before_arc_window_search() -> None:
    sample_count = 600
    source = [Point2D(-2, 0)]
    source.extend(
        Point2D(
            math.cos(-math.pi / 2 + math.pi * index / (sample_count - 1)),
            1 + math.sin(-math.pi / 2 + math.pi * index / (sample_count - 1)),
        )
        for index in range(sample_count)
    )
    source.append(Point2D(-1, 2))
    started = time.perf_counter()

    result = generate_lane(
        source,
        lane_id="dense",
        name="Dense",
        width=2,
        mode=BendMode.ROUND,
        maximum_deviation=0.01,
    )

    assert time.perf_counter() - started < 1.5
    assert any(segment.kind is SegmentKind.ARC for segment in result.lane.segments)
    assert result.metrics.maximum_deviation <= 0.01


def test_dense_jagged_round_source_has_bounded_arc_search_time() -> None:
    source = [
        Point2D(index * 0.01, 0.02 if index % 2 else -0.02)
        for index in range(1200)
    ]
    started = time.perf_counter()

    result = generate_lane(
        source,
        lane_id="jagged",
        name="Jagged",
        width=2,
        mode=BendMode.ROUND,
        maximum_deviation=0.005,
    )

    assert time.perf_counter() - started < 1.5
    assert result.metrics.maximum_deviation <= 0.005


DIMENSIONS = VehicleDimensions(width=1.20, center_front=1.00, center_rear=1.60)


def _straight(count: int = 11, *, yaw: float = 0.0) -> list[PosePoint]:
    return [PosePoint(index * 1.0, 0.0, yaw) for index in range(count)]


def _cornering() -> list[PosePoint]:
    poses = [PosePoint(-4 + index * 1.0, 0.0, 0.0) for index in range(5)]
    for step in range(1, 13):
        angle = -math.pi / 2 + math.pi / 2 * step / 12
        poses.append(
            PosePoint(2.0 * math.cos(angle), 2.0 + 2.0 * math.sin(angle), angle + math.pi / 2)
        )
    poses.extend(PosePoint(2.0, 2.0 + index * 1.0, math.pi / 2) for index in range(1, 5))
    return poses


def test_envelope_overhang_matches_the_two_centre_distances() -> None:
    pose = PosePoint(0.0, 0.0, 0.0)
    assert envelope_overhang(pose, DIMENSIONS, 0.0) == pytest.approx(1.00)
    assert envelope_overhang(pose, DIMENSIONS, math.pi) == pytest.approx(1.60)
    assert envelope_overhang(pose, DIMENSIONS, math.pi / 2) == pytest.approx(0.60)
    assert envelope_overhang(pose, DIMENSIONS, math.pi / 4) == pytest.approx(
        1.60 / math.sqrt(2)
    )


def test_envelope_overhang_needs_a_yaw() -> None:
    with pytest.raises(ValueError, match="yaw"):
        envelope_overhang(PosePoint(0.0, 0.0, None), DIMENSIONS, 0.0)


def test_extension_covers_the_rear_at_the_start_and_the_front_at_the_end() -> None:
    poses = _straight()
    points = [Point2D(pose.x, pose.y) for pose in poses]
    extended, start_overhang, end_overhang = extend_to_cover(
        points, poses[0], poses[-1], DIMENSIONS
    )
    assert start_overhang == pytest.approx(1.60)
    assert end_overhang == pytest.approx(1.00)
    assert extended[0].x == pytest.approx(-1.60)
    assert extended[-1].x == pytest.approx(11.00)


def test_a_reversing_run_swaps_which_distance_governs_each_end() -> None:
    poses = _straight(yaw=math.pi)
    points = [Point2D(pose.x, pose.y) for pose in poses]
    _extended, start_overhang, end_overhang = extend_to_cover(
        points, poses[0], poses[-1], DIMENSIONS
    )
    assert start_overhang == pytest.approx(1.00)
    assert end_overhang == pytest.approx(1.60)


def test_a_straight_connection_spans_exactly_the_two_samples_plus_both_overhangs() -> None:
    poses = _cornering()
    result = generate_lane_between(
        poses,
        DIMENSIONS,
        start_index=0,
        end_index=len(poses) - 1,
        connection=ConnectionMode.STRAIGHT,
        lane_id="L",
        name="直线",
        width=2.0,
        mode=BendMode.ROUND,
        maximum_deviation=0.05,
    )
    assert len(result.lane.anchors) == 2
    assert result.lane.segments[0].kind is SegmentKind.LINE
    span = math.dist(
        (poses[0].x, poses[0].y), (poses[-1].x, poses[-1].y)
    ) + result.metrics.start_overhang + result.metrics.end_overhang
    assert lane_centerline_length(result.lane) == pytest.approx(span, abs=1e-9)


def test_following_the_path_keeps_the_shape_a_straight_line_would_lose() -> None:
    poses = _cornering()
    following = generate_lane_between(
        poses,
        DIMENSIONS,
        start_index=0,
        end_index=len(poses) - 1,
        connection=ConnectionMode.PATH,
        lane_id="L",
        name="沿路径",
        width=2.0,
        mode=BendMode.SHARP,
        maximum_deviation=0.05,
    )
    assert len(following.lane.anchors) > 2
    assert lane_centerline_length(following.lane) > math.dist(
        (poses[0].x, poses[0].y), (poses[-1].x, poses[-1].y)
    )


def test_a_generated_lane_covers_the_footprint_at_both_chosen_samples() -> None:
    poses = _straight()
    result = generate_lane_between(
        poses,
        DIMENSIONS,
        start_index=2,
        end_index=8,
        connection=ConnectionMode.PATH,
        lane_id="L",
        name="覆盖",
        width=2.0,
        mode=BendMode.SHARP,
        maximum_deviation=0.05,
    )
    area = build_lane_area(result.lane, tolerance=0.02, miter_limit=4.0)
    assert area.covers(vehicle_polygon(poses[2], DIMENSIONS))
    assert area.covers(vehicle_polygon(poses[8], DIMENSIONS))


def test_a_lane_that_is_narrower_than_the_vehicle_still_covers_both_ends_lengthwise() -> None:
    poses = _straight()
    result = generate_lane_between(
        poses,
        DIMENSIONS,
        start_index=0,
        end_index=5,
        connection=ConnectionMode.STRAIGHT,
        lane_id="L",
        name="窄",
        width=0.4,
        mode=BendMode.SHARP,
        maximum_deviation=0.05,
    )
    line = _generated_line(result)
    assert line.bounds[0] == pytest.approx(-1.60)
    assert line.bounds[2] == pytest.approx(6.00)


def test_choosing_the_samples_in_either_order_gives_the_same_lane() -> None:
    poses = _cornering()
    shared = {
        "connection": ConnectionMode.PATH,
        "lane_id": "L",
        "name": "顺序",
        "width": 2.0,
        "mode": BendMode.SHARP,
        "maximum_deviation": 0.05,
    }
    forward = generate_lane_between(poses, DIMENSIONS, start_index=3, end_index=15, **shared)
    reverse = generate_lane_between(poses, DIMENSIONS, start_index=15, end_index=3, **shared)
    assert [anchor.point for anchor in forward.lane.anchors] == [
        anchor.point for anchor in reverse.lane.anchors
    ]


def test_the_same_sample_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="同一个样本"):
        generate_lane_between(
            _straight(),
            DIMENSIONS,
            start_index=4,
            end_index=4,
            connection=ConnectionMode.PATH,
            lane_id="L",
            name="重复",
            width=2.0,
            mode=BendMode.SHARP,
            maximum_deviation=0.05,
        )


def test_an_endpoint_without_yaw_is_refused_because_it_has_no_footprint() -> None:
    poses = _straight()
    poses[7] = PosePoint(7.0, 0.0, None)
    with pytest.raises(ValueError, match="缺少 yaw"):
        generate_lane_between(
            poses,
            DIMENSIONS,
            start_index=0,
            end_index=7,
            connection=ConnectionMode.PATH,
            lane_id="L",
            name="缺 yaw",
            width=2.0,
            mode=BendMode.SHARP,
            maximum_deviation=0.05,
        )


def test_a_sample_without_yaw_in_the_middle_is_kept_as_an_anchor() -> None:
    poses = _cornering()
    poses[9] = PosePoint(poses[9].x, poses[9].y, None)
    result = generate_lane_between(
        poses,
        DIMENSIONS,
        start_index=0,
        end_index=len(poses) - 1,
        connection=ConnectionMode.PATH,
        lane_id="L",
        name="中间缺 yaw",
        width=2.0,
        mode=BendMode.SHARP,
        maximum_deviation=0.01,
    )
    assert len(result.lane.anchors) > 2


def test_two_coincident_samples_are_refused_only_for_a_straight_connection() -> None:
    poses = _straight()
    poses[6] = PosePoint(poses[0].x, poses[0].y, 0.0)
    shared = {
        "lane_id": "L",
        "name": "重合",
        "width": 2.0,
        "mode": BendMode.SHARP,
        "maximum_deviation": 0.05,
    }
    with pytest.raises(ValueError, match="坐标重合"):
        generate_lane_between(
            poses, DIMENSIONS, start_index=0, end_index=6,
            connection=ConnectionMode.STRAIGHT, **shared,
        )
    following = generate_lane_between(
        poses, DIMENSIONS, start_index=0, end_index=6,
        connection=ConnectionMode.PATH, **shared,
    )
    assert len(following.lane.anchors) >= 2


def test_an_index_outside_the_path_is_refused() -> None:
    with pytest.raises(ValueError, match="超出路径范围"):
        generate_lane_between(
            _straight(),
            DIMENSIONS,
            start_index=0,
            end_index=99,
            connection=ConnectionMode.PATH,
            lane_id="L",
            name="越界",
            width=2.0,
            mode=BendMode.SHARP,
            maximum_deviation=0.05,
        )
