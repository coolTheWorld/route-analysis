"""QPainter drawing for the rapid-estimate plan view.

Layers from the bottom up: grid, drivable region, road centrelines, swept body,
driven path, breaching poses, then the dimension and bottleneck annotations. Everything
arrives in metres and goes through one uniform transform into widget pixels.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPolygonF

from route_analysis import theme
from route_analysis.clearance_graphics import _font, _pen, _text, _tinted, format_length
from route_analysis.models import VehicleDimensions
from route_analysis.scenario_geometry import (
    Arc,
    Line,
    Scenario,
    ScenarioLayout,
    dimension_label,
)
from route_analysis.scenario_solver import ManeuverTrace, ScenarioResult, trace_maneuvers

SCENE_MARGIN = 1.05
GRID_STEP = 0.5
ENVELOPE_RECTS = 52
DIRECTION_MARKS = 7
ARROW_PIXELS = 7.0
VIOLATION_RECTS = 16
LEGEND_HEIGHT = 18.0
LABEL_PADDING = 3.0


class BodySection(StrEnum):
    """Which half of the body the swept envelope draws, split at the front-axle centre."""

    WHOLE = "whole"
    FRONT = "front"
    REAR = "rear"


SECTION_CAPTIONS: dict[BodySection, str] = {
    BodySection.WHOLE: "整车",
    BodySection.FRONT: "仅前段",
    BodySection.REAR: "仅后段",
}


@dataclass(frozen=True, slots=True)
class PlanLayers:
    envelope: bool = True
    dimensions: bool = True
    grid: bool = True
    section: BodySection = BodySection.WHOLE


DEFAULT_LAYERS = PlanLayers()


@dataclass(frozen=True, slots=True)
class _Transform:
    """Uniform metres-to-pixels mapping with y flipped: scene y is up, screen y is down."""

    scale: float
    offset_x: float
    offset_y: float

    def point(self, x: float, y: float) -> QPointF:
        return QPointF(self.offset_x + x * self.scale, self.offset_y - y * self.scale)


def _transform(rect: QRectF, bounds: tuple[float, float, float, float]) -> _Transform:
    left, bottom, right, top = bounds
    width = max(right - left + 2 * SCENE_MARGIN, 1e-6)
    height = max(top - bottom + 2 * SCENE_MARGIN, 1e-6)
    scale = min(rect.width() / width, rect.height() / height)
    centre_x = (left + right) / 2
    centre_y = (bottom + top) / 2
    return _Transform(
        scale=scale,
        offset_x=rect.center().x() - centre_x * scale,
        offset_y=rect.center().y() + centre_y * scale,
    )


def _polygon(transform: _Transform, points: Iterable[Sequence[float]]) -> QPolygonF:
    return QPolygonF([transform.point(float(point[0]), float(point[1])) for point in points])


def _draw_grid(
    painter: QPainter,
    rect: QRectF,
    transform: _Transform,
    bounds: tuple[float, float, float, float],
) -> None:
    left, bottom, right, top = bounds
    painter.setPen(_pen(theme.GRID, 1.0))
    start = math.ceil((left - SCENE_MARGIN) / GRID_STEP) * GRID_STEP
    while start <= right + SCENE_MARGIN:
        x = transform.point(start, 0).x()
        painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        start += GRID_STEP
    start = math.ceil((bottom - SCENE_MARGIN) / GRID_STEP) * GRID_STEP
    while start <= top + SCENE_MARGIN:
        y = transform.point(0, start).y()
        painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        start += GRID_STEP


def _draw_region(
    painter: QPainter, transform: _Transform, layout: ScenarioLayout
) -> None:
    painter.setBrush(QBrush(_tinted(theme.AREA_FILL, 0.12)))
    painter.setPen(_pen(theme.AREA_STROKE, 1.5))
    painter.drawPolygon(_polygon(transform, layout.region))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(_pen(theme.TEXT_FAINT, 1.0, dashes=(6.0, 5.0)))
    for start, end in layout.centrelines:
        painter.drawLine(transform.point(*start), transform.point(*end))


def _body_axes(trace: ManeuverTrace, index: int) -> tuple[float, float, float, float]:
    """Heading and its left normal. R drives the body backwards, so the nose opposes travel."""

    ux, uy = float(trace.ux[index]), float(trace.uy[index])
    if not bool(trace.gear_is_drive[index]):
        ux, uy = -ux, -uy
    return ux, uy, -uy, ux


def _section_polygon(
    trace: ManeuverTrace,
    index: int,
    vehicle: VehicleDimensions,
    section: BodySection,
) -> list[tuple[float, float]]:
    """The body rectangle trimmed to one side of the front-axle centre."""

    hx, hy, nx, ny = _body_axes(trace, index)
    x, y = float(trace.x[index]), float(trace.y[index])
    back = 0.0 if section is BodySection.FRONT else -vehicle.center_rear
    front = 0.0 if section is BodySection.REAR else vehicle.center_front
    half = vehicle.width / 2
    return [
        (x + front * hx + half * nx, y + front * hy + half * ny),
        (x + front * hx - half * nx, y + front * hy - half * ny),
        (x + back * hx - half * nx, y + back * hy - half * ny),
        (x + back * hx + half * nx, y + back * hy + half * ny),
    ]


def _sections_drawn(section: BodySection) -> tuple[BodySection, ...]:
    if section is BodySection.WHOLE:
        return (BodySection.REAR, BodySection.FRONT)
    return (section,)


SECTION_COLOURS: dict[BodySection, str] = {
    BodySection.FRONT: theme.SECTION_FRONT,
    BodySection.REAR: theme.SECTION_REAR,
}


def _draw_envelope(
    painter: QPainter,
    transform: _Transform,
    traces: tuple[ManeuverTrace, ...],
    vehicle: VehicleDimensions,
    section: BodySection,
) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    for part in _sections_drawn(section):
        painter.setBrush(QBrush(_tinted(SECTION_COLOURS[part], 0.16)))
        for trace in traces:
            stride = max(1, math.ceil(len(trace.corners) / ENVELOPE_RECTS))
            for index in range(0, len(trace.corners), stride):
                painter.drawPolygon(
                    _polygon(transform, _section_polygon(trace, index, vehicle, part))
                )
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _draw_paths(
    painter: QPainter, transform: _Transform, traces: tuple[ManeuverTrace, ...]
) -> None:
    for drive, pen in (
        (False, _pen(theme.ACCENT, 2.0, dashes=(7.0, 4.0))),
        (True, _pen(theme.ACCENT, 2.2)),
    ):
        painter.setPen(pen)
        for trace in traces:
            path = QPainterPath()
            started = False
            for index in range(len(trace.x)):
                if bool(trace.gear_is_drive[index]) is not drive:
                    started = False
                    continue
                point = transform.point(float(trace.x[index]), float(trace.y[index]))
                if started:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                    started = True
            painter.drawPath(path)


def _draw_direction(
    painter: QPainter, transform: _Transform, traces: tuple[ManeuverTrace, ...]
) -> None:
    """Chevrons along the path, pointing the way the truck actually travels.

    Travel, not heading: on a reverse leg the two are opposite, and which way the body is
    moving is the thing the path is there to show.
    """

    for trace in traces:
        count = len(trace.x)
        if count < 2:
            continue
        stride = max(1, count // (DIRECTION_MARKS + 1))
        for index in range(stride, count - 1, stride):
            ux, uy = float(trace.ux[index]), float(trace.uy[index])
            tip = transform.point(float(trace.x[index]), float(trace.y[index]))
            painter.setPen(
                _pen(theme.ACCENT_DEEP, 1.6 if bool(trace.gear_is_drive[index]) else 1.3)
            )
            for turn in (2.5, -2.5):
                wing_x = ux * math.cos(turn) - uy * math.sin(turn)
                wing_y = ux * math.sin(turn) + uy * math.cos(turn)
                painter.drawLine(
                    tip,
                    QPointF(
                        tip.x() + wing_x * ARROW_PIXELS,
                        tip.y() - wing_y * ARROW_PIXELS,
                    ),
                )


def _draw_endpoints(
    painter: QPainter, transform: _Transform, traces: tuple[ManeuverTrace, ...]
) -> None:
    """Mark where each maneuver begins and ends, so the path reads in the right order."""

    for trace in traces:
        if not len(trace.x):
            continue
        for index, caption, colour in (
            (0, "起点", theme.SUCCESS_BAR),
            (len(trace.x) - 1, "终点", theme.DANGER_POINT),
        ):
            point = transform.point(float(trace.x[index]), float(trace.y[index]))
            painter.setPen(_pen(theme.CANVAS_BASE, 2.0))
            painter.setBrush(QBrush(QColor(colour)))
            painter.drawEllipse(point, 4.6, 4.6)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            _haloed_text(
                painter,
                QPointF(point.x(), point.y() - 13.0),
                caption,
                size=9.5,
                color=colour,
                bold=True,
            )


def _draw_violations(
    painter: QPainter, transform: _Transform, traces: tuple[ManeuverTrace, ...]
) -> None:
    painter.setPen(_pen(theme.DANGER, 1.4))
    painter.setBrush(QBrush(_tinted(theme.DANGER_POINT, 0.10)))
    for trace in traces:
        indices = [index for index in range(len(trace.corners)) if trace.breached[index]]
        if not indices:
            continue
        stride = max(1, math.ceil(len(indices) / VIOLATION_RECTS))
        for index in indices[::stride]:
            painter.drawPolygon(_polygon(transform, trace.corners[index]))
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _draw_playhead(
    painter: QPainter,
    transform: _Transform,
    traces: tuple[ManeuverTrace, ...],
    vehicle: VehicleDimensions,
    progress: float,
) -> None:
    """One body per maneuver at the same fraction along, for the run-through.

    Both halves are drawn in their own colour and outlined, so the run reads against the
    pale swept envelope underneath rather than disappearing into it. Two-way layouts run
    both maneuvers together: they are the same drive mirrored, and watching them at once is
    what shows where the two demands overlap.
    """

    for trace in traces:
        count = len(trace.x)
        if not count:
            continue
        index = min(count - 1, max(0, round(progress * (count - 1))))
        for part in (BodySection.REAR, BodySection.FRONT):
            painter.setPen(_pen(SECTION_COLOURS[part], 1.6))
            painter.setBrush(QBrush(_tinted(SECTION_COLOURS[part], 0.45)))
            painter.drawPolygon(
                _polygon(transform, _section_polygon(trace, index, vehicle, part))
            )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        centre = transform.point(float(trace.x[index]), float(trace.y[index]))
        painter.setPen(_pen(theme.ACCENT_DEEP, 1.4))
        painter.drawEllipse(centre, 2.6, 2.6)


def _primitive_anchor(item: Line | Arc) -> tuple[float, float]:
    if isinstance(item, Line):
        middle_x = (item.start[0] + item.end[0]) / 2
        middle_y = (item.start[1] + item.end[1]) / 2
        vertical = abs(item.end[0] - item.start[0]) < 1e-6
        return (
            middle_x + (0.55 if vertical else 0.0),
            middle_y + (0.0 if vertical else 0.5),
        )
    middle = (item.start_angle + item.end_angle) / 2
    reach = item.radius + 0.95
    return (
        item.centre[0] + reach * math.cos(middle),
        item.centre[1] + reach * math.sin(middle),
    )


def _gear_pills(layout: ScenarioLayout) -> list[tuple[float, float, str]]:
    """One pill per run of primitives sharing a gear.

    Labelling each primitive puts two pills on top of each other wherever adjacent arcs
    change gear, which is exactly what the borrowed-stub U-turn does, so merge the
    neighbours that share a gear and anchor the pill at the middle of the run.
    """

    if not layout.maneuvers:
        return []
    runs: list[list[Line | Arc]] = []
    for item in layout.maneuvers[0].primitives:
        if runs and runs[-1][-1].gear is item.gear:
            runs[-1].append(item)
        else:
            runs.append([item])
    pills = []
    for run in runs:
        anchors = [_primitive_anchor(item) for item in run]
        x = sum(point[0] for point in anchors) / len(anchors)
        y = sum(point[1] for point in anchors) / len(anchors)
        pills.append((x, y, f"{run[0].gear.value}档"))
    return pills


def _label_rect(painter: QPainter, point: QPointF, message: str, size: float) -> QRectF:
    painter.setFont(_font(painter, size))
    metrics = painter.fontMetrics()
    width = metrics.horizontalAdvance(message) + 2 * LABEL_PADDING
    height = metrics.height() + LABEL_PADDING
    return QRectF(point.x() - width / 2, point.y() - height / 2, width, height)


def _haloed_text(
    painter: QPainter,
    point: QPointF,
    message: str,
    *,
    size: float,
    color: str,
    bold: bool = False,
    mono: bool = False,
) -> None:
    """Annotations sit on top of the drawing, so lay a canvas-coloured halo behind them

    or the paths and the grid swallow the digits. A label anchored on the edge of the
    scene gets nudged back inside the clip, since half of it would otherwise be cut off
    and read as a different number.
    """

    painter.setFont(_font(painter, size, bold=bold, mono=mono))
    metrics = painter.fontMetrics()
    width = metrics.horizontalAdvance(message) + 2 * LABEL_PADDING
    height = metrics.height() + LABEL_PADDING
    box = QRectF(point.x() - width / 2, point.y() - height / 2, width, height)
    limit = painter.clipBoundingRect()
    if not limit.isEmpty():
        box.moveLeft(min(max(box.left(), limit.left() + 1), limit.right() - width - 1))
        box.moveTop(min(max(box.top(), limit.top() + 1), limit.bottom() - height - 1))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(_tinted(theme.CANVAS_BASE, 0.88)))
    painter.drawRoundedRect(box, 3.0, 3.0)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    _text(
        painter, box, message,
        size=size, color=color, bold=bold, mono=mono,
        align=Qt.AlignmentFlag.AlignCenter,
    )


def _draw_dimension(
    painter: QPainter,
    transform: _Transform,
    start: tuple[float, float],
    end: tuple[float, float],
    message: str,
    *,
    solved: bool,
) -> None:
    color = theme.SUCCESS if solved else theme.TEXT_SECONDARY
    head = transform.point(*start)
    tail = transform.point(*end)
    painter.setPen(_pen(color, 1.1))
    painter.drawLine(head, tail)
    angle = math.atan2(tail.y() - head.y(), tail.x() - head.x())
    for point, direction in ((head, angle), (tail, angle + math.pi)):
        for spread in (0.4, -0.4):
            painter.drawLine(
                point,
                QPointF(
                    point.x() + 7 * math.cos(direction + spread),
                    point.y() + 7 * math.sin(direction + spread),
                ),
            )
    middle = QPointF((head.x() + tail.x()) / 2, (head.y() + tail.y()) / 2)
    _haloed_text(painter, middle, message, size=10.0, color=color, bold=solved, mono=True)


def _dimension_marks(result: ScenarioResult) -> list[tuple[tuple, tuple, str, str]]:
    """Which dimensions each scenario marks, anchored on the real edges of the region."""

    dims = result.dims
    layout = result.layout
    bidirectional = result.inputs.bidirectional
    scenario = result.inputs.scenario
    marks: list[tuple[tuple, tuple, str, str]] = []

    def label(key: str) -> str:
        return dimension_label(scenario, key, bidirectional=bidirectional)

    if scenario is Scenario.CORNER:
        la, lb = layout.extents["la"], layout.extents["lb"]
        marks.append((
            (-la + 0.55, -dims.wa / 2), (-la + 0.55, dims.wa / 2),
            f"{label('wa')} {dims.wa:.2f}", "wa",
        ))
        marks.append((
            (-dims.wb / 2, lb - 0.5), (dims.wb / 2, lb - 0.5),
            f"{label('wb')} {dims.wb:.2f}", "wb",
        ))
    elif scenario is Scenario.CROSSBACK:
        lw, ln = layout.extents["lw"], layout.extents["ln"]
        marks.append((
            (-dims.wv / 2, ln - 0.45), (dims.wv / 2, ln - 0.45),
            f"{label('wv')} {dims.wv:.2f}", "wv",
        ))
        marks.append((
            (-lw + 0.5, -dims.wh / 2), (-lw + 0.5, dims.wh / 2),
            f"{label('wh')} {dims.wh:.2f}", "wh",
        ))
        marks.append((
            (dims.wv / 2 + 0.45, -dims.wh / 2),
            (dims.wv / 2 + 0.45, -dims.wh / 2 - dims.ls),
            f"{label('ls')} {dims.ls:.2f}", "ls",
        ))
    elif scenario is Scenario.STUBBACK:
        lx = layout.extents["lx"]
        marks.append((
            (lx - 0.5, -dims.wh / 2), (lx - 0.5, dims.wh / 2),
            f"{label('wh')} {dims.wh:.2f}", "wh",
        ))
        marks.append((
            (-dims.wv / 2, -dims.wh / 2 - dims.ls + 0.4),
            (dims.wv / 2, -dims.wh / 2 - dims.ls + 0.4),
            f"{label('wv')} {dims.wv:.2f}", "wv",
        ))
        marks.append((
            (dims.wv / 2 + 0.45, -dims.wh / 2),
            (dims.wv / 2 + 0.45, -dims.wh / 2 - dims.ls),
            f"{label('ls')} {dims.ls:.2f}", "ls",
        ))
    else:
        outer = layout.extents["outer"]
        ld = layout.extents["ld"]
        marks.append((
            (-outer, -ld + 0.5), (-outer + dims.w, -ld + 0.5),
            f"{label('w')} {dims.w:.2f}", "w",
        ))
        wall_left = -dims.w / 2 - dims.b if bidirectional else -dims.b / 2
        marks.append((
            (wall_left, -ld + 0.5), (wall_left + dims.b, -ld + 0.5),
            f"隔墙 {dims.b:.2f}", "b",
        ))
        marks.append((
            (outer + 0.45, 0.0), (outer + 0.45, dims.d),
            f"{label('d')} {dims.d:.2f}", "d",
        ))
    return marks


def _draw_annotations(painter: QPainter, transform: _Transform, result: ScenarioResult) -> None:
    from route_analysis.scenario_geometry import SOLVED_KEYS

    solved_keys = SOLVED_KEYS[result.inputs.scenario] if result.solved else ()
    for start, end, message, key in _dimension_marks(result):
        _draw_dimension(
            painter, transform, start, end, message, solved=key in solved_keys
        )
    if result.inputs.scenario is Scenario.UTURN and result.layout.buildable:
        outer = result.layout.extents["outer"]
        yc = result.offsets.yc
        painter.setPen(_pen(theme.ACCENT_DEEP, 1.2, dashes=(5.0, 4.0)))
        painter.drawLine(transform.point(-outer, yc), transform.point(outer, yc))
        _haloed_text(
            painter,
            transform.point(outer - 1.1, yc),
            f"起弯点 {format_length(yc, signed=True)}",
            size=9.5, color=theme.ACCENT_DEEP, mono=True,
        )


def _draw_bottleneck(painter: QPainter, transform: _Transform, result: ScenarioResult) -> None:
    probe = result.probe
    if probe.body_point is None or probe.wall_point is None:
        return
    body = transform.point(*probe.body_point)
    wall = transform.point(*probe.wall_point)
    painter.setPen(_pen(theme.DANGER, 1.2, dashes=(4.0, 3.0)))
    painter.drawLine(body, wall)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(theme.DANGER_POINT)))
    painter.drawEllipse(body, 3.6, 3.6)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    _haloed_text(
        painter,
        QPointF((body.x() + wall.x()) / 2, (body.y() + wall.y()) / 2 - 12),
        f"最小净距 {format_length(result.min_clearance)} m",
        size=10.0, color=theme.DANGER, bold=True, mono=True,
    )


LEGEND_ITEMS = (
    ("可通行区域", theme.AREA_STROKE, "block"),
    ("扫掠 前段（中心前距）", theme.SECTION_FRONT, "block"),
    ("扫掠 后段（中心后距）", theme.SECTION_REAR, "block"),
    ("路径 D档", theme.ACCENT, "solid"),
    ("路径 R档", theme.ACCENT, "dashed"),
    ("行进方向", theme.ACCENT_DEEP, "solid"),
    ("起点", theme.SUCCESS_BAR, "dot"),
    ("终点", theme.DANGER_POINT, "dot"),
    ("道路中心线", theme.TEXT_FAINT, "dashed"),
    ("越界位姿", theme.DANGER, "block"),
)
"""The two section colours are named here because that is the only place the drawing says

which half of the body is which; the plan view itself has no room for it.
"""


def _draw_legend(painter: QPainter, rect: QRectF) -> None:
    painter.setFont(_font(painter, 10.5))
    metrics = painter.fontMetrics()
    x = rect.left()
    middle = rect.center().y()
    for message, color, shape in LEGEND_ITEMS:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if shape == "block":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(color)))
            painter.drawRoundedRect(QRectF(x, middle - 3.5, 12.0, 7.0), 2.0, 2.0)
        elif shape == "dot":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(color)))
            painter.drawEllipse(QPointF(x + 6.0, middle), 3.4, 3.4)
        else:
            painter.setPen(
                _pen(color, 2.0, dashes=(4.0, 3.0) if shape == "dashed" else None)
            )
            painter.drawLine(QPointF(x, middle), QPointF(x + 12.0, middle))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        width = metrics.horizontalAdvance(message)
        _text(
            painter,
            QRectF(x + 16.0, rect.top(), width + 4.0, rect.height()),
            message,
            size=10.5, color=theme.TEXT_MUTED,
        )
        x += 16.0 + width + 16.0
        if x > rect.right():
            break


def paint_scenario_plan(
    painter: QPainter,
    rect: QRectF,
    result: ScenarioResult,
    layers: PlanLayers | None = None,
    traces: tuple[ManeuverTrace, ...] | None = None,
    playhead: float | None = None,
) -> None:
    """Draw one estimate as a plan view.

    ``traces`` lets the caller hand in poses it already expanded. Tracing runs the bodies
    through a shapely containment test, which is far too much to redo on every frame of the
    run-through, and it only changes when the result does. ``playhead`` is that run-through:
    a fraction from 0 to 1 placing one body along each maneuver.
    """

    layers = layers or DEFAULT_LAYERS
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setClipRect(rect)
    legend = QRectF(
        rect.left() + 10, rect.bottom() - LEGEND_HEIGHT - 4, rect.width() - 20, LEGEND_HEIGHT
    )
    plan = QRectF(
        rect.left() + 8, rect.top() + 8, rect.width() - 16, rect.height() - LEGEND_HEIGHT - 20
    )
    layout = result.layout
    bounds = layout.view_bounds
    transform = _transform(plan, bounds)
    left, bottom, right, top = bounds
    scene = QRectF(
        transform.point(left - SCENE_MARGIN, top + SCENE_MARGIN),
        transform.point(right + SCENE_MARGIN, bottom - SCENE_MARGIN),
    )
    painter.setClipRect(scene.intersected(plan))
    if layers.grid:
        _draw_grid(painter, plan, transform, bounds)
    _draw_region(painter, transform, layout)
    if traces is None:
        traces = trace_maneuvers(layout, result.inputs.dimensions)
    vehicle = result.inputs.dimensions
    if layers.envelope:
        _draw_envelope(painter, transform, traces, vehicle, layers.section)
    _draw_paths(painter, transform, traces)
    _draw_direction(painter, transform, traces)
    _draw_violations(painter, transform, traces)
    if playhead is not None:
        _draw_playhead(painter, transform, traces, vehicle, playhead)
    _draw_endpoints(painter, transform, traces)
    if layers.dimensions:
        _draw_annotations(painter, transform, result)
        for x, y, message in _gear_pills(layout):
            point = transform.point(x, y)
            box = _label_rect(painter, point, message, 9.5)
            painter.setPen(_pen(theme.INPUT_BORDER, 1.0))
            painter.setBrush(QBrush(QColor(theme.CARD)))
            painter.drawRoundedRect(box, 7.0, 7.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            _text(
                painter, box, message,
                size=9.5, color=theme.TEXT_SECONDARY,
                align=Qt.AlignmentFlag.AlignCenter,
            )
    _draw_bottleneck(painter, transform, result)
    if not layout.buildable:
        _haloed_text(
            painter,
            plan.center(),
            f"巷道宽 + 隔墙宽 装不下最小转弯半径，巷道至少需 "
            f"{format_length(result.required_lane_width or 0.0)} m",
            size=11.5, color=theme.DANGER, bold=True,
        )
    painter.setClipRect(rect)
    _draw_legend(painter, legend)
    painter.restore()
