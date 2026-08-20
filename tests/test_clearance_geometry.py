import math
from itertools import pairwise

import pytest

from route_analysis.clearance_geometry import (
    apex_offset,
    build_corner_poses,
    corner_radii,
    fit_corner,
    offset_profile,
    solve_offset_radius,
    swept_band_width,
    tangent_length,
)
from route_analysis.models import PosePoint, VehicleDimensions
from route_analysis.turn_radius import CornerRadiusKind, TurnSide, calculate_turn_radius

SAMPLE = VehicleDimensions(width=1.20, center_front=1.00, center_rear=1.60)


def _arc_poses(radius: float, deflection: float, count: int = 41) -> list[PosePoint]:
    """Poses along an arc entered at its tangent point, heading +x, centre to the left."""

    sign = 1.0 if deflection > 0 else -1.0
    centre_x, centre_y = 0.0, sign * radius
    poses = []
    for index in range(count):
        angle = deflection * index / (count - 1)
        radial_x, radial_y = 0.0 - centre_x, 0.0 - centre_y
        poses.append(
            PosePoint(
                centre_x + radial_x * math.cos(angle) - radial_y * math.sin(angle),
                centre_y + radial_x * math.sin(angle) + radial_y * math.cos(angle),
                angle,
            )
        )
    return poses


def test_corner_radii_match_the_handoff_worked_example() -> None:
    radii = corner_radii(SAMPLE, 1.00)
    assert radii[CornerRadiusKind.REAR_OUTER] == pytest.approx(2.263, abs=5e-4)
    assert radii[CornerRadiusKind.FRONT_OUTER] == pytest.approx(1.887, abs=5e-4)
    assert radii[CornerRadiusKind.REAR_INNER] == pytest.approx(1.649, abs=5e-4)
    assert radii[CornerRadiusKind.FRONT_INNER] == pytest.approx(1.077, abs=5e-4)


def test_rear_outer_corner_dominates_the_front_outer_corner() -> None:
    radii = corner_radii(SAMPLE, 1.00)
    swing = radii[CornerRadiusKind.REAR_OUTER] - radii[CornerRadiusKind.FRONT_OUTER]
    assert swing == pytest.approx(0.376, abs=5e-4)


def test_closed_form_agrees_with_the_measured_whole_turn_radii() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    measured = calculate_turn_radius(poses, SAMPLE, start_index=0, end_index=len(poses) - 1)
    assert measured.side is TurnSide.LEFT
    expected = corner_radii(SAMPLE, 1.00)
    for kind, value in expected.items():
        assert measured.radii[kind] == pytest.approx(value, abs=1e-9)


def test_swept_band_narrows_as_the_radius_grows() -> None:
    widths = [swept_band_width(SAMPLE, radius) for radius in (1.0, 1.35, 2.0, 3.0, 6.0)]
    assert widths == sorted(widths, reverse=True)
    assert widths[0] == pytest.approx(1.863, abs=5e-4)


def test_tangent_and_apex_geometry_of_a_right_angle_fillet() -> None:
    assert tangent_length(1.60, math.pi / 2) == pytest.approx(1.60)
    assert apex_offset(1.60, math.pi / 2) == pytest.approx(1.60 * (math.sqrt(2) - 1))


def test_offset_profile_matches_the_handoff_double_radius_example() -> None:
    profile = offset_profile(1.60, 1.00, math.pi / 2)
    assert profile.peak == pytest.approx(0.249, abs=5e-4)
    assert profile.tangent_gap == pytest.approx(0.60, abs=1e-9)
    assert profile.constant_approximation == pytest.approx(0.0, abs=1e-6)
    assert profile.overestimate == pytest.approx(profile.peak, abs=1e-6)


def test_offset_profile_rises_from_zero_to_a_single_peak_and_returns() -> None:
    profile = offset_profile(1.60, 1.00, math.pi / 2)
    separations = [sample.separation for sample in profile.samples]
    assert separations[0] == pytest.approx(0.0, abs=1e-6)
    assert separations[-1] == pytest.approx(0.0, abs=1e-3)
    assert max(separations) == pytest.approx(profile.peak, abs=5e-3)
    apex = separations.index(max(separations))
    assert separations[:apex] == sorted(separations[:apex])
    assert separations[apex:] == sorted(separations[apex:], reverse=True)


def test_equal_radii_leave_the_two_centrelines_together() -> None:
    profile = offset_profile(1.20, 1.20, math.pi / 3)
    assert profile.peak == pytest.approx(0.0, abs=1e-12)
    assert max(sample.separation for sample in profile.samples) == pytest.approx(0.0, abs=1e-3)


def test_offset_profile_rejects_degenerate_corners() -> None:
    with pytest.raises(ValueError, match="累计 yaw"):
        offset_profile(1.60, 1.00, 0.0)
    with pytest.raises(ValueError, match="小于 π"):
        offset_profile(1.60, 1.00, math.pi)


def test_fit_corner_recovers_the_generating_arc() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    assert corner.radius == pytest.approx(1.00, abs=1e-9)
    assert corner.deflection == pytest.approx(math.pi / 2, abs=1e-9)
    assert corner.side is TurnSide.LEFT
    assert corner.residual == pytest.approx(0.0, abs=1e-9)
    assert corner.corner_point is not None
    assert corner.corner_point.x == pytest.approx(1.00, abs=1e-9)
    assert corner.corner_point.y == pytest.approx(0.00, abs=1e-9)


def test_fit_corner_reports_residual_for_a_path_that_is_not_a_clean_arc() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    poses[len(poses) // 2] = PosePoint(
        poses[len(poses) // 2].x + 0.04, poses[len(poses) // 2].y, poses[len(poses) // 2].yaw
    )
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    assert corner.residual > 0.02


def test_zero_degrees_of_freedom_reproduce_the_fitted_radius() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    assert solve_offset_radius(corner) == pytest.approx(1.00, abs=1e-9)


def test_delaying_the_turn_tightens_the_radius() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    delayed = solve_offset_radius(corner, arc_start_shift=0.30)
    early = solve_offset_radius(corner, arc_start_shift=-0.30)
    assert delayed is not None and early is not None
    assert delayed < 1.00 < early


def test_delaying_the_turn_past_the_corner_is_infeasible() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    assert solve_offset_radius(corner, arc_start_shift=1.20) is None


def test_built_corner_lands_on_the_shifted_exit_leg() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    solution = build_corner_poses(
        corner, entry_offset=0.20, exit_offset=-0.15, arc_start_shift=0.10
    )
    assert solution is not None
    last = solution.poses[-1]
    assert last.yaw == pytest.approx(corner.exit_heading, abs=1e-9)
    exit_normal = (-math.sin(corner.exit_heading), math.cos(corner.exit_heading))
    lateral = (last.x - corner.exit_point.x) * exit_normal[0] + (
        last.y - corner.exit_point.y
    ) * exit_normal[1]
    assert lateral == pytest.approx(-0.15, abs=1e-9)


def test_built_corner_is_continuous_and_turns_the_full_deflection() -> None:
    poses = _arc_poses(1.00, -math.pi / 2)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    solution = build_corner_poses(corner, yaw_step=0.02)
    assert solution is not None
    assert solution.poses[0].yaw == pytest.approx(corner.entry_heading, abs=1e-9)
    assert solution.poses[-1].yaw == pytest.approx(corner.exit_heading, abs=1e-9)
    gaps = [
        math.dist((first.x, first.y), (second.x, second.y))
        for first, second in pairwise(solution.poses)
    ]
    assert max(gaps[1:-1]) < 0.05


def test_build_corner_poses_rejects_infeasible_degrees_of_freedom() -> None:
    poses = _arc_poses(1.00, math.pi / 2)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    assert build_corner_poses(corner, arc_start_shift=5.0) is None
