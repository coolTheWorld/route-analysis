from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from route_analysis.api_client import (
    CommandRecord,
    ConnectionSettings,
    OrderRecord,
    Page,
    TaskRecord,
)
from route_analysis.auto_lane_dialog import AutoLaneDialog
from route_analysis.main_window import MainWindow
from route_analysis.models import PosePoint, VehicleDimensions
from route_analysis.storage import AppConfig, ConfigRepository


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
        lambda: "未识别到转弯" in window.control_panel.radius_summaries["dispatched"].text()
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
