"""Preview and confirm automatic lane generation from loaded paths."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from route_analysis.lane_generation import (
    BendMode,
    ConnectionMode,
    LaneGenerationResult,
    generate_lane_between,
)
from route_analysis.logging_setup import log_event
from route_analysis.models import PosePoint, VehicleDimensions

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AutoLaneSelection:
    generation: LaneGenerationResult
    source: str
    mode: str
    connection: str
    maximum_deviation: float


@dataclass(frozen=True, slots=True)
class LanePickRequest:
    """The two samples a lane is to span, with the numbers the interface shows for them."""

    source: str
    poses: tuple[PosePoint, ...]
    start_index: int
    end_index: int
    start_label: int
    end_label: int
    dimensions: VehicleDimensions

    @property
    def source_text(self) -> str:
        return "下发路径" if self.source == "dispatched" else "实际执行路径"


def run_auto_lane_dialog(
    parent: QWidget,
    request: LanePickRequest,
    *,
    default_width: float,
    maximum_deviation: float,
    last_mode: BendMode,
    last_connection: ConnectionMode,
    preview_callback: Callable[[LaneGenerationResult | None], None],
) -> AutoLaneSelection | None:
    """Run one preview lifecycle and return only the confirmed generation."""

    dialog = AutoLaneDialog(
        request,
        parent=parent,
        default_width=default_width,
        maximum_deviation=maximum_deviation,
        last_mode=last_mode,
        last_connection=last_connection,
    )
    dialog.preview_changed.connect(preview_callback)
    dialog.refresh_preview()
    accepted = dialog.exec() == AutoLaneDialog.DialogCode.Accepted
    preview_callback(None)
    if not accepted or dialog.generation_result is None:
        return None
    return AutoLaneSelection(
        dialog.generation_result,
        request.source,
        str(dialog.mode_combo.currentData()),
        str(dialog.connection_combo.currentData()),
        dialog.deviation_spin.value(),
    )


def log_auto_lane_selection(
    selection: AutoLaneSelection,
    source_path: Sequence[PosePoint],
) -> None:
    """Record summary and full diagnostic data for one confirmed generation."""

    result = selection.generation
    metrics = result.metrics
    log_event(
        LOGGER,
        logging.INFO,
        "lane_generated",
        source=selection.source,
        lane_id=result.lane.id,
        mode=selection.mode,
        connection=selection.connection,
        width=result.lane.width,
        maximum_deviation=metrics.maximum_deviation,
        anchors=metrics.anchors,
        segments=metrics.segments,
        arc_failures=metrics.arc_failures,
        start_overhang=metrics.start_overhang,
        end_overhang=metrics.end_overhang,
    )
    log_event(
        LOGGER,
        logging.DEBUG,
        "lane_generation_full_source",
        source=selection.source,
        path=source_path,
        generated_lane=result.lane,
    )


class AutoLaneDialog(QDialog):
    preview_changed = Signal(object)

    def __init__(
        self,
        request: LanePickRequest,
        *,
        parent: QWidget | None = None,
        default_width: float,
        maximum_deviation: float,
        last_mode: BendMode,
        last_connection: ConnectionMode,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("在两个点位之间生成车道")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._request = request
        self.generation_result: LaneGenerationResult | None = None

        root = QVBoxLayout(self)
        explanation = QLabel(
            f"{request.source_text} · 点位 {request.start_label} → {request.end_label}。"
            "生成结果只会新增为一条普通本地车道，不会合并、替换或持续绑定来源路径；"
            "车道两端会各自延伸到该点位车体探出的最远处。"
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)

        form = QFormLayout()
        self.connection_combo = QComboBox()
        self.connection_combo.addItem("沿原路径", ConnectionMode.PATH.value)
        self.connection_combo.addItem("直线连接", ConnectionMode.STRAIGHT.value)
        self.connection_combo.setCurrentIndex(
            max(0, self.connection_combo.findData(last_connection.value))
        )
        self.connection_combo.setAccessibleName("两点连接方式")
        self.name_edit = QLineEdit(
            f"点位 {request.start_label}–{request.end_label} 生成车道"
        )
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.001, 1000)
        self.width_spin.setDecimals(3)
        self.width_spin.setSuffix(" m")
        self.width_spin.setValue(default_width)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("尖角", BendMode.SHARP.value)
        self.mode_combo.addItem("真实圆弧（失败回退尖角）", BendMode.ROUND.value)
        self.mode_combo.addItem("分段三次贝塞尔", BendMode.BEZIER.value)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(last_mode.value)))
        self.deviation_spin = QDoubleSpinBox()
        self.deviation_spin.setRange(0.000001, 1000)
        self.deviation_spin.setDecimals(6)
        self.deviation_spin.setSingleStep(0.01)
        self.deviation_spin.setSuffix(" m")
        self.deviation_spin.setValue(maximum_deviation)
        self.closed_check = QCheckBox("闭合车道")
        form.addRow("连接方式", self.connection_combo)
        form.addRow("名称", self.name_edit)
        form.addRow("车道总宽", self.width_spin)
        form.addRow("弯道模式", self.mode_combo)
        form.addRow("最大拟合偏差", self.deviation_spin)
        form.addRow("形状", self.closed_check)
        root.addLayout(form)

        self.connection_note = QLabel()
        self.connection_note.setWordWrap(True)
        root.addWidget(self.connection_note)
        self.metrics_label = QLabel()
        self.metrics_label.setWordWrap(True)
        root.addWidget(self.metrics_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("新增车道")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh_preview)
        self.connection_combo.currentIndexChanged.connect(self._connection_changed)
        for signal in (
            self.name_edit.textChanged,
            self.width_spin.valueChanged,
            self.mode_combo.currentIndexChanged,
            self.deviation_spin.valueChanged,
            self.closed_check.toggled,
        ):
            signal.connect(self._schedule_preview)

        self._apply_connection()
        self.refresh_preview()

    def connection(self) -> ConnectionMode:
        return ConnectionMode(str(self.connection_combo.currentData()))

    def _apply_connection(self) -> None:
        """A straight connection has no bend to fit, so say so instead of silently ignoring."""

        straight = self.connection() is ConnectionMode.STRAIGHT
        for widget in (self.mode_combo, self.deviation_spin, self.closed_check):
            widget.setEnabled(not straight)
        if straight:
            self.closed_check.blockSignals(True)
            self.closed_check.setChecked(False)
            self.closed_check.blockSignals(False)
            self.connection_note.setText(
                "直线连接不经过弯道拟合，弯道模式、最大拟合偏差和闭合车道对它没有作用。"
            )
            self.connection_note.setStyleSheet("color:#6e7f93;padding:2px")
        else:
            self.connection_note.setText(
                "沿原路径保留两个点位之间的实际形状，包括中间的转弯。"
            )
            self.connection_note.setStyleSheet("color:#6e7f93;padding:2px")

    def _connection_changed(self) -> None:
        self._apply_connection()
        self._schedule_preview()

    def _schedule_preview(self, *_args: object) -> None:
        self._timer.start()

    def _width_warning(self) -> str:
        vehicle_width = self._request.dimensions.width
        if self.width_spin.value() >= vehicle_width:
            return ""
        return (
            f"；当前总宽 {self.width_spin.value():.2f} m 小于车宽 "
            f"{vehicle_width:.2f} m，两端车体会露出车道"
        )

    def refresh_preview(self) -> None:
        self._timer.stop()
        request = self._request
        try:
            result = generate_lane_between(
                request.poses,
                request.dimensions,
                start_index=request.start_index,
                end_index=request.end_index,
                connection=self.connection(),
                lane_id=uuid4().hex,
                name=self.name_edit.text().strip() or "路径生成车道",
                width=self.width_spin.value(),
                mode=BendMode(str(self.mode_combo.currentData())),
                maximum_deviation=self.deviation_spin.value(),
                closed=self.closed_check.isChecked(),
            )
        except ValueError as exc:
            self.generation_result = None
            self.metrics_label.setText(f"无法生成：{exc}")
            self.metrics_label.setStyleSheet("color:#b4233f;padding:6px")
            self.ok_button.setEnabled(False)
            self.preview_changed.emit(None)
            return
        self.generation_result = result
        metrics = result.metrics
        warning = (
            f"；圆弧拟合失败并回退尖角 {metrics.arc_failures} 处"
            if metrics.arc_failures
            else ""
        )
        width_warning = self._width_warning()
        self.metrics_label.setText(
            f"两端延伸 {metrics.start_overhang:.2f} m / {metrics.end_overhang:.2f} m；"
            f"最大偏差 {metrics.maximum_deviation:.6f} m；"
            f"锚点 {metrics.anchors}；线段 {metrics.segments}{warning}{width_warning}"
        )
        self.metrics_label.setStyleSheet(
            "color:#9a6500;padding:6px"
            if metrics.arc_failures or width_warning
            else "color:#16794b;padding:6px"
        )
        self.ok_button.setEnabled(True)
        self.preview_changed.emit(result)

    def accept(self) -> None:
        self.refresh_preview()
        if self.generation_result is not None:
            super().accept()

    def reject(self) -> None:
        self._timer.stop()
        self.preview_changed.emit(None)
        super().reject()
