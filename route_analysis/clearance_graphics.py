"""QPainter drawing shared by the clearance headroom widgets and the exported report.

Every routine takes an explicit painter and rectangle so the on-screen widget and the
PDF page render from the same code. Sizes are given in design pixels; ``_font`` converts
them for whatever resolution the paint device reports.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)

from route_analysis import theme
from route_analysis.clearance_geometry import OffsetProfile
from route_analysis.clearance_solver import ClearanceAnalysis, PathSegment, WidthZones

AXIS_LEFT = 56.0
AXIS_RIGHT = 14.0
AXIS_TOP = 22.0
AXIS_BOTTOM = 56.0
PANE_GAP = 26.0
PROFILE_SHARE = 0.62
LEGEND_HEIGHT = 16.0


def _font(painter: QPainter, size: float, *, bold: bool = False, mono: bool = False) -> QFont:
    """Font of ``size`` design pixels on whatever device this painter draws to."""

    font = QFont(painter.font())
    if mono:
        font.setFamilies(["Consolas", "DejaVu Sans Mono", "Menlo", "monospace"])
    dpi = painter.device().logicalDpiY() or 96
    font.setPointSizeF(size * 72.0 / dpi)
    font.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
    return font


def _pen(color: str, width: float, *, dashes: Sequence[float] | None = None) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    if dashes:
        pen.setDashPattern([value / max(width, 0.1) for value in dashes])
    return pen


def _tinted(color: str, alpha: float) -> QColor:
    value = QColor(color)
    value.setAlphaF(alpha)
    return value


def _text(
    painter: QPainter,
    rect: QRectF,
    message: str,
    *,
    size: float,
    color: str,
    bold: bool = False,
    mono: bool = False,
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
) -> None:
    painter.setFont(_font(painter, size, bold=bold, mono=mono))
    painter.setPen(QPen(QColor(color)))
    painter.drawText(rect, int(align), message)


def format_length(value: float, *, signed: bool = False) -> str:
    """Two decimals with a real minus sign, the way every reading in the design is set."""

    text = f"{abs(value):.2f}"
    if value < 0:
        return f"−{text}"
    return f"+{text}" if signed else text


@dataclass(frozen=True, slots=True)
class ChartGeometry:
    """Where the chart put its panes, so clicks can be mapped back to path progress."""

    plot_left: float
    plot_right: float
    profile: QRectF
    band: QRectF

    def progress_at(self, x: float) -> float:
        span = self.plot_right - self.plot_left
        if span <= 0:
            return 0.0
        return min(1.0, max(0.0, (x - self.plot_left) / span))


def _nice_step(span: float) -> float:
    for step in (0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0):
        if span / step <= 6:
            return step
    return 5.0


def _ticks(low: float, high: float) -> list[float]:
    """Round tick values across a range, always including zero."""

    step = _nice_step(high - low)
    first = math.ceil(low / step)
    last = math.floor(high / step)
    values = [index * step for index in range(first, last + 1)]
    if not any(abs(value) < 1e-9 for value in values) and low <= 0 <= high:
        values.append(0.0)
    return sorted(values)


def _caption(painter: QPainter, rect: QRectF, message: str) -> None:
    _text(
        painter,
        QRectF(rect.left() - AXIS_LEFT + 4, rect.top() - 16, AXIS_LEFT + 160, 13),
        message,
        size=10.5,
        color=theme.TEXT_MUTED,
    )


def _segment_spans(
    analysis: ClearanceAnalysis, left: float, right: float
) -> list[tuple[PathSegment, float, float]]:
    span = right - left
    return [
        (
            segment,
            left + segment.start_progress * span,
            left + segment.end_progress * span,
        )
        for segment in analysis.segments
    ]


def _draw_profile_pane(
    painter: QPainter, rect: QRectF, analysis: ClearanceAnalysis, left: float, right: float
) -> None:
    clearances = [sample.clearance for sample in analysis.profile]
    deepest = min([*clearances, -0.10])
    high = max(0.60, analysis.threshold * 6)
    low = max(-0.60, min(-0.10, deepest * 1.2))

    def to_y(value: float) -> float:
        return rect.bottom() - (value - low) / (high - low) * rect.height()

    zero_y = min(rect.bottom(), max(rect.top(), to_y(0.0)))
    threshold_y = min(rect.bottom(), max(rect.top(), to_y(analysis.threshold)))
    painter.fillRect(
        QRectF(rect.left(), zero_y, rect.width(), rect.bottom() - zero_y),
        QColor(theme.DANGER_TINT),
    )
    painter.fillRect(
        QRectF(rect.left(), threshold_y, rect.width(), zero_y - threshold_y),
        QColor(theme.WARNING_TINT),
    )

    span = right - left
    points = [
        QPointF(
            left + sample.progress * span,
            min(rect.bottom(), max(rect.top() + 1, to_y(sample.clearance))),
        )
        for sample in analysis.profile
    ]
    painter.save()
    painter.setClipRect(rect)
    if len(points) >= 2:
        area = QPainterPath()
        area.moveTo(points[0].x(), zero_y)
        for point in points:
            area.lineTo(point)
        area.lineTo(points[-1].x(), zero_y)
        area.closeSubpath()
        painter.fillPath(area, QBrush(_tinted(theme.ACCENT, 0.13)))
        painter.setPen(_pen(theme.ACCENT, 2.0))
        painter.drawPolyline(QPolygonF(points))
    painter.restore()

    painter.setPen(_pen(theme.DANGER, 1.4))
    painter.drawLine(QPointF(rect.left(), zero_y), QPointF(rect.right(), zero_y))
    painter.setPen(_pen(theme.WARNING_BAR, 1.2, dashes=(6, 4)))
    painter.drawLine(QPointF(rect.left(), threshold_y), QPointF(rect.right(), threshold_y))

    _caption(painter, rect, "净距 m")
    for value in _ticks(low, high):
        y = to_y(value)
        if not rect.top() - 1 <= y <= rect.bottom() + 1:
            continue
        _text(
            painter,
            QRectF(rect.left() - AXIS_LEFT + 4, y - 7, AXIS_LEFT - 10, 14),
            format_length(value),
            size=10.5,
            color=theme.TEXT_MUTED,
            mono=True,
            align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
    _text(
        painter,
        QRectF(rect.right() - 46, threshold_y - 14, 44, 12),
        "阈值",
        size=10,
        color=theme.WARNING,
        align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
    )


def _draw_band_pane(
    painter: QPainter, rect: QRectF, analysis: ClearanceAnalysis, left: float, right: float
) -> None:
    widths = [band.width for band in analysis.bands if band.feasible]
    tightest = min(widths) if widths else 0.5
    offsets = [abs(value) for value in analysis.recommended_offsets.values()]
    reach = min(1.0, max(0.50, tightest * 1.6, max(offsets, default=0.0) * 1.8))
    low, high = -reach, reach

    def to_y(value: float) -> float:
        return rect.bottom() - (value - low) / (high - low) * rect.height()

    painter.save()
    painter.setClipRect(rect)
    for segment, start_x, end_x in _segment_spans(analysis, left, right):
        band = analysis.band_for(segment.index)
        if band is None:
            continue
        if not band.feasible:
            painter.fillRect(
                QRectF(start_x, rect.top(), end_x - start_x, rect.height()),
                QBrush(_tinted(theme.DANGER, 0.10), Qt.BrushStyle.BDiagPattern),
            )
            continue
        top_y = max(rect.top(), to_y(band.high))
        bottom_y = min(rect.bottom(), to_y(band.low))
        body = QRectF(start_x, top_y, end_x - start_x, bottom_y - top_y)
        painter.fillRect(body, QBrush(_tinted(theme.SUCCESS_BAR, 0.14)))
        painter.setPen(_pen(theme.SUCCESS_BAR, 1.4))
        painter.drawRect(body)

    zero_y = to_y(0.0)
    painter.setPen(_pen(theme.ACCENT, 2.2))
    painter.drawLine(QPointF(left, zero_y), QPointF(right, zero_y))
    painter.setPen(_pen(theme.DANGER, 3.4))
    for segment, start_x, end_x in _segment_spans(analysis, left, right):
        band = analysis.band_for(segment.index)
        if band is not None and not band.contains(0.0):
            painter.drawLine(QPointF(start_x, zero_y), QPointF(end_x, zero_y))

    painter.setPen(_pen(theme.SUCCESS, 2.0, dashes=(7, 4)))
    for segment, start_x, end_x in _segment_spans(analysis, left, right):
        value = analysis.recommended_offsets.get(segment.index)
        if value is None:
            continue
        y = min(rect.bottom(), max(rect.top(), to_y(value)))
        painter.drawLine(QPointF(start_x, y), QPointF(end_x, y))
    painter.restore()

    _caption(painter, rect, "偏置 m · 正为沿行进方向左侧")
    for value in _ticks(low, high):
        _text(
            painter,
            QRectF(rect.left() - AXIS_LEFT + 4, to_y(value) - 7, AXIS_LEFT - 10, 14),
            format_length(value, signed=value != 0),
            size=10.5,
            color=theme.TEXT_MUTED,
            mono=True,
            align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )


def _draw_shared_axis(
    painter: QPainter,
    rect: QRectF,
    analysis: ClearanceAnalysis,
    geometry: ChartGeometry,
    highlight: int | None,
) -> None:
    left, right = geometry.plot_left, geometry.plot_right
    top, bottom = geometry.profile.top(), geometry.band.bottom()

    painter.setPen(_pen(theme.BORDER_FAINT, 1.0, dashes=(2, 3)))
    for _segment, _start_x, end_x in _segment_spans(analysis, left, right)[:-1]:
        painter.drawLine(QPointF(end_x, top), QPointF(end_x, bottom))

    painter.setPen(_pen(theme.DANGER, 1.2, dashes=(4, 3)))
    for bottleneck in analysis.bottlenecks[:2]:
        x = left + bottleneck.progress * (right - left)
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))

    if highlight is not None:
        marked = next(
            (item for item in analysis.segments if item.index == highlight), None
        )
        if marked is not None:
            span = right - left
            painter.fillRect(
                QRectF(
                    left + marked.start_progress * span,
                    top,
                    (marked.end_progress - marked.start_progress) * span,
                    bottom - top,
                ),
                QBrush(_tinted(theme.ACCENT, 0.08)),
            )

    painter.setPen(_pen(theme.TEXT_FAINT, 1.0))
    painter.drawLine(QPointF(left, bottom), QPointF(right, bottom))
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = left + fraction * (right - left)
        painter.setPen(_pen(theme.TEXT_FAINT, 1.0))
        painter.drawLine(QPointF(x, bottom), QPointF(x, bottom + 4))
        _text(
            painter,
            QRectF(x - 22, bottom + 4, 44, 12),
            f"{fraction * 100:.0f}%",
            size=9.5,
            color=theme.TEXT_MUTED,
            mono=True,
            align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        )

    for segment, start_x, end_x in _segment_spans(analysis, left, right):
        if end_x - start_x < 26:
            continue
        _text(
            painter,
            QRectF(start_x, bottom + 17, end_x - start_x, 13),
            segment.short_label,
            size=10,
            color=theme.TEXT_SECONDARY,
            align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        )

    for bottleneck in analysis.bottlenecks[:2]:
        x = left + bottleneck.progress * (right - left)
        label = f"{format_length(bottleneck.clearance, signed=True)}"
        box = QRectF(x - 30, bottom + 31, 60, 14)
        if bottleneck.clearance < 0:
            colour, tint = theme.DANGER, theme.DANGER_TINT
        elif bottleneck.clearance < analysis.threshold:
            colour, tint = theme.WARNING, theme.WARNING_TINT
        else:
            colour, tint = theme.SUCCESS, theme.SUCCESS_TINT
        painter.fillRect(box, QColor(tint))
        painter.setPen(_pen(colour, 1.0))
        painter.drawRect(box)
        _text(
            painter,
            box,
            label,
            size=9.5,
            color=colour,
            mono=True,
            bold=True,
            align=Qt.AlignmentFlag.AlignCenter,
        )


def _draw_legend(painter: QPainter, rect: QRectF) -> None:
    entries = (
        (theme.ACCENT, "当前净距 / 当前偏置"),
        (theme.SUCCESS_BAR, "可行偏置带"),
        (theme.SUCCESS, "建议偏置"),
        (theme.DANGER, "落在带外"),
        (theme.WARNING_BAR, "阈值"),
    )
    x = rect.left()
    painter.setFont(_font(painter, 10.5))
    for colour, label in entries:
        painter.fillRect(QRectF(x, rect.center().y() - 4.5, 12, 9), QColor(colour))
        width = painter.fontMetrics().horizontalAdvance(label)
        _text(
            painter,
            QRectF(x + 17, rect.top(), width + 6, rect.height()),
            label,
            size=10.5,
            color=theme.TEXT_MUTED,
        )
        x += 17 + width + 18


def paint_clearance_chart(
    painter: QPainter,
    rect: QRectF,
    analysis: ClearanceAnalysis,
    *,
    highlight: int | None = None,
) -> ChartGeometry:
    """Draw the clearance profile and the feasible offset band on one shared axis."""

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    plot_left = rect.left() + AXIS_LEFT
    plot_right = rect.right() - AXIS_RIGHT
    body_top = rect.top() + AXIS_TOP
    body_bottom = rect.bottom() - AXIS_BOTTOM - LEGEND_HEIGHT
    usable = max(40.0, body_bottom - body_top - PANE_GAP)
    profile_height = usable * PROFILE_SHARE
    profile = QRectF(plot_left, body_top, plot_right - plot_left, profile_height)
    band = QRectF(
        plot_left,
        body_top + profile_height + PANE_GAP,
        plot_right - plot_left,
        usable - profile_height,
    )
    geometry = ChartGeometry(plot_left, plot_right, profile, band)

    _draw_profile_pane(painter, profile, analysis, plot_left, plot_right)
    _draw_band_pane(painter, band, analysis, plot_left, plot_right)
    _draw_shared_axis(painter, rect, analysis, geometry, highlight)
    _draw_legend(
        painter,
        QRectF(plot_left, rect.bottom() - LEGEND_HEIGHT, plot_right - plot_left, LEGEND_HEIGHT),
    )
    painter.restore()
    return geometry


def paint_width_ruler(painter: QPainter, rect: QRectF, zones: WidthZones) -> None:
    """Draw the infeasible / needs-offset / clears-centred ruler for one bottleneck."""

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.fillRect(rect, QColor(theme.DANGER_TINT_SOFT))
    painter.setPen(_pen(theme.DANGER_BORDER, 1.0))
    painter.drawLine(rect.topLeft(), rect.topRight())
    painter.drawLine(rect.bottomLeft(), rect.bottomRight())

    inset = QRectF(rect.left() + 12, rect.top() + 20, rect.width() - 24, 20)
    low, high = zones.scale_low, zones.scale_high

    def to_x(value: float) -> float:
        return inset.left() + (value - low) / max(high - low, 1e-6) * inset.width()

    offset_edge = to_x(zones.offset_limit) if zones.offset_limit is not None else inset.right()
    centre_edge = to_x(zones.centred_limit) if zones.centred_limit is not None else inset.right()
    bands = (
        (inset.left(), offset_edge, theme.DANGER_TINT, theme.DANGER_BORDER, "不可行"),
        (offset_edge, centre_edge, theme.WARNING_TINT, theme.WARNING_BORDER, "需路径偏置"),
        (centre_edge, inset.right(), theme.SUCCESS_TINT, theme.SUCCESS_BORDER, "居中即可通过"),
    )
    for start, end, fill, border, label in bands:
        if end - start <= 0.5:
            continue
        body = QRectF(start, inset.top(), end - start, inset.height())
        painter.fillRect(body, QColor(fill))
        painter.setPen(_pen(border, 1.0))
        painter.drawRect(body)
        if end - start > 44:
            _text(
                painter,
                body,
                label,
                size=10,
                color=theme.TEXT_SECONDARY,
                align=Qt.AlignmentFlag.AlignCenter,
            )

    _text(
        painter,
        QRectF(rect.left() + 12, rect.top() + 2, rect.width() - 24, 16),
        f"{zones.lane_name}宽度三区 · 阈值 {format_length(0.05)} m",
        size=10.5,
        color=theme.TEXT_SECONDARY,
        bold=True,
    )
    for value in (zones.offset_limit, zones.centred_limit):
        if value is None:
            continue
        x = to_x(value)
        painter.setPen(_pen(theme.TEXT_FAINT, 1.0))
        painter.drawLine(QPointF(x, inset.bottom()), QPointF(x, inset.bottom() + 4))
        _text(
            painter,
            QRectF(x - 24, inset.bottom() + 4, 48, 12),
            f"{value:.2f}",
            size=10,
            color=theme.TEXT_MUTED,
            mono=True,
            align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        )

    marker_x = min(inset.right(), max(inset.left(), to_x(zones.measured)))
    pointer = QPolygonF(
        [
            QPointF(marker_x, inset.bottom() + 1),
            QPointF(marker_x - 5, inset.bottom() + 9),
            QPointF(marker_x + 5, inset.bottom() + 9),
        ]
    )
    painter.setPen(_pen(theme.ACCENT_DEEP, 1.0))
    painter.setBrush(QBrush(QColor(theme.ACCENT_DEEP)))
    painter.drawPolygon(pointer)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    verdict = (
        "居中即可通过"
        if zones.centred_limit is not None and zones.measured >= zones.centred_limit
        else "偏置可救"
        if zones.offset_limit is not None and zones.measured >= zones.offset_limit
        else "不可行"
    )
    _text(
        painter,
        QRectF(rect.left() + 12, inset.bottom() + 14, rect.width() - 24, 14),
        f"实测 {zones.measured:.2f} m · {verdict}",
        size=10.5,
        color=theme.TEXT_SECONDARY,
        mono=True,
    )
    painter.restore()


def paint_offset_curve(painter: QPainter, rect: QRectF, profile: OffsetProfile) -> None:
    """Draw how far the path drifts from the lane centreline across one corner."""

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.fillRect(rect, QColor(theme.CARD))
    plot = QRectF(rect.left() + 48, rect.top() + 22, rect.width() - 62, rect.height() - 60)
    peak = max(profile.peak, max((item.separation for item in profile.samples), default=0.0))
    ceiling = max(peak * 1.35, 0.05)
    total = profile.samples[-1].arc_length if profile.samples else 1.0

    def to_x(value: float) -> float:
        return plot.left() + value / max(total, 1e-6) * plot.width()

    def to_y(value: float) -> float:
        return plot.bottom() - value / ceiling * plot.height()

    painter.setPen(_pen(theme.GRID, 1.0))
    for step in range(4):
        y = plot.bottom() - plot.height() * step / 3
        painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        _text(
            painter,
            QRectF(rect.left() + 4, y - 7, 40, 14),
            f"{ceiling * step / 3:.2f}",
            size=10,
            color=theme.TEXT_MUTED,
            mono=True,
            align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )

    points = [QPointF(to_x(item.arc_length), to_y(item.separation)) for item in profile.samples]
    if len(points) >= 2:
        area = QPainterPath()
        area.moveTo(points[0].x(), plot.bottom())
        for point in points:
            area.lineTo(point)
        area.lineTo(points[-1].x(), plot.bottom())
        area.closeSubpath()
        painter.fillPath(area, QBrush(_tinted(theme.WARNING_BAR, 0.14)))
        painter.setPen(_pen(theme.WARNING_BAR, 2.2))
        painter.drawPolyline(QPolygonF(points))

    constant_y = to_y(profile.constant_approximation)
    painter.setPen(_pen(theme.DANGER, 1.6, dashes=(6, 4)))
    painter.drawLine(QPointF(plot.left(), constant_y), QPointF(plot.right(), constant_y))
    _text(
        painter,
        QRectF(plot.left() + 4, constant_y - 14, 120, 12),
        "常数偏置近似",
        size=10,
        color=theme.DANGER,
    )

    apex_x = to_x(profile.peak_arc_length)
    painter.setPen(_pen(theme.DANGER, 1.2))
    painter.drawLine(QPointF(apex_x, constant_y), QPointF(apex_x, to_y(profile.peak)))
    for y in (constant_y, to_y(profile.peak)):
        painter.drawLine(QPointF(apex_x - 4, y), QPointF(apex_x + 4, y))
    _text(
        painter,
        QRectF(apex_x + 7, (constant_y + to_y(profile.peak)) / 2 - 7, 170, 14),
        f"常数近似高估 {profile.overestimate:.2f} m",
        size=10.5,
        color=theme.DANGER,
        bold=True,
    )

    _text(
        painter,
        QRectF(rect.left() + 4, rect.top() + 2, 150, 14),
        "横向间距 m",
        size=10.5,
        color=theme.TEXT_MUTED,
    )
    for label, position in (
        ("入弯切点", plot.left() + 12),
        ("弯心", apex_x),
        ("出弯切点", plot.right() - 12),
    ):
        _text(
            painter,
            QRectF(position - 40, plot.bottom() + 6, 80, 13),
            label,
            size=10,
            color=theme.TEXT_MUTED,
            align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        )
    _text(
        painter,
        QRectF(plot.left(), plot.bottom() + 22, plot.width(), 13),
        f"两条弧的切点在直线上相距 {profile.tangent_gap:.2f} m",
        size=10.5,
        color=theme.TEXT_SECONDARY,
        align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
    )
    painter.restore()


@dataclass(frozen=True, slots=True)
class CornerPlan:
    """What the corner plan view draws: two centrelines that must not be confused."""

    lane_centreline: Sequence[tuple[float, float]]
    path_centreline: Sequence[tuple[float, float]]
    traversable: Sequence[Sequence[tuple[float, float]]]
    footprints: Sequence[Sequence[tuple[float, float]]]
    narrowest: Sequence[tuple[float, float]] | None
    notes: Sequence[tuple[tuple[float, float], str]] = ()


def _plan_transform(
    rect: QRectF, plan: CornerPlan
) -> tuple[float, float, float] | None:
    points = [*plan.lane_centreline, *plan.path_centreline]
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    width = max(max(xs) - min(xs), 1e-6)
    height = max(max(ys) - min(ys), 1e-6)
    scale = min((rect.width() - 40) / width, (rect.height() - 40) / height)
    offset_x = rect.center().x() - (min(xs) + max(xs)) / 2 * scale
    offset_y = rect.center().y() + (min(ys) + max(ys)) / 2 * scale
    return scale, offset_x, offset_y


def paint_corner_plan(painter: QPainter, rect: QRectF, plan: CornerPlan) -> None:
    """Plan view of one corner with the lane centreline and the path drawn apart."""

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.fillRect(rect, QColor(theme.CANVAS_BASE))
    body = QRectF(rect.left(), rect.top(), rect.width(), max(20.0, rect.height() - 22))
    transform = _plan_transform(body, plan)
    if transform is None:
        painter.restore()
        return
    scale, offset_x, offset_y = transform

    def to_point(point: tuple[float, float]) -> QPointF:
        return QPointF(point[0] * scale + offset_x, offset_y - point[1] * scale)

    painter.setClipRect(body)
    for ring in plan.traversable:
        polygon = QPolygonF([to_point(point) for point in ring])
        painter.setPen(_pen(theme.AREA_STROKE, 1.5))
        painter.setBrush(QBrush(_tinted(theme.AREA_FILL, 0.15)))
        painter.drawPolygon(polygon)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(_tinted(theme.SWEEP_FILL, 0.17)))
    for ring in plan.footprints:
        painter.drawPolygon(QPolygonF([to_point(point) for point in ring]))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    painter.setPen(_pen(theme.TEXT_FAINT, 1.5))
    painter.drawPolyline(QPolygonF([to_point(point) for point in plan.lane_centreline]))
    painter.setPen(_pen(theme.ACCENT, 2.0, dashes=(7, 5)))
    painter.drawPolyline(QPolygonF([to_point(point) for point in plan.path_centreline]))
    if plan.narrowest:
        painter.setPen(_pen(theme.SUCCESS, 1.8))
        painter.drawPolygon(QPolygonF([to_point(point) for point in plan.narrowest]))
    painter.setClipping(False)

    for anchor, label in plan.notes:
        position = to_point(anchor)
        _text(
            painter,
            QRectF(position.x() + 6, position.y() - 8, 180, 14),
            label,
            size=10,
            color=theme.TEXT_SECONDARY,
        )
    legend = (
        (theme.TEXT_FAINT, "车道走向线"),
        (theme.ACCENT, "下发路径"),
        (theme.SUCCESS, "最窄位姿"),
    )
    x = rect.left() + 10
    for colour, label in legend:
        painter.setPen(_pen(colour, 2.0))
        painter.drawLine(QPointF(x, rect.bottom() - 11), QPointF(x + 16, rect.bottom() - 11))
        painter.setFont(_font(painter, 10))
        width = painter.fontMetrics().horizontalAdvance(label)
        _text(
            painter,
            QRectF(x + 21, rect.bottom() - 19, width + 6, 16),
            label,
            size=10,
            color=theme.TEXT_MUTED,
        )
        x += 21 + width + 14
    painter.restore()
