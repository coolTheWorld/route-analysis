"""Render the user-guide screenshots offscreen, from the same fake scenes as the preview.

    QT_QPA_PLATFORM=offscreen ./.venv/bin/python scripts/render_docs_screenshots.py

Writes nine deterministic PNGs into ``docs/images/``. No scheduler backend is touched:
every scene is built from fixtures, so the images carry no real order data and can be
regenerated after any UI change with this one command. A CJK font must be installed or
the captions render as boxes.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QSplitter

sys.path.insert(0, str(Path(__file__).parent))
import render_release_preview as preview

from route_analysis import __version__
from route_analysis.canvas import RouteCanvas
from route_analysis.clearance_panel import ClearanceInputs, ClearancePanel
from route_analysis.clearance_solver import (
    LaneContext,
    SegmentRole,
    analyse_clearance,
)
from route_analysis.control_panel import ControlPanel
from route_analysis.lane_generation import BendMode, generate_lane
from route_analysis.models import (
    AnalysisSettings,
    Point2D,
    PosePoint,
    VehicleDimensions,
)
from route_analysis.parsing import parse_command_details
from route_analysis.path_details_panel import PathDetailsPanel
from route_analysis.scenario_geometry import Condition, Scenario
from route_analysis.scenario_panel import ScenarioPanel
from route_analysis.settings_dialog import SettingsDialog
from route_analysis.storage import AppConfig, LaneLayout
from route_analysis.theme import APPLICATION_STYLESHEET
from route_analysis.turn_measurements import (
    RadiusMeasurementState,
    recalculate_measurements,
)

OUTPUT = Path(__file__).parent.parent / "docs" / "images"
WINDOW = (1440, 860)


def _window(central, title: str) -> QMainWindow:
    window = QMainWindow()
    window.setWindowTitle(f"Suntae 路径通行分析 {__version__} — {title}")
    window.setCentralWidget(central)
    window.resize(*WINDOW)
    window.show()
    return window


def _grab(app: QApplication, window: QMainWindow, name: str) -> bool:
    app.processEvents()
    saved = window.grab().save(str(OUTPUT / name))
    print(("写出 " if saved else "失败 ") + name)
    window.close()
    return saved


def _map_scene() -> tuple:
    """The preview's map scene: a round-fit lane beside a quarter-circle path."""

    source = [Point2D(-2, 0), Point2D(-1, 0)]
    source.extend(
        Point2D(-1 + math.cos(angle), 1 + math.sin(angle))
        for angle in (-math.pi / 2, -math.pi / 4, 0)
    )
    source.append(Point2D(0, 2))
    lane = generate_lane(
        source,
        lane_id="docs",
        name="自动圆弧车道",
        width=2.8,
        mode=BendMode.ROUND,
        maximum_deviation=0.08,
    ).lane
    path = tuple(
        PosePoint(5 * math.cos(angle), 5 * math.sin(angle), angle + math.pi / 2)
        for angle in (index * math.pi / 40 for index in range(21))
    )
    return lane, path, VehicleDimensions(2, 3, 1)


def shot_overview(app: QApplication) -> bool:
    lane, path, dimensions = _map_scene()
    canvas = RouteCanvas()
    canvas.load_layout(LaneLayout("aaaaaaaaaaaaaaaa", "42", [lane]))
    canvas.set_paths(path, (), dimensions)
    panel = ControlPanel(canvas)
    panel.set_configuration(default_lane_width=2.8, direction=0)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(canvas)
    splitter.addWidget(panel)
    splitter.setSizes([1050, 390])
    window = _window(splitter, "地图页")
    app.processEvents()
    canvas.fit_content()
    canvas.scale(1.6, 1.6)
    display = canvas.to_display(Point2D(path[6].x, path[6].y))
    canvas.centerOn(display.x, display.y)
    return _grab(app, window, "overview.png")


def shot_settings(app: QApplication) -> bool:
    config = AppConfig(
        api_root="http://scheduler.example/admin-api",
        username="viewer",
        password="········",
        default_vehicle=VehicleDimensions(1.23, 1.545, 2.223),
        default_lane_width=3.0,
    )
    dialog = SettingsDialog(config, {"VIN-0001": VehicleDimensions(1.4, 1.6, 2.4)})
    dialog.show()
    app.processEvents()
    saved = dialog.grab().save(str(OUTPUT / "settings.png"))
    print(("写出 " if saved else "失败 ") + "settings.png")
    dialog.close()
    return saved


def shot_lane_editing(app: QApplication) -> bool:
    draft_path = (
        PosePoint(-3, -1, 0),
        PosePoint(0, -1, 0),
        PosePoint(2, 1, math.pi / 4),
        PosePoint(4, 2, 0),
    )
    canvas = RouteCanvas()
    canvas.set_paths(draft_path, (), VehicleDimensions(1.2, 1.5, 0.8))
    canvas.set_path_layer("dispatched", vehicles=False)
    panel = ControlPanel(canvas)
    panel.set_configuration(default_lane_width=3.5, direction=0)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(canvas)
    splitter.addWidget(panel)
    splitter.setSizes([1050, 390])
    window = _window(splitter, "车道绘制")
    app.processEvents()
    canvas.fit_content()
    app.processEvents()
    panel.draw_button.click()
    for point in draft_path[:2]:
        display = canvas.to_display(Point2D(point.x, point.y))
        QTest.mouseClick(
            canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=canvas.mapFromScene(display.x, display.y),
        )
    return _grab(app, window, "lane-editing.png")


def shot_point_details(app: QApplication) -> bool:
    details = parse_command_details(
        {
            "commandId": 9063,
            "vin": "VIN-0001",
            "positionList": [
                {"x": 0, "y": 0, "yaw": 0, "gear": "D", "speed": 0.5},
                {"x": 2, "y": 0.5, "yaw": 0.35, "gear": "D", "speed": 0.4},
                {"x": 3.5, "y": 2, "yaw": None, "gear": "R"},
                {"x": "invalid", "y": 4, "yaw": 1.2, "gear": "R"},
            ],
        }
    )
    canvas = RouteCanvas()
    canvas.set_paths(
        details.poses,
        (),
        VehicleDimensions(1.2, 1.5, 0.8),
        source_indices={"dispatched": details.pose_source_indices, "actual": ()},
    )
    panel = PathDetailsPanel()
    panel.begin_command()
    panel.set_source_document("dispatched", details)
    panel.set_source_document("actual", parse_command_details({"positionList": []}))
    panel.point_selected.connect(canvas.select_path_point)
    panel.select_point("dispatched", 1, emit_signal=True)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(panel)
    splitter.addWidget(canvas)
    splitter.setSizes([480, 960])
    window = _window(splitter, "命令点位")
    app.processEvents()
    canvas.fit_content()
    return _grab(app, window, "point-details.png")


def shot_turn_radius(app: QApplication) -> bool:
    lane, path, dimensions = _map_scene()
    state = RadiusMeasurementState("docs")
    state.add_manual(0, len(path) - 1)
    measurements = recalculate_measurements(state, path, dimensions)
    canvas = RouteCanvas()
    canvas.load_layout(LaneLayout("aaaaaaaaaaaaaaaa", "42", [lane]))
    canvas.set_paths(path, (), dimensions)
    canvas.show_turn_radius_observation("dispatched", measurements[0].radius)
    panel = ControlPanel(canvas)
    panel.set_configuration(default_lane_width=2.8, direction=0)
    panel.set_turn_radius_measurements(measurements, (), dimensions_source="全局默认")
    panel.radius_layer_check.setChecked(True)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(canvas)
    splitter.addWidget(panel)
    splitter.setSizes([1050, 390])
    window = _window(splitter, "整弯转弯半径")
    app.processEvents()
    canvas.fit_content()
    canvas.scale(2.0, 2.0)
    display = canvas.to_display(Point2D(path[0].x, path[0].y))
    canvas.centerOn(display.x, display.y)
    panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum())
    return _grab(app, window, "turn-radius.png")


def _clearance_panel() -> ClearancePanel:
    poses, lanes, dimensions = preview.clearance_scene()
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, dimensions, lanes, settings)
    assert analysis is not None
    panel = ClearancePanel()
    panel.set_analysis(
        analysis,
        ClearanceInputs(
            poses=poses,
            dimensions=dimensions,
            settings=settings,
            context=LaneContext(lanes, settings),
            metadata={},
        ),
    )
    return panel


def shot_clearance(app: QApplication) -> bool:
    panel = _clearance_panel()
    window = _window(panel, "通行余量")
    app.processEvents()
    panel.overview.table.selectRow(0)
    preview._wait_for(app, lambda: panel.selected_zones() is not None)
    return _grab(app, window, "clearance.png")


def shot_corner(app: QApplication) -> bool:
    poses, lanes, dimensions = preview.corner_scene()
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, dimensions, lanes, settings)
    assert analysis is not None
    turn = next(item for item in analysis.segments if item.role is SegmentRole.TURN)
    panel = ClearancePanel()
    panel.set_analysis(
        analysis,
        ClearanceInputs(
            poses=poses,
            dimensions=dimensions,
            settings=settings,
            context=LaneContext(lanes, settings),
            metadata={},
        ),
    )
    window = _window(panel, "转角求解")
    app.processEvents()
    panel._open_corner(turn.index)
    preview._wait_for(app, lambda: bool(panel.corner_view._optimum))
    return _grab(app, window, "corner-solver.png")


def shot_scenario_centreline(app: QApplication) -> bool:
    panel = ScenarioPanel()
    window = _window(panel, "场景速算 · 道路中心线")
    app.processEvents()
    preview._wait_for(app, lambda: panel.result is not None)
    return _grab(app, window, "scenario-centreline.png")


def shot_scenario_pareto(app: QApplication) -> bool:
    panel = ScenarioPanel()
    window = _window(panel, "场景速算 · 帕累托极限")
    app.processEvents()
    preview._wait_for(app, lambda: panel.result is not None)
    panel.select_variant(condition=Condition.PARETO, scenario=Scenario.CORNER)
    preview._wait_for(app, lambda: panel.result is not None and panel.result.inputs.pareto)
    panel._road_spins["wa"].setValue(3.0)
    preview._wait_for(
        app, lambda: panel.result is not None and "wa" in panel.result.pins.dims
    )
    return _grab(app, window, "scenario-pareto.png")


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([])
    font = preview.select_preview_font()
    print(f"截图字体: {font.family()}")
    app.setFont(font)
    app.setStyle("Fusion")
    app.setStyleSheet(APPLICATION_STYLESHEET)
    shots = (
        shot_overview,
        shot_settings,
        shot_lane_editing,
        shot_point_details,
        shot_turn_radius,
        shot_clearance,
        shot_corner,
        shot_scenario_centreline,
        shot_scenario_pareto,
    )
    return 0 if all([shot(app) for shot in shots]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
