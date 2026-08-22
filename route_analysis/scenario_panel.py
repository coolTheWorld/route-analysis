"""The rapid-estimate tab: offline limiting-case geometry in three columns.

Parameters on the left, plan view in the middle, readings on the right. Nothing here
touches the scheduler or ``data/``, so the page works with no command selected.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QRectF, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from route_analysis import theme
from route_analysis.clearance_graphics import format_length
from route_analysis.control_panel import CORNER_LABELS
from route_analysis.models import ClearanceStatus, VehicleDimensions
from route_analysis.scenario_geometry import (
    OFFSET_LABELS,
    SCENARIO_NAMES,
    SCENARIO_SUBTITLES,
    SOLVED_KEYS,
    Gear,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    SolveMode,
    dimension_label,
    variant_name,
)
from route_analysis.scenario_graphics import PlanLayers, paint_scenario_plan
from route_analysis.scenario_solver import ScenarioResult, offset_specs, solve_scenario
from route_analysis.turn_radius import CornerRadiusKind
from route_analysis.workers import Worker

SIDEBAR_WIDTH = 290
RESULT_WIDTH = 314
LABEL_WIDTH = 84
DEBOUNCE_MS = 150

ROAD_KEYS: dict[Scenario, tuple[str, ...]] = {
    Scenario.CORNER: ("wa", "wb"),
    Scenario.CROSSBACK: ("wv", "wh", "ls"),
    Scenario.STUBBACK: ("wh", "wv", "ls"),
    Scenario.UTURN: ("w", "b", "d"),
}
CARD_NAMES: dict[Scenario, str] = {
    Scenario.CORNER: "直角转弯",
    Scenario.CROSSBACK: "直角R档\n直行转D档",
    Scenario.STUBBACK: "直角R档\n转弯转D档",
    Scenario.UTURN: "U型转弯",
}
"""Scenario names wrap by hand: on one line they widen the 290 px sidebar and clip it."""

GIVEN_ONLY_KEYS = {"b"}
"""Still supplied when solving forward: the divider is site fabric, not an unknown."""

STATUS_WORDS: dict[ClearanceStatus, tuple[str, str]] = {
    ClearanceStatus.SAFE: ("通过", "success"),
    ClearanceStatus.WARNING: ("临界", "warning"),
    ClearanceStatus.OUTSIDE: ("越界", "danger"),
    ClearanceStatus.UNAVAILABLE: ("不可行", "danger"),
}

SPIN_RANGES: dict[str, tuple[float, float, float, int]] = {
    "radius": (0.20, 20.00, 0.05, 2),
    "width": (0.30, 6.00, 0.01, 2),
    "front": (0.05, 20.00, 0.005, 3),
    "rear": (0.05, 20.00, 0.005, 3),
    "threshold": (0.00, 2.00, 0.01, 2),
    "wa": (0.20, 30.00, 0.05, 2),
    "wb": (0.20, 30.00, 0.05, 2),
    "wv": (0.20, 30.00, 0.05, 2),
    "wh": (0.20, 30.00, 0.05, 2),
    "ls": (0.00, 60.00, 0.05, 2),
    "w": (0.20, 30.00, 0.05, 2),
    "b": (0.05, 10.00, 0.05, 2),
    "d": (0.00, 60.00, 0.05, 2),
}

FOOTNOTE = (
    "偏移符号：弯道内侧为正、外侧为负。\n"
    "本页为纯几何速算，不连接调度后端，不写回任何数据。"
)
NON_UNIQUE_NOTE = (
    "各尺寸均已不可单独缩小；改变求解顺序会得到前沿上的另一组解。"
)


def _restyle(widget: QWidget, name: str, value: str) -> None:
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _spin(kind: str, value: float) -> QDoubleSpinBox:
    minimum, maximum, step, decimals = SPIN_RANGES[kind]
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    spin.setDecimals(decimals)
    spin.setSuffix(" m")
    spin.setKeyboardTracking(False)
    spin.setValue(value)
    return spin


def _scenario_icon(scenario: Scenario) -> QIcon:
    """24 px line sketch, shaped after the customer drawings this page answers."""

    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(theme.TEXT_SECONDARY), 1.4))
    if scenario is Scenario.CORNER:
        painter.drawLine(2, 20, 14, 20)
        painter.drawLine(14, 20, 14, 4)
    elif scenario is Scenario.CROSSBACK:
        painter.drawLine(12, 2, 12, 22)
        painter.drawLine(2, 12, 22, 12)
    elif scenario is Scenario.STUBBACK:
        painter.drawLine(2, 9, 22, 9)
        painter.drawLine(9, 9, 9, 22)
        painter.drawLine(15, 9, 15, 22)
    else:
        painter.drawLine(6, 22, 6, 9)
        painter.drawArc(6, 3, 12, 12, 0, 180 * 16)
        painter.drawLine(18, 9, 18, 22)
    painter.end()
    return QIcon(pixmap)


class _Segment(QWidget):
    """A row of mutually exclusive segment buttons."""

    changed = Signal(object)

    def __init__(self, choices: tuple[tuple[object, str], ...]) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[object, QToolButton] = {}
        for value, caption in choices:
            button = QToolButton()
            button.setObjectName("scenarioSegment")
            button.setText(caption)
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked, key=value: self.changed.emit(key))
            self._group.addButton(button)
            self._buttons[value] = button
            row.addWidget(button, 1)

    def select(self, value: object) -> None:
        button = self._buttons.get(value)
        if button is not None:
            button.setChecked(True)


class _Card(QFrame):
    """One card in the result column: a title over label-and-reading rows."""

    def __init__(self, title: str, *, tone: str = "plain", bar: bool = False) -> None:
        super().__init__()
        self.setObjectName("scenarioCard")
        self.setProperty("tone", tone)
        self.setFrameShape(QFrame.Shape.NoFrame)
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self._bar: QFrame | None = None
        if bar:
            self._bar = QFrame()
            self._bar.setObjectName("clearanceCardBar")
            self._bar.setFixedWidth(3)
            shell.addWidget(self._bar)
        body = QWidget()
        shell.addWidget(body, 1)
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(10, 9, 12, 10)
        self._body.setSpacing(4)
        self._title = QLabel(title)
        self._title.setObjectName("scenarioCardTitle")
        self._body.addWidget(self._title)
        self._rows = QGridLayout()
        self._rows.setContentsMargins(0, 2, 0, 0)
        self._rows.setHorizontalSpacing(8)
        self._rows.setVerticalSpacing(3)
        self._rows.setColumnStretch(0, 1)
        self._body.addLayout(self._rows)
        self._note = QLabel()
        self._note.setObjectName("scenarioHint")
        self._note.setWordWrap(True)
        self._note.hide()
        self._body.addWidget(self._note)

    def set_title(self, title: str) -> None:
        self._title.setText(title)

    def set_note(self, message: str) -> None:
        self._note.setText(message)
        self._note.setVisible(bool(message))

    def set_state(self, state: str) -> None:
        _restyle(self, "state", state)
        if self._bar is not None:
            _restyle(self._bar, "state", state)

    def clear(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

    def add_row(self, label: str, value: str, *, kind: str = "", state: str = "") -> None:
        row = self._rows.rowCount()
        caption = QLabel(label)
        caption.setObjectName("scenarioRowLabel")
        reading = QLabel(value)
        reading.setObjectName("scenarioRowValue")
        if kind:
            reading.setProperty("kind", kind)
        if state:
            reading.setProperty("state", state)
        reading.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._rows.addWidget(caption, row, 0)
        self._rows.addWidget(reading, row, 1)


class PlanView(QWidget):
    """The middle column, drawn entirely by ``scenario_graphics``."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("scenarioCanvas")
        self.setMinimumHeight(320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._result: ScenarioResult | None = None
        self._layers = PlanLayers()

    def set_result(self, result: ScenarioResult | None) -> None:
        self._result = result
        self.update()

    def set_layers(self, layers: PlanLayers) -> None:
        self._layers = layers
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self._result is None:
            return
        painter = QPainter(self)
        paint_scenario_plan(
            painter, QRectF(self.rect()).adjusted(1, 1, -1, -1), self._result, self._layers
        )
        painter.end()


class ScenarioPanel(QWidget):
    """The rapid-estimate tab."""

    solved = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._inputs = ScenarioInputs()
        self._road = RoadDimensions()
        self._result: ScenarioResult | None = None
        self._generation = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._workers: set[Worker[ScenarioResult]] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._start_solve)
        self._pending = True
        self._build()
        self.setStyleSheet(theme.CLEARANCE_STYLESHEET + theme.SCENARIO_STYLESHEET)
        self._sync_controls()

    # ---------- 构建 ----------

    def _build(self) -> None:
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        row.addWidget(self._build_plan(), 1)
        row.addWidget(self._build_results())

    @staticmethod
    def _scroller(inner: QWidget, width: int, name: str) -> QScrollArea:
        inner.setObjectName(name)
        area = QScrollArea()
        area.setWidget(inner)
        area.setWidgetResizable(True)
        area.setFixedWidth(width)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area

    @staticmethod
    def _group(title: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(title)
        box.setObjectName("scenarioGroup")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(9, 6, 9, 9)
        layout.setSpacing(5)
        return box, layout

    def _labelled(
        self, layout: QVBoxLayout, caption: str, widget: QWidget
    ) -> tuple[QWidget, QLabel]:
        """Label beside control. The caption has to stay narrow or it widens the sidebar

        past its 290 px and the whole column gets clipped.
        """

        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        label = QLabel(caption)
        label.setObjectName("scenarioRowLabel")
        label.setFixedWidth(LABEL_WIDTH)
        line.addWidget(label)
        line.addWidget(widget, 1)
        layout.addWidget(holder)
        return holder, label

    def _stacked(self, layout: QVBoxLayout, caption: str, widget: QWidget) -> QWidget:
        """Caption above the control: three CJK segments do not fit beside a caption."""

        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        label = QLabel(caption)
        label.setObjectName("scenarioRowLabel")
        column.addWidget(label)
        column.addWidget(widget)
        layout.addWidget(holder)
        return holder

    def _build_sidebar(self) -> QScrollArea:
        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(10, 10, 10, 10)
        column.setSpacing(9)

        box, layout = self._group("计算方向")
        self._mode_segment = _Segment(
            ((SolveMode.FORWARD, "求道路极限"), (SolveMode.CHECK, "校核给定道路"))
        )
        self._mode_segment.changed.connect(self._mode_changed)
        layout.addWidget(self._mode_segment)
        self._mode_hint = QLabel()
        self._mode_hint.setObjectName("scenarioHint")
        self._mode_hint.setWordWrap(True)
        layout.addWidget(self._mode_hint)
        column.addWidget(box)

        box, layout = self._group("场景")
        grid = QGridLayout()
        grid.setSpacing(6)
        self._scenario_buttons: dict[Scenario, QToolButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, scenario in enumerate(Scenario):
            button = QToolButton()
            button.setObjectName("scenarioCard")
            button.setCheckable(True)
            button.setIcon(_scenario_icon(scenario))
            button.setText(f"{CARD_NAMES[scenario]}\n{SCENARIO_SUBTITLES[scenario]}")
            button.setToolTip(SCENARIO_NAMES[scenario])
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setMinimumHeight(64)
            button.clicked.connect(
                lambda _checked, key=scenario: self._scenario_changed(key)
            )
            group.addButton(button)
            grid.addWidget(button, index // 2, index % 2)
            self._scenario_buttons[scenario] = button
        layout.addLayout(grid)

        self._direction_segment = _Segment(((False, "单向"), (True, "双向")))
        self._direction_segment.changed.connect(self._direction_changed)
        self._stacked(layout, "通行方向", self._direction_segment)

        self._gear_segment = _Segment(
            ((Gear.DRIVE, "D档（前进）"), (Gear.REVERSE, "R档（倒车）"))
        )
        self._gear_segment.changed.connect(self._gear_changed)
        self._gear_row = self._stacked(layout, "档位", self._gear_segment)
        self._fixed_gear = QLabel("R档 → D档（本场景固定）")
        self._fixed_gear.setObjectName("scenarioFixedGear")
        self._fixed_gear_row = self._stacked(layout, "档位", self._fixed_gear)

        self._condition_segment = _Segment(((False, "道路中心线"), (True, "极限工况")))
        self._condition_segment.changed.connect(self._condition_changed)
        self._stacked(layout, "工况", self._condition_segment)
        self._condition_hint = QLabel(
            "极限工况允许车辆偏离道路中心线，求解器会顺带给出最优偏移。"
        )
        self._condition_hint.setObjectName("scenarioHint")
        self._condition_hint.setWordWrap(True)
        layout.addWidget(self._condition_hint)
        column.addWidget(box)

        box, layout = self._group("车辆参数")
        self._vehicle_spins = {
            "radius": _spin("radius", self._inputs.radius),
            "width": _spin("width", self._inputs.dimensions.width),
            "front": _spin("front", self._inputs.dimensions.center_front),
            "rear": _spin("rear", self._inputs.dimensions.center_rear),
            "threshold": _spin("threshold", self._inputs.threshold),
        }
        for key, caption in (
            ("radius", "转弯半径 R"),
            ("width", "车宽 W"),
            ("front", "中心前距 Lf"),
            ("rear", "中心后距 Lr"),
            ("threshold", "净距阈值"),
        ):
            spin = self._vehicle_spins[key]
            spin.valueChanged.connect(self._vehicle_changed)
            self._labelled(layout, caption, spin)
        note = QLabel(
            "坐标按叉车前轴中心解释。转弯半径默认 1.60 m 为经验值，请按车型核实。"
        )
        note.setObjectName("scenarioHint")
        note.setWordWrap(True)
        layout.addWidget(note)
        column.addWidget(box)

        box, layout = self._group("道路参数")
        self._road_spins: dict[str, QDoubleSpinBox] = {}
        self._road_rows: dict[str, QWidget] = {}
        self._road_captions: dict[str, QLabel] = {}
        for key in ("wa", "wb", "wv", "wh", "ls", "w", "b", "d"):
            spin = _spin(key, getattr(self._road, key))
            spin.valueChanged.connect(self._road_changed)
            self._road_spins[key] = spin
            holder, road_label = self._labelled(layout, key, spin)
            self._road_rows[key] = holder
            self._road_captions[key] = road_label
        self._road_note = QLabel("道路极限尺寸由求解给出，见右侧结果 →")
        self._road_note.setObjectName("scenarioHint")
        self._road_note.setWordWrap(True)
        layout.addWidget(self._road_note)
        column.addWidget(box)

        footnote = QLabel(FOOTNOTE)
        footnote.setObjectName("scenarioFootnote")
        footnote.setWordWrap(True)
        column.addWidget(footnote)
        column.addStretch(1)
        return self._scroller(inner, SIDEBAR_WIDTH, "scenarioSidebar")

    def _build_plan(self) -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel()
        self._title.setObjectName("scenarioTitle")
        header.addWidget(self._title)
        self._busy = QLabel("计算中…")
        self._busy.setObjectName("scenarioHint")
        self._busy.hide()
        header.addWidget(self._busy)
        header.addStretch(1)
        self._layer_buttons: dict[str, QToolButton] = {}
        for key, caption in (
            ("envelope", "扫掠包络"),
            ("dimensions", "尺寸标注"),
            ("grid", "网格 0.5 m"),
        ):
            button = QToolButton()
            button.setObjectName("scenarioSegment")
            button.setText(caption)
            button.setCheckable(True)
            button.setChecked(True)
            button.toggled.connect(self._layers_changed)
            self._layer_buttons[key] = button
            header.addWidget(button)
        column.addLayout(header)
        self.plan = PlanView()
        column.addWidget(self.plan, 1)
        return holder

    def _build_results(self) -> QScrollArea:
        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(10, 10, 10, 10)
        column.setSpacing(9)
        self._status_card = _Card("求解状态", bar=True)
        self._dimension_card = _Card("道路极限尺寸")
        self._detail_card = _Card("校核明细")
        self._offset_card = _Card("路径偏移")
        self._radius_card = _Card("四角转弯半径（几何精确值）")
        self._bottleneck_card = _Card("瓶颈", tone="quiet")
        for card in (
            self._status_card, self._dimension_card, self._detail_card,
            self._offset_card, self._radius_card, self._bottleneck_card,
        ):
            column.addWidget(card)
        self._notice = QLabel()
        self._notice.setObjectName("scenarioNotice")
        self._notice.setWordWrap(True)
        self._notice.hide()
        column.addWidget(self._notice)
        column.addStretch(1)
        pill = QLabel("离线速算 · 不写回数据")
        pill.setObjectName("clearancePill")
        pill.setAlignment(Qt.AlignmentFlag.AlignRight)
        column.addWidget(pill, 0, Qt.AlignmentFlag.AlignRight)
        return self._scroller(inner, RESULT_WIDTH, "scenarioResults")

    # ---------- 输入 ----------

    def set_vehicle_defaults(
        self, dimensions: VehicleDimensions, threshold: float
    ) -> None:
        """Seed from ``config.json``. Edits here stay in memory and never write back."""

        self._inputs = replace(
            self._inputs, dimensions=dimensions, threshold=threshold
        )
        for key, value in (
            ("width", dimensions.width),
            ("front", dimensions.center_front),
            ("rear", dimensions.center_rear),
            ("threshold", threshold),
        ):
            spin = self._vehicle_spins[key]
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._schedule()

    def select_variant(
        self,
        *,
        scenario: Scenario | None = None,
        mode: SolveMode | None = None,
        bidirectional: bool | None = None,
        gear: Gear | None = None,
        extreme: bool | None = None,
    ) -> None:
        """Set several controls at once and schedule a single recalculation."""

        self._inputs = replace(
            self._inputs,
            scenario=self._inputs.scenario if scenario is None else scenario,
            mode=self._inputs.mode if mode is None else mode,
            bidirectional=(
                self._inputs.bidirectional if bidirectional is None else bidirectional
            ),
            gear=self._inputs.gear if gear is None else gear,
            extreme=self._inputs.extreme if extreme is None else extreme,
        )
        self._sync_controls()
        self._schedule()

    def _mode_changed(self, mode: SolveMode) -> None:
        self._inputs = replace(self._inputs, mode=mode)
        self._sync_controls()
        self._schedule()

    def _scenario_changed(self, scenario: Scenario) -> None:
        self._inputs = replace(self._inputs, scenario=scenario)
        self._sync_controls()
        self._schedule()

    def _direction_changed(self, bidirectional: bool) -> None:
        self._inputs = replace(self._inputs, bidirectional=bidirectional)
        self._sync_controls()
        self._schedule()

    def _gear_changed(self, gear: Gear) -> None:
        self._inputs = replace(self._inputs, gear=gear)
        self._sync_controls()
        self._schedule()

    def _condition_changed(self, extreme: bool) -> None:
        self._inputs = replace(self._inputs, extreme=extreme)
        self._sync_controls()
        self._schedule()

    def _vehicle_changed(self, _value: float) -> None:
        self._inputs = replace(
            self._inputs,
            radius=self._vehicle_spins["radius"].value(),
            threshold=self._vehicle_spins["threshold"].value(),
            dimensions=VehicleDimensions(
                width=self._vehicle_spins["width"].value(),
                center_front=self._vehicle_spins["front"].value(),
                center_rear=self._vehicle_spins["rear"].value(),
            ),
        )
        self._sync_notice()
        self._schedule()

    def _road_changed(self, _value: float) -> None:
        self._road = RoadDimensions(
            **{key: spin.value() for key, spin in self._road_spins.items()}
        )
        self._schedule()

    def _layers_changed(self, _checked: bool) -> None:
        self.plan.set_layers(
            PlanLayers(**{key: button.isChecked() for key, button in self._layer_buttons.items()})
        )

    def _sync_controls(self) -> None:
        inputs = self._inputs
        self._mode_segment.select(inputs.mode)
        self._scenario_buttons[inputs.scenario].setChecked(True)
        self._direction_segment.select(inputs.bidirectional)
        self._gear_segment.select(inputs.effective_gear)
        self._condition_segment.select(inputs.extreme)
        self._gear_row.setVisible(not inputs.gear_is_fixed)
        self._fixed_gear_row.setVisible(inputs.gear_is_fixed)
        forward = inputs.mode is SolveMode.FORWARD
        self._mode_hint.setText(
            "只凭车辆参数求出道路的极限尺寸。"
            if forward
            else "再给出实际道路尺寸，判定通过 / 临界 / 越界。"
        )
        keys = ROAD_KEYS[inputs.scenario]
        hidden = 0
        for key, row in self._road_rows.items():
            shown = key in keys and (not forward or key in GIVEN_ONLY_KEYS)
            row.setVisible(shown)
            if shown:
                suffix = "（给定）" if forward else ""
                caption = self._road_captions[key]
                caption.setText(
                    dimension_label(inputs.scenario, key, bidirectional=inputs.bidirectional)
                    + suffix
                )
            elif key in keys:
                hidden += 1
        self._road_note.setVisible(bool(hidden))
        self._title.setText(variant_name(inputs))
        self._sync_notice()

    def _sync_notice(self) -> None:
        if self._inputs.radius_too_tight:
            self._notice.setText(
                "转弯半径已不大于半车宽加净距阈值，内侧几何自交，本页结果不可信。"
            )
            self._notice.show()
        else:
            self._notice.hide()

    # ---------- 求解 ----------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._pending:
            self._schedule()

    def _schedule(self) -> None:
        """Note the work while the tab is hidden; solve once someone is actually looking."""

        self._pending = True
        if not self.isVisible():
            return
        self._busy.show()
        self._timer.start()

    def _start_solve(self) -> None:
        self._pending = False
        self._generation += 1
        generation = self._generation
        inputs = self._inputs
        road = self._road
        worker: Worker[ScenarioResult] = Worker(lambda: solve_scenario(inputs, road))
        worker.signals.succeeded.connect(
            lambda result: self._apply(generation, result)
        )
        worker.signals.failed.connect(lambda message: self._fail(generation, message))
        worker.signals.finished.connect(lambda: self._workers.discard(worker))
        self._workers.add(worker)
        self._pool.start(worker)

    def _fail(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._busy.hide()
        self._status_card.set_title("求解失败")
        self._status_card.clear()
        self._status_card.set_note(message)

    def _apply(self, generation: int, result: ScenarioResult) -> None:
        if generation != self._generation:
            return
        self._busy.hide()
        self._result = result
        self.plan.set_result(result)
        self._fill_status(result)
        self._fill_dimensions(result)
        self._fill_detail(result)
        self._fill_offsets(result)
        self._fill_radii(result)
        self._bottleneck_card.clear()
        self._bottleneck_card.set_note(result.bottleneck)
        if result.inputs.mode is SolveMode.FORWARD and not result.infeasible:
            self._mirror_solved_dimensions(result)
        self.solved.emit(result)

    def _mirror_solved_dimensions(self, result: ScenarioResult) -> None:
        """Mirror the solved dimensions into the road inputs so switching to check carries on

        from the answer just given.
        """

        for key in SOLVED_KEYS[result.inputs.scenario]:
            spin = self._road_spins[key]
            spin.blockSignals(True)
            spin.setValue(getattr(result.dims, key))
            spin.blockSignals(False)
        self._road = replace(
            self._road,
            **{key: getattr(result.dims, key) for key in SOLVED_KEYS[result.inputs.scenario]},
        )

    # ---------- 结果卡 ----------

    def _fill_status(self, result: ScenarioResult) -> None:
        card = self._status_card
        card.clear()
        forward = result.inputs.mode is SolveMode.FORWARD
        if result.infeasible:
            card.set_title("求解状态" if forward else "判定")
            card.add_row("结果", "不可行", state="danger")
            card.set_state("danger")
            if result.radius_shortfall > 0 and result.required_lane_width is not None:
                card.set_note(
                    f"巷道宽 + 隔墙宽 装不下最小转弯半径，还差 "
                    f"{format_length(result.radius_shortfall)} m；巷道至少需 "
                    f"{format_length(result.required_lane_width)} m。"
                )
            elif result.threshold_ceiling is not None:
                card.set_note(
                    f"在搜索上界处最多只能达到 "
                    f"{format_length(result.threshold_ceiling)} m 净距，"
                    f"阈值降到这个值以下才有解。"
                )
            return
        word, state = STATUS_WORDS[result.status]
        card.set_title("求解状态" if forward else "判定")
        card.add_row("结果", "已求解" if forward else word, state=state)
        card.add_row("最小净距", f"{format_length(result.min_clearance)} m", state=state)
        card.set_state(state)
        card.set_note(
            NON_UNIQUE_NOTE
            if forward
            else f"相对阈值余量 {format_length(result.margin, signed=True)} m。"
        )

    def _fill_dimensions(self, result: ScenarioResult) -> None:
        card = self._dimension_card
        card.clear()
        forward = result.inputs.mode is SolveMode.FORWARD
        card.set_title("一组最小可行尺寸（非唯一）" if forward else "给定道路尺寸")
        solved = set(SOLVED_KEYS[result.inputs.scenario]) if forward else set()
        for key in ROAD_KEYS[result.inputs.scenario]:
            card.add_row(
                dimension_label(
                    result.inputs.scenario, key, bidirectional=result.inputs.bidirectional
                ),
                f"{format_length(getattr(result.dims, key))} m",
                kind="solved" if key in solved else "",
            )
        if result.turn_radius is not None:
            card.add_row("实际掉头半径 rs", f"{format_length(result.turn_radius)} m")
        card.set_note("")

    def _fill_detail(self, result: ScenarioResult) -> None:
        card = self._detail_card
        card.clear()
        if result.inputs.mode is not SolveMode.CHECK or result.infeasible:
            card.hide()
            return
        card.show()
        _, state = STATUS_WORDS[result.status]
        card.add_row("最小净距", f"{format_length(result.min_clearance)} m", state=state)
        card.add_row("净距阈值", f"{format_length(result.inputs.threshold)} m")
        card.add_row("相对阈值余量", f"{format_length(result.margin, signed=True)} m")
        if result.centred_clearance is not None:
            card.add_row(
                "居中直行时最小净距", f"{format_length(result.centred_clearance)} m"
            )

    def _fill_offsets(self, result: ScenarioResult) -> None:
        card = self._offset_card
        card.clear()
        if result.infeasible or not result.inputs.optimises_offsets:
            card.hide()
            return
        card.show()
        bands = {band.key: band for band in result.bands}
        free = set(_offset_keys(result))
        for key in ("ea", "eb", "ev", "eh", "a", "so", "e1", "e2", "eo"):
            if key not in free:
                continue
            card.add_row(
                OFFSET_LABELS[key],
                f"{format_length(getattr(result.offsets, key), signed=True)} m",
                kind="offset",
            )
            band = bands.get(key)
            if band is not None:
                if band.low is None or band.high is None:
                    card.add_row("　可行区间", "无可行值", state="danger")
                else:
                    card.add_row(
                        "　可行区间",
                        f"{format_length(band.low, signed=True)}…"
                        f"{format_length(band.high, signed=True)} m",
                    )
        if result.inputs.scenario is Scenario.UTURN:
            card.add_row(
                OFFSET_LABELS["yc"],
                f"{format_length(result.offsets.yc, signed=True)} m",
                kind="offset",
            )
        card.set_note("弯道内侧为正、外侧为负。")

    def _fill_radii(self, result: ScenarioResult) -> None:
        card = self._radius_card
        card.clear()
        for kind in CornerRadiusKind:
            card.add_row(
                CORNER_LABELS[kind], f"{result.corner_radii[kind]:.3f} m"
            )

    @property
    def result(self) -> ScenarioResult | None:
        return self._result

    @property
    def vehicle_inputs(self) -> ScenarioInputs:
        return self._inputs


def _offset_keys(result: ScenarioResult) -> tuple[str, ...]:
    """Which lateral offsets were free for this result; ``yc`` gets its own row."""

    return tuple(
        spec.key
        for spec in offset_specs(result.inputs, result.dims)
        if spec.key != "yc"
    )
