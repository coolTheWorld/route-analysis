"""Right-side controls for display layers, lanes, and analysis results."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from route_analysis.canvas import RouteCanvas
from route_analysis.geometry import lane_centerline_length
from route_analysis.lane_generation import arc_radius, maximum_arc_radius
from route_analysis.models import (
    AnalysisResult,
    ClearanceStatus,
    JoinStyle,
    Lane,
    Point2D,
    SegmentKind,
)
from route_analysis.turn_measurements import CalculatedMeasurement, MeasurementSource
from route_analysis.turn_radius import CornerRadiusKind, TurnKind, TurnSide

CORNER_LABELS = {
    CornerRadiusKind.FRONT_OUTER: "前外角半径",
    CornerRadiusKind.REAR_OUTER: "后外角半径",
    CornerRadiusKind.FRONT_INNER: "前内角半径",
    CornerRadiusKind.REAR_INNER: "后内角半径",
}


_SPIN_WIDTH_FLOOR = 115


def _displayed_spin_width(spin: QDoubleSpinBox) -> int:
    """Return the width the resting value needs under the font actually in use."""

    metrics = spin.fontMetrics()

    def rendered(value: float) -> int:
        return metrics.horizontalAdvance(
            f"{spin.prefix()}{spin.textFromValue(value)}{spin.suffix()}"
        )

    widest = max(rendered(spin.minimum()), rendered(spin.maximum()))
    chrome = spin.sizeHint().width() - widest
    resting = 0.0 if spin.minimum() <= 0 <= spin.maximum() else spin.minimum()
    return rendered(resting) + chrome


class _CoordinateSpinBox(QDoubleSpinBox):
    """Coordinate input that stays compact without clipping the value it shows.

    The cap keeps the historical width unless the active font needs more room, so
    fonts wider than the Windows baseline neither truncate the value nor the
    suffix. It is refreshed on font and style changes because a stylesheet may
    arrive after construction.
    """

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self.fit_width()

    def fit_width(self) -> None:
        self.setMaximumWidth(max(_SPIN_WIDTH_FLOOR, _displayed_spin_width(self)))


def _coordinate_spin(
    *,
    minimum: float = -1_000_000,
    maximum: float = 1_000_000,
    decimals: int = 4,
    suffix: str = "",
) -> _CoordinateSpinBox:
    spin = _CoordinateSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSuffix(suffix)
    spin.setSingleStep(0.1)
    spin.setKeyboardTracking(False)
    spin.setMinimumWidth(72)
    spin.fit_width()
    return spin


class ControlPanel(QScrollArea):
    save_requested = Signal()
    import_requested = Signal()
    export_requested = Signal()
    settings_requested = Signal()
    analyze_requested = Signal()
    generate_lane_requested = Signal()
    direction_changed = Signal(float)
    auto_radius_requested = Signal(str)
    manual_radius_requested = Signal(str)
    radius_delete_requested = Signal(str, str)
    radius_rename_requested = Signal(str, str, str)

    def __init__(self, canvas: RouteCanvas) -> None:
        super().__init__()
        self.canvas = canvas
        self.default_lane_width = 2.0
        self._updating = False
        self.setObjectName("controlPanel")
        self.setAccessibleName("路径显示、车道编辑与分析结果")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(330)
        self.setMaximumWidth(430)
        body = QFrame()
        body.setObjectName("controlPanel")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._build_display_group())
        layout.addWidget(self._build_lane_group())
        layout.addWidget(self._build_result_group())
        settings_button = QPushButton("连接、车辆与高级设置…")
        settings_button.setAccessibleName("打开应用设置")
        settings_button.clicked.connect(self.settings_requested)
        layout.addWidget(settings_button)
        layout.addStretch(1)
        self.setWidget(body)

        canvas.layout_changed.connect(self.refresh_lane_list)
        canvas.selection_changed.connect(self._selection_changed)
        canvas.drawing_state_changed.connect(self._drawing_changed)
        self.refresh_lane_list()

    @staticmethod
    def _group(title: str) -> tuple[QGroupBox, QVBoxLayout]:
        group = QGroupBox(title)
        group.setCheckable(True)
        group.setChecked(True)
        outer = QVBoxLayout(group)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(content)

        def toggle(checked: bool) -> None:
            content.setVisible(checked)
            group.setMaximumHeight(16_777_215 if checked else 34)

        group.toggled.connect(toggle)
        return group, layout

    def _build_display_group(self) -> QGroupBox:
        group, layout = self._group("显示")
        grid = QGridLayout()
        grid.addWidget(QLabel("路径"), 0, 0)
        grid.addWidget(QLabel("中心线"), 0, 1)
        grid.addWidget(QLabel("车辆"), 0, 2)
        grid.addWidget(QLabel("越界"), 0, 3)
        self.layer_checks: dict[str, tuple[QCheckBox, QCheckBox, QCheckBox]] = {}
        for row, (name, label) in enumerate(
            (("dispatched", "下发"), ("actual", "实际")),
            start=1,
        ):
            grid.addWidget(QLabel(label), row, 0)
            checks = (QCheckBox(), QCheckBox(), QCheckBox())
            for column, checkbox in enumerate(checks, start=1):
                checkbox.setChecked(True)
                layer_label = ("中心线", "车辆矩形", "异常点")[column - 1]
                checkbox.setAccessibleName(f"{label}路径{layer_label}")
                grid.addWidget(checkbox, row, column)
            checks[0].toggled.connect(
                lambda value, path=name: self.canvas.set_path_layer(path, centerline=value)
            )
            checks[1].toggled.connect(
                lambda value, path=name: self.canvas.set_path_layer(path, vehicles=value)
            )
            checks[2].toggled.connect(
                lambda value, path=name: self.canvas.set_path_layer(path, violations=value)
            )
            self.layer_checks[name] = checks
        layout.addLayout(grid)
        isolate = QHBoxLayout()
        dispatched_only = QPushButton("仅下发")
        actual_only = QPushButton("仅实际")
        show_all = QPushButton("全部")
        fit_button = QPushButton("适应内容")
        dispatched_only.clicked.connect(lambda: self._isolate("dispatched"))
        actual_only.clicked.connect(lambda: self._isolate("actual"))
        show_all.clicked.connect(lambda: self._isolate(None))
        fit_button.clicked.connect(self.canvas.fit_content)
        isolate.addWidget(dispatched_only)
        isolate.addWidget(actual_only)
        isolate.addWidget(show_all)
        isolate.addWidget(fit_button)
        layout.addLayout(isolate)
        direction_row = QHBoxLayout()
        direction_row.addWidget(QLabel("地图方向"))
        self.direction_spin = _coordinate_spin(
            minimum=-1000, maximum=1000, decimals=6, suffix=" rad"
        )
        self.direction_spin.setAccessibleName("地图显示方向弧度")
        self.direction_spin.valueChanged.connect(self.direction_changed)
        direction_row.addWidget(self.direction_spin)
        layout.addLayout(direction_row)
        self.radius_layer_check = QCheckBox("显示转弯半径定位图层")
        self.radius_layer_check.setChecked(False)
        self.radius_layer_check.setAccessibleName("转弯半径画布图层")
        self.radius_layer_check.toggled.connect(self.canvas.set_turn_radius_layer)
        layout.addWidget(self.radius_layer_check)
        return group

    def _isolate(self, path_name: str | None) -> None:
        self._updating = True
        try:
            for name, checks in self.layer_checks.items():
                checked = path_name is None or name == path_name
                for checkbox in checks:
                    checkbox.setChecked(checked)
        finally:
            self._updating = False
        self.canvas.isolate_path(path_name)

    def _build_lane_group(self) -> QGroupBox:
        group, layout = self._group("车道编辑")
        actions = QGridLayout()
        self.draw_button = QPushButton("绘制新车道")
        self.draw_button.clicked.connect(self._toggle_drawing)
        generate_button = QPushButton("按路径生成")
        generate_button.setAccessibleName("按下发或实际路径自动生成车道")
        generate_button.clicked.connect(self.generate_lane_requested)
        delete_button = QPushButton("删除")
        delete_button.clicked.connect(self._delete_selected)
        undo_button = QPushButton("撤销")
        redo_button = QPushButton("重做")
        undo_button.clicked.connect(self.canvas.undo_stack.undo)
        redo_button.clicked.connect(self.canvas.undo_stack.redo)
        save_button = QPushButton("保存")
        save_button.clicked.connect(self.save_requested)
        import_button = QPushButton("导入替换")
        import_button.clicked.connect(self.import_requested)
        export_button = QPushButton("导出")
        export_button.clicked.connect(self.export_requested)
        for index, button in enumerate(
            (
                self.draw_button,
                generate_button,
                delete_button,
                undo_button,
                redo_button,
                save_button,
                import_button,
                export_button,
            )
        ):
            actions.addWidget(button, index // 2, index % 2)
        layout.addLayout(actions)

        self.lane_list = QListWidget()
        self.lane_list.setAccessibleName("当前地图的车道列表")
        self.lane_list.setMinimumHeight(110)
        self.lane_list.currentItemChanged.connect(self._lane_selected)
        self.lane_list.itemChanged.connect(self._lane_item_changed)
        layout.addWidget(self.lane_list)

        properties = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._name_changed)
        self.length_spin = _coordinate_spin(minimum=0, maximum=1_000_000, suffix=" m")
        self.length_spin.editingFinished.connect(self._length_changed)
        self.width_spin = _coordinate_spin(minimum=0, maximum=1000, suffix=" m")
        self.width_spin.valueChanged.connect(self._draft_width_changed)
        self.width_spin.editingFinished.connect(self._width_changed)
        self.enabled_check = QCheckBox("参与可通行区域并集")
        self.enabled_check.toggled.connect(self._enabled_changed)
        self.closed_check = QCheckBox("闭合环")
        self.closed_check.toggled.connect(self._closed_changed)
        self.default_join_combo = QComboBox()
        self.default_join_combo.addItem("尖角（miter）", JoinStyle.MITER)
        self.default_join_combo.addItem("圆角", JoinStyle.ROUND)
        self.default_join_combo.currentIndexChanged.connect(self._default_join_changed)
        properties.addRow("名称", self.name_edit)
        properties.addRow("中心线长度", self.length_spin)
        properties.addRow("总宽", self.width_spin)
        properties.addRow("状态", self.enabled_check)
        properties.addRow("形状", self.closed_check)
        properties.addRow("默认连接", self.default_join_combo)
        layout.addLayout(properties)

        anchor_form = QFormLayout()
        self.anchor_combo = QComboBox()
        self.anchor_combo.currentIndexChanged.connect(self._anchor_selected)
        self.anchor_x = _coordinate_spin()
        self.anchor_y = _coordinate_spin()
        self.anchor_x.valueChanged.connect(self._anchor_coordinate_changed)
        self.anchor_y.valueChanged.connect(self._anchor_coordinate_changed)
        self.anchor_join_combo = QComboBox()
        self.anchor_join_combo.addItem("继承车道", None)
        self.anchor_join_combo.addItem("尖角（miter）", JoinStyle.MITER)
        self.anchor_join_combo.addItem("圆角", JoinStyle.ROUND)
        self.anchor_join_combo.currentIndexChanged.connect(self._anchor_join_changed)
        anchor_form.addRow("锚点", self.anchor_combo)
        anchor_coordinates = QHBoxLayout()
        anchor_coordinates.addWidget(QLabel("X"))
        anchor_coordinates.addWidget(self.anchor_x)
        anchor_coordinates.addWidget(QLabel("Y"))
        anchor_coordinates.addWidget(self.anchor_y)
        anchor_form.addRow("原始坐标", anchor_coordinates)
        anchor_form.addRow("连接覆盖", self.anchor_join_combo)
        layout.addLayout(anchor_form)

        segment_form = QFormLayout()
        self.segment_combo = QComboBox()
        self.segment_combo.currentIndexChanged.connect(self._segment_selected)
        self.segment_kind_combo = QComboBox()
        self.segment_kind_combo.addItem("直线", SegmentKind.LINE)
        self.segment_kind_combo.addItem("真实圆弧", SegmentKind.ARC)
        self.segment_kind_combo.addItem("三次贝塞尔", SegmentKind.CUBIC)
        self.segment_kind_combo.currentIndexChanged.connect(self._segment_kind_changed)
        segment_form.addRow("线段", self.segment_combo)
        segment_form.addRow("类型", self.segment_kind_combo)
        self.control_spins = [_coordinate_spin() for _ in range(4)]
        for spin in self.control_spins:
            spin.valueChanged.connect(self._control_changed)
        c1 = QHBoxLayout()
        c1.addWidget(QLabel("X"))
        c1.addWidget(self.control_spins[0])
        c1.addWidget(QLabel("Y"))
        c1.addWidget(self.control_spins[1])
        c2 = QHBoxLayout()
        c2.addWidget(QLabel("X"))
        c2.addWidget(self.control_spins[2])
        c2.addWidget(QLabel("Y"))
        c2.addWidget(self.control_spins[3])
        segment_form.addRow("控制点 1", c1)
        segment_form.addRow("控制点 2", c2)
        self.arc_radius_spin = _coordinate_spin(
            minimum=0.0001, maximum=1_000_000, suffix=" m"
        )
        self.arc_radius_spin.valueChanged.connect(self._arc_radius_changed)
        segment_form.addRow("圆弧半径", self.arc_radius_spin)
        layout.addLayout(segment_form)
        return group

    def _build_result_group(self) -> QGroupBox:
        group, layout = self._group("通行分析结果")
        self.dispatched_result = QLabel("下发路径：尚未分析")
        self.actual_result = QLabel("实际路径：尚未分析")
        for label in (self.dispatched_result, self.actual_result):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
        analyze_button = QPushButton("立即重新分析")
        analyze_button.clicked.connect(self.analyze_requested)
        layout.addWidget(analyze_button)
        radius_title = QLabel("整弯转弯半径")
        radius_title.setStyleSheet("font-weight:600;margin-top:6px")
        layout.addWidget(radius_title)
        self.dimension_source_label = QLabel("车辆尺寸来源：—")
        self.dimension_source_label.setWordWrap(True)
        layout.addWidget(self.dimension_source_label)
        self.radius_summaries: dict[str, QLabel] = {}
        self.radius_trees: dict[str, QTreeWidget] = {}
        self.radius_auto_buttons: dict[str, QPushButton] = {}
        self.radius_manual_buttons: dict[str, QPushButton] = {}
        self.radius_delete_buttons: dict[str, QPushButton] = {}
        self._radius_tree_updating = False
        for path_name, title in (("dispatched", "下发路径"), ("actual", "实际路径")):
            path_title = QLabel(title)
            path_title.setStyleSheet("font-weight:600;margin-top:4px")
            layout.addWidget(path_title)
            actions = QHBoxLayout()
            auto_button = QPushButton("自动计算半径")
            manual_button = QPushButton("手动计算半径")
            delete_button = QPushButton("删除选中")
            auto_button.clicked.connect(
                lambda _checked=False, path=path_name: self.auto_radius_requested.emit(path)
            )
            manual_button.clicked.connect(
                lambda _checked=False, path=path_name: self.manual_radius_requested.emit(path)
            )
            delete_button.clicked.connect(
                lambda _checked=False, path=path_name: self._delete_radius_measurement(path)
            )
            actions.addWidget(auto_button)
            actions.addWidget(manual_button)
            actions.addWidget(delete_button)
            layout.addLayout(actions)
            self.radius_auto_buttons[path_name] = auto_button
            self.radius_manual_buttons[path_name] = manual_button
            self.radius_delete_buttons[path_name] = delete_button
            summary = QLabel(f"{title}：自动 0，手动 0")
            summary.setWordWrap(True)
            summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(summary)
            tree = QTreeWidget()
            tree.setColumnCount(3)
            tree.setHeaderLabels(["测量 / 半径", "端点 / 数值", "状态 / 转向"])
            tree.setAccessibleName(f"{title}整弯转弯半径测量")
            tree.setMinimumHeight(145)
            tree.setRootIsDecorated(True)
            tree.itemActivated.connect(
                lambda item, _column, path=path_name: self._radius_item_activated(path, item)
            )
            tree.itemClicked.connect(
                lambda item, _column, path=path_name: self._radius_item_activated(path, item)
            )
            tree.itemChanged.connect(
                lambda item, column, path=path_name: self._radius_item_changed(
                    path, item, column
                )
            )
            layout.addWidget(tree)
            self.radius_summaries[path_name] = summary
            self.radius_trees[path_name] = tree
        return group

    def _radius_item_activated(self, path_name: str, item: QTreeWidgetItem) -> None:
        measurement = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(measurement, CalculatedMeasurement) and measurement.radius.valid:
            self.radius_layer_check.setChecked(True)
            self.canvas.show_turn_radius_observation(path_name, measurement.radius)

    def _radius_item_changed(
        self,
        path_name: str,
        item: QTreeWidgetItem,
        column: int,
    ) -> None:
        if self._radius_tree_updating or column != 0:
            return
        measurement = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(measurement, CalculatedMeasurement):
            return
        name = item.text(0).strip()
        old_name = str(item.data(0, Qt.ItemDataRole.UserRole + 1))
        if not name or name == old_name:
            if not name:
                self._radius_tree_updating = True
                item.setText(0, old_name)
                self._radius_tree_updating = False
            return
        self.radius_rename_requested.emit(path_name, measurement.record.id, name)

    def _delete_radius_measurement(self, path_name: str) -> None:
        item = self.radius_trees[path_name].currentItem()
        if item is None:
            return
        measurement = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(measurement, CalculatedMeasurement):
            self.radius_delete_requested.emit(path_name, measurement.record.id)

    @staticmethod
    def _measurement_status(measurement: CalculatedMeasurement) -> str:
        radius = measurement.radius
        if not radius.valid:
            return f"无法计算：{radius.error or '未知原因'}"
        side = "车体左侧" if radius.side is TurnSide.LEFT else "车体右侧"
        category = "掉头" if radius.kind is TurnKind.UTURN else "转弯"
        return f"{side}{category}"

    def _add_measurement_item(
        self,
        parent: QTreeWidgetItem,
        measurement: CalculatedMeasurement,
    ) -> None:
        record = measurement.record
        item = QTreeWidgetItem(
            [
                record.name,
                f"点 {record.start_index} → {record.end_index}",
                self._measurement_status(measurement),
            ]
        )
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setData(0, Qt.ItemDataRole.UserRole, measurement)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, record.name)
        parent.addChild(item)
        radius = measurement.radius
        if not radius.valid or radius.front_axle_radius is None:
            detail = QTreeWidgetItem(["原因", radius.error or "未知原因", ""])
            detail.setData(0, Qt.ItemDataRole.UserRole, measurement)
            item.addChild(detail)
            return
        values: list[tuple[str, float]] = [
            ("前轴中心转弯半径", radius.front_axle_radius),
            *[(CORNER_LABELS[kind], radius.radii[kind]) for kind in CornerRadiusKind],
        ]
        for label, value in values:
            detail = QTreeWidgetItem([label, f"{value:.3f} m", ""])
            detail.setData(0, Qt.ItemDataRole.UserRole, measurement)
            item.addChild(detail)

    def _populate_measurements(
        self,
        path_name: str,
        measurements: tuple[CalculatedMeasurement, ...],
    ) -> None:
        tree = self.radius_trees[path_name]
        automatic = tuple(
            item
            for item in measurements
            if item.record.source is MeasurementSource.AUTOMATIC
        )
        manual = tuple(
            item for item in measurements if item.record.source is MeasurementSource.MANUAL
        )
        self._radius_tree_updating = True
        tree.blockSignals(True)
        try:
            tree.clear()
            for label, group_measurements in (
                ("自动测量", automatic),
                ("手动测量", manual),
            ):
                group = QTreeWidgetItem([label, f"{len(group_measurements)} 条", ""])
                tree.addTopLevelItem(group)
                for measurement in group_measurements:
                    self._add_measurement_item(group, measurement)
                group.setExpanded(True)
            self.radius_summaries[path_name].setText(
                f"自动 {len(automatic)}，手动 {len(manual)}"
            )
            for column in range(3):
                tree.resizeColumnToContents(column)
        finally:
            tree.blockSignals(False)
            self._radius_tree_updating = False

    def set_turn_radius_measurements(
        self,
        dispatched: tuple[CalculatedMeasurement, ...],
        actual: tuple[CalculatedMeasurement, ...],
        *,
        dimensions_source: str,
    ) -> None:
        self.dimension_source_label.setText(f"车辆尺寸来源：{dimensions_source}")
        self._populate_measurements("dispatched", dispatched)
        self._populate_measurements("actual", actual)

    def set_manual_radius_mode(self, path_name: str, active: bool) -> None:
        self.radius_manual_buttons[path_name].setText(
            "结束手动计算" if active else "手动计算半径"
        )

    def select_radius_measurement(self, path_name: str, measurement_id: str) -> None:
        tree = self.radius_trees[path_name]
        for group_index in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(group_index)
            if group is None:
                continue
            for measurement_index in range(group.childCount()):
                item = group.child(measurement_index)
                measurement = item.data(0, Qt.ItemDataRole.UserRole)
                if (
                    isinstance(measurement, CalculatedMeasurement)
                    and measurement.record.id == measurement_id
                ):
                    tree.setCurrentItem(item)
                    group.setExpanded(True)
                    return

    def set_configuration(self, *, default_lane_width: float, direction: float) -> None:
        self.default_lane_width = default_lane_width
        self.direction_spin.blockSignals(True)
        self.direction_spin.setValue(direction)
        self.direction_spin.blockSignals(False)

    def refresh_lane_list(self) -> None:
        current_id = self.canvas.selected_lane_id
        layout = self.canvas.current_layout()
        self._updating = True
        try:
            self.lane_list.clear()
            selected_row = -1
            for row, lane in enumerate(layout.lanes):
                item = QListWidgetItem(lane.name)
                item.setData(Qt.ItemDataRole.UserRole, lane.id)
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEditable
                )
                item.setCheckState(
                    Qt.CheckState.Checked if lane.enabled else Qt.CheckState.Unchecked
                )
                self.lane_list.addItem(item)
                if lane.id == current_id:
                    selected_row = row
            if selected_row >= 0:
                self.lane_list.setCurrentRow(selected_row)
        finally:
            self._updating = False
        self._refresh_properties()

    def _selected_lane(self) -> Lane | None:
        lane_id = self.canvas.selected_lane_id
        if not lane_id:
            return None
        return next(
            (lane for lane in self.canvas.current_layout().lanes if lane.id == lane_id),
            None,
        )

    def _lane_selected(self, current: QListWidgetItem | None) -> None:
        if self._updating:
            return
        lane_id = str(current.data(Qt.ItemDataRole.UserRole)) if current else None
        self.canvas.select_lane(lane_id)
        self._refresh_properties()

    def _lane_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating:
            return
        lane_id = str(item.data(Qt.ItemDataRole.UserRole))
        self.canvas.set_lane_name(lane_id, item.text().strip() or "未命名车道")
        self.canvas.set_lane_enabled(lane_id, item.checkState() == Qt.CheckState.Checked)

    def _selection_changed(self, _lane_id: str, _anchor: int, _segment: int) -> None:
        self.refresh_lane_list()

    def _refresh_properties(self) -> None:
        lane = self._selected_lane()
        self._updating = True
        try:
            if self.canvas.is_drawing:
                for widget in (
                    self.name_edit,
                    self.length_spin,
                    self.enabled_check,
                    self.closed_check,
                    self.default_join_combo,
                    self.anchor_combo,
                    self.anchor_x,
                    self.anchor_y,
                    self.anchor_join_combo,
                    self.segment_combo,
                    self.segment_kind_combo,
                    self.arc_radius_spin,
                    *self.control_spins,
                ):
                    widget.setEnabled(False)
                self.width_spin.setEnabled(True)
                self.width_spin.setValue(self.canvas.draft_width)
                self.width_spin.setToolTip("正在绘制的新车道总宽，修改后预览立即更新")
                return
            enabled = lane is not None
            for widget in (
                self.name_edit,
                self.length_spin,
                self.width_spin,
                self.enabled_check,
                self.closed_check,
                self.default_join_combo,
                self.anchor_combo,
                self.anchor_x,
                self.anchor_y,
                self.anchor_join_combo,
                self.segment_combo,
                self.segment_kind_combo,
            ):
                widget.setEnabled(enabled)
            self.width_spin.setToolTip("")
            if lane is None:
                self.name_edit.clear()
                self.length_spin.setValue(0)
                self.anchor_combo.clear()
                self.segment_combo.clear()
                return
            self.name_edit.setText(lane.name)
            length = lane_centerline_length(lane)
            self.length_spin.setValue(length)
            self.length_spin.setEnabled(length > 1e-12)
            self.length_spin.setToolTip(
                "" if length > 1e-12 else "零长度车道无法按比例缩放"
            )
            self.width_spin.setValue(lane.width)
            self.enabled_check.setChecked(lane.enabled)
            self.closed_check.setChecked(lane.closed)
            self.default_join_combo.setCurrentIndex(
                self.default_join_combo.findData(lane.default_join)
            )
            self.anchor_combo.clear()
            for index in range(len(lane.anchors)):
                self.anchor_combo.addItem(f"锚点 {index + 1}", index)
            anchor_index = min(max(self.canvas._selected_anchor, 0), len(lane.anchors) - 1)
            self.anchor_combo.setCurrentIndex(anchor_index)
            self.segment_combo.clear()
            for index in range(len(lane.segments)):
                self.segment_combo.addItem(f"线段 {index + 1}", index)
            segment_index = min(max(self.canvas._selected_segment, 0), len(lane.segments) - 1)
            self.segment_combo.setCurrentIndex(segment_index)
            self._refresh_anchor(lane, anchor_index)
            self._refresh_segment(lane, segment_index)
        finally:
            self._updating = False

    def _refresh_anchor(self, lane: Lane, index: int) -> None:
        anchor = lane.anchors[index]
        self.anchor_x.setValue(anchor.point.x)
        self.anchor_y.setValue(anchor.point.y)
        self.anchor_join_combo.setCurrentIndex(
            self.anchor_join_combo.findData(anchor.join_override)
        )

    def _refresh_segment(self, lane: Lane, index: int) -> None:
        segment = lane.segments[index]
        self.segment_kind_combo.setCurrentIndex(self.segment_kind_combo.findData(segment.kind))
        cubic = segment.kind is SegmentKind.CUBIC
        for spin in self.control_spins:
            spin.setEnabled(cubic)
        self.arc_radius_spin.setEnabled(False)
        self.arc_radius_spin.setToolTip("")
        if segment.kind is SegmentKind.ARC:
            try:
                radius = arc_radius(lane, index)
                maximum = maximum_arc_radius(lane, index)
            except ValueError as exc:
                self.arc_radius_spin.setToolTip(str(exc))
            else:
                self.arc_radius_spin.blockSignals(True)
                self.arc_radius_spin.setMaximum(maximum)
                self.arc_radius_spin.setValue(radius)
                self.arc_radius_spin.blockSignals(False)
                self.arc_radius_spin.setEnabled(True)
                self.arc_radius_spin.setToolTip(f"最大允许半径 {maximum:.4f} m")
        if cubic and segment.control1 and segment.control2:
            for spin, value in zip(
                self.control_spins,
                (segment.control1.x, segment.control1.y, segment.control2.x, segment.control2.y),
                strict=True,
            ):
                spin.setValue(value)

    def _toggle_drawing(self) -> None:
        if self.canvas.is_drawing:
            self.canvas.finish_lane_drawing()
        else:
            self.canvas.start_lane_drawing(width=self.default_lane_width)

    def _drawing_changed(self, drawing: bool) -> None:
        self.draw_button.setText("完成车道（Enter）" if drawing else "绘制新车道")
        self.lane_list.setEnabled(not drawing)
        self._refresh_properties()

    def _delete_selected(self) -> None:
        if self.canvas.selected_lane_id:
            self.canvas.delete_lane(self.canvas.selected_lane_id)

    def _name_changed(self) -> None:
        if not self._updating and self.canvas.selected_lane_id:
            self.canvas.set_lane_name(
                self.canvas.selected_lane_id,
                self.name_edit.text().strip() or "未命名车道",
            )

    def _length_changed(self) -> None:
        if self._updating or not self.canvas.selected_lane_id:
            return
        lane = self._selected_lane()
        if lane is None:
            return
        try:
            self.canvas.set_lane_length(self.canvas.selected_lane_id, self.length_spin.value())
        except ValueError as exc:
            self.length_spin.setValue(lane_centerline_length(lane))
            QMessageBox.warning(self, "车道长度无效", str(exc))

    def _width_changed(self) -> None:
        if self._updating:
            return
        if self.canvas.is_drawing:
            try:
                self.canvas.set_draft_lane_width(self.width_spin.value())
            except ValueError as exc:
                self.width_spin.setValue(self.canvas.draft_width)
                QMessageBox.warning(self, "车道宽度无效", str(exc))
            return
        if not self.canvas.selected_lane_id:
            return
        lane = self._selected_lane()
        if lane is None:
            return
        try:
            self.canvas.set_lane_width(self.canvas.selected_lane_id, self.width_spin.value())
        except ValueError as exc:
            self.width_spin.setValue(lane.width)
            QMessageBox.warning(self, "车道宽度无效", str(exc))

    def _draft_width_changed(self, width: float) -> None:
        if self._updating or not self.canvas.is_drawing or width <= 0:
            return
        self.canvas.set_draft_lane_width(width)

    def _enabled_changed(self, enabled: bool) -> None:
        if not self._updating and self.canvas.selected_lane_id:
            self.canvas.set_lane_enabled(self.canvas.selected_lane_id, enabled)

    def _closed_changed(self, closed: bool) -> None:
        if not self._updating and self.canvas.selected_lane_id:
            self.canvas.set_lane_closed(self.canvas.selected_lane_id, closed)

    def _default_join_changed(self) -> None:
        if not self._updating and self.canvas.selected_lane_id:
            join = self.default_join_combo.currentData()
            self.canvas.set_lane_default_join(self.canvas.selected_lane_id, join)

    def _anchor_selected(self, index: int) -> None:
        if self._updating or index < 0:
            return
        self.canvas.select_anchor(index)
        lane = self._selected_lane()
        if lane:
            self._updating = True
            self._refresh_anchor(lane, index)
            self._updating = False

    def _anchor_coordinate_changed(self) -> None:
        if self._updating or not self.canvas.selected_lane_id:
            return
        self.canvas.set_anchor_position(
            self.canvas.selected_lane_id,
            self.anchor_combo.currentIndex(),
            Point2D(self.anchor_x.value(), self.anchor_y.value()),
        )

    def _anchor_join_changed(self) -> None:
        if self._updating or not self.canvas.selected_lane_id:
            return
        self.canvas.set_anchor_join(
            self.canvas.selected_lane_id,
            self.anchor_combo.currentIndex(),
            self.anchor_join_combo.currentData(),
        )

    def _segment_selected(self, index: int) -> None:
        if self._updating or index < 0:
            return
        self.canvas.select_segment(index)
        lane = self._selected_lane()
        if lane:
            self._updating = True
            self._refresh_segment(lane, index)
            self._updating = False

    def _segment_kind_changed(self) -> None:
        if self._updating or not self.canvas.selected_lane_id:
            return
        self.canvas.set_segment_kind(
            self.canvas.selected_lane_id,
            self.segment_combo.currentIndex(),
            self.segment_kind_combo.currentData(),
        )

    def _control_changed(self) -> None:
        if self._updating or not self.canvas.selected_lane_id:
            return
        segment_index = self.segment_combo.currentIndex()
        self.canvas.set_control_point(
            self.canvas.selected_lane_id,
            segment_index,
            1,
            Point2D(self.control_spins[0].value(), self.control_spins[1].value()),
        )
        self.canvas.set_control_point(
            self.canvas.selected_lane_id,
            segment_index,
            2,
            Point2D(self.control_spins[2].value(), self.control_spins[3].value()),
        )

    def _arc_radius_changed(self, radius: float) -> None:
        if self._updating or not self.canvas.selected_lane_id:
            return
        self.canvas.set_arc_radius(
            self.canvas.selected_lane_id,
            self.segment_combo.currentIndex(),
            radius,
        )

    @staticmethod
    def _result_text(title: str, result: AnalysisResult | None) -> tuple[str, str]:
        if result is None:
            return f"{title}：尚未分析", "#5c6778"
        status_text = {
            ClearanceStatus.SAFE: "绿色 / 可通过",
            ClearanceStatus.WARNING: "黄色 / 净距不足或接触边界",
            ClearanceStatus.OUTSIDE: "红色 / 越界",
            ClearanceStatus.UNAVAILABLE: "不可分析",
        }[result.status]
        color = {
            ClearanceStatus.SAFE: "#16794b",
            ClearanceStatus.WARNING: "#9a6500",
            ClearanceStatus.OUTSIDE: "#b4233f",
            ClearanceStatus.UNAVAILABLE: "#5c6778",
        }[result.status]
        clearance = (
            "—" if result.minimum_clearance is None else f"{result.minimum_clearance:.3f} m"
        )
        minimum_location = (
            "—"
            if result.minimum_clearance_pose is None
            else f"({result.minimum_clearance_pose.x:.3f}, {result.minimum_clearance_pose.y:.3f})"
        )
        outside_location = (
            "—"
            if result.first_outside is None
            else f"({result.first_outside.x:.3f}, {result.first_outside.y:.3f})"
        )
        incomplete = (
            f"；缺 yaw 点 {len(result.missing_yaw_indices)} 个，"
            f"跳过相邻段 {result.skipped_segments} 条"
            if result.incomplete
            else ""
        )
        sampling = (
            f"；采样步长 {result.position_step:.3f} m / {result.yaw_step:.3f} rad"
            if result.position_step is not None and result.yaw_step is not None
            else ""
        )
        text = (
            f"{title}：{status_text}\n最小净距 {clearance} @ {minimum_location}；"
            f"首次越界 {outside_location}；越界样本 {result.outside_samples}；"
            f"有效样本 {result.analyzed_samples}{incomplete}{sampling}"
        )
        return text, color

    def set_results(
        self,
        dispatched: AnalysisResult | None,
        actual: AnalysisResult | None,
    ) -> None:
        for label, title, result in (
            (self.dispatched_result, "下发路径", dispatched),
            (self.actual_result, "实际路径", actual),
        ):
            text, color = self._result_text(title, result)
            label.setText(text)
            label.setStyleSheet(f"color:{color};padding:6px")
