import math
from itertools import pairwise

import pytest

from route_analysis.clearance_solver import (
    LaneContext,
    SegmentRole,
    analyse_clearance,
    best_clearance,
    corner_for_segment,
    couple_bands,
    solve_band,
    solve_corner,
    solve_width_zones,
    split_segments,
)
from route_analysis.models import (
    AnalysisSettings,
    ClearanceStatus,
    Lane,
    Point2D,
    PosePoint,
    VehicleDimensions,
)
from route_analysis.turn_radius import TurnSide

DIMENSIONS = VehicleDimensions(width=1.20, center_front=1.00, center_rear=1.60)


def _arc(centre_x: float, centre_y: float, radius: float, start: float, end: float, steps: int,
         sign: float) -> list[PosePoint]:
    poses = []
    for step in range(1, steps + 1):
        angle = start + (end - start) * step / steps
        poses.append(
            PosePoint(
                centre_x + radius * math.cos(angle),
                centre_y + radius * math.sin(angle),
                angle + sign * math.pi / 2,
            )
        )
    return poses


def _two_turn_path() -> list[PosePoint]:
    """East along a main run, left into a branch, north, then right back east."""

    poses = [PosePoint(-12 + index * 0.5, 0.0, 0.0) for index in range(25)]
    poses += _arc(0.0, 1.2, 1.2, -math.pi / 2, 0.0, 24, 1.0)
    poses += [PosePoint(1.2, 1.2 + index * 0.5, math.pi / 2) for index in range(1, 17)]
    poses += _arc(2.4, 9.2, 1.2, math.pi, math.pi / 2, 24, -1.0)
    poses += [PosePoint(2.4 + index * 0.5, 10.4, 0.0) for index in range(1, 21)]
    return poses


def _lanes(branch_width: float = 2.6) -> list[Lane]:
    return [
        Lane.create("main", "主通道", 3.4, [Point2D(-16, 0), Point2D(1.2, 0), Point2D(1.2, 1.2)]),
        Lane.create(
            "branch", "支通道", branch_width,
            [Point2D(1.2, 0.6), Point2D(1.2, 10.4), Point2D(26, 10.4)],
        ),
    ]


def _straight_corridor() -> tuple[list[PosePoint], list[Lane]]:
    poses = [PosePoint(index * 0.5, 0.0, 0.0) for index in range(21)]
    lanes = [Lane.create("main", "主通道", 3.0, [Point2D(-4, 0), Point2D(14, 0)])]
    return poses, lanes


def test_split_segments_keeps_a_straight_run_between_two_opposite_turns() -> None:
    segments = split_segments(_two_turn_path(), turn_threshold=math.pi / 6)
    roles = [segment.role for segment in segments]
    assert roles == [
        SegmentRole.STRAIGHT,
        SegmentRole.TURN,
        SegmentRole.STRAIGHT,
        SegmentRole.TURN,
        SegmentRole.STRAIGHT,
    ]
    assert [segment.side for segment in segments if segment.side] == [
        TurnSide.LEFT,
        TurnSide.RIGHT,
    ]


def test_segments_cover_the_path_without_gaps_and_carry_one_based_labels() -> None:
    poses = _two_turn_path()
    segments = split_segments(poses, turn_threshold=math.pi / 6)
    assert segments[0].start_pose == 0
    assert segments[-1].end_pose == len(poses) - 1
    for first, second in pairwise(segments):
        assert first.end_pose == second.start_pose
    assert segments[0].start_label == 1
    assert segments[-1].end_label == len(poses)


def test_segment_labels_use_custom_sample_numbers() -> None:
    poses, _ = _straight_corridor()
    labels = [index * 2 + 1 for index in range(len(poses))]
    segments = split_segments(poses, turn_threshold=math.pi / 6, sample_labels=labels)
    assert segments[0].start_label == 1
    assert segments[0].end_label == labels[-1]


def test_offsets_are_worded_by_turn_direction_not_by_sign() -> None:
    segments = split_segments(_two_turn_path(), turn_threshold=math.pi / 6)
    left = next(item for item in segments if item.side is TurnSide.LEFT)
    right = next(item for item in segments if item.side is TurnSide.RIGHT)
    straight = segments[0]
    assert left.describe_offset(0.21) == "向内 0.21"
    assert right.describe_offset(0.21) == "向外 0.21"
    assert right.describe_offset(-0.21) == "向内 0.21"
    assert straight.describe_offset(0.21) == "左 0.21"
    assert straight.describe_offset(0.0) == "居中"


def test_band_in_a_straight_corridor_is_symmetric_about_the_centreline() -> None:
    poses, lanes = _straight_corridor()
    settings = AnalysisSettings()
    context = LaneContext(lanes, settings)
    segment = split_segments(poses, turn_threshold=math.pi / 6)[0]
    band = solve_band(poses, segment, DIMENSIONS, context.area(), settings)
    assert band.feasible
    assert band.low == pytest.approx(-band.high, abs=0.02)
    assert band.high == pytest.approx((3.0 - 1.20) / 2 - settings.clearance_threshold, abs=0.02)
    assert band.contains(0.0)
    assert band.nearest_edge(0.0) is None


def test_a_corridor_narrower_than_the_vehicle_has_no_feasible_band() -> None:
    poses = [PosePoint(index * 0.5, 0.0, 0.0) for index in range(21)]
    lanes = [Lane.create("main", "主通道", 0.8, [Point2D(-4, 0), Point2D(14, 0)])]
    settings = AnalysisSettings()
    context = LaneContext(lanes, settings)
    segment = split_segments(poses, turn_threshold=math.pi / 6)[0]
    band = solve_band(poses, segment, DIMENSIONS, context.area(), settings)
    assert not band.feasible
    assert band.width == 0.0
    assert band.nearest_edge(0.0) is None
    coupled = couple_bands([segment], [band])
    assert not coupled[0].conflicting
    assert coupled[0].shortfall == 0.0


def test_nearest_edge_is_the_smallest_move_back_into_the_band() -> None:
    poses, lanes = _straight_corridor()
    settings = AnalysisSettings()
    context = LaneContext(lanes, settings)
    segment = split_segments(poses, turn_threshold=math.pi / 6)[0]
    band = solve_band(poses, segment, DIMENSIONS, context.area(), settings)
    assert band.nearest_edge(band.high + 0.5) == pytest.approx(band.high)
    assert band.nearest_edge(band.low - 0.5) == pytest.approx(band.low)


def test_a_connecting_run_takes_the_intersection_of_both_turns() -> None:
    segments = split_segments(_two_turn_path(), turn_threshold=math.pi / 6)
    from route_analysis.clearance_solver import OffsetBand

    bands = [
        OffsetBand(0, -1.0, 1.0, 0.0, 0.5),
        OffsetBand(1, 0.10, 0.40, 0.25, 0.3),
        OffsetBand(2, -1.0, 1.0, 0.0, 0.5),
        OffsetBand(3, 0.30, 0.80, 0.55, 0.3),
        OffsetBand(4, -1.0, 1.0, 0.0, 0.5),
    ]
    coupled = couple_bands(segments, bands)
    middle = next(item for item in coupled if item.segment_index == 2)
    assert middle.low == pytest.approx(0.30)
    assert middle.high == pytest.approx(0.40)
    assert middle.feasible
    assert middle.sources == (2, 1, 3)


def test_incompatible_turns_leave_the_connecting_run_in_conflict() -> None:
    segments = split_segments(_two_turn_path(), turn_threshold=math.pi / 6)
    from route_analysis.clearance_solver import OffsetBand

    bands = [
        OffsetBand(0, -1.0, 1.0, 0.0, 0.5),
        OffsetBand(1, -0.60, -0.30, -0.45, 0.3),
        OffsetBand(2, -1.0, 1.0, 0.0, 0.5),
        OffsetBand(3, 0.30, 0.80, 0.55, 0.3),
        OffsetBand(4, -1.0, 1.0, 0.0, 0.5),
    ]
    coupled = couple_bands(segments, bands)
    middle = next(item for item in coupled if item.segment_index == 2)
    assert middle.conflicting
    assert not middle.feasible
    assert middle.shortfall == pytest.approx(0.60)


def test_width_zones_need_at_least_as_much_room_centred_as_offset() -> None:
    poses = _two_turn_path()
    lanes = _lanes()
    settings = AnalysisSettings()
    context = LaneContext(lanes, settings)
    segments = analyse_clearance(poses, DIMENSIONS, lanes, settings)
    assert segments is not None
    turn = next(
        item
        for item in segments.segments
        if item.role is SegmentRole.TURN and item.lane_name == "支通道"
    )
    zones = solve_width_zones(poses, turn, DIMENSIONS, context, settings)
    assert zones is not None
    assert zones.lane_name == "支通道"
    assert zones.offset_limit is not None and zones.centred_limit is not None
    assert zones.offset_limit <= zones.centred_limit
    assert zones.scale_low < zones.measured < zones.scale_high


def test_width_zones_are_unavailable_off_lane() -> None:
    poses, lanes = _straight_corridor()
    settings = AnalysisSettings()
    context = LaneContext(lanes, settings)
    segment = split_segments(poses, turn_threshold=math.pi / 6)[0]
    assert solve_width_zones(poses, segment, DIMENSIONS, context, settings) is None


def test_analyse_clearance_names_segments_after_the_lane_covering_them() -> None:
    poses = _two_turn_path()
    analysis = analyse_clearance(poses, DIMENSIONS, _lanes(), AnalysisSettings())
    assert analysis is not None
    names = {segment.lane_name for segment in analysis.segments}
    assert names == {"主通道", "支通道"}
    assert analysis.segments[1].label.startswith("点位 ")
    assert "左转" in analysis.segments[1].label
    assert analysis.segments[1].label.endswith("主通道")


def test_analyse_clearance_ranks_the_tightest_segment_first() -> None:
    poses = _two_turn_path()
    analysis = analyse_clearance(poses, DIMENSIONS, _lanes(), AnalysisSettings())
    assert analysis is not None
    clearances = [item.clearance for item in analysis.bottlenecks]
    assert clearances == sorted(clearances)
    assert analysis.bottlenecks[0].rank == 1
    assert analysis.bottlenecks[0].segment.role is SegmentRole.TURN
    assert len(analysis.profile) == analysis.analyzed_samples
    assert all(0.0 <= sample.progress <= 1.0 for sample in analysis.profile)


def test_a_narrow_branch_reports_a_breach_that_offsets_cannot_repair() -> None:
    poses = _two_turn_path()
    analysis = analyse_clearance(poses, DIMENSIONS, _lanes(branch_width=1.8), AnalysisSettings())
    assert analysis is not None
    assert analysis.status is ClearanceStatus.OUTSIDE
    assert analysis.deepest_breach is not None and analysis.deepest_breach < 0
    worst = analysis.bottlenecks[0]
    assert not worst.band_feasible
    assert worst.offset_text == "无可行偏置"


def test_analyse_clearance_needs_a_traversable_area() -> None:
    poses, _ = _straight_corridor()
    assert analyse_clearance(poses, DIMENSIONS, [], AnalysisSettings()) is None


def test_analyse_clearance_needs_two_usable_poses() -> None:
    _, lanes = _straight_corridor()
    single = [PosePoint(0.0, 0.0, 0.0), PosePoint(1.0, 0.0, None)]
    assert analyse_clearance(single, DIMENSIONS, lanes, AnalysisSettings()) is None


def test_best_clearance_never_beats_a_full_band_search() -> None:
    poses, lanes = _straight_corridor()
    settings = AnalysisSettings()
    context = LaneContext(lanes, settings)
    segment = split_segments(poses, turn_threshold=math.pi / 6)[0]
    band = solve_band(poses, segment, DIMENSIONS, context.area(), settings)
    probe = best_clearance(poses, segment, DIMENSIONS, context.area(), settings)
    assert probe == pytest.approx(band.best_clearance, abs=1e-9)


def test_the_third_degree_of_freedom_never_loses_ground() -> None:
    poses = _two_turn_path()
    lanes = _lanes()
    settings = AnalysisSettings()
    context = LaneContext(lanes, settings)
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, settings)
    assert analysis is not None
    turn = next(
        item
        for item in analysis.segments
        if item.role is SegmentRole.TURN and item.lane_name == "支通道"
    )
    corner = corner_for_segment(poses, turn)
    assert corner is not None
    solution = solve_corner(corner, DIMENSIONS, context.area(), settings)
    assert solution is not None
    assert solution.two_degree_clearance >= solution.baseline_clearance
    assert solution.clearance >= solution.two_degree_clearance
    assert solution.third_degree_gain >= 0
    assert all(item.feasible for item in solution.ranges.values())
    assert solution.ranges["arc_start_shift"].width > 0


def test_corner_fitting_only_applies_to_turns() -> None:
    poses = _two_turn_path()
    segments = split_segments(poses, turn_threshold=math.pi / 6)
    assert corner_for_segment(poses, segments[0]) is None
    corner = corner_for_segment(poses, segments[1])
    assert corner is not None
    assert corner.radius == pytest.approx(1.2, abs=0.02)
    assert corner.residual < 0.01
