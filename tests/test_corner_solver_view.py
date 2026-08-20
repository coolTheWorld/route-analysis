import math

import pytest
from pytestqt.qtbot import QtBot

from route_analysis.clearance_geometry import fit_corner
from route_analysis.clearance_panel import ClearanceInputs
from route_analysis.clearance_solver import LaneContext, SegmentRole, analyse_clearance
from route_analysis.corner_solver_view import CornerSolverView, lane_fillet_radius
from route_analysis.models import (
    AnalysisSettings,
    Lane,
    LaneSegment,
    Point2D,
    PosePoint,
    SegmentKind,
    VehicleDimensions,
)

DIMENSIONS = VehicleDimensions(width=1.20, center_front=1.00, center_rear=1.60)


def _arc(centre_x: float, centre_y: float, radius: float, start: float, end: float,
         steps: int, sign: float) -> list[PosePoint]:
    return [
        PosePoint(
            centre_x + radius * math.cos(start + (end - start) * step / steps),
            centre_y + radius * math.sin(start + (end - start) * step / steps),
            start + (end - start) * step / steps + sign * math.pi / 2,
        )
        for step in range(1, steps + 1)
    ]


def _prepared() -> tuple[object, ClearanceInputs]:
    poses = [PosePoint(-12 + index * 0.5, 0.0, 0.0) for index in range(25)]
    poses += _arc(0.0, 1.2, 1.2, -math.pi / 2, 0.0, 24, 1.0)
    poses += [PosePoint(1.2, 1.2 + index * 0.5, math.pi / 2) for index in range(1, 17)]
    lanes = [
        Lane.create("main", "主通道", 3.4, [Point2D(-16, 0), Point2D(1.2, 0), Point2D(1.2, 12)]),
    ]
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, settings)
    assert analysis is not None
    inputs = ClearanceInputs(
        poses=tuple(poses),
        dimensions=DIMENSIONS,
        settings=settings,
        context=LaneContext(lanes, settings),
        metadata={},
    )
    return analysis, inputs


def test_view_loads_a_turn_and_refuses_a_straight_run(qtbot: QtBot) -> None:
    analysis, inputs = _prepared()
    view = CornerSolverView()
    qtbot.addWidget(view)
    turn = next(item for item in analysis.segments if item.role is SegmentRole.TURN)
    straight = next(item for item in analysis.segments if item.role is SegmentRole.STRAIGHT)
    assert view.load(turn, analysis, inputs)
    assert "1.20" in view.path_radius_label.text()
    assert not view.load(straight, analysis, inputs)


def test_loading_reports_the_fit_residual_so_it_is_never_hidden(qtbot: QtBot) -> None:
    analysis, inputs = _prepared()
    view = CornerSolverView()
    qtbot.addWidget(view)
    turn = next(item for item in analysis.segments if item.role is SegmentRole.TURN)
    assert view.load(turn, analysis, inputs)
    assert "拟合残差" in view._subtitle.text()


def test_a_miter_lane_offers_no_fillet_radius_and_says_so(qtbot: QtBot) -> None:
    analysis, inputs = _prepared()
    view = CornerSolverView()
    qtbot.addWidget(view)
    turn = next(item for item in analysis.segments if item.role is SegmentRole.TURN)
    assert view.load(turn, analysis, inputs)
    assert view.lane_radius_spin.value() == pytest.approx(0.0)
    assert "没有圆弧倒角段" in view.radius_notice.text()
    assert "不回写车道" in view.radius_notice.text()


def test_lane_fillet_radius_reads_an_arc_segment() -> None:
    poses = _arc(0.0, 1.2, 1.2, -math.pi / 2, 0.0, 24, 1.0)
    corner = fit_corner(poses, start_index=0, end_index=len(poses) - 1)
    assert corner is not None
    lane = Lane.create("main", "主通道", 3.0, [Point2D(-6, 0), Point2D(0, 0), Point2D(0, 6)])
    assert lane_fillet_radius(lane, corner) is None
    lane.segments[1] = LaneSegment(
        kind=SegmentKind.ARC, arc_center=Point2D(0.0, 1.2), clockwise=False
    )
    assert lane_fillet_radius(None, corner) is None


def test_sliders_clamp_to_their_bounds(qtbot: QtBot) -> None:
    analysis, inputs = _prepared()
    view = CornerSolverView()
    qtbot.addWidget(view)
    turn = next(item for item in analysis.segments if item.role is SegmentRole.TURN)
    assert view.load(turn, analysis, inputs)
    slider = view._degrees["entry_offset"].slider
    slider.set_bounds(-0.5, 0.5)
    slider.set_value(9.0)
    assert slider.value() == pytest.approx(0.5)
    slider.set_value(-9.0)
    assert slider.value() == pytest.approx(-0.5)


def test_the_uncovered_list_admits_the_fitted_corner_limitation(qtbot: QtBot) -> None:
    view = CornerSolverView()
    qtbot.addWidget(view)
    from route_analysis.corner_solver_view import UNCOVERED

    assert any("拟合残差" in line for line in UNCOVERED)
    assert any("回旋线" in line for line in UNCOVERED)
