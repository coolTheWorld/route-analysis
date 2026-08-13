"""Connection, vehicle, analysis, and per-VIN settings editor."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from route_analysis.models import AnalysisSettings, VehicleDimensions
from route_analysis.storage import AppConfig


def _spin(
    minimum: float,
    maximum: float,
    value: float,
    *,
    decimals: int = 3,
    suffix: str = "",
) -> QDoubleSpinBox:
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setDecimals(decimals)
    widget.setSingleStep(0.05)
    widget.setValue(value)
    widget.setSuffix(suffix)
    widget.setKeyboardTracking(False)
    return widget


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: AppConfig,
        profiles: dict[str, VehicleDimensions],
        *,
        force_initial: bool = False,
    ) -> None:
        super().__init__()
        self.setWindowTitle("首次设置" if force_initial else "设置")
        self.setMinimumSize(660, 570)
        self.setModal(True)
        self.result_config: AppConfig | None = None
        self.result_profiles: dict[str, VehicleDimensions] = {}
        self._source_config = copy.deepcopy(config)

        root = QVBoxLayout(self)
        if force_initial:
            notice = QLabel("首次运行必须填写车宽、中心前距、中心后距和默认车道宽。")
            notice.setWordWrap(True)
            notice.setStyleSheet("color:#7a4b00;background:#fff4ce;padding:8px;border-radius:4px")
            root.addWidget(notice)

        tabs = QTabWidget()
        tabs.setAccessibleName("应用设置分类")
        tabs.addTab(self._build_connection_tab(config), "连接")
        tabs.addTab(self._build_vehicle_tab(config, profiles), "车辆与地图")
        tabs.addTab(self._build_analysis_tab(config), "高级分析")
        root.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        if not force_initial:
            buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_connection_tab(self, config: AppConfig) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.api_root_edit = QLineEdit(config.api_root)
        self.api_root_edit.setPlaceholderText("http://host/admin-api")
        self.api_root_edit.setAccessibleName("调度后端 API 根地址")
        self.tenant_edit = QLineEdit(config.tenant or "suntae")
        self.username_edit = QLineEdit(config.username)
        self.password_edit = QLineEdit(config.password)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        self.password_edit.setAccessibleDescription("密码按已确认决策明文保存在本地配置")
        self.timeout_spin = _spin(1, 300, config.timeout_seconds, decimals=0, suffix=" s")
        self.verify_tls_check = QCheckBox("校验证书")
        self.verify_tls_check.setChecked(config.verify_tls)
        form.addRow("API 根地址", self.api_root_edit)
        form.addRow("租户", self.tenant_edit)
        form.addRow("用户名", self.username_edit)
        form.addRow("密码（本地明文）", self.password_edit)
        form.addRow("请求超时", self.timeout_spin)
        form.addRow("HTTPS", self.verify_tls_check)
        return tab

    def _build_vehicle_tab(
        self,
        config: AppConfig,
        profiles: dict[str, VehicleDimensions],
    ) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        vehicle = config.default_vehicle
        self.width_spin = _spin(0, 100, vehicle.width if vehicle else 0, suffix=" m")
        self.front_spin = _spin(0, 100, vehicle.center_front if vehicle else 0, suffix=" m")
        self.rear_spin = _spin(0, 100, vehicle.center_rear if vehicle else 0, suffix=" m")
        self.length_label = QLabel()
        self.width_spin.valueChanged.connect(self._refresh_length)
        self.front_spin.valueChanged.connect(self._refresh_length)
        self.rear_spin.valueChanged.connect(self._refresh_length)
        self.lane_width_spin = _spin(
            0,
            100,
            config.default_lane_width or 0,
            suffix=" m",
        )
        self.map_direction_spin = _spin(
            -1000,
            1000,
            config.map_direction,
            decimals=6,
            suffix=" rad",
        )
        self.snap_check = QCheckBox("绘制时吸附当前可见路径点")
        self.snap_check.setChecked(config.snap_to_path)
        form.addRow("车宽", self.width_spin)
        form.addRow("中心前距", self.front_spin)
        form.addRow("中心后距", self.rear_spin)
        form.addRow("车长（前距 + 后距）", self.length_label)
        form.addRow("新车道默认总宽", self.lane_width_spin)
        form.addRow("地图显示方向", self.map_direction_spin)
        form.addRow("吸附", self.snap_check)
        layout.addLayout(form)
        layout.addWidget(QLabel("按 VIN 覆盖车辆尺寸"))

        self.profile_table = QTableWidget(0, 4)
        self.profile_table.setHorizontalHeaderLabels(
            ["VIN", "车宽 (m)", "中心前距 (m)", "中心后距 (m)"]
        )
        self.profile_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.profile_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.profile_table.setAlternatingRowColors(True)
        layout.addWidget(self.profile_table, 1)
        for vin, dimensions in sorted(profiles.items()):
            self.add_profile(vin, dimensions)
        actions = QHBoxLayout()
        add_button = QPushButton("添加 VIN")
        remove_button = QPushButton("删除选中")
        add_button.clicked.connect(lambda: self.add_profile("", VehicleDimensions(1, 1, 1)))
        remove_button.clicked.connect(self._remove_selected_profile)
        actions.addWidget(add_button)
        actions.addWidget(remove_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._refresh_length()
        return tab

    def _refresh_length(self) -> None:
        self.length_label.setText(f"{self.front_spin.value() + self.rear_spin.value():.3f} m")

    def _build_analysis_tab(self, config: AppConfig) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        settings = config.analysis
        self.position_step_spin = _spin(0.001, 10, settings.position_step, suffix=" m")
        self.yaw_step_spin = _spin(0.001, 10, settings.yaw_step, decimals=4, suffix=" rad")
        self.clearance_spin = _spin(0, 100, settings.clearance_threshold, suffix=" m")
        self.bezier_tolerance_spin = _spin(0.001, 1, settings.bezier_tolerance, suffix=" m")
        self.miter_limit_spin = _spin(0.1, 100, settings.miter_limit, decimals=2)
        explanation = QLabel(
            "连续扫掠按位置和 yaw 步长插值。步长越小越精细，但分析时间和样本数会增加。"
        )
        explanation.setWordWrap(True)
        form.addRow(explanation)
        form.addRow("最大位置步长", self.position_step_spin)
        form.addRow("最大 yaw 步长", self.yaw_step_spin)
        form.addRow("净距警告阈值", self.clearance_spin)
        form.addRow("贝塞尔离散容差", self.bezier_tolerance_spin)
        form.addRow("尖角 miter 上限", self.miter_limit_spin)
        return tab

    def add_profile(self, vin: str, dimensions: VehicleDimensions) -> None:
        row = self.profile_table.rowCount()
        self.profile_table.insertRow(row)
        for column, value in enumerate(
            (vin, dimensions.width, dimensions.center_front, dimensions.center_rear)
        ):
            item = QTableWidgetItem(str(value))
            if column == 0:
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.profile_table.setItem(row, column, item)
        self.profile_table.setCurrentCell(row, 0)

    def _remove_selected_profile(self) -> None:
        rows = sorted({index.row() for index in self.profile_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.profile_table.removeRow(row)

    def _collect_profiles(self) -> dict[str, VehicleDimensions]:
        profiles: dict[str, VehicleDimensions] = {}
        for row in range(self.profile_table.rowCount()):
            values: list[str] = []
            for column in range(4):
                item = self.profile_table.item(row, column)
                values.append(item.text().strip() if item is not None else "")
            vin = values[0]
            if not vin:
                raise ValueError(f"第 {row + 1} 行 VIN 不能为空")
            if vin in profiles:
                raise ValueError(f"VIN 重复：{vin}")
            profiles[vin] = VehicleDimensions(
                float(values[1]),
                float(values[2]),
                float(values[3]),
            )
        return profiles

    def accept(self) -> None:
        try:
            vehicle = VehicleDimensions(
                self.width_spin.value(),
                self.front_spin.value(),
                self.rear_spin.value(),
            )
            lane_width = self.lane_width_spin.value()
            if lane_width <= 0:
                raise ValueError("默认车道宽度必须大于零")
            analysis = AnalysisSettings(
                position_step=self.position_step_spin.value(),
                yaw_step=self.yaw_step_spin.value(),
                clearance_threshold=self.clearance_spin.value(),
                bezier_tolerance=self.bezier_tolerance_spin.value(),
                miter_limit=self.miter_limit_spin.value(),
            )
            profiles = self._collect_profiles()
            config = AppConfig(
                api_root=self.api_root_edit.text().strip(),
                tenant=self.tenant_edit.text().strip() or "suntae",
                username=self.username_edit.text().strip(),
                password=self.password_edit.text(),
                timeout_seconds=self.timeout_spin.value(),
                verify_tls=self.verify_tls_check.isChecked(),
                default_vehicle=vehicle,
                default_lane_width=lane_width,
                map_direction=self.map_direction_spin.value(),
                analysis=analysis,
                snap_to_path=self.snap_check.isChecked(),
            )
            if config.api_root:
                config.connection().validated_root()
        except ValueError as exc:
            QMessageBox.warning(self, "设置无效", str(exc))
            return
        self.result_config = config
        self.result_profiles = profiles
        super().accept()
