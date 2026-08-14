"""Single-window application shell and navigation orchestration."""

from __future__ import annotations

import copy
import logging
import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

from PySide6.QtCore import QRegularExpression, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QKeySequence,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from route_analysis.analysis import analyze_path
from route_analysis.api_client import (
    CommandRecord,
    ConnectionSettings,
    OrderRecord,
    Page,
    SchedulerClient,
    TaskRecord,
)
from route_analysis.auto_lane_dialog import log_auto_lane_selection, run_auto_lane_dialog
from route_analysis.canvas import RouteCanvas
from route_analysis.control_panel import ControlPanel
from route_analysis.errors import RouteAnalysisError, StorageError
from route_analysis.geometry import build_traversable_area
from route_analysis.lane_generation import BendMode
from route_analysis.logging_setup import LoggingManager, LoggingState, log_event
from route_analysis.logging_ui import add_log_menu
from route_analysis.models import AnalysisResult, PosePoint, VehicleDimensions
from route_analysis.settings_dialog import SettingsDialog
from route_analysis.storage import (
    ConfigRepository,
    LaneLayout,
    LaneRepository,
    VehicleProfileRepository,
    server_id_for,
)
from route_analysis.turn_measurements import (
    CalculatedMeasurement,
    MeasurementScope,
    RadiusMeasurementRepository,
    RadiusMeasurementState,
    path_fingerprint,
    recalculate_measurements,
)
from route_analysis.turn_radius import calculate_turn_radius, detect_turns
from route_analysis.workers import Worker

LOGGER = logging.getLogger(__name__)


class SchedulerApi(Protocol):
    def list_orders(
        self,
        *,
        page_no: int,
        page_size: int,
        order_id: int | None = None,
    ) -> Page[OrderRecord]: ...

    def list_tasks(
        self,
        *,
        order_id: int,
        page_no: int,
        page_size: int,
    ) -> Page[TaskRecord]: ...

    def list_commands(self, *, task_id: int) -> tuple[CommandRecord, ...]: ...

    def get_dispatched_path(self, *, command_id: int) -> tuple[PosePoint, ...]: ...

    def get_actual_path(self, *, command_id: int, vin: str) -> tuple[PosePoint, ...]: ...


Record = OrderRecord | TaskRecord | CommandRecord


class MainWindow(QMainWindow):
    logging_state_changed = Signal(object)

    def __init__(
        self,
        data_dir: Path,
        *,
        client_factory: Callable[[ConnectionSettings], SchedulerApi] | None = None,
        auto_load: bool = True,
        logging_manager: LoggingManager | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Suntae 路径通行分析")
        self.setMinimumSize(1180, 720)
        self.resize(1540, 920)
        self._data_dir = data_dir
        self._logging_manager = logging_manager
        self._config_repository = ConfigRepository(data_dir)
        self._profile_repository = VehicleProfileRepository(data_dir)
        self._lane_repository = LaneRepository(data_dir)
        self._radius_repository = RadiusMeasurementRepository(data_dir)
        self.config = self._config_repository.load()
        self._profiles = dict(self._profile_repository.load().values)
        factory = client_factory or (lambda settings: SchedulerClient(settings))
        self._client_factory = factory
        self._client: SchedulerApi | None = None
        with suppress(ValueError):
            self._client = factory(self.config.connection())
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._analysis_pool = QThreadPool(self)
        self._analysis_pool.setMaxThreadCount(1)
        self._radius_pool = QThreadPool(self)
        self._radius_pool.setMaxThreadCount(1)
        self._workers: set[Worker[object]] = set()
        self._busy = False
        self._analysis_generation = 0
        self._analysis_timer = QTimer(self)
        self._analysis_timer.setSingleShot(True)
        self._analysis_timer.setInterval(250)
        self._analysis_timer.timeout.connect(self.analyze_now)

        self._navigation_level = "orders"
        self._page_no = 1
        self._page_size = 20
        self._total = 0
        self._records: list[Record] = []
        self._current_order: OrderRecord | None = None
        self._current_task: TaskRecord | None = None
        self._current_command: CommandRecord | None = None
        self._lane_key: tuple[str, str] | None = None
        self._dispatched_path: tuple[PosePoint, ...] = ()
        self._actual_path: tuple[PosePoint, ...] = ()
        self._radius_states: dict[str, RadiusMeasurementState | None] = {
            "dispatched": None,
            "actual": None,
        }
        self._calculated_radii: dict[str, tuple[CalculatedMeasurement, ...]] = {
            "dispatched": (),
            "actual": (),
        }
        self._manual_radius_path: str | None = None
        self._manual_radius_start: int | None = None
        self._radius_generations = {"dispatched": 0, "actual": 0}

        self._build_ui()
        self._build_menus()
        self._apply_config()
        self.logging_state_changed.connect(self._apply_logging_state)
        if self._logging_manager is not None:
            self._logging_manager.handler.state_callback = self.logging_state_changed.emit
            self._apply_logging_state(self._logging_manager.state)
        self._update_navigation_header()
        self.canvas.layout_changed.connect(lambda: self._analysis_timer.start())
        self.canvas.undo_stack.cleanChanged.connect(
            lambda clean: self._dirty_changed(not clean)
        )
        if auto_load and self.config.api_root.strip():
            QTimer.singleShot(0, self.refresh_current_level)
        elif auto_load:
            self.status_label.setText("请先在设置中填写调度后端 API 根地址和登录信息")

    @property
    def navigation_level(self) -> str:
        return self._navigation_level

    def _build_ui(self) -> None:
        self.status_label = QLabel("就绪")
        self.coordinate_label = QLabel("原始坐标  X —   Y —")
        root_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_splitter.setChildrenCollapsible(False)
        root_splitter.addWidget(self._build_navigation_panel())
        right = QSplitter(Qt.Orientation.Horizontal)
        right.setChildrenCollapsible(False)
        self.canvas = RouteCanvas()
        self.canvas.mouse_coordinate_changed.connect(
            lambda x, y: self.coordinate_label.setText(f"原始坐标  X {x:.3f} m   Y {y:.3f} m")
        )
        self.control_panel = ControlPanel(self.canvas)
        self.control_panel.save_requested.connect(self.save_lanes)
        self.control_panel.import_requested.connect(self.import_lanes)
        self.control_panel.export_requested.connect(self.export_lanes)
        self.control_panel.settings_requested.connect(self.open_settings)
        self.control_panel.analyze_requested.connect(self.analyze_now)
        self.control_panel.generate_lane_requested.connect(self.open_auto_lane_dialog)
        self.control_panel.direction_changed.connect(self._direction_changed)
        self.control_panel.auto_radius_requested.connect(self._calculate_automatic_radii)
        self.control_panel.manual_radius_requested.connect(self._toggle_manual_radius)
        self.control_panel.radius_delete_requested.connect(self._delete_radius_measurement)
        self.control_panel.radius_rename_requested.connect(self._rename_radius_measurement)
        self.canvas.radius_endpoint_selected.connect(self._radius_endpoint_selected)
        self.canvas.manual_radius_cancelled.connect(
            lambda _path: self._finish_manual_radius()
        )
        right.addWidget(self.canvas)
        right.addWidget(self.control_panel)
        right.setStretchFactor(0, 1)
        root_splitter.addWidget(right)
        root_splitter.setStretchFactor(1, 1)
        root_splitter.setSizes([330, 1200])
        self.setCentralWidget(root_splitter)

        self.statusBar().addWidget(self.status_label, 1)
        self.log_status_label = QLabel()
        self.log_status_label.setStyleSheet("color:#b4233f;font-weight:600")
        self.log_status_label.setAccessibleName("日志状态")
        self.statusBar().addPermanentWidget(self.log_status_label)
        self.statusBar().addPermanentWidget(self.coordinate_label)

    def _build_navigation_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("navigationPanel")
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(460)
        layout = QVBoxLayout(panel)
        header = QHBoxLayout()
        self.back_button = QPushButton("← 返回")
        self.back_button.clicked.connect(self.go_back)
        self.breadcrumb_label = QLabel("订单")
        self.breadcrumb_label.setWordWrap(True)
        self.breadcrumb_label.setStyleSheet("font-weight:600;font-size:14px")
        header.addWidget(self.back_button)
        header.addWidget(self.breadcrumb_label, 1)
        layout.addLayout(header)

        search = QHBoxLayout()
        self.order_id_edit = QLineEdit()
        self.order_id_edit.setPlaceholderText("订单 ID（精确）")
        self.order_id_edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d*")))
        self.order_id_edit.setAccessibleName("订单 ID 精确查询")
        self.search_button = QPushButton("查询")
        self.search_button.clicked.connect(self._search_orders)
        self.order_id_edit.returnPressed.connect(self._search_orders)
        search.addWidget(self.order_id_edit, 1)
        search.addWidget(self.search_button)
        layout.addLayout(search)

        self.table = QTableWidget()
        self.table.setAccessibleName("订单任务命令列表")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda _item: self.activate_selected())
        self.table.itemActivated.connect(lambda _item: self.activate_selected())
        layout.addWidget(self.table, 1)

        pager = QHBoxLayout()
        self.previous_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 页")
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        self.next_button.clicked.connect(lambda: self._change_page(1))
        pager.addWidget(self.previous_button)
        pager.addWidget(self.page_label, 1, Qt.AlignmentFlag.AlignCenter)
        pager.addWidget(self.next_button)
        layout.addLayout(pager)
        return panel

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        save_action = QAction("保存车道", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_lanes)
        import_action = QAction("导入车道并替换…", self)
        import_action.triggered.connect(self.import_lanes)
        export_action = QAction("导出车道…", self)
        export_action.triggered.connect(self.export_lanes)
        quit_action = QAction("退出", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addActions([save_action, import_action, export_action])
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        edit_menu = self.menuBar().addMenu("编辑")
        edit_menu.addAction(self.canvas.undo_stack.createUndoAction(self, "撤销"))
        edit_menu.addAction(self.canvas.undo_stack.createRedoAction(self, "重做"))
        settings_action = QAction("设置…", self)
        settings_action.triggered.connect(self.open_settings)
        self.menuBar().addAction(settings_action)

        log_file = (
            self._logging_manager.current_file
            if self._logging_manager is not None
            else self._data_dir.with_name("log") / "route-analysis.log"
        )
        add_log_menu(self.menuBar(), self, log_file)

    def _apply_logging_state(self, state: LoggingState) -> None:
        self.log_status_label.setText("" if state.available else "日志不可用")
        self.log_status_label.setToolTip(state.error or str(state.current_file))

    def _apply_config(self) -> None:
        if self.config.default_vehicle is None or self.config.default_lane_width is None:
            return
        self.canvas.set_map_direction(self.config.map_direction)
        self.canvas.set_snap_enabled(self.config.snap_to_path)
        self.canvas.set_geometry_settings(
            bezier_tolerance=self.config.analysis.bezier_tolerance,
            miter_limit=self.config.analysis.miter_limit,
        )
        self.control_panel.set_configuration(
            default_lane_width=self.config.default_lane_width,
            direction=self.config.map_direction,
        )

    def open_auto_lane_dialog(self) -> None:
        if self.config.default_lane_width is None:
            self._show_error("请先在设置中填写新车道默认总宽")
            return
        if len(self._dispatched_path) < 2 and len(self._actual_path) < 2:
            self._show_error("当前命令没有可用于生成车道的路径坐标")
            return
        selection = run_auto_lane_dialog(
            self,
            {
                "dispatched": self._dispatched_path,
                "actual": self._actual_path,
            },
            default_width=self.config.default_lane_width,
            maximum_deviation=self.config.analysis.lane_generation_deviation,
            last_mode=BendMode(self.config.lane_generation_mode),
            preview_callback=lambda result: self.canvas.set_lane_preview(
                None if result is None else result.lane
            ),
        )
        if selection is None:
            return

        result = selection.generation
        selected_source = selection.source
        new_config = replace(
            self.config,
            analysis=replace(
                self.config.analysis,
                lane_generation_deviation=selection.maximum_deviation,
            ),
            lane_generation_mode=selection.mode,
        )
        try:
            self._config_repository.save(new_config)
        except RouteAnalysisError as exc:
            self._show_error(str(exc))
            return
        self.config = new_config
        self.canvas.add_generated_lane(result.lane)
        metrics = result.metrics
        self.status_label.setText(
            f"已新增车道：{result.lane.name}；最大偏差 {metrics.maximum_deviation:.6f} m"
        )
        source_path = (
            self._dispatched_path if selected_source == "dispatched" else self._actual_path
        )
        log_auto_lane_selection(selection, source_path)

    def _search_orders(self) -> None:
        if self._navigation_level != "orders":
            self._navigation_level = "orders"
            self._current_order = None
            self._current_task = None
        self._page_no = 1
        self.refresh_current_level()

    def _change_page(self, delta: int) -> None:
        target = self._page_no + delta
        max_page = max(1, (self._total + self._page_size - 1) // self._page_size)
        if 1 <= target <= max_page:
            self._page_no = target
            self.refresh_current_level()

    def refresh_current_level(self) -> None:
        if self._busy:
            return
        client = self._ensure_client()
        if client is None:
            return
        if self._navigation_level == "orders":
            order_text = self.order_id_edit.text().strip()
            order_id = int(order_text) if order_text else None
            self._run_network(
                "加载订单",
                lambda: client.list_orders(
                    page_no=self._page_no,
                    page_size=self._page_size,
                    order_id=order_id,
                ),
                lambda result: self.show_order_page(cast(Page[OrderRecord], result)),
            )
        elif self._navigation_level == "tasks" and self._current_order:
            order_id = self._current_order.id
            self._run_network(
                "加载任务",
                lambda: client.list_tasks(
                    order_id=order_id,
                    page_no=self._page_no,
                    page_size=self._page_size,
                ),
                lambda result: self.show_task_page(cast(Page[TaskRecord], result)),
            )
        elif self._navigation_level == "commands" and self._current_task:
            task_id = self._current_task.id
            self._run_network(
                "加载命令",
                lambda: client.list_commands(task_id=task_id),
                lambda result: self.show_commands(cast(tuple[CommandRecord, ...], result)),
            )

    def _ensure_client(self) -> SchedulerApi | None:
        if self._client is not None:
            return self._client
        try:
            self._client = self._client_factory(self.config.connection())
        except ValueError as exc:
            self._show_error(f"连接配置无效：{exc}")
            return None
        return self._client

    def _run_network(
        self,
        label: str,
        operation: Callable[[], object],
        on_success: Callable[[object], None],
    ) -> None:
        self._set_busy(True, f"{label}…")
        worker: Worker[object] = Worker(operation)
        self._workers.add(worker)
        worker.signals.succeeded.connect(
            lambda result: self._network_succeeded(on_success, result)
        )
        worker.signals.failed.connect(self._network_failed)
        worker.signals.finished.connect(lambda: self._worker_finished(worker))
        self._pool.start(worker)

    def _network_succeeded(
        self,
        on_success: Callable[[object], None],
        result: object,
    ) -> None:
        on_success(result)
        self._set_busy(False, "就绪")

    def _network_failed(self, message: str) -> None:
        self._set_busy(False, "加载失败")
        self._show_error(message)

    def _worker_finished(self, worker: Worker[object]) -> None:
        self._workers.discard(worker)

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.search_button.setEnabled(not busy)
        self.table.setEnabled(not busy)
        self.status_label.setText(message)

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "操作失败", message)

    @staticmethod
    def _item(value: object) -> QTableWidgetItem:
        item = QTableWidgetItem("" if value is None else str(value))
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _populate(
        self,
        headers: list[str],
        rows: list[list[object]],
        records: list[Record],
    ) -> None:
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, self._item(value))
        self._records = records
        if rows:
            self.table.selectRow(0)

    def show_order_page(self, page: Page[OrderRecord]) -> None:
        self._navigation_level = "orders"
        self._page_no = page.page_no
        self._page_size = page.page_size
        self._total = page.total
        self._populate(
            ["订单 ID", "地图", "状态", "创建时间"],
            [[item.id, item.map_id, item.status, item.created_at] for item in page.items],
            list(page.items),
        )
        self._update_navigation_header()
        log_event(
            LOGGER,
            logging.INFO,
            "orders_loaded",
            page_no=page.page_no,
            page_size=page.page_size,
            total=page.total,
            order_ids=[item.id for item in page.items],
        )

    def show_task_page(self, page: Page[TaskRecord]) -> None:
        self._navigation_level = "tasks"
        self._page_no = page.page_no
        self._page_size = page.page_size
        self._total = page.total
        self._populate(
            ["任务 ID", "VIN", "地图", "状态"],
            [[item.id, item.vin, item.map_id, item.status] for item in page.items],
            list(page.items),
        )
        self._update_navigation_header()
        log_event(
            LOGGER,
            logging.INFO,
            "tasks_loaded",
            order_id=self._current_order.id if self._current_order else None,
            page_no=page.page_no,
            page_size=page.page_size,
            total=page.total,
            task_ids=[item.id for item in page.items],
        )

    def show_commands(self, commands: tuple[CommandRecord, ...]) -> None:
        self._navigation_level = "commands"
        self._total = len(commands)
        self._populate(
            ["命令 ID", "VIN", "能力", "状态"],
            [[item.id, item.vin, item.capability, item.status] for item in commands],
            list(commands),
        )
        self._update_navigation_header()
        log_event(
            LOGGER,
            logging.INFO,
            "commands_loaded",
            order_id=self._current_order.id if self._current_order else None,
            task_id=self._current_task.id if self._current_task else None,
            command_ids=[item.id for item in commands],
        )

    def _update_navigation_header(self) -> None:
        parts = ["订单"]
        if self._current_order:
            parts.append(str(self._current_order.id))
        if self._current_task:
            parts.extend(["任务", str(self._current_task.id)])
        if self._current_command:
            parts.extend(["命令", str(self._current_command.id)])
        self.breadcrumb_label.setText(" › ".join(parts))
        self.back_button.setVisible(self._navigation_level != "orders")
        self.order_id_edit.setVisible(self._navigation_level == "orders")
        self.search_button.setVisible(self._navigation_level == "orders")
        paged = self._navigation_level in {"orders", "tasks"}
        self.previous_button.setVisible(paged)
        self.next_button.setVisible(paged)
        self.page_label.setVisible(paged)
        max_page = max(1, (self._total + self._page_size - 1) // self._page_size)
        self.page_label.setText(f"第 {self._page_no} / {max_page} 页，共 {self._total} 条")
        self.previous_button.setEnabled(self._page_no > 1 and not self._busy)
        self.next_button.setEnabled(self._page_no < max_page and not self._busy)

    def activate_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._records):
            return
        record = self._records[row]
        if isinstance(record, OrderRecord):
            self._current_order = record
            self._current_task = None
            self._current_command = None
            self._navigation_level = "tasks"
            self._page_no = 1
            self._clear_navigation_rows()
            self.refresh_current_level()
        elif isinstance(record, TaskRecord):
            self._current_task = record
            self._current_command = None
            self._navigation_level = "commands"
            self._page_no = 1
            self._clear_navigation_rows()
            self.refresh_current_level()
        else:
            self._open_command(record)
        self._update_navigation_header()

    def _clear_navigation_rows(self) -> None:
        self._records.clear()
        self.table.setRowCount(0)

    def go_back(self) -> None:
        if self._navigation_level == "commands":
            self._navigation_level = "tasks"
            self._current_task = None
            self._current_command = None
        elif self._navigation_level == "tasks":
            self._navigation_level = "orders"
            self._current_order = None
            self._current_task = None
            self._current_command = None
        self._page_no = 1
        self._clear_navigation_rows()
        self.refresh_current_level()

    def _record_map_id(self, command: CommandRecord) -> int | None:
        return command.map_id or (
            self._current_task.map_id if self._current_task else None
        ) or (self._current_order.map_id if self._current_order else None)

    def _open_command(self, command: CommandRecord) -> None:
        map_id = self._record_map_id(command)
        if map_id is None:
            self._show_error("命令、任务和订单均未提供 mapId，无法选择车道布局")
            return
        if not self._switch_lane_context(str(map_id)):
            return
        vin = command.vin or (self._current_task.vin if self._current_task else "")
        if not vin:
            self._show_error("命令和任务均未提供 VIN，无法查询实际路径")
            return
        client = self._ensure_client()
        if client is None:
            return
        self._current_command = command
        self._update_navigation_header()
        self._run_network(
            "加载两类路径",
            lambda: (
                client.get_dispatched_path(command_id=command.id),
                client.get_actual_path(command_id=command.id, vin=vin),
            ),
            lambda result: self._show_paths(
                vin,
                cast(tuple[tuple[PosePoint, ...], tuple[PosePoint, ...]], result),
            ),
        )

    def _server_id(self) -> str:
        root = self.config.connection().validated_root()
        return server_id_for(root)

    def _switch_lane_context(self, map_id: str) -> bool:
        try:
            key = (self._server_id(), map_id)
        except ValueError as exc:
            self._show_error(str(exc))
            return False
        if key == self._lane_key:
            return True
        if not self._confirm_dirty():
            return False
        try:
            layout = self._lane_repository.load(*key)
        except StorageError as exc:
            self._show_error(str(exc))
            return False
        self._lane_key = key
        self.canvas.load_layout(layout)
        return True

    def _dimensions_for(self, vin: str) -> VehicleDimensions:
        default = self.config.default_vehicle
        if default is None:
            raise ValueError("尚未配置默认车辆尺寸")
        return self._profiles.get(vin, default)

    def _measurement_scope(self, path_name: str) -> MeasurementScope | None:
        if (
            self._current_order is None
            or self._current_task is None
            or self._current_command is None
        ):
            return None
        try:
            server_id = self._server_id()
        except ValueError:
            return None
        return MeasurementScope(
            server_id,
            self.config.tenant.strip() or "suntae",
            self._current_order.id,
            self._current_task.id,
            self._current_command.id,
            path_name,
        )

    def _path_for(self, path_name: str) -> tuple[PosePoint, ...]:
        if path_name == "dispatched":
            return self._dispatched_path
        if path_name == "actual":
            return self._actual_path
        raise ValueError(f"unknown path: {path_name}")

    def _load_radius_measurements(self) -> None:
        self._finish_manual_radius()
        for path_name in ("dispatched", "actual"):
            self._radius_generations[path_name] += 1
            scope = self._measurement_scope(path_name)
            points = self._path_for(path_name)
            if scope is None:
                self._radius_states[path_name] = RadiusMeasurementState(
                    path_fingerprint(points)
                )
                continue
            try:
                self._radius_states[path_name] = self._radius_repository.load(
                    scope,
                    path_fingerprint(points),
                )
            except StorageError as exc:
                self._show_error(str(exc))
                self._radius_states[path_name] = RadiusMeasurementState(
                    path_fingerprint(points)
                )

    def _persist_radius_state(self, path_name: str) -> bool:
        scope = self._measurement_scope(path_name)
        state = self._radius_states[path_name]
        if scope is None or state is None:
            return False
        try:
            self._radius_repository.save(scope, state)
        except StorageError as exc:
            self._show_error(str(exc))
            return False
        return True

    def _commit_radius_state(
        self,
        path_name: str,
        previous: RadiusMeasurementState,
    ) -> bool:
        if self._persist_radius_state(path_name):
            return True
        self._radius_states[path_name] = previous
        self._refresh_radius_measurements()
        return False

    def _dimensions_source(self, vin: str) -> str:
        return f"VIN {vin} 专属配置" if vin in self._profiles else "全局默认配置"

    def _refresh_radius_measurements(self) -> None:
        if self._current_command is None:
            self._calculated_radii = {"dispatched": (), "actual": ()}
            self.control_panel.set_turn_radius_measurements(
                (), (), dimensions_source="—"
            )
            self.canvas.clear_turn_radius_observation()
            return
        vin = self._current_command.vin or (
            self._current_task.vin if self._current_task else ""
        )
        try:
            dimensions = self._dimensions_for(vin)
        except ValueError:
            return
        for path_name in ("dispatched", "actual"):
            state = self._radius_states[path_name]
            self._calculated_radii[path_name] = (
                ()
                if state is None
                else recalculate_measurements(state, self._path_for(path_name), dimensions)
            )
        self.canvas.set_vehicle_dimensions(dimensions)
        self.control_panel.set_turn_radius_measurements(
            self._calculated_radii["dispatched"],
            self._calculated_radii["actual"],
            dimensions_source=self._dimensions_source(vin),
        )

    def _calculate_automatic_radii(self, path_name: str) -> None:
        state = self._radius_states.get(path_name)
        if state is None:
            return
        points = self._path_for(path_name)
        fingerprint = path_fingerprint(points)
        threshold = self.config.analysis.turn_threshold
        self._radius_generations[path_name] += 1
        generation = self._radius_generations[path_name]
        self.status_label.setText(
            f"正在计算{('下发' if path_name == 'dispatched' else '实际')}路径自动半径…"
        )

        def operation() -> tuple[tuple[int, int], ...]:
            return tuple(
                (turn.start_index, turn.end_index)
                for turn in detect_turns(points, threshold=threshold)
            )

        worker: Worker[object] = Worker(operation)
        self._workers.add(worker)

        def show(result: object) -> None:
            if generation != self._radius_generations[path_name]:
                return
            if path_fingerprint(self._path_for(path_name)) != fingerprint:
                return
            current_state = self._radius_states.get(path_name)
            if current_state is None:
                return
            pairs = cast(tuple[tuple[int, int], ...], result)
            previous = copy.deepcopy(current_state)
            current_state.replace_automatic(pairs)
            if not self._commit_radius_state(path_name, previous):
                return
            self._refresh_radius_measurements()
            self.status_label.setText(
                f"{('下发' if path_name == 'dispatched' else '实际')}路径自动半径已更新："
                f"{len(current_state.automatic_records)} 条"
            )

        worker.signals.succeeded.connect(show)

        def show_failure(message: str) -> None:
            if generation == self._radius_generations[path_name]:
                self._show_error(message)

        worker.signals.failed.connect(show_failure)
        worker.signals.finished.connect(lambda: self._workers.discard(worker))
        self._radius_pool.start(worker)

    def _toggle_manual_radius(self, path_name: str) -> None:
        if self._manual_radius_path == path_name:
            self._finish_manual_radius()
            return
        self._finish_manual_radius()
        points = self._path_for(path_name)
        suggestions = {
            index
            for turn in detect_turns(
                points,
                threshold=self.config.analysis.turn_threshold,
            )
            for index in (turn.start_index, turn.end_index)
        }
        self._manual_radius_path = path_name
        self._manual_radius_start = None
        self.control_panel.layer_checks[path_name][0].setChecked(True)
        self.control_panel.set_manual_radius_mode(path_name, True)
        self.canvas.set_manual_radius_mode(path_name, suggestions)
        self.status_label.setText("请选择入弯路径点")

    def _finish_manual_radius(self) -> None:
        self._manual_radius_path = None
        self._manual_radius_start = None
        self.canvas.set_manual_radius_mode(None)
        for path_name in ("dispatched", "actual"):
            self.control_panel.set_manual_radius_mode(path_name, False)

    def _radius_endpoint_selected(self, path_name: str, index: int) -> None:
        if self._manual_radius_path != path_name:
            return
        if self._manual_radius_start is None:
            self._manual_radius_start = index
            self.canvas.set_manual_radius_start(index)
            self.status_label.setText(f"已选择入弯样本 {index}，请选择其后的出弯样本")
            return
        start_index = self._manual_radius_start
        self._manual_radius_start = None
        self.canvas.set_manual_radius_start(None)
        state = self._radius_states[path_name]
        if state is None:
            return
        if index <= start_index:
            self._show_error("出弯样本必须位于入弯样本之后")
            return
        vin = self._current_command.vin if self._current_command else ""
        if not vin and self._current_task is not None:
            vin = self._current_task.vin
        radius = calculate_turn_radius(
            self._path_for(path_name),
            self._dimensions_for(vin),
            start_index=start_index,
            end_index=index,
        )
        if not radius.valid:
            self._show_error(radius.error or "无法计算转弯半径")
            return
        previous = copy.deepcopy(state)
        record, created = state.add_manual(start_index, index)
        if created and not self._commit_radius_state(path_name, previous):
            return
        self._refresh_radius_measurements()
        self.control_panel.select_radius_measurement(path_name, record.id)
        selected = next(
            (
                item
                for item in self._calculated_radii[path_name]
                if item.record.id == record.id
            ),
            None,
        )
        if selected is not None:
            self.canvas.show_turn_radius_observation(path_name, selected.radius)
        self.status_label.setText(
            ("已创建" if created else "已定位已有") + f"手动测量：{record.name}"
        )

    def _delete_radius_measurement(self, path_name: str, measurement_id: str) -> None:
        state = self._radius_states.get(path_name)
        if state is None:
            return
        previous = copy.deepcopy(state)
        if not state.delete(measurement_id):
            return
        self._radius_generations[path_name] += 1
        if self._commit_radius_state(path_name, previous):
            self._refresh_radius_measurements()

    def _clear_automatic_radii_for_threshold_change(self) -> None:
        changed = False
        for path_name in ("dispatched", "actual"):
            state = self._radius_states.get(path_name)
            if state is None:
                continue
            self._radius_generations[path_name] += 1
            had_automatic = bool(state.automatic_records)
            previous = copy.deepcopy(state)
            state.clear_automatic()
            if had_automatic and self._commit_radius_state(path_name, previous):
                changed = True
        self._refresh_radius_measurements()
        self.status_label.setText(
            "转弯识别阈值已变化，请重新自动计算半径"
            if changed
            else "转弯识别阈值已变化；自动建议将使用新阈值"
        )

    def _rename_radius_measurement(
        self,
        path_name: str,
        measurement_id: str,
        name: str,
    ) -> None:
        state = self._radius_states.get(path_name)
        if state is None:
            return
        previous = copy.deepcopy(state)
        try:
            state.rename(measurement_id, name)
        except (KeyError, ValueError) as exc:
            self._show_error(str(exc))
            self._refresh_radius_measurements()
            return
        if self._commit_radius_state(path_name, previous):
            self._refresh_radius_measurements()

    def _show_paths(
        self,
        vin: str,
        paths: tuple[tuple[PosePoint, ...], tuple[PosePoint, ...]],
    ) -> None:
        self._dispatched_path, self._actual_path = paths
        try:
            dimensions = self._dimensions_for(vin)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.canvas.set_paths(self._dispatched_path, self._actual_path, dimensions)
        self._load_radius_measurements()
        self._refresh_radius_measurements()
        self.status_label.setText(
            f"下发 {len(self._dispatched_path)} 点；实际 {len(self._actual_path)} 点；VIN {vin}"
        )
        log_event(
            LOGGER,
            logging.INFO,
            "command_paths_loaded",
            order_id=self._current_order.id if self._current_order else None,
            task_id=self._current_task.id if self._current_task else None,
            command_id=self._current_command.id if self._current_command else None,
            map_id=self._lane_key[1] if self._lane_key else None,
            vin=vin,
            dispatched_points=len(self._dispatched_path),
            actual_points=len(self._actual_path),
        )
        self.analyze_now()

    def analyze_now(self) -> None:
        if self._current_command is None or self._lane_key is None:
            return
        vin = self._current_command.vin or (self._current_task.vin if self._current_task else "")
        try:
            dimensions = self._dimensions_for(vin)
        except ValueError:
            return
        layout = self.canvas.current_layout()
        settings = self.config.analysis
        dispatched = self._dispatched_path
        actual = self._actual_path
        self._analysis_generation += 1
        generation = self._analysis_generation
        started = time.perf_counter()

        def operation() -> tuple[AnalysisResult, AnalysisResult]:
            area = build_traversable_area(
                layout.lanes,
                tolerance=settings.bezier_tolerance,
                miter_limit=settings.miter_limit,
            )
            return (
                analyze_path(dispatched, dimensions, area, settings),
                analyze_path(actual, dimensions, area, settings),
            )

        worker: Worker[object] = Worker(operation)
        self._workers.add(worker)

        def show(result: object) -> None:
            if generation != self._analysis_generation:
                return
            dispatched_result, actual_result = cast(
                tuple[AnalysisResult, AnalysisResult], result
            )
            self.canvas.set_analysis_results(dispatched_result, actual_result)
            self.control_panel.set_results(dispatched_result, actual_result)
            dimensions_source = self._dimensions_source(vin)
            log_event(
                LOGGER,
                logging.INFO,
                "path_analysis_completed",
                command_id=self._current_command.id if self._current_command else None,
                map_id=self._lane_key[1] if self._lane_key else None,
                vin=vin,
                duration_ms=(time.perf_counter() - started) * 1000,
                dispatched_points=len(dispatched),
                actual_points=len(actual),
                dimensions_source=dimensions_source,
            )
            log_event(
                LOGGER,
                logging.DEBUG,
                "path_analysis_full_result",
                dispatched_path=dispatched,
                actual_path=actual,
                dispatched_clearance=dispatched_result,
                actual_clearance=actual_result,
            )

        worker.signals.succeeded.connect(show)
        worker.signals.failed.connect(self._show_error)
        worker.signals.finished.connect(lambda: self._workers.discard(worker))
        self._analysis_pool.start(worker)

    def save_lanes(self) -> bool:
        if self._lane_key is None:
            self.status_label.setText("尚未进入带 mapId 的命令")
            return False
        try:
            path = self._lane_repository.save(self.canvas.current_layout())
        except RouteAnalysisError as exc:
            self._show_error(str(exc))
            return False
        self.canvas.mark_saved()
        self.status_label.setText(f"车道已保存：{path.name}")
        log_event(
            LOGGER,
            logging.INFO,
            "lanes_saved",
            server_id=self._lane_key[0],
            map_id=self._lane_key[1],
            lane_count=len(self.canvas.current_layout().lanes),
            file=path,
        )
        log_event(
            LOGGER,
            logging.DEBUG,
            "lanes_saved_full_layout",
            configuration=self.config.to_dict(),
            layout=self.canvas.current_layout().to_dict(),
        )
        return True

    def _confirm_dirty(self) -> bool:
        if self.canvas.undo_stack.isClean():
            return True
        result = QMessageBox.warning(
            self,
            "车道尚未保存",
            "当前车道有未保存修改。是否先保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if result == QMessageBox.StandardButton.Cancel:
            return False
        if result == QMessageBox.StandardButton.Save:
            return self.save_lanes()
        return True

    def import_lanes(self) -> None:
        if self._lane_key is None or not self._confirm_dirty():
            return
        filename, _ = QFileDialog.getOpenFileName(self, "导入车道并替换", "", "JSON (*.json)")
        if not filename:
            return
        source = Path(filename)
        try:
            import_body = source.read_text(encoding="utf-8")
        except OSError:
            import_body = None
        log_event(
            LOGGER,
            logging.DEBUG,
            "lane_import_full_file",
            file=source,
            body=import_body,
        )
        try:
            preview = self._lane_repository.preview_import(
                source,
                expected_server_id=self._lane_key[0],
                expected_map_id=self._lane_key[1],
            )
        except RouteAnalysisError as exc:
            self._show_error(str(exc))
            return
        mismatch = "\n服务器或 mapId 不一致，将改写为当前上下文。" if preview.mismatches else ""
        prompt = (
            f"导入文件包含 {len(preview.layout.lanes)} 条车道。"
            f"当前车道将被整体替换并备份。{mismatch}"
        )
        answer = QMessageBox.question(
            self,
            "确认替换车道",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            layout = self._lane_repository.replace_from_import(
                source,
                self._lane_key[0],
                self._lane_key[1],
                allow_mismatch=True,
            )
        except RouteAnalysisError as exc:
            self._show_error(str(exc))
            return
        self.canvas.load_layout(layout)
        self.status_label.setText("车道已由导入文件替换")
        log_event(
            LOGGER,
            logging.INFO,
            "lanes_imported_replacing_layout",
            server_id=self._lane_key[0],
            map_id=self._lane_key[1],
            lane_count=len(layout.lanes),
            source=source,
            mismatches=preview.mismatches,
        )

    def export_lanes(self) -> None:
        if self._lane_key is None:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "导出车道",
            f"lanes-{self._lane_key[1]}.json",
            "JSON (*.json)",
        )
        if not filename:
            return
        destination = Path(filename)
        if destination.suffix.lower() != ".json":
            destination = destination.with_suffix(".json")
        try:
            self._lane_repository.export(self.canvas.current_layout(), destination)
        except RouteAnalysisError as exc:
            self._show_error(str(exc))
            return
        self.status_label.setText(f"车道已导出：{destination.name}")
        log_event(
            LOGGER,
            logging.INFO,
            "lanes_exported",
            server_id=self._lane_key[0],
            map_id=self._lane_key[1],
            lane_count=len(self.canvas.current_layout().lanes),
            destination=destination,
        )

    def open_settings(self) -> None:
        old_turn_threshold = self.config.analysis.turn_threshold
        old_tenant = self.config.tenant.strip()
        dialog = SettingsDialog(self.config, self._profiles)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted or dialog.result_config is None:
            return
        new_config = dialog.result_config
        old_root = self.config.api_root.strip().rstrip("/")
        new_root = new_config.api_root.strip().rstrip("/")
        new_tenant = new_config.tenant.strip()
        context_changed = old_root != new_root or old_tenant != new_tenant
        if context_changed and not self._confirm_dirty():
            return
        try:
            self._config_repository.save(new_config)
            self._profile_repository.save(dialog.result_profiles)
        except RouteAnalysisError as exc:
            self._show_error(str(exc))
            return
        self.config = new_config
        self._profiles = dict(dialog.result_profiles)
        try:
            self._client = self._client_factory(self.config.connection())
        except ValueError:
            self._client = None
        if context_changed:
            self._lane_key = None
            self.canvas.load_layout(LaneLayout("0000000000000000", "0", []))
            self._navigation_level = "orders"
            self._current_order = None
            self._current_task = None
            self._current_command = None
            self.canvas.clear_paths()
            self._radius_states = {"dispatched": None, "actual": None}
            self._radius_generations["dispatched"] += 1
            self._radius_generations["actual"] += 1
            self._refresh_radius_measurements()
        self._apply_config()
        if not context_changed and not math.isclose(
            old_turn_threshold,
            self.config.analysis.turn_threshold,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            self._clear_automatic_radii_for_threshold_change()
        elif not context_changed:
            self._refresh_radius_measurements()
        if self._logging_manager is not None:
            self._logging_manager.set_level(self.config.log_level)
        log_event(
            LOGGER,
            logging.DEBUG,
            "configuration_saved",
            configuration=self.config.to_dict(),
            vehicle_profiles=self._profiles,
        )
        self._analysis_timer.start()

    def _direction_changed(self, radians: float) -> None:
        self.config.map_direction = radians
        self.canvas.set_map_direction(radians)
        try:
            self._config_repository.save(self.config)
        except RouteAnalysisError as exc:
            self._show_error(str(exc))

    def _dirty_changed(self, dirty: bool) -> None:
        suffix = " *" if dirty else ""
        self.setWindowTitle(f"Suntae 路径通行分析{suffix}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._confirm_dirty():
            event.accept()
        else:
            event.ignore()
