import math

import pytest
from pytestqt.qtbot import QtBot

from route_analysis.clearance_panel import (
    ClearanceInputs,
    ClearancePanel,
    build_suggestions,
    offset_rows,
)
from route_analysis.clearance_solver import (
    ClearanceAnalysis,
    LaneContext,
    SegmentRole,
    analyse_clearance,
)
from route_analysis.models import (
    AnalysisSettings,
    Lane,
    Point2D,
    PosePoint,
    VehicleDimensions,
)

DIMENSIONS = VehicleDimensions(width=1.20, center_front=1.00, center_rear=1.60)


def _arc(centre_x: float, centre_y: float, radius: float, start: float, end: float,
         steps: int, sign: float) -> list[PosePoint]:
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


def _poses() -> list[PosePoint]:
    poses = [PosePoint(-12 + index * 0.5, 0.0, 0.0) for index in range(25)]
    poses += _arc(0.0, 1.2, 1.2, -math.pi / 2, 0.0, 24, 1.0)
    poses += [PosePoint(1.2, 1.2 + index * 0.5, math.pi / 2) for index in range(1, 17)]
    poses += _arc(2.4, 9.2, 1.2, math.pi, math.pi / 2, 24, -1.0)
    poses += [PosePoint(2.4 + index * 0.5, 10.4, 0.0) for index in range(1, 21)]
    return poses


def _lanes(branch: float = 2.2) -> list[Lane]:
    return [
        Lane.create("main", "主通道", 3.4, [Point2D(-16, 0), Point2D(1.2, 0), Point2D(1.2, 1.2)]),
        Lane.create(
            "branch", "支通道", branch,
            [Point2D(1.2, 0.6), Point2D(1.2, 10.4), Point2D(26, 10.4)],
        ),
    ]


@pytest.fixture
def prepared() -> tuple[ClearanceAnalysis, ClearanceInputs]:
    poses = _poses()
    lanes = _lanes()
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, settings)
    assert analysis is not None
    inputs = ClearanceInputs(
        poses=tuple(poses),
        dimensions=DIMENSIONS,
        settings=settings,
        context=LaneContext(lanes, settings),
        metadata={"命令": "CMD-1"},
    )
    return analysis, inputs


def test_panel_without_an_analysis_disables_both_exports(qtbot: QtBot) -> None:
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(None, None)
    assert not panel.overview.csv_button.isEnabled()
    assert not panel.overview.pdf_button.isEnabled()
    assert panel.overview.table.rowCount() == 0
    assert not panel.showing_corner


def test_panel_fills_the_ranking_and_the_advice(
    qtbot: QtBot, prepared: tuple[ClearanceAnalysis, ClearanceInputs]
) -> None:
    analysis, inputs = prepared
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(analysis, inputs)
    assert panel.overview.table.rowCount() == len(analysis.bottlenecks)
    assert panel.overview.csv_button.isEnabled()
    assert panel.overview.pdf_button.isEnabled()
    assert panel.overview._advice_holder.count() == 3
    first = panel.overview.table.item(0, 2)
    assert first is not None and first.text().startswith("点位 ")


def test_selecting_a_row_reports_the_pose_and_opens_the_ruler(
    qtbot: QtBot, prepared: tuple[ClearanceAnalysis, ClearanceInputs]
) -> None:
    analysis, inputs = prepared
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(analysis, inputs)
    with qtbot.waitSignal(panel.pose_selected, timeout=2000):
        panel.overview.table.selectRow(0)
    assert panel.overview.table.rowCount() == len(analysis.bottlenecks) + 1


def _row_of_role(panel: ClearancePanel, analysis: ClearanceAnalysis, role: SegmentRole) -> int:
    for row in range(panel.overview.table.rowCount()):
        index = panel.overview._segment_at(row)
        if index is not None and analysis.segments[index].role is role:
            return row
    raise AssertionError(f"no {role} row")


def test_the_corner_button_is_the_way_into_the_solver(
    qtbot: QtBot, prepared: tuple[ClearanceAnalysis, ClearanceInputs]
) -> None:
    analysis, inputs = prepared
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(analysis, inputs)
    assert not panel.overview.corner_button.isEnabled()
    panel.overview.table.selectRow(_row_of_role(panel, analysis, SegmentRole.TURN))
    assert panel.overview.corner_button.isEnabled()
    panel.overview.corner_button.click()
    assert panel.showing_corner
    panel.corner_view.back_requested.emit()
    assert not panel.showing_corner


def test_the_corner_button_stays_off_for_a_straight_run(
    qtbot: QtBot, prepared: tuple[ClearanceAnalysis, ClearanceInputs]
) -> None:
    analysis, inputs = prepared
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(analysis, inputs)
    panel.overview.table.selectRow(_row_of_role(panel, analysis, SegmentRole.STRAIGHT))
    assert not panel.overview.corner_button.isEnabled()
    panel.overview.corner_button.click()
    assert not panel.showing_corner


def test_selecting_a_row_inserts_the_ruler_without_rebuilding_the_others(
    qtbot: QtBot, prepared: tuple[ClearanceAnalysis, ClearanceInputs]
) -> None:
    analysis, inputs = prepared
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(analysis, inputs)
    table = panel.overview.table
    before = [table.item(row, 2) for row in range(table.rowCount())]
    row = _row_of_role(panel, analysis, SegmentRole.TURN)
    table.selectRow(row)
    assert panel.overview._ruler_row == row + 1
    assert table.rowCount() == len(analysis.bottlenecks) + 1
    # The other rows keep their original items; clearing them would break click gestures.
    assert table.item(row, 2) is before[row]
    assert table.cellWidget(row + 1, 0) is not None


def test_moving_the_selection_moves_the_ruler(
    qtbot: QtBot, prepared: tuple[ClearanceAnalysis, ClearanceInputs]
) -> None:
    analysis, inputs = prepared
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(analysis, inputs)
    table = panel.overview.table
    table.selectRow(0)
    first = panel.overview._ruler_row
    table.selectRow(_row_of_role(panel, analysis, SegmentRole.STRAIGHT))
    assert panel.overview._ruler_row != first
    assert table.rowCount() == len(analysis.bottlenecks) + 1


def test_a_straight_run_has_no_corner_to_open(
    qtbot: QtBot, prepared: tuple[ClearanceAnalysis, ClearanceInputs]
) -> None:
    analysis, inputs = prepared
    panel = ClearancePanel()
    qtbot.addWidget(panel)
    panel.set_analysis(analysis, inputs)
    straight = next(
        item for item in analysis.segments if item.role is SegmentRole.STRAIGHT
    )
    panel._open_corner(straight.index)
    assert not panel.showing_corner


def test_suggestions_lead_with_offsets_when_offsets_can_still_help(
    prepared: tuple[ClearanceAnalysis, ClearanceInputs],
) -> None:
    analysis, _ = prepared
    cards = build_suggestions(analysis)
    assert [rank for rank, _title, _body in cards] == ["primary", "secondary", "discouraged"]
    assert "只建议" not in cards[0][2]
    assert cards[0][0] == "primary"


def test_suggestions_say_so_when_no_offset_can_repair_the_path() -> None:
    poses = _poses()
    lanes = _lanes(branch=1.6)
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, AnalysisSettings())
    assert analysis is not None
    title = build_suggestions(analysis)[0][1]
    assert "偏置救不回来" in title


def test_offset_rows_carry_one_line_per_segment_with_a_header(
    prepared: tuple[ClearanceAnalysis, ClearanceInputs],
) -> None:
    analysis, _ = prepared
    rows = offset_rows(analysis)
    assert len(rows) == len(analysis.segments) + 1
    assert rows[0][0] == "区段"
    assert all(len(row) == len(rows[0]) for row in rows)
    states = {row[-1] for row in rows[1:]}
    assert states <= {"带内", "需偏置", "无可行偏置"} | {
        value for value in states if value.startswith("与相邻转角冲突")
    }
