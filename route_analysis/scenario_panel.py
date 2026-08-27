"""The rapid-estimate tab: offline limiting-case geometry in three columns.

Parameters on the left, plan view in the middle, readings on the right. Nothing here
touches the scheduler or ``data/``, so the page works with no command selected.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QThreadPool, QTimer, Signal
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
    QPushButton,
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
    SCENARIO_NAMES,
    SCENARIO_SUBTITLES,
    Condition,
    Gear,
    Offsets,
    Pins,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    dimension_label,
    offset_rows,
    variant_name,
)
from route_analysis.scenario_graphics import (
    SECTION_CAPTIONS,
    BodySection,
    PlanLayers,
    paint_scenario_plan,
)
from route_analysis.scenario_solver import (
    ManeuverTrace,
    ScenarioResult,
    solve_scenario,
    trace_maneuvers,
)
from route_analysis.turn_radius import CornerRadiusKind
from route_analysis.workers import Worker

SIDEBAR_WIDTH = 290
RESULT_WIDTH = 314
LABEL_WIDTH = 96
"""Eight CJK characters at 12 px: room for 隔墙宽（给定）, the longest caption listed."""
DEBOUNCE_MS = 150
RUN_FRAME_MS = 33
RUN_CAPTION = "模拟运行"
RUN_STOP_CAPTION = "停止"
RUN_SECONDS = 3.4
"""How long one run-through takes end to end, whatever the path length.

A fixed duration rather than a fixed speed: the point is to read the order the legs
happen in and where the body swings widest, and a short maneuver crawling through in
the same seconds as a long one makes that easier to follow, not harder.
"""

ROAD_KEYS: dict[Scenario, tuple[str, ...]] = {
    Scenario.CORNER: ("wa", "wb"),
    Scenario.CROSSBACK: ("wv", "wh", "ls"),
    Scenario.STUBBACK: ("wh", "wv"),
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
"""Always supplied, never solved: the divider is site fabric, not an unknown."""

MIDDLE_ROW = "_middle"
"""Stands in for the two-way U-turn middle aisle: no variable behind it, but listed so the

layout reads complete.
"""
OFFSET_ROW_KEYS = ("ea", "eb", "ev", "eh", "a", "so", "e1", "e2", MIDDLE_ROW, "eo", "yc")
"""One spin row per offset the layouts between them can list, in ``offset_rows`` order."""
SHARED_CAPTION = "共用"

PIN_CAPTION = "固定"
PIN_WIDTH = 40
SPIN_FLOOR = 72
"""Pin toggle and shared tag share one width so the spin column lines up."""
PIN_TOOLTIP = (
    "固定：这一项按你给定的值，求解器不再改动；取消固定即交回求解器。\n改动数值会自动固定。"
)
CENTRELINE_HINT = "车辆沿道路中心线行驶，只凭车辆参数求出道路的极限尺寸。"
PARETO_HINT = (
    "车辆可偏离道路中心线。改过的尺寸或偏移即固定，其余由求解器压到极限并回填。"
)
CENTRELINE_ROAD_NOTE = "道路极限尺寸由求解给出，见右侧结果 →"
PARETO_ROAD_NOTE = (
    "未固定的项由求解器给出并回填；改动即固定，点「固定」可取消。"
    "双向布局里共用腿的偏移恒为 0。"
)

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
    "offset": (-15.00, 15.00, 0.05, 2),
    "yc": (-10.00, 60.00, 0.05, 2),
}
"""Offsets are deliberately not clamped to what the road can hold: an operator may type a

value the leg cannot take, and the result then says so instead of the control refusing it.
"""

FOOTNOTE = (
    "偏移符号：弯道内侧为正、外侧为负。\n"
    "本页为纯几何速算，不连接调度后端，不写回任何数据。"
)
NON_UNIQUE_NOTE = (
    "各尺寸均已不可单独缩小；改变求解顺序会得到前沿上的另一组解。"
)


class _WheelGuard(QObject):
    """Wheel turns only the spin box that has focus.

    Scrolling the sidebar with the wheel passes over the road inputs, and a spin box
    under the pointer would take the wheel as an edit -- which now pins the row. The
    guard hands the wheel back to the scroll area unless the operator has clicked into
    the box first.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() is QEvent.Type.Wheel
            and isinstance(watched, QWidget)
            and not watched.hasFocus()
        ):
            event.ignore()
            return True
        return super().eventFilter(watched, event)


_WHEEL_GUARD: _WheelGuard | None = None
"""Created with the first spin box, once a QApplication exists."""


def _wheel_guard() -> _WheelGuard:
    global _WHEEL_GUARD
    if _WHEEL_GUARD is None:
        _WHEEL_GUARD = _WheelGuard()
    return _WHEEL_GUARD


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
    spin.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    spin.installEventFilter(_wheel_guard())
    spin.setValue(value)
    return spin


def _compact(spin: QDoubleSpinBox) -> QDoubleSpinBox:
    """Let a road-row spin give up width to the pin column.

    A spin box asks for the width of its widest legal value; with the pin toggle beside it
    that request no longer fits the 290 px sidebar and the whole column gets clipped. The
    values these rows actually show are a few metres, so the row keeps a floor well below
    the hint and lets the layout hand the spin whatever is left.
    """

    spin.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    spin.setMinimumWidth(SPIN_FLOOR)
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
        self._traces: tuple[ManeuverTrace, ...] = ()
        self._playhead: float | None = None

    def set_result(self, result: ScenarioResult | None) -> None:
        self._result = result
        self._traces = (
            trace_maneuvers(result.layout, result.inputs.dimensions)
            if result is not None
            else ()
        )
        self.update()

    def set_layers(self, layers: PlanLayers) -> None:
        self._layers = layers
        self.update()

    def set_playhead(self, progress: float | None) -> None:
        """Where the run-through has got to, or ``None`` when it is not running."""

        self._playhead = progress
        self.update()

    @property
    def pose_count(self) -> int:
        return max((len(trace.x) for trace in self._traces), default=0)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self._result is None:
            return
        painter = QPainter(self)
        paint_scenario_plan(
            painter,
            QRectF(self.rect()).adjusted(1, 1, -1, -1),
            self._result,
            self._layers,
            traces=self._traces,
            playhead=self._playhead,
        )
        painter.end()


class ScenarioPanel(QWidget):
    """The rapid-estimate tab."""

    solved = Signal(object)
    export_pdf_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._inputs = ScenarioInputs()
        self._road = RoadDimensions()
        self._offsets = Offsets()
        self._pins = Pins()
        self._result: ScenarioResult | None = None
        self._generation = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._workers: set[Worker[ScenarioResult]] = set()
        self._section = BodySection.WHOLE
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(RUN_FRAME_MS)
        self._run_timer.timeout.connect(self._advance_run)
        self._run_progress = 0.0
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
    def _scroller(
        inner: QWidget, width: int, name: str, *, takes_focus: bool = True
    ) -> QScrollArea:
        inner.setObjectName(name)
        area = QScrollArea()
        area.setWidget(inner)
        area.setWidgetResizable(True)
        area.setFixedWidth(width)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if not takes_focus:
            # A click on a button that declines focus is offered to its ancestors, and
            # the scroll area would take it -- pulling focus off a spin box mid-edit,
            # committing the text and auto-pinning the row before the pin toggle itself
            # runs. Wheel and scrollbar scrolling need no focus. The result column keeps
            # focus so the keyboard can still scroll it.
            area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self,
        layout: QVBoxLayout,
        caption: str,
        widget: QWidget,
        trailing: QWidget | None = None,
    ) -> tuple[QWidget, QLabel]:
        """Label beside control. The caption has to stay narrow or it widens the sidebar

        past its 290 px and the whole column gets clipped. ``trailing`` is the pin toggle
        the road rows carry.
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
        if trailing is not None:
            line.addWidget(trailing)
        layout.addWidget(holder)
        return holder, label

    @staticmethod
    def _pin_button() -> QToolButton:
        button = QToolButton()
        button.setObjectName("scenarioPin")
        button.setText(PIN_CAPTION)
        button.setToolTip(PIN_TOOLTIP)
        button.setCheckable(True)
        button.setFixedWidth(PIN_WIDTH)
        # Tab reaches it; a mouse click must not. Taking focus from the spin would commit
        # the text being typed before the toggle runs, auto-pinning the row, and the same
        # click's toggle would then release it again. The toggle commits the text itself.
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return button

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
        self._condition_segment = _Segment(
            ((Condition.CENTRELINE, "道路中心线"), (Condition.PARETO, "帕累托极限"))
        )
        self._condition_segment.changed.connect(self._condition_changed)
        layout.addWidget(self._condition_segment)
        self._condition_hint = QLabel()
        self._condition_hint.setObjectName("scenarioHint")
        self._condition_hint.setWordWrap(True)
        layout.addWidget(self._condition_hint)
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
            if key == "rear":
                # Sits under the two dimensions it splits on, not up in the plan header:
                # three CJK segments alongside the run button and the layer toggles left
                # every caption elided to an ellipsis and ate the title as well.
                self._section_segment = _Segment(
                    tuple((section, SECTION_CAPTIONS[section]) for section in BodySection)
                )
                self._section_segment.changed.connect(self._section_changed)
                self._section_segment.select(BodySection.WHOLE)
                self._stacked(layout, "扫掠包络分段", self._section_segment)
        note = QLabel(
            "坐标按叉车前轴中心解释。转弯半径默认 1.20 m 为经验值，请按车型核实。"
        )
        note.setObjectName("scenarioHint")
        note.setWordWrap(True)
        layout.addWidget(note)
        column.addWidget(box)

        box, layout = self._group("道路参数")
        self._road_spins: dict[str, QDoubleSpinBox] = {}
        self._road_rows: dict[str, QWidget] = {}
        self._road_captions: dict[str, QLabel] = {}
        self._road_pins: dict[str, QToolButton] = {}
        for key in ("wa", "wb", "wv", "wh", "ls", "w", "b", "d"):
            spin = _compact(_spin(key, getattr(self._road, key)))
            spin.valueChanged.connect(lambda _value, key=key: self._road_changed(key))
            self._road_spins[key] = spin
            pin = self._pin_button()
            pin.toggled.connect(lambda checked, key=key: self._dim_pin_toggled(key, checked))
            self._road_pins[key] = pin
            holder, road_label = self._labelled(layout, key, spin, pin)
            self._road_rows[key] = holder
            self._road_captions[key] = road_label
        self._offset_spins: dict[str, QDoubleSpinBox] = {}
        self._offset_holders: dict[str, QWidget] = {}
        self._offset_captions: dict[str, QLabel] = {}
        self._offset_pins: dict[str, QToolButton] = {}
        self._offset_shared: dict[str, QLabel] = {}
        for key in OFFSET_ROW_KEYS:
            spin = _compact(_spin("yc" if key == "yc" else "offset", 0.0))
            spin.valueChanged.connect(lambda _value, key=key: self._offset_changed(key))
            self._offset_spins[key] = spin
            pin = self._pin_button()
            pin.toggled.connect(
                lambda checked, key=key: self._offset_pin_toggled(key, checked)
            )
            self._offset_pins[key] = pin
            shared = QLabel(SHARED_CAPTION)
            shared.setObjectName("scenarioSharedTag")
            shared.setToolTip("两条镜像机动共用这条腿，偏移固定为 0。")
            shared.setFixedWidth(PIN_WIDTH)
            shared.setAlignment(Qt.AlignmentFlag.AlignCenter)
            shared.hide()
            self._offset_shared[key] = shared
            trailing = QWidget()
            stack = QHBoxLayout(trailing)
            stack.setContentsMargins(0, 0, 0, 0)
            stack.setSpacing(0)
            stack.addWidget(pin)
            stack.addWidget(shared)
            holder, offset_label = self._labelled(layout, key, spin, trailing)
            self._offset_holders[key] = holder
            self._offset_captions[key] = offset_label
        self._road_note = QLabel(CENTRELINE_ROAD_NOTE)
        self._road_note.setObjectName("scenarioHint")
        self._road_note.setWordWrap(True)
        layout.addWidget(self._road_note)
        column.addWidget(box)

        footnote = QLabel(FOOTNOTE)
        footnote.setObjectName("scenarioFootnote")
        footnote.setWordWrap(True)
        column.addWidget(footnote)
        column.addStretch(1)
        return self._scroller(inner, SIDEBAR_WIDTH, "scenarioSidebar", takes_focus=False)

    def _build_plan(self) -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(8)
        # Title on its own row. Variant names run to about 350 px and the controls want
        # another 290; sharing one row left both elided, the buttons down to a bare "…".
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._title = QLabel()
        self._title.setObjectName("scenarioTitle")
        self._title.setWordWrap(True)
        title_row.addWidget(self._title, 1)
        self._busy = QLabel("计算中…")
        self._busy.setObjectName("scenarioHint")
        self._busy.hide()
        title_row.addWidget(self._busy)
        column.addLayout(title_row)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addStretch(1)
        self._run_button = QToolButton()
        self._run_button.setObjectName("scenarioSegment")
        self._run_button.setText(RUN_CAPTION)
        self._run_button.setToolTip("车体包络自起点沿路径跑到终点")
        self._run_button.clicked.connect(self._toggle_run)
        header.addWidget(self._run_button)
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
        self._offset_card = _Card("路径偏移")
        self._radius_card = _Card("四角转弯半径（几何精确值）")
        self._bottleneck_card = _Card("瓶颈", tone="quiet")
        for card in (
            self._status_card, self._dimension_card,
            self._offset_card, self._radius_card, self._bottleneck_card,
        ):
            column.addWidget(card)
        self._notice = QLabel()
        self._notice.setObjectName("scenarioNotice")
        self._notice.setWordWrap(True)
        self._notice.hide()
        column.addWidget(self._notice)
        self._export_button = QPushButton("导出 PDF 报告")
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self.export_pdf_requested)
        column.addWidget(self._export_button)
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
        condition: Condition | None = None,
        bidirectional: bool | None = None,
        gear: Gear | None = None,
    ) -> None:
        """Set several controls at once and schedule a single recalculation."""

        self._relayout(
            replace(
                self._inputs,
                scenario=self._inputs.scenario if scenario is None else scenario,
                condition=self._inputs.condition if condition is None else condition,
                bidirectional=(
                    self._inputs.bidirectional if bidirectional is None else bidirectional
                ),
                gear=self._inputs.gear if gear is None else gear,
            )
        )

    def _relayout(self, inputs: ScenarioInputs) -> None:
        """A different layout or condition: every pin is released.

        What was pinned belonged to the road that is going away, so the new one starts
        from the solver's answer again. Vehicle edits keep the pins -- see
        ``_vehicle_changed``.
        """

        if inputs == self._inputs:
            return
        self._inputs = inputs
        self._pins = Pins()
        self._offsets = Offsets()
        self._sync_controls()
        self._schedule()

    def _condition_changed(self, condition: Condition) -> None:
        self._relayout(replace(self._inputs, condition=condition))

    def _scenario_changed(self, scenario: Scenario) -> None:
        self._relayout(replace(self._inputs, scenario=scenario))

    def _direction_changed(self, bidirectional: bool) -> None:
        self._relayout(replace(self._inputs, bidirectional=bidirectional))

    def _gear_changed(self, gear: Gear) -> None:
        self._relayout(replace(self._inputs, gear=gear))

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

    def _road_changed(self, key: str) -> None:
        """An edited dimension is the operator's from now on, so it pins itself."""

        self._road = self._road.with_value(key, self._road_spins[key].value())
        if self._inputs.pareto and key not in GIVEN_ONLY_KEYS:
            self._pins = self._pins.with_dim(key, True)
            self._sync_pins()
        self._schedule()

    def _offset_changed(self, key: str) -> None:
        if key == MIDDLE_ROW or not self._inputs.pareto:
            return
        self._offsets = self._offsets.with_value(key, self._offset_spins[key].value())
        self._pins = self._pins.with_offset(key, True)
        self._sync_pins()
        self._schedule()

    def _dim_pin_toggled(self, key: str, checked: bool) -> None:
        """Pinning through the toggle freezes the value on screen, whatever it came from."""

        spin = self._road_spins[key]
        if checked:
            # Text typed but not yet committed is what the operator means to pin.
            spin.interpretText()
        else:
            # Releasing hands the row back to the solver; text left half-typed would
            # commit -- and pin again -- the moment focus moved on.
            spin.setValue(spin.value())
        if (key in self._pins.dims) == checked:
            return
        if checked:
            self._road = self._road.with_value(key, self._road_spins[key].value())
        self._pins = self._pins.with_dim(key, checked)
        self._sync_pins()
        self._schedule()

    def _offset_pin_toggled(self, key: str, checked: bool) -> None:
        if key == MIDDLE_ROW:
            return
        spin = self._offset_spins[key]
        if checked:
            spin.interpretText()
        else:
            spin.setValue(spin.value())
        if (key in self._pins.offsets) == checked:
            return
        if checked:
            self._offsets = self._offsets.with_value(key, self._offset_spins[key].value())
        self._pins = self._pins.with_offset(key, checked)
        self._sync_pins()
        self._schedule()

    def _layers_changed(self, _checked: bool) -> None:
        self.plan.set_layers(self.plan_layers)

    @property
    def plan_layers(self) -> PlanLayers:
        """The toggles as drawn right now; the PDF export reads them so the page matches."""

        return PlanLayers(
            **{key: button.isChecked() for key, button in self._layer_buttons.items()},
            section=self._section,
        )

    def _section_changed(self, section: BodySection) -> None:
        self._section = section
        self._layers_changed(True)

    def _toggle_run(self) -> None:
        if self._run_timer.isActive():
            self._stop_run()
            return
        if self.plan.pose_count < 2:
            return
        self._run_progress = 0.0
        self.plan.set_playhead(0.0)
        self._run_button.setText(RUN_STOP_CAPTION)
        self._run_timer.start()

    def _stop_run(self, *, clear: bool = True) -> None:
        """Stop the run. ``clear`` removes the body; a completed run leaves it at the end.

        Reaching the terminus and vanishing reads as a glitch, and where the maneuver puts
        the truck at the end is worth seeing. Interrupting one is different: the body would
        be resting somewhere arbitrary, so that clears.
        """

        self._run_timer.stop()
        self._run_button.setText(RUN_CAPTION)
        if clear:
            self.plan.set_playhead(None)

    def _advance_run(self) -> None:
        self._run_progress += RUN_FRAME_MS / (RUN_SECONDS * 1000.0)
        if self._run_progress >= 1.0:
            self._run_progress = 1.0
            self.plan.set_playhead(1.0)
            self._stop_run(clear=False)
            return
        self.plan.set_playhead(self._run_progress)

    def _sync_controls(self) -> None:
        inputs = self._inputs
        self._condition_segment.select(inputs.condition)
        self._scenario_buttons[inputs.scenario].setChecked(True)
        self._direction_segment.select(inputs.bidirectional)
        self._gear_segment.select(inputs.effective_gear)
        self._gear_row.setVisible(not inputs.gear_is_fixed)
        self._fixed_gear_row.setVisible(inputs.gear_is_fixed)
        pareto = inputs.pareto
        self._condition_hint.setText(PARETO_HINT if pareto else CENTRELINE_HINT)
        keys = ROAD_KEYS[inputs.scenario]
        for key, holder in self._road_rows.items():
            given_only = key in GIVEN_ONLY_KEYS
            shown = key in keys and (pareto or given_only)
            holder.setVisible(shown)
            if not shown:
                continue
            self._road_captions[key].setText(
                dimension_label(inputs.scenario, key, bidirectional=inputs.bidirectional)
                + ("（给定）" if given_only else "")
            )
            self._road_pins[key].setVisible(pareto and not given_only)
        listed = (
            {row.key or MIDDLE_ROW: row for row in
             offset_rows(inputs.scenario, bidirectional=inputs.bidirectional)}
            if pareto
            else {}
        )
        for key, holder in self._offset_holders.items():
            listed_row = listed.get(key)
            holder.setVisible(listed_row is not None)
            if listed_row is None:
                continue
            spin = self._offset_spins[key]
            self._offset_captions[key].setText(listed_row.label)
            spin.setEnabled(not listed_row.shared)
            self._offset_pins[key].setVisible(not listed_row.shared)
            self._offset_shared[key].setVisible(listed_row.shared)
            if listed_row.shared:
                spin.blockSignals(True)
                spin.setValue(0.0)
                spin.blockSignals(False)
                if key != MIDDLE_ROW:
                    self._offsets = self._offsets.with_value(key, 0.0)
        self._road_note.setText(PARETO_ROAD_NOTE if pareto else CENTRELINE_ROAD_NOTE)
        self._road_note.setVisible(pareto or len(keys) > len(set(keys) & GIVEN_ONLY_KEYS))
        self._sync_pins()
        self._title.setText(variant_name(inputs))
        self._sync_notice()

    def _sync_pins(self) -> None:
        """Pin toggles follow ``self._pins``; the toggles never own the state."""

        for key, pin in self._road_pins.items():
            pin.blockSignals(True)
            pin.setChecked(key in self._pins.dims)
            pin.blockSignals(False)
        for key, pin in self._offset_pins.items():
            pin.blockSignals(True)
            pin.setChecked(key in self._pins.offsets)
            pin.blockSignals(False)

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
        """Note the work while the tab is hidden; solve once someone is actually looking.

        The generation moves here, not only when the solve starts: a result still in
        flight was computed from inputs that are now stale, and letting it land would
        write the solver's values over what the operator just typed and pinned.
        """

        self._generation += 1
        self._pending = True
        if not self.isVisible():
            return
        self._busy.show()
        self._timer.start()

    def _start_solve(self) -> None:
        # The path is about to be rebuilt, so a run-through part way along it is stale.
        self._stop_run()
        self._pending = False
        self._generation += 1
        generation = self._generation
        inputs = self._inputs
        road = self._road
        offsets = self._offsets
        pins = self._pins
        worker: Worker[ScenarioResult] = Worker(
            lambda: solve_scenario(inputs, road, offsets, pins)
        )
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
        self._export_button.setEnabled(True)
        self.plan.set_result(result)
        self._fill_status(result)
        self._fill_dimensions(result)
        self._fill_offsets(result)
        self._fill_radii(result)
        self._bottleneck_card.clear()
        self._bottleneck_card.set_note(result.bottleneck)
        if not result.infeasible:
            self._mirror_solution(result)
        self.solved.emit(result)

    def _mirror_solution(self, result: ScenarioResult) -> None:
        """Write what the solver chose back into the unpinned inputs.

        The rows the operator has not taken over show the solver's answer, so the next edit
        starts from it and pinning a row freezes the value that is actually on screen.
        Pinned rows are left alone: they already hold the operator's value.
        """

        for key in result.solved_keys:
            if key in self._pins.dims:
                continue
            self._road = self._road.with_value(key, getattr(result.dims, key))
            self._refill(self._road_spins[key], getattr(result.dims, key))
        for row in offset_rows(result.inputs.scenario, bidirectional=result.inputs.bidirectional):
            if row.key is None or row.shared or row.key in self._pins.offsets:
                continue
            value = getattr(result.offsets, row.key)
            self._offsets = self._offsets.with_value(row.key, value)
            self._refill(self._offset_spins[row.key], value)

    @staticmethod
    def _refill(spin: QDoubleSpinBox, value: float) -> None:
        """Show a solver value without firing the edit path, and never over a live edit.

        Keyboard tracking is off, so text being typed has not reached ``value()`` yet; a
        refill would throw it away mid-word. Focus alone is not typing: a spin that merely
        holds the cursor still takes the refill, or it would sit on a stale value.
        """

        if spin.hasFocus() and spin.lineEdit().isModified():
            return
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

    # ---------- 结果卡 ----------

    def _fill_status(self, result: ScenarioResult) -> None:
        fill_status(self._status_card, result)

    def _fill_dimensions(self, result: ScenarioResult) -> None:
        fill_dimensions(self._dimension_card, result)

    def _fill_offsets(self, result: ScenarioResult) -> None:
        fill_offsets(self._offset_card, result)

    def _fill_radii(self, result: ScenarioResult) -> None:
        fill_radii(self._radius_card, result)

    @property
    def result(self) -> ScenarioResult | None:
        return self._result

    @property
    def vehicle_inputs(self) -> ScenarioInputs:
        return self._inputs


# ---------- 结果卡填充, 界面与 PDF 报告共用 ----------

def fill_status(card: _Card, result: ScenarioResult) -> None:
    card.clear()
    if result.infeasible:
        card.set_title("求解状态")
        card.add_row("结果", "不可行", state="danger")
        card.set_state("danger")
        if result.radius_shortfall > 0 and result.required_lane_width is not None:
            card.set_note(
                f"巷道宽 + 隔墙宽 装不下最小转弯半径，还差 "
                f"{format_length(result.radius_shortfall)} m；巷道至少需 "
                f"{format_length(result.required_lane_width)} m。"
            )
        elif result.pins.dims or result.pins.offsets:
            card.set_note(
                "在当前固定的尺寸与偏移下找不到可行解；放宽或取消部分固定再试。"
            )
        elif result.threshold_ceiling is not None:
            card.set_note(
                f"在搜索上界处最多只能达到 "
                f"{format_length(result.threshold_ceiling)} m 净距，"
                f"阈值降到这个值以下才有解。"
            )
        return
    word, state = STATUS_WORDS[result.status]
    fits = result.solved and result.status is ClearanceStatus.SAFE
    card.set_title("求解状态" if result.solved else "判定")
    card.add_row("结果", "已求解" if fits else word, state=state)
    card.add_row("最小净距", f"{format_length(result.min_clearance)} m", state=state)
    card.add_row("相对阈值余量", f"{format_length(result.margin, signed=True)} m")
    card.set_state(state)
    pinned = result.pins.dims or result.pins.offsets
    if result.solved and not fits:
        card.set_note("求解停在阈值以下：放宽或取消部分固定，或降低净距阈值。")
    elif result.solved:
        card.set_note(NON_UNIQUE_NOTE + (" 已固定的项按给定值。" if pinned else ""))
    elif result.inputs.pareto:
        card.set_note("全部尺寸已固定，这是对给定道路的判定。")
    else:
        card.set_note("")


def fill_dimensions(card: _Card, result: ScenarioResult) -> None:
    card.clear()
    if result.solved:
        card.set_title("一组最小可行尺寸（非唯一）")
    elif result.infeasible:
        card.set_title("道路尺寸（未求解）")
    else:
        card.set_title("给定道路尺寸")
    solved = set(result.solved_keys)
    for key in ROAD_KEYS[result.inputs.scenario]:
        if key in result.pins.dims:
            suffix = "（固定）"
        elif key in GIVEN_ONLY_KEYS:
            suffix = "（给定）"
        else:
            suffix = ""
        card.add_row(
            dimension_label(
                result.inputs.scenario, key, bidirectional=result.inputs.bidirectional
            )
            + suffix,
            f"{format_length(getattr(result.dims, key))} m",
            kind="solved" if key in solved else "",
        )
    if result.turn_radius is not None:
        card.add_row("实际掉头半径 rs", f"{format_length(result.turn_radius)} m")
    if result.trunk_reach is not None:
        card.add_row("出弯主路深度", f"{format_length(result.trunk_reach)} m")
        card.set_note(
            "出弯主路深度自支路开口的外侧壁量起，是倒车摆出占用的那一段主路，"
            "由扫掠结果量得而非求解得出 —— 主路本身继续延伸，两端都不是墙。"
        )
        return
    card.set_note("")


def fill_offsets(card: _Card, result: ScenarioResult) -> None:
    card.clear()
    if result.infeasible or not result.inputs.optimises_offsets:
        card.hide()
        return
    card.show()
    bands = {band.key: band for band in result.bands}
    pareto = result.inputs.pareto
    for row in offset_rows(result.inputs.scenario, bidirectional=result.inputs.bidirectional):
        if row.key is None or row.shared or (row.key != "yc" and not pareto):
            continue
        pinned = row.key in result.pins.offsets
        card.add_row(
            row.label + ("（固定）" if pinned else ""),
            f"{format_length(getattr(result.offsets, row.key), signed=True)} m",
            kind="offset",
        )
        band = bands.get(row.key)
        if band is not None:
            if band.low is None or band.high is None:
                card.add_row("　可行区间", "无可行值", state="danger")
            else:
                card.add_row(
                    "　可行区间",
                    f"{format_length(band.low, signed=True)}…"
                    f"{format_length(band.high, signed=True)} m",
                )
    note = "弯道内侧为正、外侧为负。"
    if result.centred_clearance is not None:
        note += f" 全部偏移归零时最小净距 {format_length(result.centred_clearance)} m。"
    card.set_note(note)


def fill_radii(card: _Card, result: ScenarioResult) -> None:
    card.clear()
    for kind in CornerRadiusKind:
        card.add_row(
            CORNER_LABELS[kind], f"{result.corner_radii[kind]:.3f} m"
        )


def build_result_cards(result: ScenarioResult) -> list[QWidget]:
    """The same result cards the view fills, freshly built for the report page.

    Cards a state hides on screen (the offsets card when nothing optimises) are left
    out here too, so the page shows exactly what the tab would.
    """

    status = _Card("求解状态", bar=True)
    fill_status(status, result)
    dimensions = _Card("道路极限尺寸")
    fill_dimensions(dimensions, result)
    radii = _Card("四角转弯半径（几何精确值）")
    fill_radii(radii, result)
    bottleneck = _Card("瓶颈", tone="quiet")
    bottleneck.set_note(result.bottleneck)
    cards: list[QWidget] = [status, dimensions]
    # Same condition the view uses to show or hide this card; a freshly built widget
    # reads as hidden either way, so the state cannot be read off the card itself.
    if result.inputs.optimises_offsets and not result.infeasible:
        offsets = _Card("路径偏移")
        fill_offsets(offsets, result)
        cards.append(offsets)
    cards += [radii, bottleneck]
    for card in cards:
        card.setVisible(True)
    return cards
