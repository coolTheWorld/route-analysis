import math

import pytest
from shapely.geometry import LineString, Point

from route_analysis.geometry import lane_segment_points
from route_analysis.lane_generation import BendMode, LaneGenerationResult, generate_lane
from route_analysis.models import Point2D, PosePoint, SegmentKind


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
