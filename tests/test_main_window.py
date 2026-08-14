import math
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QGraphicsPolygonItem
from pytestqt.qtbot import QtBot

from route_analysis.api_client import (
    CommandRecord,
    ConnectionSettings,
    OrderRecord,
    Page,
    TaskRecord,
)
from route_analysis.auto_lane_dialog import AutoLaneDialog
from route_analysis.logging_setup import configure_logging
from route_analysis.main_window import MainWindow
from route_analysis.models import PosePoint, VehicleDimensions
from route_analysis.settings_dialog import SettingsDialog
from route_analysis.storage import AppConfig, ConfigRepository
from route_analysis.turn_measurements import (
    MeasurementScope,
    RadiusMeasurementRepository,
    RadiusMeasurementState,
    path_fingerprint,
)


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

    def get_dispatched_path(self, *, command_id: int) -> tuple[PosePoint, ...]:
        return (PosePoint(0, 0, 0), PosePoint(2, 0, 0))

    def get_actual_path(self, *, command_id: int, vin: str) -> tuple[PosePoint, ...]:
        return (PosePoint(0, 0.1, 0), PosePoint(2, 0.1, 0))


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
        lambda: "自动 0" in window.control_panel.radius_summaries["dispatched"].text()
    )
    assert "929" in window.breadcrumb_label.text()
    assert "41330" in window.breadcrumb_label.text()
    assert window.windowTitle().startswith("Suntae")


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
    window._show_paths("VIN-1", (path, ()))
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


def test_automatic_and_manual_radius_measurements_are_saved_locally(
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

    window._show_paths("VIN-1", (path, ()))
    window.control_panel.radius_auto_buttons["dispatched"].click()

    tree = window.control_panel.radius_trees["dispatched"]

    def group_child_count(index: int) -> int:
        group = tree.topLevelItem(index)
        assert group is not None
        return group.childCount()

    qtbot.waitUntil(lambda: group_child_count(0) == 1)
    assert group_child_count(1) == 0
    window.control_panel.radius_manual_buttons["dispatched"].click()
    window._radius_endpoint_selected("dispatched", 0)
    window._radius_endpoint_selected("dispatched", 20)
    assert group_child_count(1) == 1

    scope = MeasurementScope(
        window._server_id(),
        "suntae",
        order.id,
        task.id,
        command.id,
        "dispatched",
    )
    saved = RadiusMeasurementRepository(tmp_path).load(scope, path_fingerprint(path))
    assert len(saved.automatic_records) == 1
    assert len(saved.manual_records) == 1

    window._clear_automatic_radii_for_threshold_change()

    assert group_child_count(0) == 0
    assert group_child_count(1) == 1
    saved_after_threshold_change = RadiusMeasurementRepository(tmp_path).load(
        scope, path_fingerprint(path)
    )
    assert saved_after_threshold_change.automatic_records == ()
    assert len(saved_after_threshold_change.manual_records) == 1
    assert "重新自动计算" in window.status_label.text()
