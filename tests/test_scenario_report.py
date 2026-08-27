"""The one-page scenario report: same painter, same cards, premises in the header."""

import unicodedata
from pathlib import Path

from PySide6.QtPdf import QPdfDocument
from pytestqt.qtbot import QtBot

from route_analysis.clearance_graphics import format_length
from route_analysis.scenario_geometry import (
    Condition,
    Offsets,
    Pins,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
)
from route_analysis.scenario_graphics import PlanLayers
from route_analysis.scenario_report import export_scenario_pdf
from route_analysis.scenario_solver import solve_scenario

DIMENSIONS_TEXT_NOTE = """中文断言只在装有 CJK 字体的机器上有意义，这里全部用数字断言：
数字与单位的字形任何机器都有，报告里的读数照样可以核对。"""


def _page_text(document: QPdfDocument, page: int) -> str:
    return unicodedata.normalize("NFKC", document.getAllText(page).text())


def _export(tmp_path: Path, result) -> QPdfDocument:
    target = tmp_path / "scenario.pdf"
    pages = export_scenario_pdf(
        target, result, report_id="RPT-1", generated_at="2026-08-27 10:00"
    )
    assert pages == 1
    document = QPdfDocument()
    document.load(str(target))
    assert document.pageCount() == 1
    return document


def test_the_report_carries_the_result_and_its_premises(qtbot: QtBot, tmp_path: Path) -> None:
    result = solve_scenario(ScenarioInputs(scenario=Scenario.CORNER), RoadDimensions())
    text = _page_text(_export(tmp_path, result), 0)
    assert "RPT-1" in text
    assert "2026-08-27 10:00" in text
    assert format_length(result.inputs.radius) in text
    assert format_length(result.inputs.threshold) in text
    assert format_length(result.dims.wa) in text
    assert format_length(result.dims.wb) in text


def test_pinned_values_are_printed_in_the_header(qtbot: QtBot, tmp_path: Path) -> None:
    """A page that leaves out what was pinned misrepresents what the numbers mean."""

    result = solve_scenario(
        ScenarioInputs(scenario=Scenario.CORNER, condition=Condition.PARETO),
        RoadDimensions(wa=3.0),
        Offsets(eb=0.2),
        Pins(dims=frozenset({"wa"}), offsets=frozenset({"eb"})),
    )
    text = _page_text(_export(tmp_path, result), 0)
    assert "3.00" in text
    assert "+0.20" in text


def test_the_report_respects_the_layer_toggles(qtbot: QtBot, tmp_path: Path) -> None:
    """What the tab hides the page hides: the export is what-you-see-is-what-you-get."""

    result = solve_scenario(ScenarioInputs(scenario=Scenario.CORNER), RoadDimensions())
    target = tmp_path / "no-dimensions.pdf"
    export_scenario_pdf(
        target,
        result,
        report_id="RPT-2",
        generated_at="2026-08-27 10:00",
        layers=PlanLayers(dimensions=False),
    )
    document = QPdfDocument()
    document.load(str(target))
    with_marks = _page_text(_export(tmp_path, result), 0)
    without_marks = _page_text(document, 0)
    assert len(without_marks) < len(with_marks)
