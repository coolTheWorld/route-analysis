import math
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QGraphicsPolygonItem, QGraphicsTextItem
from pytestqt.qtbot import QtBot

from route_analysis.api_client import (
    CommandRecord,
    ConnectionSettings,
    OrderRecord,
    Page,
    TaskRecord,
)
from route_analysis.auto_lane_dialog import AutoLaneDialog
from route_analysis.errors import ApiError
from route_analysis.logging_setup import configure_logging
from route_analysis.main_window import MainWindow
from route_analysis.models import (
    ClearanceStatus,
    CommandPathData,
    Point2D,
    PosePoint,
    VehicleDimensions,
)
from route_analysis.parsing import parse_command_details
from route_analysis.settings_dialog import SettingsDialog
from route_analysis.storage import AppConfig, ConfigRepository
from route_analysis.turn_measurements import (
    MeasurementScope,
    RadiusMeasurementRepository,
    RadiusMeasurementState,
    path_fingerprint,
)
from route_analysis.workers import Worker


class FakeClient:
    def list_orders(
        self,
        *,
        page_no: int,
        page_size: int,
        order_id: int | None = None,
    ) -> Page[OrderRecord]:
        return Page(
            (OrderRecord(929, 7, "执行中", "2026-01-01", {"id": 929}),),
            1,
            page_no,
            page_size,
        )

    def list_tasks(self, *, order_id: int, page_no: int, page_size: int) -> Page[TaskRecord]:
        return Page(
            (TaskRecord(41330, order_id, "VIN-1", 7, "执行中", {"id": 41330}),),
            1,
            page_no,
            page_size,
        )

    def list_commands(self, *, task_id: int) -> tuple[CommandRecord, ...]:
        return (CommandRecord(9063, task_id, "VIN-1", 7, "完成", "MOVE", {"id": 9063}),)

    def get_dispatched_path(self, *, command_id: int) -> CommandPathData:
        return parse_command_details(
            {
                "commandId": command_id,
                "positionList": [
                    {"x": 0, "y": 0, "yaw": 0, "gear": "D"},
                    {"x": 2, "y": 0, "yaw": 0, "gear": "D"},
                ],
            }
        )

    def get_actual_path(self, *, command_id: int, vin: str) -> CommandPathData:
        return parse_command_details(
            {
                "commandId": command_id,
                "vin": vin,
                "positionList": [
                    {"x": 0, "y": 0.1, "yaw": 0, "gear": "D"},
                    {"x": 2, "y": 0.1, "yaw": 0, "gear": "R"},
                ],
            }
        )


class RetryActualClient(FakeClient):
    def __init__(self) -> None:
        self.actual_calls = 0

    def get_actual_path(self, *, command_id: int, vin: str) -> CommandPathData:
        self.actual_calls += 1
        if self.actual_calls == 1:
            raise ApiError("实际路径接口暂时不可用")
        return super().get_actual_path(command_id=command_id, vin=vin)


def make_config(data_dir: Path) -> AppConfig:
    config = AppConfig(
        api_root="http://example.test/admin-api",
        username="operator",
        password="secret",
        default_vehicle=VehicleDimensions(1, 1, 1),
        default_lane_width=2,
    )
    ConfigRepository(data_dir).save(config)
    return config


def test_single_window_drills_order_task_command_and_loads_both_paths(
    qtbot: QtBot, tmp_path: Path
) -> None:
    make_config(tmp_path)

    def client_factory(_settings: ConnectionSettings) -> FakeClient:
        return FakeClient()

    window = MainWindow(tmp_path, client_factory=client_factory, auto_load=False)
    qtbot.addWidget(window)
    window.show()

    window.refresh_current_level()
    qtbot.waitUntil(lambda: window.navigation_level == "orders" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "tasks" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "commands" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()

    qtbot.waitUntil(lambda: window.canvas.path_point_counts == {"dispatched": 2, "actual": 2})
    qtbot.waitUntil(
        lambda: window.control_panel.radius_summaries["dispatched"].text() == "0 条"
    )
    assert "929" in window.breadcrumb_label.text()
    assert "41330" in window.breadcrumb_label.text()
    assert window.windowTitle().startswith("Suntae")


def test_command_points_show_raw_json_and_select_canvas_both_directions(
    qtbot: QtBot, tmp_path: Path
) -> None:
    make_config(tmp_path)
    window = MainWindow(
        tmp_path,
        client_factory=lambda _settings: FakeClient(),
        auto_load=False,
    )
    qtbot.addWidget(window)

    window.refresh_current_level()
    qtbot.waitUntil(lambda: window.table.rowCount() == 1)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "tasks" and window.table.rowCount() == 1)
    window.activate_selected()
    qtbot.waitUntil(
        lambda: window.navigation_level == "commands" and window.table.rowCount() == 1
    )
    window.activate_selected()

    qtbot.waitUntil(lambda: window.path_details.models["dispatched"].rowCount() == 2)
    assert not window.path_details.isHidden()
    assert '"commandId": 9063' in window.path_details.command_json_editor.toPlainText()

    window.path_details.tables["dispatched"].selectRow(1)
    assert window.canvas.selected_path_point == ("dispatched", 1)
    assert '"gear": "D"' in window.path_details.point_json_editor.toPlainText()

    window.path_details.source_tabs.setCurrentIndex(1)
    window.canvas.path_point_selected.emit("actual", 1)
    assert window.path_details.tables["actual"].currentIndex().row() == 1
    assert '"gear": "R"' in window.path_details.point_json_editor.toPlainText()


def test_path_sources_fail_independently_and_actual_can_retry(
    qtbot: QtBot, tmp_path: Path
) -> None:
    make_config(tmp_path)
    client = RetryActualClient()
    window = MainWindow(
        tmp_path,
        client_factory=lambda _settings: client,
        auto_load=False,
    )
    qtbot.addWidget(window)

    window.refresh_current_level()
    qtbot.waitUntil(lambda: window.table.rowCount() == 1)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "tasks" and window.table.rowCount() == 1)
    window.activate_selected()
    qtbot.waitUntil(
        lambda: window.navigation_level == "commands" and window.table.rowCount() == 1
    )
    window.activate_selected()

    qtbot.waitUntil(lambda: not window.path_details.retry_buttons["actual"].isHidden())
    assert window.path_details.models["dispatched"].rowCount() == 2
    assert window.path_details.models["actual"].rowCount() == 0

    window.path_details.retry_buttons["actual"].click()

    qtbot.waitUntil(lambda: window.path_details.models["actual"].rowCount() == 2)
    assert window.path_details.retry_buttons["actual"].isHidden()
    assert client.actual_calls == 2


def test_stale_path_request_completion_does_not_leave_navigation_busy(
    qtbot: QtBot, tmp_path: Path
) -> None:
    make_config(tmp_path)
    window = MainWindow(
        tmp_path,
        client_factory=lambda _settings: FakeClient(),
        auto_load=False,
    )
    qtbot.addWidget(window)
    stale_worker: Worker[object] = Worker(lambda: None)
    window._workers.add(stale_worker)
    window._path_load_generation = 2
    window._pending_path_loads.add((1, "actual"))
    window._path_loading_busy = True
    window._set_busy(True, "正在加载旧命令")

    window._path_source_finished(stale_worker, 1, "actual")

    assert window._busy is False
    assert window.table.isEnabled()


def test_returning_to_tasks_keeps_displayed_paths_analyzable(qtbot: QtBot, tmp_path: Path) -> None:
    make_config(tmp_path)
    window = MainWindow(
        tmp_path,
        client_factory=lambda _settings: FakeClient(),
        auto_load=False,
    )
    qtbot.addWidget(window)

    window.refresh_current_level()
    qtbot.waitUntil(lambda: window.navigation_level == "orders" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "tasks" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "commands" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.canvas.path_point_counts == {"dispatched": 2, "actual": 2})
    qtbot.waitUntil(lambda: window.canvas._results["dispatched"] is not None)
    assert window.canvas._results["dispatched"].status is ClearanceStatus.UNAVAILABLE

    window.go_back()
    qtbot.waitUntil(lambda: window.navigation_level == "tasks" and window.table.rowCount() == 1)
    assert window._current_command is None
    assert window.canvas.path_point_counts == {"dispatched": 2, "actual": 2}

    try:
        window.canvas.add_lane(
            [Point2D(-1, 0), Point2D(3, 0)],
            width=3.5,
            name="覆盖路径",
        )
        window.control_panel.analyze_requested.emit()

        qtbot.waitUntil(
            lambda: (
                window.canvas._results["dispatched"] is not None
                and window.canvas._results["dispatched"].analyzed_samples > 0
            )
        )
        assert window.canvas._results["dispatched"].status is not ClearanceStatus.UNAVAILABLE
    finally:
        qtbot.waitUntil(lambda: not window._analysis_timer.isActive() and not window._workers)
        window.canvas.mark_saved()


def test_confirming_auto_lane_adds_once_and_persists_last_generation_mode(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_config(tmp_path)
    window = MainWindow(tmp_path, client_factory=lambda _settings: FakeClient(), auto_load=False)
    qtbot.addWidget(window)
    window._dispatched_path = (
        PosePoint(0, 0, None),
        PosePoint(1, 0.01, None),
        PosePoint(2, 0, None),
    )
    monkeypatch.setattr(
        AutoLaneDialog,
        "exec",
        lambda _dialog: AutoLaneDialog.DialogCode.Accepted,
    )

    window.open_auto_lane_dialog()

    assert len(window.canvas.current_layout().lanes) == 1
    assert window.canvas.undo_stack.count() == 1
    assert ConfigRepository(tmp_path).load().lane_generation_mode == "sharp"
    window.canvas.undo_stack.undo()
    assert window.canvas.current_layout().lanes == []


def test_logging_failure_status_is_persistent_and_recovers(
    qtbot: QtBot, tmp_path: Path
) -> None:
    make_config(tmp_path / "data")
    manager = configure_logging(tmp_path / "log")
    window = MainWindow(
        tmp_path / "data",
        client_factory=lambda _settings: FakeClient(),
        auto_load=False,
        logging_manager=manager,
    )
    qtbot.addWidget(window)

    manager.handler._publish_state(False, "disk unavailable")
    qtbot.waitUntil(lambda: window.log_status_label.text() == "日志不可用")
    assert "disk unavailable" in window.log_status_label.toolTip()

    manager.handler._publish_state(True)
    qtbot.waitUntil(lambda: window.log_status_label.text() == "")
    manager.close()


def test_failed_radius_persistence_restores_previous_state(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_config(tmp_path)
    window = MainWindow(
        tmp_path,
        client_factory=lambda _settings: FakeClient(),
        auto_load=False,
    )
    qtbot.addWidget(window)
    previous = RadiusMeasurementState("fingerprint")
    mutated = RadiusMeasurementState("fingerprint")
    mutated.add_manual(0, 2)
    window._radius_states["dispatched"] = mutated
    monkeypatch.setattr(window, "_persist_radius_state", lambda _path_name: False)

    committed = window._commit_radius_state("dispatched", previous)

    assert committed is False
    restored = window._radius_states["dispatched"]
    assert restored is previous
    assert restored.records == ()


def test_saving_changed_vehicle_dimensions_refreshes_canvas_vehicle_frames(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_config(tmp_path)
    client = FakeClient()
    window = MainWindow(
        tmp_path,
        client_factory=lambda _settings: client,
        auto_load=False,
    )
    qtbot.addWidget(window)
    order = client.list_orders(page_no=1, page_size=20).items[0]
    task = client.list_tasks(order_id=order.id, page_no=1, page_size=20).items[0]
    command = client.list_commands(task_id=task.id)[0]
    window._current_order = order
    window._current_task = task
    window._current_command = command
    window._lane_key = (window._server_id(), "7")
    path = tuple(
        PosePoint(5 * math.cos(angle), 5 * math.sin(angle), angle + math.pi / 2)
        for angle in (index * math.pi / 40 for index in range(21))
    )
    window._show_paths(
        "VIN-1",
        (CommandPathData.from_poses(path), CommandPathData.empty()),
    )
    state = window._radius_states["dispatched"]
    assert state is not None
    state.add_manual(0, 20)
    window._refresh_radius_measurements()
    previous_measurement = window._calculated_radii["dispatched"][0]
    previous_radius = previous_measurement.radius.radii
    window.canvas.show_turn_radius_observation("dispatched", previous_measurement.radius)

    def frame_dimensions(z_value: float) -> tuple[float, float]:
        frame = next(
            item
            for item in window.canvas.scene().items()
            if isinstance(item, QGraphicsPolygonItem) and item.zValue() == z_value
        )
        polygon = frame.polygon()
        edge_lengths = sorted(
            math.hypot(
                polygon.at(index + 1).x() - polygon.at(index).x(),
                polygon.at(index + 1).y() - polygon.at(index).y(),
            )
            for index in range(4)
        )
        return edge_lengths[0], edge_lengths[-1]

    assert frame_dimensions(6) == pytest.approx((1, 2))
    assert frame_dimensions(60) == pytest.approx((1, 2))
    changed_dimensions = VehicleDimensions(width=4, center_front=5, center_rear=2)
    changed_config = replace(window.config, default_vehicle=changed_dimensions)

    def accept_settings(dialog: SettingsDialog) -> SettingsDialog.DialogCode:
        dialog.result_config = changed_config
        dialog.result_profiles = {}
        return SettingsDialog.DialogCode.Accepted

    monkeypatch.setattr(SettingsDialog, "exec", accept_settings)

    window.open_settings()

    current_measurement = window._calculated_radii["dispatched"][0]
    current_radius = current_measurement.radius.radii
    window.canvas.show_turn_radius_observation("dispatched", current_measurement.radius)
    assert current_radius != previous_radius
    assert frame_dimensions(6) == pytest.approx((4, 7))
    assert frame_dimensions(60) == pytest.approx((4, 7))


def test_radius_measurements_are_saved_locally_and_deleted_from_the_tree(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    make_config(tmp_path)
    client = FakeClient()
    window = MainWindow(tmp_path, client_factory=lambda _settings: client, auto_load=False)
    qtbot.addWidget(window)
    order = client.list_orders(page_no=1, page_size=20).items[0]
    task = client.list_tasks(order_id=order.id, page_no=1, page_size=20).items[0]
    command = client.list_commands(task_id=task.id)[0]
    window._current_order = order
    window._current_task = task
    window._current_command = command
    window._lane_key = (window._server_id(), "7")
    path = tuple(
        PosePoint(5 * math.cos(angle), 5 * math.sin(angle), angle + math.pi / 2)
        for angle in (index * math.pi / 40 for index in range(21))
    )

    window._show_paths(
        "VIN-1",
        (CommandPathData.from_poses(path), CommandPathData.empty()),
    )
    window.control_panel.radius_manual_buttons["dispatched"].click()
    window._radius_endpoint_selected("dispatched", 0)
    window._radius_endpoint_selected("dispatched", 20)

    tree = window.control_panel.radius_trees["dispatched"]
    assert tree.topLevelItemCount() == 1
    measurement_item = tree.topLevelItem(0)
    assert measurement_item is not None
    assert measurement_item.childCount() == 5
    # Both endpoints stay highlighted next to the finished measurement's graphics.
    labels = sorted(
        item.toPlainText()
        for item in window.canvas.scene().items()
        if isinstance(item, QGraphicsTextItem) and item.toPlainText().isdigit()
    )
    assert labels == ["1", "21"]

    scope = MeasurementScope(
        window._server_id(),
        "suntae",
        order.id,
        task.id,
        command.id,
        "dispatched",
    )
    saved = RadiusMeasurementRepository(tmp_path).load(scope, path_fingerprint(path))
    assert len(saved.records) == 1
    assert saved.records[0].start_index == 0
    assert saved.records[0].end_index == 20

    window._delete_radius_measurement("dispatched", saved.records[0].id)

    assert tree.topLevelItemCount() == 0
    assert RadiusMeasurementRepository(tmp_path).load(
        scope, path_fingerprint(path)
    ).records == ()


def test_lane_drawing_guidance_is_shown_in_status_bar(qtbot: QtBot, tmp_path: Path) -> None:
    make_config(tmp_path)
    window = MainWindow(tmp_path, auto_load=False)
    qtbot.addWidget(window)

    window.canvas.start_lane_drawing(width=2)
    assert "第一个车道锚点" in window.status_label.text()

    result = window.canvas.finish_lane_drawing()

    assert result is None
    assert "至少需要两个锚点" in window.status_label.text()


def _loaded_window(qtbot: QtBot, tmp_path: Path) -> MainWindow:
    make_config(tmp_path)
    window = MainWindow(
        tmp_path, client_factory=lambda _settings: FakeClient(), auto_load=False
    )
    qtbot.addWidget(window)
    window.show()
    window.refresh_current_level()
    qtbot.waitUntil(lambda: window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "tasks" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.navigation_level == "commands" and window.table.rowCount() == 1)
    window.table.selectRow(0)
    window.activate_selected()
    qtbot.waitUntil(lambda: window.canvas.path_point_counts == {"dispatched": 2, "actual": 2})
    return window


def test_canvas_area_offers_the_map_and_the_clearance_tab(qtbot: QtBot, tmp_path: Path) -> None:
    make_config(tmp_path)
    window = MainWindow(
        tmp_path, client_factory=lambda _settings: FakeClient(), auto_load=False
    )
    qtbot.addWidget(window)
    assert window.canvas_tabs.count() == 2
    assert window.canvas_tabs.tabText(0) == "地图"
    assert window.canvas_tabs.tabText(1) == "通行余量"
    assert window.canvas_tabs.widget(0) is window.canvas
    assert window.canvas_tabs.currentIndex() == 0


def test_clearance_is_not_solved_while_its_tab_is_hidden(qtbot: QtBot, tmp_path: Path) -> None:
    window = _loaded_window(qtbot, tmp_path)
    window.analyze_now()
    qtbot.wait(200)
    assert window.canvas_tabs.currentIndex() == 0
    assert window._clearance_stale
    assert window.clearance_panel.analysis is None


def test_opening_the_clearance_tab_without_lanes_leaves_the_panel_empty(
    qtbot: QtBot, tmp_path: Path
) -> None:
    window = _loaded_window(qtbot, tmp_path)
    window.canvas_tabs.setCurrentIndex(1)
    qtbot.wait(300)
    assert window.clearance_panel.analysis is None
    assert not window.clearance_panel.overview.csv_button.isEnabled()


def test_selecting_a_clearance_pose_returns_to_the_map(qtbot: QtBot, tmp_path: Path) -> None:
    window = _loaded_window(qtbot, tmp_path)
    window.canvas_tabs.setCurrentIndex(1)
    window._clearance_pose_selected(0)
    assert window.canvas_tabs.currentIndex() == 0
    assert window.path_details.selected_source_index("dispatched") == 0


def test_clearance_exports_do_nothing_without_an_analysis(
    qtbot: QtBot, tmp_path: Path
) -> None:
    window = _loaded_window(qtbot, tmp_path)
    window.export_offset_table()
    window.export_clearance_report()
    assert window.clearance_panel.analysis is None
