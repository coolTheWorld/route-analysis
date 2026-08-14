"""Preview and confirm automatic lane generation from loaded paths."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
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
    LaneGenerationResult,
    generate_lane,
)
from route_analysis.logging_setup import log_event
from route_analysis.models import PosePoint

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AutoLaneSelection:
    generation: LaneGenerationResult
    source: str
    mode: str
    maximum_deviation: float


def run_auto_lane_dialog(
    parent: QWidget,
    paths: Mapping[str, Sequence[PosePoint]],
    *,
    default_width: float,
    maximum_deviation: float,
    last_mode: BendMode,
    preview_callback: Callable[[LaneGenerationResult | None], None],
) -> AutoLaneSelection | None:
    """Run one preview lifecycle and return only the confirmed generation."""

    dialog = AutoLaneDialog(
        paths,
        parent=parent,
        default_width=default_width,
        maximum_deviation=maximum_deviation,
        last_mode=last_mode,
    )
    dialog.preview_changed.connect(preview_callback)
    dialog.refresh_preview()
    accepted = dialog.exec() == AutoLaneDialog.DialogCode.Accepted
    preview_callback(None)
    if not accepted or dialog.generation_result is None:
        return None
    return AutoLaneSelection(
        dialog.generation_result,
        str(dialog.source_combo.currentData()),
        str(dialog.mode_combo.currentData()),
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
        width=result.lane.width,
        maximum_deviation=metrics.maximum_deviation,
        anchors=metrics.anchors,
        segments=metrics.segments,
        arc_failures=metrics.arc_failures,
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
        paths: Mapping[str, Sequence[PosePoint]],
        *,
        parent: QWidget | None = None,
        default_width: float,
        maximum_deviation: float,
        last_mode: BendMode,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("按坐标路径生成车道")
        self.setMinimumWidth(500)
        self.setModal(True)
        self._paths = {name: tuple(points) for name, points in paths.items()}
        self.generation_result: LaneGenerationResult | None = None

        root = QVBoxLayout(self)
        explanation = QLabel(
            "生成结果只会新增为一条普通本地车道，不会合并、替换或持续绑定来源路径。"
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)
        form = QFormLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem("下发路径", "dispatched")
        self.source_combo.addItem("实际执行路径", "actual")
        self.source_combo.setAccessibleName("自动车道来源路径")
        self.name_edit = QLineEdit("路径生成车道")
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
        form.addRow("来源", self.source_combo)
        form.addRow("名称", self.name_edit)
        form.addRow("车道总宽", self.width_spin)
        form.addRow("弯道模式", self.mode_combo)
        form.addRow("最大拟合偏差", self.deviation_spin)
        form.addRow("形状", self.closed_check)
        root.addLayout(form)
        self.metrics_label = QLabel()
        self.metrics_label.setWordWrap(True)
        self.metrics_label.setTextInteractionFlags(self.metrics_label.textInteractionFlags())
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
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        for signal in (
            self.name_edit.textChanged,
            self.width_spin.valueChanged,
            self.mode_combo.currentIndexChanged,
            self.deviation_spin.valueChanged,
            self.closed_check.toggled,
        ):
            signal.connect(self._schedule_preview)

        self._auto_set_closed()
        self.refresh_preview()

    def _source_points(self) -> tuple[PosePoint, ...]:
        return self._paths.get(str(self.source_combo.currentData()), ())

    def _auto_set_closed(self) -> None:
        points = self._source_points()
        should_close = (
            len(points) >= 3
            and math.hypot(points[-1].x - points[0].x, points[-1].y - points[0].y)
            <= self.deviation_spin.value()
        )
        self.closed_check.blockSignals(True)
        self.closed_check.setChecked(should_close)
        self.closed_check.blockSignals(False)

    def _source_changed(self) -> None:
        label = "下发路径" if self.source_combo.currentData() == "dispatched" else "实际路径"
        self.name_edit.setText(f"{label}生成车道")
        self._auto_set_closed()
        self._schedule_preview()

    def _schedule_preview(self, *_args: object) -> None:
        self._timer.start()

    def refresh_preview(self) -> None:
        self._timer.stop()
        try:
            result = generate_lane(
                self._source_points(),
                lane_id=uuid4().hex,
                name=self.name_edit.text().strip() or "路径生成车道",
                width=self.width_spin.value(),
                mode=BendMode(str(self.mode_combo.currentData())),
                maximum_deviation=self.deviation_spin.value(),
                closed=self.closed_check.isChecked(),
            )
        except ValueError as exc:
            self.generation_result = None
            self.metrics_label.setText(f"无法生成：{exc}。来源路径至少需要两个不同坐标点。")
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
        self.metrics_label.setText(
            f"最大偏差 {metrics.maximum_deviation:.6f} m；"
            f"锚点 {metrics.anchors}；线段 {metrics.segments}{warning}"
        )
        self.metrics_label.setStyleSheet(
            "color:#9a6500;padding:6px" if metrics.arc_failures else "color:#16794b;padding:6px"
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
