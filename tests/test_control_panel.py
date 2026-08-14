import math

import pytest
from pytestqt.qtbot import QtBot

from route_analysis.canvas import RouteCanvas
from route_analysis.control_panel import ControlPanel
from route_analysis.geometry import lane_centerline_length
from route_analysis.models import Lane, Point2D, PosePoint, VehicleDimensions
from route_analysis.storage import LaneLayout
from route_analysis.turn_measurements import (
    MeasurementSource,
    RadiusMeasurementState,
    path_fingerprint,
    recalculate_measurements,
)


def _turn_path() -> tuple[PosePoint, ...]:
    return tuple(
        PosePoint(
            5 * math.cos(angle),
            5 * math.sin(angle),
            angle + math.pi / 2,
        )
        for angle in (index * math.pi / 40 for index in range(21))
    )


def test_lane_length_and_width_commit_only_when_editing_finishes(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    canvas.load_layout(
        LaneLayout(
            "aaaaaaaaaaaaaaaa",
            "42",
            [Lane.create("lane", "Lane", 2, [Point2D(0, 0), Point2D(3, 0)])],
        )
    )

    assert panel.length_spin.value() == pytest.approx(3)
    panel.length_spin.setValue(6)
    assert lane_centerline_length(canvas.current_layout().lanes[0]) == pytest.approx(3)
    panel.length_spin.editingFinished.emit()

    panel.width_spin.setValue(3)
    assert canvas.current_layout().lanes[0].width == 2
    panel.width_spin.editingFinished.emit()

    lane = canvas.current_layout().lanes[0]
    assert lane_centerline_length(lane) == pytest.approx(6)
    assert lane.width == 3
    assert canvas.undo_stack.count() == 2


def test_zero_length_lane_disables_only_length_input(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    canvas.load_layout(
        LaneLayout(
            "aaaaaaaaaaaaaaaa",
            "42",
            [Lane.create("zero", "Zero", 2, [Point2D(1, 1), Point2D(1, 1)])],
        )
    )

    assert panel.length_spin.value() == 0
    assert panel.length_spin.isEnabled() is False
    assert "零长度" in panel.length_spin.toolTip()
    assert panel.width_spin.isEnabled() is True


def test_radius_panel_groups_automatic_and_manual_measurements_with_five_values(
    qtbot: QtBot,
) -> None:
    dimensions = VehicleDimensions(2, 3, 1)
    path = _turn_path()
    state = RadiusMeasurementState(path_fingerprint(path))
    automatic = state.replace_automatic(((0, 20),))[0]
    manual, _created = state.add_manual(1, 19)
    measurements = recalculate_measurements(state, path, dimensions)
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    canvas.set_paths(path, (), dimensions)

    panel.set_turn_radius_measurements(
        measurements,
        (),
        dimensions_source="VIN V1 专属配置",
    )

    assert "VIN V1 专属配置" in panel.dimension_source_label.text()
    tree = panel.radius_trees["dispatched"]
    assert tree.topLevelItemCount() == 2
    automatic_group = tree.topLevelItem(0)
    manual_group = tree.topLevelItem(1)
    assert automatic_group is not None
    assert manual_group is not None
    assert automatic_group.text(0) == "自动测量"
    assert manual_group.text(0) == "手动测量"
    automatic_item = automatic_group.child(0)
    manual_item = manual_group.child(0)
    assert automatic_item is not None
    assert manual_item is not None
    assert automatic_item.text(0) == automatic.name
    assert manual_item.text(0) == manual.name
    assert automatic_item.childCount() == 5
    front_axle_item = automatic_item.child(0)
    assert front_axle_item is not None
    assert front_axle_item.text(0) == "前轴中心转弯半径"
    assert automatic.source is MeasurementSource.AUTOMATIC


def test_radius_buttons_emit_their_path_and_manual_mode_changes_label(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)

    with qtbot.waitSignal(panel.auto_radius_requested, timeout=1000) as automatic:
        panel.radius_auto_buttons["actual"].click()
    assert automatic.args == ["actual"]

    panel.set_manual_radius_mode("dispatched", True)
    assert panel.radius_manual_buttons["dispatched"].text() == "结束手动计算"
    with qtbot.waitSignal(panel.manual_radius_requested, timeout=1000) as manual:
        panel.radius_manual_buttons["dispatched"].click()
    assert manual.args == ["dispatched"]
