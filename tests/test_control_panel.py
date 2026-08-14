import math

from pytestqt.qtbot import QtBot

from route_analysis.canvas import RouteCanvas
from route_analysis.control_panel import ControlPanel
from route_analysis.models import PosePoint, VehicleDimensions
from route_analysis.turn_radius import analyze_turn_radii


def _turn_path() -> tuple[PosePoint, ...]:
    return tuple(
        PosePoint(
            5 * math.cos(angle),
            5 * math.sin(angle),
            angle + math.pi / 2,
        )
        for angle in (index * math.pi / 40 for index in range(21))
    )


def test_turn_radius_tree_shows_global_and_per_bend_statistics_and_locates(
    qtbot: QtBot,
) -> None:
    dimensions = VehicleDimensions(2, 3, 1)
    path = _turn_path()
    result = analyze_turn_radii(path, dimensions)
    canvas = RouteCanvas()
    panel = ControlPanel(canvas)
    qtbot.addWidget(canvas)
    qtbot.addWidget(panel)
    canvas.set_paths(path, (), dimensions)

    panel.set_turn_radius_results(result, None, dimensions_source="VIN V1 专属配置")

    assert "前外角半径" in panel.radius_summaries["dispatched"].text()
    assert "VIN V1 专属配置" in panel.dimension_source_label.text()
    tree = panel.radius_trees["dispatched"]
    assert tree.topLevelItemCount() == 1
    parent = tree.topLevelItem(0)
    assert parent is not None
    assert parent.childCount() == 4
    assert "车体左侧转弯" in parent.text(0)
    assert "m @" in parent.child(0).text(3)
    items_before = len(canvas.scene().items())

    panel._radius_item_activated("dispatched", parent.child(0))

    assert panel.radius_layer_check.isChecked() is True
    assert len(canvas.scene().items()) > items_before
