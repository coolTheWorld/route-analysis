import math
from pathlib import Path

import pytest
from PySide6.QtPdf import QPdfDocument
from pytestqt.qtbot import QtBot

from route_analysis.clearance_panel import build_suggestions, offset_rows
from route_analysis.clearance_report import (
    DISCLAIMER,
    ReportContext,
    export_offsets_csv,
    export_report_pdf,
)
from route_analysis.clearance_solver import (
    LaneContext,
    SegmentRole,
    analyse_clearance,
    solve_width_zones,
)
from route_analysis.models import (
    AnalysisSettings,
    Lane,
    Point2D,
    PosePoint,
    VehicleDimensions,
)

DIMENSIONS = VehicleDimensions(width=1.20, center_front=1.00, center_rear=1.60)


def _arc(centre_x: float, centre_y: float, radius: float, start: float, end: float,
         steps: int, sign: float) -> list[PosePoint]:
    return [
        PosePoint(
            centre_x + radius * math.cos(start + (end - start) * step / steps),
            centre_y + radius * math.sin(start + (end - start) * step / steps),
            start + (end - start) * step / steps + sign * math.pi / 2,
        )
        for step in range(1, steps + 1)
    ]


def _scenario() -> tuple[list[PosePoint], list[Lane]]:
    poses = [PosePoint(-12 + index * 0.5, 0.0, 0.0) for index in range(25)]
    poses += _arc(0.0, 1.2, 1.2, -math.pi / 2, 0.0, 24, 1.0)
    poses += [PosePoint(1.2, 1.2 + index * 0.5, math.pi / 2) for index in range(1, 17)]
    poses += _arc(2.4, 9.2, 1.2, math.pi, math.pi / 2, 24, -1.0)
    poses += [PosePoint(2.4 + index * 0.5, 10.4, 0.0) for index in range(1, 21)]
    lanes = [
        Lane.create("main", "主通道", 3.4, [Point2D(-16, 0), Point2D(1.2, 0), Point2D(1.2, 1.2)]),
        Lane.create(
            "branch", "支通道", 2.2,
            [Point2D(1.2, 0.6), Point2D(1.2, 10.4), Point2D(26, 10.4)],
        ),
    ]
    return poses, lanes


def _context() -> ReportContext:
    return ReportContext(
        report_id="CMD-1-20260820-1042",
        generated_at="2026-08-20 10:42",
        order="SO-1",
        task="T-1",
        command="CMD-1",
        vehicle="宽 1.20 / 前 1.00 / 后 1.60 m",
        vehicle_source="全局默认",
        lane_layout="测试地图",
        steps="位置 0.05 m · 航向 0.02 rad",
        samples="793 个",
    )


def test_offset_csv_opens_cleanly_in_excel(tmp_path: Path) -> None:
    poses, lanes = _scenario()
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, AnalysisSettings())
    assert analysis is not None
    target = tmp_path / "offsets.csv"
    export_offsets_csv(target, offset_rows(analysis))
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    lines = target.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("区段,")
    assert len(lines) == len(analysis.segments) + 1


def test_report_context_hides_fields_it_was_not_given() -> None:
    rows = ReportContext(report_id="X", generated_at="Y", order="SO-1").rows()
    assert ("订单", "SO-1") in rows
    assert all(value for _name, value in rows)


def test_report_renders_two_pages_with_the_ranking_on_its_own(
    qtbot: QtBot, tmp_path: Path
) -> None:
    poses, lanes = _scenario()
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, settings)
    assert analysis is not None
    target = tmp_path / "report.pdf"
    pages = export_report_pdf(target, analysis, _context(), build_suggestions(analysis))
    assert pages == 2
    assert target.stat().st_size > 10_000
    document = QPdfDocument()
    document.load(str(target))
    assert document.pageCount() == 2


def test_report_carries_its_premises_and_the_disclaimer(qtbot: QtBot, tmp_path: Path) -> None:
    poses, lanes = _scenario()
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, AnalysisSettings())
    assert analysis is not None
    target = tmp_path / "report.pdf"
    export_report_pdf(target, analysis, _context(), build_suggestions(analysis))
    document = QPdfDocument()
    document.load(str(target))
    first = document.getAllText(0).text()
    last = document.getAllText(1).text()
    assert "SO-1" in first
    assert "CMD-1-20260820-1042" in first
    assert "只建议" in first
    assert DISCLAIMER[:12] in last


def test_report_page_is_a4_landscape(qtbot: QtBot, tmp_path: Path) -> None:
    poses, lanes = _scenario()
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, AnalysisSettings())
    assert analysis is not None
    target = tmp_path / "report.pdf"
    export_report_pdf(target, analysis, _context(), build_suggestions(analysis))
    document = QPdfDocument()
    document.load(str(target))
    size = document.pagePointSize(0)
    assert size.width() > size.height()
    assert size.width() == pytest.approx(842, abs=2)


def test_report_includes_the_selected_width_ruler(qtbot: QtBot, tmp_path: Path) -> None:
    poses, lanes = _scenario()
    settings = AnalysisSettings()
    analysis = analyse_clearance(poses, DIMENSIONS, lanes, settings)
    assert analysis is not None
    context = LaneContext(lanes, settings)
    turn = next(
        item
        for item in analysis.segments
        if item.role is SegmentRole.TURN and item.lane_name == "支通道"
    )
    zones = solve_width_zones(poses, turn, DIMENSIONS, context, settings)
    assert zones is not None
    target = tmp_path / "report.pdf"
    export_report_pdf(target, analysis, _context(), build_suggestions(analysis), zones=zones)
    document = QPdfDocument()
    document.load(str(target))
    assert "不可行" in document.getAllText(1).text()
