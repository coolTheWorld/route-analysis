"""Render a deterministic offscreen release preview for visual QA."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QMainWindow, QSplitter

from route_analysis import __version__
from route_analysis.canvas import RouteCanvas
from route_analysis.control_panel import ControlPanel
from route_analysis.lane_generation import BendMode, generate_lane
from route_analysis.models import Point2D, PosePoint, VehicleDimensions
from route_analysis.parsing import parse_command_details
from route_analysis.path_details_panel import PathDetailsPanel
from route_analysis.storage import LaneLayout
from route_analysis.theme import APPLICATION_STYLESHEET
from route_analysis.turn_measurements import (
    RadiusMeasurementState,
    recalculate_measurements,
)


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "build/release-preview.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    app.setFont(QFont("Microsoft YaHei", 9))
    app.setStyle("Fusion")
    app.setStyleSheet(APPLICATION_STYLESHEET)

    rounded_source = [Point2D(-2, 0), Point2D(-1, 0)]
    rounded_source.extend(
        Point2D(-1 + math.cos(angle), 1 + math.sin(angle))
        for angle in (-math.pi / 2, -math.pi / 4, 0)
    )
    rounded_source.append(Point2D(0, 2))
    lane = generate_lane(
        rounded_source,
        lane_id="release-preview",
        name="自动圆弧车道",
        width=2.8,
        mode=BendMode.ROUND,
        maximum_deviation=0.08,
    ).lane
    path = tuple(
        PosePoint(5 * math.cos(angle), 5 * math.sin(angle), angle + math.pi / 2)
        for angle in (index * math.pi / 40 for index in range(21))
    )
    dimensions = VehicleDimensions(2, 3, 1)
    radius_state = RadiusMeasurementState("release-preview")
    radius_state.replace_automatic(((0, len(path) - 1),))
    measurements = recalculate_measurements(radius_state, path, dimensions)

    canvas = RouteCanvas()
    canvas.load_layout(LaneLayout("aaaaaaaaaaaaaaaa", "42", [lane]))
    canvas.set_paths(path, (), dimensions)
    canvas.show_turn_radius_observation("dispatched", measurements[0].radius)
    panel = ControlPanel(canvas)
    panel.set_configuration(default_lane_width=2.8, direction=0)
    panel.set_turn_radius_measurements(
        measurements,
        (),
        dimensions_source="VIN PREVIEW 专属配置",
    )
    panel.radius_layer_check.setChecked(True)

    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(canvas)
    splitter.addWidget(panel)
    splitter.setSizes([1050, 390])
    window = QMainWindow()
    window.setWindowTitle(f"Suntae 路径通行分析 {__version__} — 发布视觉检查")
    window.setCentralWidget(splitter)
    window.resize(1440, 860)
    window.show()
    app.processEvents()
    canvas.fit_content()
    canvas.scale(2.0, 2.0)
    display = canvas.to_display(Point2D(path[0].x, path[0].y))
    canvas.centerOn(display.x, display.y)
    app.processEvents()
    overview_saved = window.grab().save(str(output))
    panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum())
    app.processEvents()
    radius_output = output.with_name(f"{output.stem}-radius{output.suffix}")
    radius_saved = window.grab().save(str(radius_output))

    point_details = parse_command_details(
        {
            "commandId": 9063,
            "vin": "VIN-PREVIEW",
            "positionList": [
                {"x": 0, "y": 0, "yaw": 0, "gear": "D", "speed": 0.5},
                {"x": 2, "y": 0.5, "yaw": 0.35, "gear": "D", "speed": 0.4},
                {"x": 3.5, "y": 2, "yaw": None, "gear": "R"},
                {"x": "invalid", "y": 4, "yaw": 1.2, "gear": "R"},
            ],
        }
    )
    point_canvas = RouteCanvas()
    point_canvas.set_paths(
        point_details.poses,
        (),
        dimensions,
        source_indices={"dispatched": point_details.pose_source_indices, "actual": ()},
    )
    point_panel = PathDetailsPanel()
    point_panel.begin_command()
    point_panel.set_source_document("dispatched", point_details)
    point_panel.set_source_document("actual", parse_command_details({"positionList": []}))
    point_panel.point_selected.connect(point_canvas.select_path_point)
    point_canvas.path_point_selected.connect(point_panel.select_point)
    point_panel.select_point("dispatched", 1, emit_signal=True)
    point_splitter = QSplitter(Qt.Orientation.Horizontal)
    point_splitter.addWidget(point_panel)
    point_splitter.addWidget(point_canvas)
    point_splitter.setSizes([480, 960])
    point_window = QMainWindow()
    point_window.setWindowTitle(
        f"Suntae 路径通行分析 {__version__} — 命令点位视觉检查"
    )
    point_window.setCentralWidget(point_splitter)
    point_window.resize(1440, 860)
    point_window.show()
    app.processEvents()
    point_canvas.fit_content()
    app.processEvents()
    point_output = output.with_name(f"{output.stem}-points{output.suffix}")
    points_saved = point_window.grab().save(str(point_output))
    return 0 if overview_saved and radius_saved and points_saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
