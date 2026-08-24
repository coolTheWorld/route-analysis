import math

import pytest
from pytestqt.qtbot import QtBot

from route_analysis.canvas import RouteCanvas
from route_analysis.control_panel import ControlPanel
from route_analysis.geometry import lane_centerline_length
from route_analysis.models import (
    Lane,
    Point2D,
    PosePoint,
    VehicleDimensions,
    VehicleSection,
)
from route_analysis.storage import LaneLayout
from route_analysis.theme import APPLICATION_STYLESHEET
from route_analysis.turn_measurements import (
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


def test_coordinate_inputs_show_their_value_without_clipping(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    panel.setStyleSheet(APPLICATION_STYLESHEET)
    panel.set_configuration(default_lane_width=2.8, direction=0)
    panel.show()
    qtbot.waitExposed(panel)

    for spin in (panel.direction_spin, panel.width_spin, panel.length_spin, panel.anchor_x):
        editor = spin.lineEdit()
        rendered = editor.fontMetrics().horizontalAdvance(editor.text())
        assert rendered <= editor.contentsRect().width(), (
            f"{editor.text()!r} needs {rendered} px but only has "
            f"{editor.contentsRect().width()} px"
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


def test_drawing_mode_uses_width_input_for_live_draft_without_editing_selected_lane(
    qtbot: QtBot,
) -> None:
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
    panel.set_configuration(default_lane_width=2.5, direction=0)

    panel.draw_button.click()

    assert canvas.is_drawing is True
    assert panel.width_spin.isEnabled() is True
    assert panel.width_spin.value() == pytest.approx(2.5)
    panel.width_spin.setValue(4)
    panel.width_spin.editingFinished.emit()

    assert canvas.draft_width == 4
    assert canvas.current_layout().lanes[0].width == 2
    assert canvas.undo_stack.count() == 0


def test_radius_panel_lists_measurements_as_top_level_rows_with_five_values(
    qtbot: QtBot,
) -> None:
    dimensions = VehicleDimensions(2, 3, 1)
    path = _turn_path()
    state = RadiusMeasurementState(path_fingerprint(path))
    first, _created = state.add_manual(0, 20)
    second, _created = state.add_manual(1, 19)
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
    first_item = tree.topLevelItem(0)
    second_item = tree.topLevelItem(1)
    assert first_item is not None
    assert second_item is not None
    assert first_item.text(0) == first.name
    assert second_item.text(0) == second.name
    assert first_item.childCount() == 5
    front_axle_item = first_item.child(0)
    assert front_axle_item is not None
    assert front_axle_item.text(0) == "前轴中心转弯半径"
    assert panel.radius_summaries["dispatched"].text() == "2 条"


def test_radius_buttons_emit_their_path_and_manual_mode_changes_label(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)

    assert panel.radius_manual_buttons["dispatched"].text() == "计算转弯半径"

    panel.set_manual_radius_mode("dispatched", True)
    assert panel.radius_manual_buttons["dispatched"].text() == "结束选择端点"
    with qtbot.waitSignal(panel.manual_radius_requested, timeout=1000) as manual:
        panel.radius_manual_buttons["dispatched"].click()
    assert manual.args == ["dispatched"]


def test_vehicle_layer_offers_whole_front_rear_and_off(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    combo = panel.vehicle_combos["dispatched"]

    assert [combo.itemData(row) for row in range(combo.count())] == [
        "full",
        "front",
        "rear",
        "off",
    ]
    assert combo.currentData() == "full"
    assert canvas._visibility["dispatched"].vehicle_section is VehicleSection.FULL
    assert canvas._visibility["dispatched"].vehicles is True


def test_choosing_one_end_keeps_the_layer_on_and_only_changes_the_section(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    combo = panel.vehicle_combos["actual"]

    combo.setCurrentIndex(combo.findData("rear"))

    assert canvas._visibility["actual"].vehicles is True
    assert canvas._visibility["actual"].vehicle_section is VehicleSection.REAR
    assert canvas._visibility["dispatched"].vehicle_section is VehicleSection.FULL


def test_turning_the_vehicle_layer_off_leaves_the_section_alone(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    combo = panel.vehicle_combos["dispatched"]

    combo.setCurrentIndex(combo.findData("front"))
    combo.setCurrentIndex(combo.findData("off"))

    assert canvas._visibility["dispatched"].vehicles is False
    assert canvas._visibility["dispatched"].vehicle_section is VehicleSection.FRONT


def test_isolating_a_path_remembers_which_end_was_being_looked_at(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    dispatched = panel.vehicle_combos["dispatched"]
    dispatched.setCurrentIndex(dispatched.findData("rear"))

    panel._isolate("actual")
    assert dispatched.currentData() == "off"

    panel._isolate(None)
    assert dispatched.currentData() == "rear"
    assert canvas._visibility["dispatched"].vehicle_section is VehicleSection.REAR
