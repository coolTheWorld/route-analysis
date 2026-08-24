"""Offset CSV and the A4 clearance report, drawn with the same code as the screen."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QMarginsF, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
)
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget

from route_analysis import theme
from route_analysis.clearance_geometry import FittedCorner, offset_profile
from route_analysis.clearance_graphics import (
    CornerPlan,
    format_length,
    paint_clearance_chart,
    paint_corner_plan,
    paint_offset_curve,
    paint_width_ruler,
)
from route_analysis.clearance_solver import (
    ClearanceAnalysis,
    ClearanceStatus,
    PathSegment,
    SegmentRole,
    WidthZones,
)

RESOLUTION = 300
DESIGN_DPI = 96.0
MARGIN_MM = 10.0
DISCLAIMER = (
    "本报告是路线分析的可视化辅助结论，不代表正式安全认证。所有偏置与半径均为建议，"
    "本工具不修改下发路径，也不修改车道。"
)
TABLE_COLUMNS: tuple[tuple[str, float], ...] = (
    ("#", 30),
    ("净距 m", 76),
    ("位置", 0),
    ("约束角", 84),
    ("需偏置", 90),
    ("进度", 70),
)
ROW_HEIGHT = 21.0
HEADER_HEIGHT = 23.0


@dataclass(slots=True)
class ReportContext:
    """Everything printed in the header, so a page never travels without its premises."""

    report_id: str
    generated_at: str
    order: str = ""
    task: str = ""
    command: str = ""
    vehicle: str = ""
    vehicle_source: str = ""
    lane_layout: str = ""
    steps: str = ""
    samples: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def rows(self) -> list[tuple[str, str]]:
        pairs = [
            ("订单", self.order),
            ("任务", self.task),
            ("命令", self.command),
            ("车型", self.vehicle),
            ("尺寸来源", self.vehicle_source),
            ("车道布局", self.lane_layout),
            ("步长", self.steps),
            ("样本", self.samples),
        ]
        pairs.extend(self.extra.items())
        return [(name, value) for name, value in pairs if value]


def export_offsets_csv(path: Path, rows: Sequence[Sequence[str]]) -> None:
    """Write the offset table as UTF-8 with a BOM, so Excel opens it without mangling."""

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)


def _font(painter: QPainter, size: float, *, bold: bool = False, mono: bool = False) -> None:
    font = QFont(painter.font())
    if mono:
        font.setFamilies(["Consolas", "DejaVu Sans Mono", "Menlo", "monospace"])
    dpi = painter.device().logicalDpiY() or DESIGN_DPI
    font.setPointSizeF(size * 72.0 / dpi)
    font.setBold(bold)
    painter.setFont(font)


def _write(
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
    _font(painter, size, bold=bold, mono=mono)
    painter.setPen(QPen(QColor(color)))
    painter.drawText(rect, int(align), message)


def _render_widget(painter: QPainter, rect: QRectF, widget: QWidget) -> None:
    """Draw a live, stylesheet-styled widget onto the page at its design size.

    This is what keeps the report visually identical to the screen: the summary cards and
    the advice cards on paper are the same widget classes the view builds, not a second
    drawing routine that would drift from them.
    """

    widget.setStyleSheet(theme.CLEARANCE_STYLESHEET)
    widget.resize(int(rect.width()), int(rect.height()))
    widget.ensurePolished()
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    painter.save()
    painter.translate(rect.topLeft())
    widget.render(painter, QPoint(0, 0))
    painter.restore()


def _draw_header(
    painter: QPainter, rect: QRectF, context: ReportContext, analysis: ClearanceAnalysis
) -> float:
    _write(painter, QRectF(rect.left(), rect.top(), rect.width() * 0.6, 26),
           "通行余量分析报告", size=21, color=theme.TEXT_PRIMARY, bold=True)
    _write(painter, QRectF(rect.left(), rect.top() + 27, rect.width() * 0.6, 16),
           "沿下发路径的净距剖面、可行偏置带与瓶颈排行", size=12, color=theme.TEXT_MUTED)
    right = QRectF(rect.right() - 260, rect.top(), 260, 46)
    lines = (
        f"报告号 {context.report_id}",
        f"日期 {context.generated_at}",
        f"阈值 {format_length(analysis.threshold)} m",
    )
    for index, line in enumerate(lines):
        _write(
            painter,
            QRectF(right.left(), right.top() + index * 15, right.width(), 14),
            line,
            size=10.5,
            color=theme.TEXT_SECONDARY,
            mono=True,
            align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
    line_y = rect.top() + 50
    painter.setPen(QPen(QColor(theme.TEXT_PRIMARY), 2))
    painter.drawLine(QPointF(rect.left(), line_y), QPointF(rect.right(), line_y))

    cursor = line_y + 7
    pairs = context.rows()
    for row_start in range(0, len(pairs), 4):
        chunk = pairs[row_start : row_start + 4]
        for column, (name, value) in enumerate(chunk):
            cell = QRectF(
                rect.left() + column * rect.width() / 4, cursor, rect.width() / 4 - 8, 14
            )
            _write(painter, cell, name, size=10, color=theme.TEXT_MUTED)
            _write(
                painter,
                QRectF(cell.left() + 54, cell.top(), cell.width() - 54, cell.height()),
                value,
                size=10.5,
                color=theme.TEXT_PRIMARY,
                mono=True,
            )
        cursor += 16
    return cursor + 4


def _draw_footer(painter: QPainter, rect: QRectF, page: int, total: int) -> None:
    painter.fillRect(rect, QColor(theme.PANEL_BASE))
    _write(
        painter,
        QRectF(rect.left() + 8, rect.top(), rect.width() - 90, rect.height()),
        DISCLAIMER,
        size=10.5,
        color=theme.TEXT_MUTED,
    )
    _write(
        painter,
        QRectF(rect.right() - 80, rect.top(), 72, rect.height()),
        f"第 {page} / {total} 页",
        size=10.5,
        color=theme.TEXT_MUTED,
        mono=True,
        align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
    )


def _status_word(status: ClearanceStatus) -> str:
    return {
        ClearanceStatus.SAFE: "通过",
        ClearanceStatus.WARNING: "临界",
        ClearanceStatus.OUTSIDE: "越界",
        ClearanceStatus.UNAVAILABLE: "无结果",
    }[status]


def _summary_widget(analysis: ClearanceAnalysis) -> QWidget:
    """The same five metric cards the view shows, laid out for the page width."""

    from route_analysis.clearance_panel import build_metric_cards

    holder = QWidget()
    grid = QGridLayout(holder)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(10)
    for column, card in enumerate(build_metric_cards(analysis)):
        grid.addWidget(card, 0, column)
        grid.setColumnStretch(column, 1)
    return holder


def _advice_widget(cards: Sequence[tuple[str, str, str]]) -> QWidget:
    """The same advice cards the view shows, side by side instead of stacked."""

    from route_analysis.clearance_panel import SuggestionCard

    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    for rank, title, body in cards:
        row.addWidget(SuggestionCard(rank, title, body, pill=rank != "discouraged"), 1)
    return holder


def _draw_table_header(painter: QPainter, rect: QRectF) -> None:
    painter.fillRect(rect, QColor(theme.HEADER_BASE))
    painter.setPen(QPen(QColor(theme.BORDER), 1))
    painter.drawRect(rect)
    fixed = sum(width for _name, width in TABLE_COLUMNS if width)
    stretch = max(60.0, rect.width() - fixed)
    x = rect.left()
    for name, width in TABLE_COLUMNS:
        span = width or stretch
        _write(painter, QRectF(x + 6, rect.top(), span - 12, rect.height()), name,
               size=10.5, color=theme.TEXT_PRIMARY, bold=True)
        x += span


def _draw_table_row(
    painter: QPainter, rect: QRectF, cells: Sequence[str], tint: str | None, index: int
) -> None:
    background = tint or (theme.CARD if index % 2 == 0 else theme.CANVAS_BASE)
    painter.fillRect(rect, QColor(background))
    painter.setPen(QPen(QColor(theme.BORDER_FAINT), 1))
    painter.drawLine(rect.bottomLeft(), rect.bottomRight())
    fixed = sum(width for _name, width in TABLE_COLUMNS if width)
    stretch = max(60.0, rect.width() - fixed)
    x = rect.left()
    for position, ((_name, width), text) in enumerate(zip(TABLE_COLUMNS, cells, strict=True)):
        span = width or stretch
        mono = position in (0, 1, 5)
        align = (
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            if mono
            else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        _write(painter, QRectF(x + 6, rect.top(), span - 12, rect.height()), text,
               size=10.5, color=theme.TEXT_PRIMARY, mono=mono, align=align)
        x += span


def _bottleneck_cells(analysis: ClearanceAnalysis) -> list[tuple[list[str], str | None]]:
    rows: list[tuple[list[str], str | None]] = []
    for item in analysis.bottlenecks:
        if item.clearance < 0:
            tint: str | None = theme.DANGER_TINT
        elif item.clearance < analysis.threshold:
            tint = theme.WARNING_TINT
        else:
            tint = None
        rows.append(
            (
                [
                    str(item.rank),
                    format_length(item.clearance, signed=True),
                    item.segment.label,
                    item.feature,
                    item.offset_text,
                    f"{item.progress * 100:.1f}%",
                ],
                tint,
            )
        )
    return rows


@dataclass(slots=True)
class CornerReport:
    """The corner page, present only when the corner solver is the open view."""

    segment: PathSegment
    corner: FittedCorner
    lane_radius: float | None
    plan: CornerPlan | None
    entry_offset: float
    exit_offset: float
    arc_start_shift: float
    radius: float | None
    clearance: float | None


def _page_rects(writer: QPdfWriter) -> tuple[QRectF, QRectF]:
    scale = writer.logicalDpiX() / DESIGN_DPI
    full = QRectF(0, 0, writer.width() / scale, writer.height() / scale)
    footer = QRectF(full.left(), full.bottom() - 26, full.width(), 26)
    return full, footer


def export_report_pdf(
    path: Path,
    analysis: ClearanceAnalysis,
    context: ReportContext,
    advice: Sequence[tuple[str, str, str]],
    *,
    zones: WidthZones | None = None,
    corner: CornerReport | None = None,
) -> int:
    """Render the clearance report to A4 landscape, returning how many pages it took."""

    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageOrientation(QPageLayout.Orientation.Landscape)
    writer.setPageMargins(QMarginsF(MARGIN_MM, MARGIN_MM, MARGIN_MM, MARGIN_MM))
    writer.setResolution(RESOLUTION)
    writer.setTitle(f"通行余量分析报告 {context.report_id}")

    painter = QPainter(writer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = writer.logicalDpiX() / DESIGN_DPI
    painter.scale(scale, scale)
    painter.setFont(theme.select_cjk_font(9))

    rows = _bottleneck_cells(analysis)
    full, footer = _page_rects(writer)
    ruler_height = 80.0 if zones is not None else 0.0
    first_page_rows = 0
    total_pages = 2 + (1 if corner is not None else 0)

    # Page 1: premises, summary, the shared-axis chart, the advice.
    body_top = _draw_header(painter, full, context, analysis)
    _render_widget(
        painter, QRectF(full.left(), body_top, full.width(), 66), _summary_widget(analysis)
    )
    chart_top = body_top + 72
    chart_height = footer.top() - chart_top - 128
    paint_clearance_chart(
        painter, QRectF(full.left(), chart_top, full.width(), chart_height), analysis
    )
    _render_widget(
        painter,
        QRectF(full.left(), chart_top + chart_height + 8, full.width(), 112),
        _advice_widget(advice),
    )
    _draw_footer(painter, footer, 1, total_pages)

    # Page 2 onwards: the ranking, paginated so no row is ever half-printed.
    page = 1
    index = first_page_rows
    while index < len(rows) or page == 1:
        writer.newPage()
        page += 1
        top = _draw_header(painter, full, context, analysis)
        _write(painter, QRectF(full.left(), top, full.width(), 18), "瓶颈排行榜",
               size=13, color=theme.TEXT_PRIMARY, bold=True)
        table_top = top + 22
        header_rect = QRectF(full.left(), table_top, full.width(), HEADER_HEIGHT)
        _draw_table_header(painter, header_rect)
        cursor = header_rect.bottom()
        available = footer.top() - 10 - cursor
        printed = 0
        while index < len(rows) and available >= ROW_HEIGHT:
            cells, tint = rows[index]
            _draw_table_row(
                painter, QRectF(full.left(), cursor, full.width(), ROW_HEIGHT), cells, tint, index
            )
            cursor += ROW_HEIGHT
            available -= ROW_HEIGHT
            index += 1
            printed += 1
            if (
                zones is not None
                and analysis.bottlenecks[index - 1].segment.index == _zone_owner(analysis, zones)
                and available >= ruler_height
            ):
                paint_width_ruler(
                    painter, QRectF(full.left(), cursor, full.width(), ruler_height), zones
                )
                cursor += ruler_height
                available -= ruler_height
        if printed == 0 and index < len(rows):
            break
        total_pages = max(total_pages, page + (1 if corner is not None else 0))
        _draw_footer(painter, footer, page, total_pages)
        if index >= len(rows):
            break

    if corner is not None:
        writer.newPage()
        page += 1
        _draw_corner_page(painter, full, footer, context, analysis, corner, page, total_pages)

    painter.end()
    return page


def _zone_owner(analysis: ClearanceAnalysis, zones: WidthZones) -> int | None:
    return next(
        (
            item.segment.index
            for item in analysis.bottlenecks
            if item.segment.lane_id == zones.lane_id
            and item.segment.role is SegmentRole.TURN
        ),
        None,
    )


def _draw_corner_page(
    painter: QPainter,
    full: QRectF,
    footer: QRectF,
    context: ReportContext,
    analysis: ClearanceAnalysis,
    corner: CornerReport,
    page: int,
    total: int,
) -> None:
    top = _draw_header(painter, full, context, analysis)
    _write(painter, QRectF(full.left(), top, full.width(), 18),
           f"转角求解 · {corner.segment.label}", size=13, color=theme.TEXT_PRIMARY, bold=True)
    summary = (
        f"偏转 {abs(corner.corner.deflection) * 180 / 3.141592653589793:.1f}° · "
        f"拟合路径 R {corner.corner.radius:.2f} m · 拟合残差 {corner.corner.residual:.3f} m · "
        f"入弯 {format_length(corner.entry_offset, signed=True)} · "
        f"出弯 {format_length(corner.exit_offset, signed=True)} · "
        f"起弯点 {format_length(corner.arc_start_shift, signed=True)}"
        + (f" · 本解 R {corner.radius:.2f} m" if corner.radius is not None else "")
        + (
            f" · 最小净距 {format_length(corner.clearance, signed=True)} m"
            if corner.clearance is not None
            else ""
        )
    )
    _write(painter, QRectF(full.left(), top + 20, full.width(), 16), summary,
           size=10.5, color=theme.TEXT_SECONDARY, mono=True)
    body_top = top + 42
    body_height = footer.top() - body_top - 8
    half = full.width() / 2 - 6
    if corner.lane_radius is not None and abs(corner.lane_radius - corner.corner.radius) > 1e-6:
        profile = offset_profile(
            corner.lane_radius, corner.corner.radius, corner.corner.deflection
        )
        paint_offset_curve(
            painter, QRectF(full.left(), body_top, half, body_height * 0.5), profile
        )
    else:
        _write(
            painter,
            QRectF(full.left(), body_top, half, 40),
            "该转角所在车道没有圆弧倒角段，两条中心线的半径差未知，偏置沿弯曲线不可绘制。",
            size=10.5,
            color=theme.TEXT_MUTED,
        )
    if corner.plan is not None:
        paint_corner_plan(
            painter, QRectF(full.left() + half + 12, body_top, half, body_height), corner.plan
        )
    _draw_footer(painter, footer, page, total)
