"""通行余量 view: one screen answering where the path is tightest and whether offset saves it."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from route_analysis import theme
from route_analysis.clearance_graphics import (
    AXIS_LEFT,
    AXIS_RIGHT,
    format_length,
    paint_clearance_chart,
    paint_width_ruler,
)
from route_analysis.clearance_solver import (
    Bottleneck,
    ClearanceAnalysis,
    LaneContext,
    SegmentRole,
    WidthZones,
    solve_width_zones,
)
from route_analysis.corner_solver_view import CornerSolverView
from route_analysis.models import (
    AnalysisSettings,
    ClearanceStatus,
    PosePoint,
    VehicleDimensions,
)
from route_analysis.workers import Worker

RULER_HEIGHT = 78
ADVICE_WIDTH = 400
PILL_TEXT = "只建议 · 不修改"


def _restyle(widget: QWidget, name: str, value: str) -> None:
    """Set a stylesheet property and re-polish, so QSS state selectors take effect."""

    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _state_for(value: float | None, threshold: float) -> str:
    if value is None:
        return "neutral"
    if value < 0:
        return "danger"
    return "warning" if value < threshold else "success"


@dataclass(frozen=True, slots=True)
class ClearanceInputs:
    """Everything the view needs besides the analysis itself."""

    poses: tuple[PosePoint, ...]
    dimensions: VehicleDimensions
    settings: AnalysisSettings
    context: LaneContext
    metadata: dict[str, str]


class MetricCard(QFrame):
    """One of the five summary readings across the top of the view."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setObjectName("clearanceCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._bar = QFrame()
        self._bar.setObjectName("clearanceCardBar")
        self._bar.setFixedWidth(3)
        row.addWidget(self._bar)
        body = QVBoxLayout()
        body.setContentsMargins(10, 10, 12, 10)
        body.setSpacing(2)
        self._caption = QLabel(caption)
        self._caption.setObjectName("clearanceCardLabel")
        self._value = QLabel("—")
        self._value.setObjectName("clearanceCardValue")
        self._unit = QLabel("")
        self._unit.setObjectName("clearanceCardUnit")
        body.addWidget(self._caption)
        body.addWidget(self._value)
        body.addWidget(self._unit)
        row.addLayout(body, 1)

    def show_value(self, value: str, unit: str = "", state: str = "neutral") -> None:
        self._value.setText(value)
        self._unit.setText(unit)
        _restyle(self, "state", state)
        _restyle(self._value, "state", state)
        _restyle(self._bar, "state", state)


class ClearanceChartView(QWidget):
    """The shared-axis clearance profile and offset band, drawn by clearance_graphics."""

    progress_clicked = Signal(float)
    segment_clicked = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._analysis: ClearanceAnalysis | None = None
        self._highlight: int | None = None

    def set_analysis(self, analysis: ClearanceAnalysis | None) -> None:
        self._analysis = analysis
        self._highlight = None
        self.update()

    def set_highlight(self, segment_index: int | None) -> None:
        self._highlight = segment_index
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.CARD))
        if self._analysis is None:
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                self.rect(), int(Qt.AlignmentFlag.AlignCenter), "选择一条命令后显示通行余量"
            )
            painter.end()
            return
        paint_clearance_chart(
            painter,
            QRectF(self.rect()).adjusted(8, 8, -8, -8),
            self._analysis,
            highlight=self._highlight,
        )
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._analysis is None:
            return
        image_rect = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        left = image_rect.left() + AXIS_LEFT
        right = image_rect.right() - AXIS_RIGHT
        span = right - left
        if span <= 0:
            return
        progress = min(1.0, max(0.0, (event.position().x() - left) / span))
        self.progress_clicked.emit(progress)
        owner = next(
            (
                segment.index
                for segment in self._analysis.segments
                if segment.start_progress <= progress <= segment.end_progress
            ),
            None,
        )
        if owner is not None:
            self.segment_clicked.emit(owner)


class _RulerView(QWidget):
    """Inline three-zone width ruler shown under the selected bottleneck."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(RULER_HEIGHT)
        self._zones: WidthZones | None = None
        self._message = "正在计算需求道宽…"

    def show_zones(self, zones: WidthZones | None, message: str = "") -> None:
        self._zones = zones
        self._message = message
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        if self._zones is None:
            painter.fillRect(self.rect(), QColor(theme.HEADER_BASE))
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                self.rect().adjusted(12, 0, -12, 0),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._message,
            )
        else:
            paint_width_ruler(painter, QRectF(self.rect()), self._zones)
        painter.end()


class SuggestionCard(QFrame):
    def __init__(self, rank: str, title: str, body: str, *, pill: bool) -> None:
        super().__init__()
        self.setObjectName("suggestionCard")
        _restyle(self, "rank", rank)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 9, 11, 10)
        layout.setSpacing(5)
        header = QHBoxLayout()
        header.setSpacing(6)
        caption = QLabel(title)
        caption.setObjectName("suggestionTitle")
        caption.setWordWrap(True)
        header.addWidget(caption, 1)
        if pill:
            badge = QLabel(PILL_TEXT)
            badge.setObjectName("clearancePill")
            header.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        text = QLabel(body)
        text.setObjectName("suggestionBody")
        _restyle(text, "rank", rank)
        text.setWordWrap(True)
        layout.addWidget(text)


def build_suggestions(analysis: ClearanceAnalysis) -> list[tuple[str, str, str]]:
    """Turn the solved bands into the three advice cards, worst case first."""

    conflicts = [item for item in analysis.coupled if item.conflicting]
    stuck = [item for item in analysis.bottlenecks if not item.band_feasible]
    movable = [
        item
        for item in analysis.bottlenecks
        if item.band_feasible and not item.inside_band and item.required_offset is not None
    ]

    cards: list[tuple[str, str, str]] = []
    if stuck:
        names = "、".join(item.segment.short_label for item in stuck[:3])
        widened = "、".join(
            sorted({item.segment.lane_name for item in stuck if item.segment.lane_name})
        )
        cards.append(
            (
                "primary",
                "偏置救不回来，只能动道路",
                f"{names} 在任何横向偏置下都达不到阈值 "
                f"{format_length(analysis.threshold)} m。"
                + (f"需加宽的是{widened}；" if widened else "")
                + "具体需求道宽见选中行下方的三区标尺。",
            )
        )
    elif movable:
        moves = "、".join(
            f"{item.segment.short_label} {item.segment.describe_offset(item.required_offset or 0)}"
            for item in movable[:3]
        )
        after = (
            format_length(analysis.suggested_clearance)
            if analysis.suggested_clearance is not None
            else "—"
        )
        before = (
            format_length(analysis.minimum_clearance, signed=True)
            if analysis.minimum_clearance is not None
            else "—"
        )
        cards.append(
            (
                "primary",
                "整条路径一次改完",
                f"{moves}，均落回可行带内。最小净距 {before} → {after} m，道路一米不用改。",
            )
        )
    else:
        cards.append(
            (
                "primary",
                "当前路径已在可行带内",
                f"每一段的当前偏置都落在可行区间里，最小净距 "
                f"{format_length(analysis.minimum_clearance or 0.0, signed=True)} m。",
            )
        )

    if conflicts:
        detail = "、".join(
            f"{analysis.segments[item.segment_index].short_label} 差 "
            f"{format_length(item.shortfall)} m"
            for item in conflicts[:3]
        )
        cards.append(
            (
                "secondary",
                "相邻转角互相冲突",
                f"{detail}。连接段只有一个偏置值，前后两个转角要求的区间没有交集，"
                "单靠偏置解不开；需要调整其中一个转角的半径。本版只报冲突，不求所需的半径调整量。",
            )
        )
    else:
        turns = [item for item in analysis.bottlenecks if item.segment.role is SegmentRole.TURN]
        target = turns[0].segment.short_label if turns else "最紧的转角"
        cards.append(
            (
                "secondary",
                "若偏置不便下发",
                f"改用更大的转弯半径同样能收窄扫掠带。点开 {target} 进入转角求解，"
                "可以看到半径、起弯点和两侧偏置各自能买到多少余量。",
            )
        )

    cards.append(
        (
            "discouraged",
            "不建议",
            "在偏置尚有余地时先加宽通道 —— 那是最贵的一档，且改完仍要重新验证转角。",
        )
    )
    return cards


CARD_CAPTIONS: tuple[tuple[str, str], ...] = (
    ("status", "状态"),
    ("breach", "最深越界"),
    ("outside", "落在可行带外"),
    ("narrow", "可行带最窄"),
    ("suggested", "按建议偏置后"),
)


def card_readings(analysis: ClearanceAnalysis) -> dict[str, tuple[str, str, str]]:
    """The five summary readings, so the view and the report never disagree on them."""

    words = {
        ClearanceStatus.SAFE: ("通过", "success"),
        ClearanceStatus.WARNING: ("临界", "warning"),
        ClearanceStatus.OUTSIDE: ("越界", "danger"),
        ClearanceStatus.UNAVAILABLE: ("无结果", "neutral"),
    }
    status_text, status_state = words[analysis.status]
    narrow = analysis.narrowest_band
    return {
        "status": (status_text, f"{analysis.pose_count} 点位", status_state),
        "breach": (
            "无" if analysis.deepest_breach is None else format_length(analysis.deepest_breach),
            "" if analysis.deepest_breach is None else "m",
            "success" if analysis.deepest_breach is None else "danger",
        ),
        "outside": (
            str(analysis.outside_band_segments),
            "段",
            "danger" if analysis.outside_band_segments else "success",
        ),
        "narrow": (
            "无可行带" if narrow is None else format_length(narrow.width),
            "" if narrow is None else f"m @ {analysis.segments[narrow.segment_index].short_label}",
            "danger" if narrow is None else ("warning" if narrow.width < 0.2 else "success"),
        ),
        "suggested": (
            "—"
            if analysis.suggested_clearance is None
            else format_length(analysis.suggested_clearance, signed=True),
            "" if analysis.suggested_clearance is None else "m",
            _state_for(analysis.suggested_clearance, analysis.threshold),
        ),
    }


def build_metric_cards(analysis: ClearanceAnalysis) -> list[MetricCard]:
    """Detached copies of the five summary cards, for rendering onto a report page."""

    readings = card_readings(analysis)
    cards = []
    for key, caption in CARD_CAPTIONS:
        card = MetricCard(caption)
        value, unit, state = readings[key]
        card.show_value(value, unit, state)
        cards.append(card)
    return cards


class ClearanceOverview(QWidget):
    """Assembles the summary cards, the shared-axis chart, the ranking and the advice."""

    pose_selected = Signal(int)
    map_requested = Signal(int)
    corner_requested = Signal(int)
    export_csv_requested = Signal()
    export_pdf_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._analysis: ClearanceAnalysis | None = None
        self._inputs: ClearanceInputs | None = None
        self._selected: int | None = None
        self._zones: dict[int, WidthZones | None] = {}
        self._by_segment: dict[int, Bottleneck] = {}
        self._ruler_row: int | None = None
        self._zone_messages: dict[int, str] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._workers: set[Worker[object]] = set()
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 0)
        layout.setSpacing(10)

        cards = QGridLayout()
        cards.setSpacing(10)
        self._cards = {key: MetricCard(caption) for key, caption in CARD_CAPTIONS}
        for column, card in enumerate(self._cards.values()):
            cards.addWidget(card, 0, column)
            cards.setColumnStretch(column, 1)
        layout.addLayout(cards)

        caption = QHBoxLayout()
        chart_title = QLabel("净距剖面 + 可行偏置带")
        chart_title.setObjectName("clearanceSectionTitle")
        chart_hint = QLabel("共用横轴 · 上下对齐读同一位置 · 点击图上任意处跳到地图定位")
        chart_hint.setObjectName("clearanceSectionHint")
        caption.addWidget(chart_title)
        caption.addWidget(chart_hint, 1)
        layout.addLayout(caption)

        self.chart = ClearanceChartView()
        self.chart.progress_clicked.connect(self._chart_clicked)
        self.chart.segment_clicked.connect(self._highlight_segment)
        layout.addWidget(self.chart, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addWidget(self._build_ranking(), 1)
        bottom.addWidget(self._build_advice(), 0)
        layout.addLayout(bottom, 1)
        layout.addWidget(self._build_status_bar())

    def _build_ranking(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("瓶颈排行榜")
        title.setObjectName("clearanceSectionTitle")
        hint = QLabel("选中行下方给出该处三区标尺")
        hint.setObjectName("clearanceSectionHint")
        header.addWidget(title)
        header.addWidget(hint, 1)
        self.corner_button = QPushButton("进入转角求解")
        self.corner_button.setEnabled(False)
        self.corner_button.clicked.connect(self._open_selected_corner)
        header.addWidget(self.corner_button)
        column.addLayout(header)

        self.table = QTableWidget(0, 6)
        self.table.setObjectName("bottleneckTable")
        self.table.setAccessibleName("通行余量瓶颈排行榜")
        self.table.setHorizontalHeaderLabels(["#", "净距 m", "位置", "约束角", "需偏置", "进度"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        widths = (26, 74, 0, 84, 78, 68)
        header_view = self.table.horizontalHeader()
        for column_index, width in enumerate(widths):
            if width:
                self.table.setColumnWidth(column_index, width)
                header_view.setSectionResizeMode(
                    column_index, QHeaderView.ResizeMode.Fixed
                )
            else:
                header_view.setSectionResizeMode(
                    column_index, QHeaderView.ResizeMode.Stretch
                )
        self.table.itemSelectionChanged.connect(self._row_selected)
        self.table.itemDoubleClicked.connect(self._row_activated)
        column.addWidget(self.table, 1)
        return panel

    def _build_advice(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(ADVICE_WIDTH)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        title = QLabel("优化建议")
        title.setObjectName("clearanceSectionTitle")
        column.addWidget(title)
        self._advice_holder = QVBoxLayout()
        self._advice_holder.setSpacing(8)
        column.addLayout(self._advice_holder)
        column.addStretch(1)
        buttons = QHBoxLayout()
        self.csv_button = QPushButton("导出偏置表 CSV")
        self.csv_button.clicked.connect(self.export_csv_requested)
        self.pdf_button = QPushButton("导出 PDF 报告")
        self.pdf_button.clicked.connect(self.export_pdf_requested)
        buttons.addWidget(self.csv_button)
        buttons.addWidget(self.pdf_button)
        column.addLayout(buttons)
        return panel

    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("clearanceStatusBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)
        self._status_labels: list[QLabel] = []
        for index in range(4):
            if index:
                separator = QLabel("|")
                separator.setObjectName("clearanceStatusSeparator")
                row.addWidget(separator)
            label = QLabel("—")
            label.setObjectName("clearanceStatusText")
            self._status_labels.append(label)
            row.addWidget(label)
        row.addStretch(1)
        return bar

    def set_analysis(
        self, analysis: ClearanceAnalysis | None, inputs: ClearanceInputs | None
    ) -> None:
        self._analysis = analysis
        self._inputs = inputs
        self._selected = None
        self._zones.clear()
        self._zone_messages.clear()
        self.chart.set_analysis(analysis)
        self._refresh_cards()
        self._refresh_table()
        self._refresh_advice()
        self._refresh_status()
        enabled = analysis is not None
        self.csv_button.setEnabled(enabled)
        self.pdf_button.setEnabled(enabled)

    def _refresh_cards(self) -> None:
        analysis = self._analysis
        if analysis is None:
            for card in self._cards.values():
                card.show_value("—")
            return
        for key, (value, unit, state) in card_readings(analysis).items():
            self._cards[key].show_value(value, unit, state)

    def selected_zones(self) -> WidthZones | None:
        """Width ruler for the row the user has open, so the report shows what they see."""

        if self._selected is None:
            return None
        return self._zones.get(self._selected)

    def _row_tint(self, bottleneck: Bottleneck) -> QColor | None:
        if bottleneck.clearance < 0:
            return QColor(theme.DANGER_TINT)
        if self._analysis and bottleneck.clearance < self._analysis.threshold:
            return QColor(theme.WARNING_TINT)
        return None

    def _refresh_table(self) -> None:
        """Rebuild every row. Only called when the analysis changes, never on selection:
        clearing the table mid-gesture destroys the items a double click is tracking."""

        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(0)
        self._by_segment.clear()
        self._ruler_row = None
        analysis = self._analysis
        if analysis is not None:
            for bottleneck in analysis.bottlenecks:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._by_segment[bottleneck.segment.index] = bottleneck
                cells = (
                    str(bottleneck.rank),
                    format_length(bottleneck.clearance, signed=True),
                    bottleneck.segment.label,
                    bottleneck.feature,
                    bottleneck.offset_text,
                    f"{bottleneck.progress * 100:.1f}%",
                )
                tint = self._row_tint(bottleneck)
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, bottleneck.segment.index)
                    if column in (0, 1, 5):
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        )
                    if tint is not None:
                        item.setBackground(tint)
                    self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        self._sync_ruler()

    def _segment_at(self, row: int) -> int | None:
        item = self.table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, int) else None

    def _row_of(self, segment_index: int) -> int | None:
        for row in range(self.table.rowCount()):
            if self._segment_at(row) == segment_index:
                return row
        return None

    def _sync_ruler(self) -> None:
        """Add or move the inline width ruler without disturbing any other row."""

        self.table.blockSignals(True)
        if self._ruler_row is not None:
            self.table.removeRow(self._ruler_row)
            self._ruler_row = None
        if self._selected is not None:
            row = self._row_of(self._selected)
            if row is not None:
                self.table.insertRow(row + 1)
                self.table.setSpan(row + 1, 0, 1, 6)
                ruler = _RulerView()
                ruler.show_zones(
                    self._zones.get(self._selected),
                    self._zone_messages.get(self._selected, "正在计算需求道宽…"),
                )
                self.table.setCellWidget(row + 1, 0, ruler)
                self.table.setRowHeight(row + 1, RULER_HEIGHT)
                self._ruler_row = row + 1
        self.table.blockSignals(False)
        self._refresh_corner_button()

    def _refresh_corner_button(self) -> None:
        bottleneck = (
            self._by_segment.get(self._selected) if self._selected is not None else None
        )
        self.corner_button.setEnabled(
            bottleneck is not None and bottleneck.segment.role is SegmentRole.TURN
        )

    def _open_selected_corner(self) -> None:
        if self._selected is not None:
            self.corner_requested.emit(self._selected)

    def _refresh_advice(self) -> None:
        while self._advice_holder.count():
            item = self._advice_holder.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        if self._analysis is None:
            return
        for rank, title, body in build_suggestions(self._analysis):
            self._advice_holder.addWidget(
                SuggestionCard(rank, title, body, pill=rank != "discouraged")
            )

    def _refresh_status(self) -> None:
        analysis = self._analysis
        if analysis is None:
            for label in self._status_labels:
                label.setText("—")
            return
        conflicts = sum(1 for item in analysis.coupled if item.conflicting)
        words = {
            ClearanceStatus.SAFE: "通过",
            ClearanceStatus.WARNING: "临界",
            ClearanceStatus.OUTSIDE: "越界",
            ClearanceStatus.UNAVAILABLE: "无结果",
        }
        summary = (
            f"{words[analysis.status]} · {analysis.outside_band_segments} 段落在可行带外"
            + (f" · {conflicts} 段相邻转角冲突" if conflicts else "")
        )
        settings = self._inputs.settings if self._inputs else None
        steps = (
            f"位置步长 {format_length(settings.position_step)} m · "
            f"航向步长 {settings.yaw_step:.2f} rad"
            if settings
            else f"样本 {analysis.analyzed_samples}"
        )
        texts = (summary, f"阈值 {format_length(analysis.threshold)} m", steps, PILL_TEXT)
        for label, text in zip(self._status_labels, texts, strict=True):
            label.setText(text)

    def _chart_clicked(self, progress: float) -> None:
        analysis = self._analysis
        inputs = self._inputs
        if analysis is None or inputs is None or not inputs.poses:
            return
        index = min(
            len(inputs.poses) - 1, max(0, round(progress * (len(inputs.poses) - 1)))
        )
        # The chart click has no payload of its own; locating on the map is the whole
        # point of it, so this is the one gesture that changes tabs.
        self.map_requested.emit(index)

    def _highlight_segment(self, segment_index: int) -> None:
        self.chart.set_highlight(segment_index)

    def _row_selected(self) -> None:
        segments = {
            found
            for index in self.table.selectedIndexes()
            if (found := self._segment_at(index.row())) is not None
        }
        bottleneck = next(
            (self._by_segment[value] for value in sorted(segments) if value in self._by_segment),
            None,
        )
        if bottleneck is None or bottleneck.segment.index == self._selected:
            return
        self._selected = bottleneck.segment.index
        self.chart.set_highlight(self._selected)
        self._request_zones(bottleneck)
        self._sync_ruler()
        # Only position the map; do not switch to it. This row's own payload is the width
        # ruler that just opened underneath it, and leaving the page would destroy it.
        self.pose_selected.emit(bottleneck.pose_index)

    def _row_activated(self, item: QTableWidgetItem) -> None:
        segment_index = item.data(Qt.ItemDataRole.UserRole)
        bottleneck = (
            self._by_segment.get(segment_index) if isinstance(segment_index, int) else None
        )
        if bottleneck is not None and bottleneck.segment.role is SegmentRole.TURN:
            self.corner_requested.emit(bottleneck.segment.index)

    def _request_zones(self, bottleneck: Bottleneck) -> None:
        inputs = self._inputs
        index = bottleneck.segment.index
        if inputs is None:
            return
        if index in self._zones:
            return
        if bottleneck.segment.lane_id is None:
            self._zones[index] = None
            self._zone_messages[index] = "该段不在任何启用车道内，无法给出需求道宽"
            return
        self._zone_messages[index] = "正在计算需求道宽…"
        segment = bottleneck.segment

        def solve() -> tuple[int, WidthZones | None]:
            return index, solve_width_zones(
                inputs.poses, segment, inputs.dimensions, inputs.context, inputs.settings
            )

        worker: Worker[object] = Worker(solve)
        self._workers.add(worker)
        # Bound methods, never lambdas: Qt drops these connections when this widget is
        # destroyed, so a solve still running after the command changes cannot land on it.
        worker.signals.succeeded.connect(self._zones_ready)
        worker.signals.failed.connect(self._zones_failed)
        worker.signals.finished.connect(self._worker_finished)
        self._pool.start(worker)

    def _worker_finished(self) -> None:
        self._workers = {worker for worker in self._workers if worker.signals is not None}

    def _zones_ready(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        index, zones = payload
        if not isinstance(index, int):
            return
        resolved = zones if isinstance(zones, WidthZones) else None
        self._zones[index] = resolved
        if resolved is None:
            self._zone_messages[index] = "该段无法给出需求道宽"
        else:
            self._zone_messages.pop(index, None)
        if self._selected == index:
            self._sync_ruler()

    def _zones_failed(self, message: str) -> None:
        if self._selected is None:
            return
        self._zones[self._selected] = None
        self._zone_messages[self._selected] = message
        self._sync_ruler()


class ClearancePanel(QWidget):
    """通行余量 tab: the overview, plus the corner solver it drills down into."""

    pose_selected = Signal(int)
    map_requested = Signal(int)
    export_csv_requested = Signal()
    export_pdf_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(theme.CLEARANCE_STYLESHEET)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        self.overview = ClearanceOverview()
        self.corner_view = CornerSolverView()
        self._stack.addWidget(self.overview)
        self._stack.addWidget(self.corner_view)
        layout.addWidget(self._stack)
        self.overview.pose_selected.connect(self.pose_selected)
        self.overview.map_requested.connect(self.map_requested)
        self.overview.export_csv_requested.connect(self.export_csv_requested)
        self.overview.export_pdf_requested.connect(self.export_pdf_requested)
        self.overview.corner_requested.connect(self._open_corner)
        self.corner_view.back_requested.connect(self._show_overview)
        self._inputs: ClearanceInputs | None = None
        self._analysis: ClearanceAnalysis | None = None

    @property
    def analysis(self) -> ClearanceAnalysis | None:
        return self._analysis

    @property
    def inputs(self) -> ClearanceInputs | None:
        return self._inputs

    @property
    def showing_corner(self) -> bool:
        return self._stack.currentIndex() == 1

    def selected_zones(self) -> WidthZones | None:
        return self.overview.selected_zones()

    def corner_report(self) -> object | None:
        """The corner page for the report, present only while the corner view is open."""

        return self.corner_view.report() if self.showing_corner else None

    def set_analysis(
        self, analysis: ClearanceAnalysis | None, inputs: ClearanceInputs | None
    ) -> None:
        self._analysis = analysis
        self._inputs = inputs
        self.overview.set_analysis(analysis, inputs)
        self._show_overview()

    def _open_corner(self, segment_index: int) -> None:
        if self._analysis is None or self._inputs is None:
            return
        segment = next(
            (item for item in self._analysis.segments if item.index == segment_index), None
        )
        if segment is None or segment.role is not SegmentRole.TURN:
            return
        if self.corner_view.load(segment, self._analysis, self._inputs):
            self._stack.setCurrentIndex(1)

    def _show_overview(self) -> None:
        self._stack.setCurrentIndex(0)


def offset_rows(analysis: ClearanceAnalysis) -> list[Sequence[str]]:
    """Rows for the offset CSV export, one per path segment."""

    rows: list[Sequence[str]] = [
        ["区段", "类型", "车道", "起始点位", "结束点位", "可行带下界", "可行带上界",
         "耦合下界", "耦合上界", "建议偏置", "状态"]
    ]
    for segment in analysis.segments:
        band = analysis.band_for(segment.index)
        pair = analysis.coupled_for(segment.index)
        recommended = analysis.recommended_offsets.get(segment.index)
        if band is None or not band.feasible:
            state = "无可行偏置"
        elif pair is not None and pair.conflicting:
            state = f"与相邻转角冲突 差 {pair.shortfall:.2f}"
        elif band.contains(0.0):
            state = "带内"
        else:
            state = "需偏置"
        rows.append(
            [
                segment.short_label,
                "转弯" if segment.role is SegmentRole.TURN else "直行",
                segment.lane_name or "",
                str(segment.start_label),
                str(segment.end_label),
                f"{band.low:.3f}" if band and band.feasible else "",
                f"{band.high:.3f}" if band and band.feasible else "",
                f"{pair.low:.3f}" if pair and pair.feasible else "",
                f"{pair.high:.3f}" if pair and pair.feasible else "",
                f"{recommended:.3f}" if recommended is not None else "",
                state,
            ]
        )
    return rows
