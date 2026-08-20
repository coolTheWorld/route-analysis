"""转角求解 view: three degrees of freedom against a lane fillet that is not the path arc."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from route_analysis import theme
from route_analysis.analysis import analyze_path
from route_analysis.clearance_geometry import (
    FittedCorner,
    OffsetProfile,
    build_corner_poses,
    offset_profile,
)
from route_analysis.clearance_graphics import (
    CornerPlan,
    format_length,
    paint_corner_plan,
    paint_offset_curve,
)
from route_analysis.clearance_report import CornerReport
from route_analysis.clearance_solver import (
    ClearanceAnalysis,
    DegreeRange,
    PathSegment,
    corner_for_segment,
    solve_corner,
)
from route_analysis.geometry import vehicle_polygon
from route_analysis.lane_generation import arc_radius
from route_analysis.models import Lane, PosePoint, SegmentKind
from route_analysis.workers import Worker

INPUT_WIDTH = 340
SLIDER_HEIGHT = 30
HANDLE_RADIUS = 7.0
TRACK_HEIGHT = 8.0
SLOW_RECALCULATION_MS = 30.0

UNCOVERED = (
    "变半径过渡曲线（回旋线）替代单圆弧",
    "客户转角为多段倒角或不规则墙面",
    "相邻两转角太近，可行带互相耦合",
    "本视图求解的是拟合出的理想转角，与原始离散路径存在拟合残差",
    "连接处的偏置突变未建模，两段各自按自身偏置评估",
)


class _RangeSlider(QWidget):
    """Slider whose track shades the interval that still clears the threshold."""

    value_changed = Signal(float)

    def __init__(self, minimum: float, maximum: float) -> None:
        super().__init__()
        self.setFixedHeight(SLIDER_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._minimum = minimum
        self._maximum = maximum
        self._value = 0.0
        self._range: DegreeRange | None = None

    def set_bounds(self, minimum: float, maximum: float) -> None:
        self._minimum, self._maximum = minimum, maximum
        self.update()

    def set_feasible(self, span: DegreeRange | None) -> None:
        self._range = span
        self.update()

    def value(self) -> float:
        return self._value

    def set_value(self, value: float, *, notify: bool = False) -> None:
        clamped = min(self._maximum, max(self._minimum, value))
        if abs(clamped - self._value) < 1e-9:
            return
        self._value = clamped
        self.update()
        if notify:
            self.value_changed.emit(clamped)

    def _track(self) -> QRectF:
        return QRectF(
            HANDLE_RADIUS,
            (self.height() - TRACK_HEIGHT) / 2,
            max(1.0, self.width() - 2 * HANDLE_RADIUS),
            TRACK_HEIGHT,
        )

    def _to_x(self, value: float) -> float:
        track = self._track()
        span = max(self._maximum - self._minimum, 1e-9)
        return track.left() + (value - self._minimum) / span * track.width()

    def _to_value(self, x: float) -> float:
        track = self._track()
        span = max(self._maximum - self._minimum, 1e-9)
        return self._minimum + (x - track.left()) / max(track.width(), 1e-9) * span

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._track()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(theme.HEADER_BASE)))
        painter.drawRoundedRect(track, 4, 4)
        if self._range is not None and self._range.feasible:
            left = self._to_x(max(self._minimum, self._range.low))
            right = self._to_x(min(self._maximum, self._range.high))
            shade = QColor(theme.SUCCESS_BAR)
            shade.setAlphaF(0.28)
            painter.setBrush(QBrush(shade))
            painter.drawRoundedRect(
                QRectF(left, track.top(), max(1.0, right - left), track.height()), 4, 4
            )
        centre = QPointF(self._to_x(self._value), track.center().y())
        painter.setPen(QPen(QColor(theme.ACCENT), 2.4))
        painter.setBrush(QBrush(QColor(theme.CARD)))
        painter.drawEllipse(centre, HANDLE_RADIUS, HANDLE_RADIUS)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.set_value(self._to_value(event.position().x()), notify=True)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.set_value(self._to_value(event.position().x()), notify=True)


class _CurveView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(190)
        self._profile: OffsetProfile | None = None

    def set_profile(self, profile: OffsetProfile | None) -> None:
        self._profile = profile
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.CARD))
        if self._profile is None:
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                self.rect(),
                int(Qt.AlignmentFlag.AlignCenter),
                "两条线半径相同时偏置恒为零，无拱形可画",
            )
        else:
            paint_offset_curve(painter, QRectF(self.rect()), self._profile)
        painter.end()


class _PlanView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(240)
        self._plan: CornerPlan | None = None

    def set_plan(self, plan: CornerPlan | None) -> None:
        self._plan = plan
        self.update()

    def plan(self) -> CornerPlan | None:
        return self._plan

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BASE))
        if self._plan is not None:
            paint_corner_plan(painter, QRectF(self.rect()), self._plan)
        painter.end()


@dataclass(slots=True)
class _Degree:
    key: str
    title: str
    slider: _RangeSlider
    value_label: QLabel
    range_label: QLabel


def lane_fillet_radius(lane: Lane | None, corner: FittedCorner) -> float | None:
    """Fillet radius of the lane arc nearest this corner, when the lane has one."""

    if lane is None:
        return None
    best: tuple[float, float] | None = None
    for index, segment in enumerate(lane.segments):
        if segment.kind is not SegmentKind.ARC or segment.arc_center is None:
            continue
        distance = math.hypot(
            segment.arc_center.x - corner.centre.x, segment.arc_center.y - corner.centre.y
        )
        radius = arc_radius(lane, index)
        if radius > 0 and (best is None or distance < best[0]):
            best = (distance, radius)
    return best[1] if best else None


class CornerSolverView(QWidget):
    """One corner solved across entry offset, exit offset and where the arc starts."""

    back_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._corner: FittedCorner | None = None
        self._segment: PathSegment | None = None
        self._inputs: object | None = None
        self._analysis: ClearanceAnalysis | None = None
        self._lane: Lane | None = None
        self._updating = False
        self._optimum: dict[str, float] = {}
        self._current_radius: float | None = None
        self._current_clearance: float | None = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._workers: set[Worker[object]] = set()
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        back = QPushButton("← 返回通行余量")
        back.clicked.connect(self.back_requested)
        self._title = QLabel("转角自由度求解")
        self._title.setObjectName("clearanceSectionTitle")
        self._subtitle = QLabel("")
        self._subtitle.setObjectName("clearanceSectionHint")
        header.addWidget(back)
        header.addWidget(self._title)
        header.addWidget(self._subtitle, 1)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_inputs(), 0)
        body.addWidget(self._build_results(), 1)
        layout.addLayout(body, 1)

    def _build_inputs(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("cornerInputColumn")
        panel.setFixedWidth(INPUT_WIDTH)
        column = QVBoxLayout(panel)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(9)

        radii = QLabel("两条线的半径")
        radii.setObjectName("cornerDegreeTitle")
        column.addWidget(radii)
        lane_row = QHBoxLayout()
        lane_row.addWidget(QLabel("车道倒角半径"))
        self.lane_radius_spin = QDoubleSpinBox()
        self.lane_radius_spin.setRange(0.0, 99.0)
        self.lane_radius_spin.setDecimals(2)
        self.lane_radius_spin.setSingleStep(0.05)
        self.lane_radius_spin.setSuffix(" m")
        self.lane_radius_spin.setSpecialValueText("未设置")
        self.lane_radius_spin.setAccessibleName("车道倒角半径")
        self.lane_radius_spin.valueChanged.connect(self._radius_changed)
        lane_row.addWidget(self.lane_radius_spin)
        column.addLayout(lane_row)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("路径转弯半径"))
        self.path_radius_label = QLabel("—")
        self.path_radius_label.setObjectName("cornerDegreeValue")
        path_row.addWidget(self.path_radius_label, 1, Qt.AlignmentFlag.AlignRight)
        column.addLayout(path_row)
        self.radius_notice = QLabel("")
        self.radius_notice.setObjectName("clearanceNotice")
        self.radius_notice.setWordWrap(True)
        column.addWidget(self.radius_notice)

        degrees_title = QLabel("三个自由度")
        degrees_title.setObjectName("cornerDegreeTitle")
        column.addWidget(degrees_title)
        self._degrees: dict[str, _Degree] = {}
        for key, title, third in (
            ("entry_offset", "入弯段偏置", False),
            ("exit_offset", "出弯段偏置", False),
            ("arc_start_shift", "起弯点 · 第三自由度", True),
        ):
            if third:
                rule = QFrame()
                rule.setFrameShape(QFrame.Shape.HLine)
                rule.setStyleSheet("")
                column.addWidget(rule)
            caption = QHBoxLayout()
            label = QLabel(title)
            label.setObjectName("cornerDegreeTitle")
            if third:
                label.setProperty("emphasis", "third")
            value = QLabel("+0.00 m")
            value.setObjectName("cornerDegreeValue")
            caption.addWidget(label)
            caption.addWidget(value, 1, Qt.AlignmentFlag.AlignRight)
            column.addLayout(caption)
            slider = _RangeSlider(-1.5, 1.5)
            slider.setAccessibleName(title)
            slider.value_changed.connect(self._slider_moved)
            column.addWidget(slider)
            span = QLabel("可行区间 —")
            span.setObjectName("cornerHint")
            column.addWidget(span)
            self._degrees[key] = _Degree(key, title, slider, value, span)

        note = QLabel(
            "正 = 推迟起弯（车多走一段直线再切）。推迟起弯让后外角外摆点后移，"
            "把入弯侧余量让给出弯侧；因为两条腿一旦定下，起弯点选的就是半径。"
        )
        note.setObjectName("cornerHint")
        note.setWordWrap(True)
        column.addWidget(note)

        self.worth_label = QLabel("加第三自由度值不值 —")
        self.worth_label.setObjectName("clearanceNotice")
        self.worth_label.setWordWrap(True)
        column.addWidget(self.worth_label)

        self.solve_button = QPushButton("三自由度求最优")
        self.solve_button.clicked.connect(self._solve)
        column.addWidget(self.solve_button)
        column.addStretch(1)
        return panel

    def _build_results(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        curve_title = QLabel("偏置沿弯变化")
        curve_title.setObjectName("clearanceSectionTitle")
        column.addWidget(curve_title)
        self.curve = _CurveView()
        column.addWidget(self.curve, 2)

        plan_title = QLabel("俯视 · 两条中心线不重合")
        plan_title.setObjectName("clearanceSectionTitle")
        column.addWidget(plan_title)
        self.plan = _PlanView()
        column.addWidget(self.plan, 3)

        lower = QHBoxLayout()
        lower.setSpacing(12)
        contribution = QVBoxLayout()
        contribution_title = QLabel("自由度贡献")
        contribution_title.setObjectName("clearanceSectionTitle")
        contribution.addWidget(contribution_title)
        self.reading = QLabel("当前 —")
        self.reading.setObjectName("cornerHint")
        contribution.addWidget(self.reading)
        self.contribution = QTableWidget(3, 3)
        self.contribution.setAccessibleName("转角自由度贡献")
        self.contribution.setHorizontalHeaderLabels(["自由度", "当前", "最优"])
        self.contribution.verticalHeader().setVisible(False)
        self.contribution.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.contribution.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.contribution.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.contribution.setFixedHeight(124)
        contribution.addWidget(self.contribution)
        lower.addLayout(contribution, 1)

        uncovered = QVBoxLayout()
        uncovered_title = QLabel("仍未覆盖")
        uncovered_title.setObjectName("clearanceSectionTitle")
        uncovered.addWidget(uncovered_title)
        for line in UNCOVERED:
            item = QLabel(f"· {line}")
            item.setObjectName("cornerHint")
            item.setWordWrap(True)
            uncovered.addWidget(item)
        uncovered.addStretch(1)
        lower.addLayout(uncovered, 1)
        column.addLayout(lower)
        return panel

    def load(self, segment: PathSegment, analysis: ClearanceAnalysis, inputs: object) -> bool:
        """Fit the corner behind this segment and show it, or refuse if it will not fit."""

        poses = getattr(inputs, "poses", ())
        corner = corner_for_segment(poses, segment)
        if corner is None:
            return False
        self._corner = corner
        self._segment = segment
        self._analysis = analysis
        self._inputs = inputs
        context = getattr(inputs, "context", None)
        self._lane = None
        if context is not None and segment.lane_id is not None:
            self._lane = next(
                (item for item in context.lanes if item.id == segment.lane_id), None
            )
        self._title.setText(f"转角自由度求解 · {segment.label}")
        self.path_radius_label.setText(f"{corner.radius:.2f} m")
        fillet = lane_fillet_radius(self._lane, corner)
        self._updating = True
        self.lane_radius_spin.setValue(fillet if fillet is not None else 0.0)
        for degree in self._degrees.values():
            degree.slider.set_value(0.0)
            degree.slider.set_feasible(None)
        self._updating = False
        self._optimum.clear()
        self._refresh_subtitle()
        self._refresh_radius_notice()
        self._recalculate()
        self._solve()
        return True

    def _refresh_subtitle(self) -> None:
        corner = self._corner
        segment = self._segment
        if corner is None or segment is None:
            self._subtitle.setText("")
            return
        width = f" · {segment.lane_name} {segment.lane_width:.2f} m" if segment.lane_width else ""
        self._subtitle.setText(
            f"偏转 {abs(math.degrees(corner.deflection)):.1f}° · 路径 R {corner.radius:.2f} m"
            f"{width} · 拟合残差 {corner.residual:.3f} m"
        )

    def _radius_changed(self, _value: float) -> None:
        if self._updating:
            return
        self._refresh_radius_notice()
        self._refresh_curve()

    def _refresh_radius_notice(self) -> None:
        corner = self._corner
        lane_radius = self.lane_radius_spin.value()
        if corner is None:
            self.radius_notice.setText("")
            return
        if lane_radius <= 0:
            self.radius_notice.setText(
                "该转角所在车道没有圆弧倒角段，无法读出倒角半径。填入拟采用的值只用于本次求解，不回写车道。"
            )
            return
        profile = self._current_profile()
        if profile is None:
            self.radius_notice.setText("两条线半径相同，偏置沿弯恒为零。")
            return
        self.radius_notice.setText(
            f"两半径不等 ⇒ 偏置沿弯变化。切点相距 {profile.tangent_gap:.2f} m，"
            f"弯心处横向间距 {profile.peak:.2f} m。常数偏置近似会恰在弯心高估余量。"
        )

    def _current_profile(self) -> OffsetProfile | None:
        corner = self._corner
        lane_radius = self.lane_radius_spin.value()
        if corner is None or lane_radius <= 0:
            return None
        if abs(lane_radius - corner.radius) < 1e-6:
            return None
        try:
            return offset_profile(lane_radius, corner.radius, corner.deflection)
        except ValueError:
            return None

    def _refresh_curve(self) -> None:
        self.curve.set_profile(self._current_profile())

    def _slider_moved(self, _value: float) -> None:
        if not self._updating:
            self._recalculate()

    def _values(self) -> tuple[float, float, float]:
        return (
            self._degrees["entry_offset"].slider.value(),
            self._degrees["exit_offset"].slider.value(),
            self._degrees["arc_start_shift"].slider.value(),
        )

    def _recalculate(self) -> None:
        corner = self._corner
        inputs = self._inputs
        if corner is None or inputs is None:
            return
        settings = getattr(inputs, "settings", None)
        dimensions = getattr(inputs, "dimensions", None)
        context = getattr(inputs, "context", None)
        if settings is None or dimensions is None or context is None:
            return
        entry, exit_offset, shift = self._values()
        for key, value in zip(self._degrees, (entry, exit_offset, shift), strict=True):
            self._degrees[key].value_label.setText(format_length(value, signed=True) + " m")
        built = build_corner_poses(
            corner,
            entry_offset=entry,
            exit_offset=exit_offset,
            arc_start_shift=shift,
            yaw_step=settings.yaw_step,
        )
        area = context.area()
        if built is None:
            self.plan.set_plan(None)
            self._fill_contribution(None, None)
            self._refresh_curve()
            return
        result = analyze_path(built.poses, dimensions, area, settings)
        self._fill_contribution(built.radius, result.minimum_clearance)
        self._refresh_curve()
        self.plan.set_plan(
            self._build_plan(built.poses, result.minimum_clearance_pose, dimensions, area)
        )

    def _build_plan(
        self,
        poses: tuple[PosePoint, ...],
        narrowest: PosePoint | None,
        dimensions: object,
        area: object,
    ) -> CornerPlan:
        corner = self._corner
        assert corner is not None
        lane_line = [
            (corner.entry_point.x, corner.entry_point.y),
            *(
                []
                if corner.corner_point is None
                else [(corner.corner_point.x, corner.corner_point.y)]
            ),
            (corner.exit_point.x, corner.exit_point.y),
        ]
        rings: list[list[tuple[float, float]]] = []
        geometries = getattr(area, "geoms", None)
        for piece in geometries if geometries is not None else [area]:
            exterior = getattr(piece, "exterior", None)
            if exterior is not None:
                rings.append([(x, y) for x, y in exterior.coords])
        footprints = [
            [(x, y) for x, y in vehicle_polygon(pose, dimensions).exterior.coords]  # type: ignore[arg-type]
            for pose in poses[:: max(1, len(poses) // 22)]
            if pose.yaw is not None
        ]
        narrow_ring = (
            [(x, y) for x, y in vehicle_polygon(narrowest, dimensions).exterior.coords]  # type: ignore[arg-type]
            if narrowest is not None
            else None
        )
        return CornerPlan(
            lane_centreline=lane_line,
            path_centreline=[(pose.x, pose.y) for pose in poses],
            traversable=rings,
            footprints=footprints,
            narrowest=narrow_ring,
        )

    def _fill_contribution(self, radius: float | None, clearance: float | None) -> None:
        self._current_radius = radius
        self._current_clearance = clearance
        entry, exit_offset, shift = self._values()
        current = (entry, exit_offset, shift)
        for row, (key, name) in enumerate(
            (("entry_offset", "入弯偏置"), ("exit_offset", "出弯偏置"), ("起弯点", "起弯点"))
        ):
            best = self._optimum.get(key if row < 2 else "arc_start_shift")
            cells = (
                name,
                format_length(current[row], signed=True),
                "—" if best is None else format_length(best, signed=True),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if row == 2:
                    item.setBackground(QColor(theme.SUCCESS_TINT))
                self.contribution.setItem(row, column, item)
        if radius is None:
            self.reading.setText("当前三个自由度组合无可行几何解")
        else:
            gap = "—" if clearance is None else format_length(clearance, signed=True)
            self.reading.setText(f"当前 · 路径半径 R {radius:.2f} m · 最小净距 {gap} m")

    def _solve(self) -> None:
        corner = self._corner
        inputs = self._inputs
        if corner is None or inputs is None:
            return
        settings = getattr(inputs, "settings", None)
        dimensions = getattr(inputs, "dimensions", None)
        context = getattr(inputs, "context", None)
        if settings is None or dimensions is None or context is None:
            return
        self.solve_button.setEnabled(False)
        self.solve_button.setText("正在求解…")
        area = context.area()
        worker: Worker[object] = Worker(
            lambda: solve_corner(corner, dimensions, area, settings)
        )
        # Bound methods only, so Qt drops these when the view goes away mid-solve.
        worker.signals.succeeded.connect(self._apply_solution)
        worker.signals.failed.connect(self._solving_failed)
        worker.signals.finished.connect(self._solving_finished)
        self._workers.add(worker)
        self._pool.start(worker)

    def report(self) -> CornerReport | None:
        """Snapshot of what this view currently shows, for the report's corner page."""

        corner = self._corner
        segment = self._segment
        if corner is None or segment is None:
            return None
        entry, exit_offset, shift = self._values()
        lane_radius = self.lane_radius_spin.value()
        return CornerReport(
            segment=segment,
            corner=corner,
            lane_radius=lane_radius if lane_radius > 0 else None,
            plan=self.plan.plan(),
            entry_offset=entry,
            exit_offset=exit_offset,
            arc_start_shift=shift,
            radius=self._current_radius,
            clearance=self._current_clearance,
        )

    def _solving_failed(self, message: str) -> None:
        self.worth_label.setText(message)

    def _solving_finished(self) -> None:
        self._workers.clear()
        self.solve_button.setEnabled(True)
        self.solve_button.setText("三自由度求最优")

    def _apply_solution(self, solution: object) -> None:
        if solution is None:
            self.worth_label.setText("该转角在搜索范围内没有可行解。")
            return
        self._updating = True
        for key in self._degrees:
            value = getattr(solution, key)
            self._optimum[key] = value
            span = getattr(solution, "ranges", {}).get(key)
            edges = (abs(span.low), abs(span.high)) if span is not None else (0.0, 0.0)
            reach = max(1.5, abs(value) * 1.6, *edges)
            self._degrees[key].slider.set_bounds(-reach, reach)
            self._degrees[key].slider.set_feasible(span)
            self._degrees[key].slider.set_value(value)
            self._degrees[key].range_label.setText(
                f"可行区间 {format_length(span.low, signed=True)}…"
                f"{format_length(span.high, signed=True)}"
                if span is not None and span.feasible
                else "可行区间 无"
            )
        self._updating = False
        two = getattr(solution, "two_degree_clearance", 0.0)
        three = getattr(solution, "clearance", 0.0)
        gain = getattr(solution, "third_degree_gain", 0.0)
        self.worth_label.setText(
            f"加第三自由度值不值：只调两个偏置 {format_length(two)} m，"
            f"加上起弯点 {format_length(three)} m，多出 {format_length(gain)} m。"
        )
        self._recalculate()
