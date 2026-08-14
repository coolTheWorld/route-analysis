import math

import pytest

from route_analysis.models import Point2D, PosePoint, VehicleDimensions
from route_analysis.turn_radius import (
    CornerRadiusKind,
    TurnKind,
    TurnSide,
    analyze_turn_radii,
    calculate_turn_radius,
    detect_turns,
)


def _arc_poses(
    radius: float,
    start: float,
    end: float,
    count: int,
    *,
    reverse_heading: bool = False,
) -> list[PosePoint]:
    poses = []
    for index in range(count):
        angle = start + (end - start) * index / (count - 1)
        yaw = angle + math.pi / 2 + (math.pi if reverse_heading else 0)
        poses.append(PosePoint(radius * math.cos(angle), radius * math.sin(angle), yaw))
    return poses


def test_turn_threshold_is_strict_and_yaw_is_unwrapped() -> None:
    exact = [PosePoint(index, 0, index * math.pi / 12) for index in range(3)]
    assert detect_turns(exact, threshold=math.pi / 6) == ()

    above = [PosePoint(index, 0, index * (math.pi / 12 + 1e-5)) for index in range(3)]
    sections = detect_turns(above, threshold=math.pi / 6)
    assert len(sections) == 1
    assert sections[0].cumulative_yaw > math.pi / 6

    wrapped = [
        PosePoint(0, 0, math.radians(170)),
        PosePoint(1, 0, math.radians(-170)),
        PosePoint(2, 0, math.radians(-150)),
    ]
    sections = detect_turns(wrapped, threshold=math.radians(30))
    assert len(sections) == 1
    assert sections[0].cumulative_yaw == pytest.approx(math.radians(40))


def test_missing_yaw_splits_automatic_sections_and_manual_interval_is_invalid() -> None:
    points = [
        PosePoint(0, 0, 0),
        PosePoint(1, 0, 0.4),
        PosePoint(2, 0, 0.8),
        PosePoint(3, 0, None),
        PosePoint(4, 0, 0),
        PosePoint(5, 0, -0.4),
        PosePoint(6, 0, -0.8),
    ]

    result = analyze_turn_radii(
        points,
        VehicleDimensions(width=2, center_front=3, center_rear=1),
        threshold=0.5,
    )

    assert len(result.turns) == 2
    assert result.missing_yaw_indices == (3,)
    assert result.incomplete is True
    assert result.turns[0].start_index == 0
    assert result.turns[0].end_index == 2
    assert result.turns[1].start_index == 4
    assert result.turns[1].end_index == 6

    manual = calculate_turn_radius(
        points,
        VehicleDimensions(width=2, center_front=3, center_rear=1),
        start_index=0,
        end_index=6,
    )
    assert manual.valid is False
    assert manual.error is not None
    assert "3" in manual.error


def test_small_reversal_offsets_current_turn_and_threshold_reversal_flips_it() -> None:
    small_reversal = [
        PosePoint(0, 0, 0),
        PosePoint(1, 0, 0.4),
        PosePoint(2, 0, 0.8),
        PosePoint(3, 0, 0.6),
        PosePoint(4, 0, 1.0),
    ]
    sections = detect_turns(small_reversal, threshold=0.5)
    assert len(sections) == 1
    assert sections[0].cumulative_yaw == pytest.approx(1.0)

    threshold_reversal = [
        PosePoint(0, 0, 0),
        PosePoint(1, 0, 0.4),
        PosePoint(2, 0, 0.8),
        PosePoint(3, 0, 0.2),
        PosePoint(4, 0, -0.4),
    ]
    sections = detect_turns(threshold_reversal, threshold=0.5)
    assert [(section.start_index, section.end_index) for section in sections] == [
        (0, 2),
        (2, 4),
    ]
    assert sections[0].cumulative_yaw == pytest.approx(0.8)
    assert sections[1].cumulative_yaw == pytest.approx(-1.2)


def test_whole_turn_has_front_axle_and_four_fixed_corner_radii() -> None:
    points = _arc_poses(5, 0, math.pi / 2, 21)

    result = analyze_turn_radii(
        points,
        VehicleDimensions(width=2, center_front=3, center_rear=1),
        threshold=math.pi / 6,
    )

    assert len(result.turns) == 1
    turn = result.turns[0]
    assert turn.valid is True
    assert turn.side is TurnSide.LEFT
    assert turn.kind is TurnKind.TURN
    assert turn.front_axle_radius == pytest.approx(5, abs=1e-6)
    expected = {
        CornerRadiusKind.FRONT_OUTER: math.sqrt(45),
        CornerRadiusKind.REAR_OUTER: math.sqrt(37),
        CornerRadiusKind.FRONT_INNER: 5,
        CornerRadiusKind.REAR_INNER: math.sqrt(17),
    }
    for kind, radius in expected.items():
        assert turn.radii[kind] == pytest.approx(radius, abs=1e-6)


def test_whole_turn_uses_unwrapped_yaw_for_a_270_degree_turn() -> None:
    points = _arc_poses(5, 0, 1.5 * math.pi, 61)

    turn = calculate_turn_radius(
        points,
        VehicleDimensions(width=2, center_front=3, center_rear=1),
        start_index=0,
        end_index=60,
    )

    assert turn.valid is True
    assert turn.cumulative_yaw == pytest.approx(1.5 * math.pi)
    assert turn.rotation_center == Point2D(0, 0)
    assert turn.front_axle_radius == pytest.approx(5)
    assert turn.side is TurnSide.LEFT


def test_turn_side_is_relative_to_vehicle_heading_even_when_reversing() -> None:
    points = _arc_poses(5, 0, math.pi / 2, 21, reverse_heading=True)

    result = analyze_turn_radii(
        points,
        VehicleDimensions(width=2, center_front=3, center_rear=1),
        threshold=math.pi / 6,
    )

    assert result.turns[0].side is TurnSide.RIGHT
    assert result.turns[0].radii[CornerRadiusKind.FRONT_OUTER] == pytest.approx(
        math.sqrt(45), abs=1e-6
    )


def test_uturn_is_classified_and_an_invalid_automatic_turn_is_retained() -> None:
    uturn = _arc_poses(5, 0, math.pi, 31)
    plateau_start = uturn[14]
    assert plateau_start.yaw is not None
    heading = (math.cos(plateau_start.yaw), math.sin(plateau_start.yaw))
    uturn.insert(
        15,
        PosePoint(
            plateau_start.x + heading[0] * 0.1,
            plateau_start.y + heading[1] * 0.1,
            plateau_start.yaw,
        ),
    )
    uturn.insert(
        16,
        PosePoint(
            plateau_start.x + heading[0] * 0.2,
            plateau_start.y + heading[1] * 0.2,
            plateau_start.yaw,
        ),
    )

    result = analyze_turn_radii(
        uturn,
        VehicleDimensions(width=2, center_front=3, center_rear=1),
        threshold=math.pi / 6,
    )

    assert result.turns[0].kind is TurnKind.UTURN
    assert abs(result.turns[0].cumulative_yaw) > 0.75 * math.pi

    invalid = analyze_turn_radii(
        (PosePoint(0, 0, 0), PosePoint(0, 0, 0.4), PosePoint(0, 0, 0.8)),
        VehicleDimensions(width=2, center_front=3, center_rear=1),
        threshold=0.5,
    )
    assert len(invalid.turns) == 1
    assert invalid.turns[0].valid is False
    assert invalid.turns[0].error


def test_exact_uturn_threshold_remains_a_normal_turn() -> None:
    exact = _arc_poses(5, 0, 0.75 * math.pi, 31)
    above = _arc_poses(5, 0, 0.75 * math.pi + 1e-5, 31)
    dimensions = VehicleDimensions(width=2, center_front=3, center_rear=1)

    exact_result = analyze_turn_radii(exact, dimensions)
    above_result = analyze_turn_radii(above, dimensions)

    assert exact_result.turns[0].kind is TurnKind.TURN
    assert above_result.turns[0].kind is TurnKind.UTURN
