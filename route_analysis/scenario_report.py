"""One-page A4 report for the rapid-estimate tab, drawn with the same code as the screen."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMarginsF, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen
from PySide6.QtWidgets import QVBoxLayout, QWidget

from route_analysis import theme
from route_analysis.clearance_graphics import format_length
from route_analysis.clearance_report import (
    DESIGN_DPI,
    MARGIN_MM,
    RESOLUTION,
    _page_rects,
    _write,
)
from route_analysis.scenario_geometry import dimension_label, offset_label, variant_name
from route_analysis.scenario_graphics import PlanLayers, paint_scenario_plan
from route_analysis.scenario_solver import ScenarioResult, trace_maneuvers

DISCLAIMER = (
    "本报告为纯几何离线速算的可视化结论，不连接调度后端、不使用任何真实路径数据，"
    "不代表正式安全认证。全部尺寸仅在页眉所列车辆参数与前提下成立。"
)
PLAN_SHARE = 0.63
"""How much of the body width the plan view takes; the result cards get the rest."""


def _premise_rows(result: ScenarioResult) -> list[tuple[str, str]]:
    """Everything the estimate depended on: the page must not travel without it."""

    inputs = result.inputs
    vehicle = inputs.dimensions
    rows = [
        (
            "车辆",
            f"宽 {vehicle.width:.2f} / 前 {vehicle.center_front:.3f}"
            f" / 后 {vehicle.center_rear:.3f} m",
        ),
        ("转弯半径", f"{format_length(inputs.radius)} m"),
        ("净距阈值", f"{format_length(inputs.threshold)} m"),
        ("固定项", _pinned_text(result)),
    ]
    return rows


def _pinned_text(result: ScenarioResult) -> str:
    from route_analysis.scenario_panel import ROAD_KEYS

    inputs = result.inputs
    parts = [
        f"{dimension_label(inputs.scenario, key, bidirectional=inputs.bidirectional)} "
        f"{format_length(getattr(result.dims, key))} m"
        for key in ROAD_KEYS[inputs.scenario]
        if key in result.pins.dims
    ]
    parts.extend(
        f"{offset_label(inputs.scenario, key, bidirectional=inputs.bidirectional)} "
        f"{format_length(getattr(result.offsets, key), signed=True)} m"
        for key in sorted(result.pins.offsets)
    )
    return "、".join(parts) if parts else "无"


def _draw_header(
    painter: QPainter, rect: QRectF, result: ScenarioResult, report_id: str, generated_at: str
) -> float:
    _write(painter, QRectF(rect.left(), rect.top(), rect.width() * 0.66, 26),
           "场景速算报告", size=21, color=theme.TEXT_PRIMARY, bold=True)
    _write(painter, QRectF(rect.left(), rect.top() + 27, rect.width() * 0.66, 16),
           variant_name(result.inputs), size=12, color=theme.TEXT_MUTED)
    right = QRectF(rect.right() - 260, rect.top(), 260, 46)
    lines = (f"报告号 {report_id}", f"日期 {generated_at}", "离线速算 · 不写回数据")
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
    for column, (name, value) in enumerate(_premise_rows(result)):
        cell = QRectF(rect.left() + column * rect.width() / 4, cursor, rect.width() / 4 - 8, 14)
        _write(painter, cell, name, size=10, color=theme.TEXT_MUTED)
        _write(
            painter,
            QRectF(cell.left() + 54, cell.top(), cell.width() - 54, cell.height()),
            value,
            size=10.5,
            color=theme.TEXT_PRIMARY,
            mono=True,
        )
    return cursor + 20


def _draw_footer(painter: QPainter, rect: QRectF) -> None:
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
        "第 1 / 1 页",
        size=10.5,
        color=theme.TEXT_MUTED,
        mono=True,
        align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
    )


def _cards_column(result: ScenarioResult) -> QWidget:
    """The result cards the tab shows, stacked for the page's right-hand column."""

    from route_analysis.scenario_panel import build_result_cards

    holder = QWidget()
    holder.setObjectName("scenarioReportColumn")
    holder.setStyleSheet(
        theme.CLEARANCE_STYLESHEET
        + theme.SCENARIO_STYLESHEET
        + "QWidget#scenarioReportColumn { background: transparent; }"
    )
    holder.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    column = QVBoxLayout(holder)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(8)
    for card in build_result_cards(result):
        column.addWidget(card)
    column.addStretch(1)
    return holder


def _render_column(painter: QPainter, rect: QRectF, widget: QWidget) -> None:
    widget.resize(int(rect.width()), int(rect.height()))
    widget.ensurePolished()
    # Activate every nested layout, deepest first: a card the fill code flashed visible
    # has children still sitting at their default top-level geometry, and only its own
    # grid can put them back where they belong.
    for child in reversed(widget.findChildren(QWidget)):
        child_layout = child.layout()
        if child_layout is not None:
            child_layout.activate()
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    painter.save()
    painter.translate(rect.topLeft())
    widget.render(painter, QPoint(0, 0))
    painter.restore()


def export_scenario_pdf(
    path: Path,
    result: ScenarioResult,
    *,
    report_id: str,
    generated_at: str,
    layers: PlanLayers | None = None,
) -> int:
    """Render the current variant to one A4 landscape page, returning the page count.

    What the page shows is what the tab shows: the same plan-view painter with the
    caller's layer toggles, and the same result cards the view fills.
    """

    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageOrientation(QPageLayout.Orientation.Landscape)
    writer.setPageMargins(QMarginsF(MARGIN_MM, MARGIN_MM, MARGIN_MM, MARGIN_MM))
    writer.setResolution(RESOLUTION)
    writer.setTitle(f"场景速算报告 {report_id}")

    painter = QPainter(writer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = writer.logicalDpiX() / DESIGN_DPI
    painter.scale(scale, scale)
    painter.setFont(theme.select_cjk_font(9))

    full, footer = _page_rects(writer)
    body_top = _draw_header(painter, full, result, report_id, generated_at)
    body = QRectF(full.left(), body_top, full.width(), footer.top() - body_top - 8)
    plan_width = body.width() * PLAN_SHARE
    paint_scenario_plan(
        painter,
        QRectF(body.left(), body.top(), plan_width, body.height()),
        result,
        layers if layers is not None else PlanLayers(),
        traces=trace_maneuvers(result.layout, result.inputs.dimensions),
    )
    cards = QRectF(
        body.left() + plan_width + 12, body.top(),
        body.width() - plan_width - 12, body.height(),
    )
    _render_column(painter, cards, _cards_column(result))
    _draw_footer(painter, footer)
    painter.end()
    return 1
