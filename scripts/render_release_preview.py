"""Render a deterministic offscreen release preview for visual QA."""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer
from PySide6.QtGui import QFont, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QSplitter

from route_analysis import __version__
from route_analysis.canvas import RouteCanvas
from route_analysis.clearance_panel import ClearanceInputs, ClearancePanel
from route_analysis.clearance_solver import LaneContext, SegmentRole, analyse_clearance
from route_analysis.control_panel import ControlPanel
from route_analysis.lane_generation import BendMode, generate_lane, replace_arc_radius
from route_analysis.models import (
    AnalysisSettings,
    Lane,
    LaneSegment,
    Point2D,
    PosePoint,
    SegmentKind,
    VehicleDimensions,
)
from route_analysis.parsing import parse_command_details
from route_analysis.path_details_panel import PathDetailsPanel
from route_analysis.scenario_geometry import Scenario, SolveMode
from route_analysis.scenario_panel import ScenarioPanel
from route_analysis.storage import LaneLayout
from route_analysis.theme import APPLICATION_STYLESHEET, select_cjk_font
from route_analysis.turn_measurements import (
    RadiusMeasurementState,
    recalculate_measurements,
)


def select_preview_font() -> QFont:
    """Return an installed CJK family so the preview never renders tofu blocks."""

    return select_cjk_font(9)


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "build/release-preview.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([])
    font = select_preview_font()
    print(f"Preview font family: {font.family()}")
    app.setFont(font)
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
    radius_state.add_manual(0, len(path) - 1)
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

    draft_path = (
        PosePoint(-3, -1, 0),
        PosePoint(0, -1, 0),
        PosePoint(2, 1, math.pi / 4),
        PosePoint(4, 2, 0),
    )
    draft_canvas = RouteCanvas()
    draft_canvas.set_paths(draft_path, (), VehicleDimensions(1.2, 1.5, 0.8))
    draft_canvas.set_path_layer("dispatched", vehicles=False)
    draft_panel = ControlPanel(draft_canvas)
    draft_panel.set_configuration(default_lane_width=3.5, direction=0)
    draft_splitter = QSplitter(Qt.Orientation.Horizontal)
    draft_splitter.addWidget(draft_canvas)
    draft_splitter.addWidget(draft_panel)
    draft_splitter.setSizes([1050, 390])
    draft_window = QMainWindow()
    draft_window.setWindowTitle(
        f"Suntae 路径通行分析 {__version__} — 动态手绘车道视觉检查"
    )
    draft_window.setCentralWidget(draft_splitter)
    draft_window.resize(1440, 860)
    draft_window.show()
    app.processEvents()
    draft_canvas.fit_content()
    app.processEvents()
    draft_panel.draw_button.click()
    for point in draft_path[:2]:
        display = draft_canvas.to_display(Point2D(point.x, point.y))
        QTest.mouseClick(
            draft_canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=draft_canvas.mapFromScene(display.x, display.y),
        )
    candidate = draft_canvas.to_display(Point2D(draft_path[2].x, draft_path[2].y))
    candidate_position = draft_canvas.mapFromScene(candidate.x, candidate.y)
    candidate_event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(candidate_position),
        QPointF(draft_canvas.viewport().mapToGlobal(candidate_position)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(draft_canvas.viewport(), candidate_event)
    app.processEvents()
    draft_output = output.with_name(f"{output.stem}-lane-draft{output.suffix}")
    draft_saved = draft_window.grab().save(str(draft_output))

    clearance_saved = render_clearance(app, output)
    corner_saved = render_corner(app, output)
    scenario_saved = render_scenario(app, output)
    return (
        0
        if overview_saved
        and radius_saved
        and points_saved
        and draft_saved
        and clearance_saved
        and corner_saved
        and scenario_saved
        else 1
    )


def clearance_scene() -> tuple[tuple[PosePoint, ...], list[Lane], VehicleDimensions]:
    """A main run, a left turn into a narrow branch and a right turn back out."""

    poses = [PosePoint(-12 + index * 0.5, 0.0, 0.0) for index in range(25)]
    for step in range(1, 25):
        angle = -math.pi / 2 + math.pi / 2 * step / 24
        poses.append(
            PosePoint(
                1.2 * math.cos(angle), 1.2 + 1.2 * math.sin(angle), angle + math.pi / 2
            )
        )
    poses.extend(PosePoint(1.2, 1.2 + index * 0.5, math.pi / 2) for index in range(1, 17))
    for step in range(1, 25):
        angle = math.pi - math.pi / 2 * step / 24
        poses.append(
            PosePoint(
                2.4 + 1.2 * math.cos(angle), 9.2 + 1.2 * math.sin(angle), angle - math.pi / 2
            )
        )
    poses.extend(PosePoint(2.4 + index * 0.5, 10.4, 0.0) for index in range(1, 21))
    lanes = [
        Lane.create(
            "main", "主通道", 3.4, [Point2D(-16, 0), Point2D(1.2, 0), Point2D(1.2, 1.2)]
        ),
        Lane.create(
            "branch", "支通道", 2.2,
            [Point2D(1.2, 0.6), Point2D(1.2, 10.4), Point2D(26, 10.4)],
        ),
    ]
    return tuple(poses), lanes, VehicleDimensions(1.20, 1.00, 1.60)


def corner_scene() -> tuple[tuple[PosePoint, ...], list[Lane], VehicleDimensions]:
    """A lane filleted at R 1.60 with a path that turns at R 1.00 through the same legs.

    The two radii must differ, otherwise the offset-along-the-bend arch is flat and the
    view has nothing to say.
    """

    lane = Lane.create(
        "L", "主通道", 3.2,
        [Point2D(-10, 0), Point2D(-1.2, 0), Point2D(0.4, 1.6), Point2D(0.4, 11)],
    )
    lane.segments[1] = LaneSegment(
        kind=SegmentKind.ARC, arc_center=Point2D(-1.2, 1.6), clockwise=False
    )
    lane = replace_arc_radius(lane, 1, 1.60)
    poses = [PosePoint(-10 + index * 0.4, 0.0, 0.0) for index in range(24)]
    for step in range(1, 25):
        angle = -math.pi / 2 + math.pi / 2 * step / 24
        poses.append(
            PosePoint(-0.6 + math.cos(angle), 1.0 + math.sin(angle), angle + math.pi / 2)
        )
    poses.extend(PosePoint(0.4, 1.0 + index * 0.4, math.pi / 2) for index in range(1, 26))
    return tuple(poses), [lane], VehicleDimensions(1.20, 1.00, 1.60)


def _wait_for(app: QApplication, ready: Callable[[], bool], timeout_ms: int = 30_000) -> None:
    """Wait on an idle event loop, never on QTest.qWait.

    A spinning main thread holds the GIL between switch intervals and starves the solver
    thread so badly that a 40 ms job does not finish inside ten seconds.
    """

    loop = QEventLoop()
    QTimer.singleShot(timeout_ms, loop.quit)

    def poll() -> None:
        if ready():
            loop.quit()
            return
        QTimer.singleShot(50, poll)

    QTimer.singleShot(50, poll)
    loop.exec()
    app.processEvents()


def render_corner(app: QApplication, output: Path) -> bool:
    """Grab the corner solver, the one view whose whole point is two unequal radii."""

    poses, lanes, dimensions = corner_scene()
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, dimensions, lanes, settings)
    if analysis is None:
        return False
    turn = next(
        (item for item in analysis.segments if item.role is SegmentRole.TURN), None
    )
    if turn is None:
        return False
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
    window = QMainWindow()
    window.setWindowTitle(f"Suntae 路径通行分析 {__version__} — 转角求解视觉检查")
    window.setCentralWidget(panel)
    window.resize(1240, 860)
    window.show()
    app.processEvents()
    panel._open_corner(turn.index)
    _wait_for(app, lambda: bool(panel.corner_view._optimum))
    corner_output = output.with_name(f"{output.stem}-corner{output.suffix}")
    return bool(window.grab().save(str(corner_output)))


def render_clearance(app: QApplication, output: Path) -> bool:
    """Grab the clearance headroom view, so a whole page of UI is not left unwatched."""

    poses, lanes, dimensions = clearance_scene()
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, dimensions, lanes, settings)
    if analysis is None:
        return False
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
    window = QMainWindow()
    window.setWindowTitle(f"Suntae 路径通行分析 {__version__} — 通行余量视觉检查")
    window.setCentralWidget(panel)
    window.resize(1240, 860)
    window.show()
    app.processEvents()
    panel.overview.table.selectRow(0)
    _wait_for(app, lambda: panel.selected_zones() is not None)
    clearance_output = output.with_name(f"{output.stem}-clearance{output.suffix}")
    return bool(window.grab().save(str(clearance_output)))


def render_scenario(app: QApplication, output: Path) -> bool:
    """Grab the rapid-estimate tab in the variant that exercises every result card."""

    panel = ScenarioPanel()
    window = QMainWindow()
    window.setWindowTitle(f"Suntae 路径通行分析 {__version__} — 场景速算视觉检查")
    window.setCentralWidget(panel)
    window.resize(1280, 800)
    window.show()
    app.processEvents()
    panel.select_variant(
        scenario=Scenario.UTURN, mode=SolveMode.CHECK, extreme=True, bidirectional=True
    )
    _wait_for(app, lambda: panel.result is not None and panel.result.inputs.extreme)
    scenario_output = output.with_name(f"{output.stem}-scenario{output.suffix}")
    return bool(window.grab().save(str(scenario_output)))


if __name__ == "__main__":
    raise SystemExit(main())
